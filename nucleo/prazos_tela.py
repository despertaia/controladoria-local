"""Os dados das telas de prazo (1.6.0): aba Prazos, conferir um prazo, selo do cartão, faixa
do topo e calendário forense. Só leitura: as ações ficam em `nucleo/prazos_banco.py`.

Nada aqui refaz a conta: a data de cada prazo é a que está gravada. A faixa, o contador e o
selo rodam no quadro, para todo cartão, e por isso só consultam o calendário dos prazos que
estão perto de vencer (`_Calendarios.horizonte`) e guardam o que já carregaram."""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from datetime import date, timedelta
from itertools import groupby

from captura.processo_parser import formatar_numero_cnj
from nucleo import carteira, prazos, prazos_banco, prazos_legais

URGENTE_UTEIS = 2  # vence nesta quantidade de dias úteis, ou antes, é urgente
ROTULOS = {"aguardando_lex": "Aguardando o Lex", "a_conferir": "A conferir",
           "a_definir": "A definir", "sem_prazo": "Sem prazo", "conferido": "Conferido",
           "cumprido": "Cumprido", "descartado": "Descartado"}
ROTULOS_DE_DIA = {"sem_expediente": "Sem expediente", "reduzido": "Expediente reduzido",
                  "suspensao": "Prazos suspensos"}
ROTULOS_DE_ORIGEM_DO_DIA = {"lei": "Lei", "ato": "Ato do tribunal", "manual": "Anotação sua"}
ROTULOS_DE_UNIDADE = {"uteis": "dias úteis", "corridos": "dias corridos", "horas": "horas",
                      "meses": "meses"}
ROTULOS_DE_ORIGEM = {"texto": "Dias lidos na publicação",
                     "decisao": "Dias fixados pelo juiz, lidos na publicação",
                     "lei": "Dias da lei", "manual": "Dias informados por você"}
_MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
          "outubro", "novembro", "dezembro")
_MOTIVO_SEM_LEX = "O Lex ainda não leu esta publicação. Se preferir, preencha à mão."
_MOTIVO_NAO_SEU = ("Você marcou este processo como não seu, então o Lex não lê esta publicação. "
                   "Se for seu, traga o processo de volta à carteira ou preencha à mão.")
PROCESSOS_DA_LISTA = ("arquivado_voce", "arquivado_tribunal", "lixeira")
_SEM_DATA = ("aguardando_lex", "a_definir")
_SEM_DATA_SQL = "('aguardando_lex', 'a_definir')"
_VER_FUTURO = timedelta(days=400)  # até onde o horizonte procura dias sem expediente


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
# Data gravada que o SQLite lê como dia do calendário, e só então (a mesma regra de `_ler`).
_LEGIVEL = "date({c}) = {c}"
_ILEGIVEL = "({c} IS NOT NULL AND date({c}) IS NOT {c})"
_ILEGIVEL_NO_PRAZO = ("((estado = 'conferido' AND fatal IS NULL) OR "
                      + _ILEGIVEL.format(c="fatal") + ")")
_LIGACAO = re.compile(r"https?://\S+", re.IGNORECASE)
_SEM_DATA_LEGIVEL = "Esta data não pôde ser lida. Abra o prazo e preencha à mão."


def _ler(texto) -> date | None:
    """O dia gravado (AAAA-MM-DD), ou None se não for uma data do calendário. Nunca levanta."""
    if not isinstance(texto, str) or not _ISO.fullmatch(texto):
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _escrito(texto) -> str:
    """Uma data que não se lê aparece como foi escrita, se couber; do contrário, «data ilegível»."""
    escrito = str(texto).strip()
    return escrito if escrito.isprintable() and len(escrito) <= 20 else "data ilegível"


def _br(iso: str | None) -> str:
    if not iso:
        return ""
    dia = _ler(iso)
    return prazos.dia_br(dia) if dia else _escrito(iso)


def _dia_mes(iso: str | None) -> str:
    if not iso:
        return ""
    dia = _ler(iso)
    return dia.strftime("%d/%m") if dia else _escrito(iso)


def versao_do_prazo(r) -> str:
    """A versão do prazo que uma tela mostrou: a data fatal e o momento da última mudança."""
    return f"{r['fatal'] or ''}|{r['atualizado_em']}"


def _lista(valor) -> list:
    return list(valor) if isinstance(valor, list) else []


def _json(texto, padrao):
    try:
        valor = json.loads(texto or "")
    except ValueError:
        return padrao
    return valor if isinstance(valor, type(padrao)) else padrao


def _marcas(valores) -> str:
    return ", ".join("?" for _ in valores)


class _Calendarios:
    """Um calendário por tribunal e órgão, e o processo de cada número, carregados uma vez só:
    o quadro inteiro usa o mesmo objeto, e a página não repete a mesma consulta. O calendário de
    um prazo é o mesmo da conta dele (o órgão da própria publicação vem primeiro)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._chaves: dict = {}
        self._calendarios: dict = {}
        self._processos: dict = {}
        self._limites: dict = {}
        self._horizontes: dict = {}

    def chave(self, r) -> tuple[str, str]:
        ref = r["origem_ref"] if r["origem_tipo"] == "publicacao" and str(
            r["origem_ref"]).isdigit() else None
        por = (r["numero"], int(ref) if ref is not None else None)
        if por not in self._chaves:
            self._chaves[por] = prazos_banco._tribunal_e_orgao(self.conn, *por)
        return self._chaves[por]

    def de(self, r) -> prazos.Calendario:
        chave = self.chave(r)
        if chave not in self._calendarios:
            self._calendarios[chave] = prazos_banco.carregar_calendario(self.conn, *chave)
        return self._calendarios[chave]

    def processo(self, numero: str):
        if numero not in self._processos:
            self._processos[numero] = self.conn.execute(
                "SELECT * FROM processo WHERE numero = ?", (numero,)).fetchone()
        return self._processos[numero]

    def horizonte(self, hoje: date) -> str:
        """O dia (texto ISO) a partir do qual nenhum prazo é urgente, seja qual for o calendário.
        Conta só com os dias sem expediente de toda a tabela, que são mais do que os de qualquer
        calendário; por isso o limite de cada calendário nunca passa dele. Prazo depois do
        horizonte dispensa carregar calendário."""
        if hoje not in self._horizontes:
            fechados = {r[0] for r in self.conn.execute(
                "SELECT data FROM dia_forense WHERE tipo = 'sem_expediente' AND ativo = 1 "
                "AND data > ? AND data <= ?", (hoje.isoformat(), (hoje + _VER_FUTURO).isoformat()))}
            dia, faltam = hoje, URGENTE_UTEIS + 1
            while faltam:
                dia += timedelta(days=1)
                if dia.weekday() not in prazos.FIM_DE_SEMANA and dia.isoformat() not in fechados:
                    faltam -= 1
            self._horizontes[hoje] = dia.isoformat()
        return self._horizontes[hoje]

    def limites(self, r, hoje: date) -> tuple[str, str]:
        """(próximo dia útil, primeiro dia que já não é urgente) deste calendário, em ISO. Urgente
        é o que vence antes do segundo."""
        chave = (self.chave(r), hoje)
        if chave not in self._limites:
            cal = self.de(r)
            dia, proximo = hoje, None
            for _ in range(URGENTE_UTEIS + 1):
                dia = cal.proximo_util(dia)
                proximo = proximo or dia
            self._limites[chave] = (proximo.isoformat(), dia.isoformat())
        return self._limites[chave]


def calendarios(conn: sqlite3.Connection) -> _Calendarios:
    """Para quem monta muitos cartões de uma vez e quer dividir o que já foi carregado."""
    return _Calendarios(conn)


def _urgente(p, hoje: date, cals: _Calendarios) -> bool:
    """Vence em até `URGENTE_UTEIS` dias úteis (o atrasado também). Mesma resposta de
    `prazos.uteis_ate`, sem andar dia a dia. Data que não se lê pede atenção: urgente."""
    fatal = p["fatal"]
    if not fatal:
        return False
    if _ler(fatal) is None or fatal < hoje.isoformat():
        return True
    if fatal >= cals.horizonte(hoje):
        return False
    return fatal < cals.limites(p, hoje)[1]


def _dias_desde(momento, hoje: date) -> int:
    """Dias inteiros entre o dia de `momento` (texto ISO com hora) e hoje; 0 se não se lê."""
    dia = _ler(str(momento or "")[:10])
    return max(0, (hoje - dia).days) if dia else 0


def _linha(r, hoje: date, cals: _Calendarios) -> dict:
    proc = cals.processo(r["numero"])
    motivo = r["motivo"] or (_MOTIVO_SEM_LEX if r["estado"] == "aguardando_lex" else "")
    fatal = _ler(r["fatal"])
    return {"id": r["id"], "numero": r["numero"], "numero_formatado": formatar_numero_cnj(r["numero"]),
            "ato": r["ato"] or ("Intimação no PJe" if r["origem_tipo"] == "aviso" else "Prazo"),
            "estado": r["estado"], "estado_rotulo": ROTULOS[r["estado"]],
            "e_aviso": r["origem_tipo"] == "aviso",
            "fatal": r["fatal"], "fatal_br": _br(r["fatal"]),
            # a data gravada que não se lê aparece como foi escrita, mas ninguém a confere
            "fatal_legivel": fatal is not None, "trabalho": r["trabalho"],
            "trabalho_br": _br(r["trabalho"]), "fatal_alternativa_br": _br(r["fatal_alternativa"]),
            "fatal_maior": bool(r["fatal_maior"]),
            "atrasado": fatal is not None and fatal < hoje,
            "cliente": (proc["cliente"] or "") if proc else "", "motivo": motivo,
            "alerta": r["alerta"], "urgente": _urgente(r, hoje, cals),
            "codigo_ato": r["codigo_ato"],
            # há quantos dias a linha espera o Lex: a data de criação dela (`criado_em`); a tela
            # do prazo recomeça a conta no último «Ler de novo»
            "dias_na_fila": (_dias_desde(r["criado_em"], hoje)
                             if r["estado"] == "aguardando_lex" else 0),
            # o formulário devolve a versão que a tela mostrou (`versao_do_prazo`)
            "versao": versao_do_prazo(r),
            "selo_processo": (carteira.situacao(proc) if proc
                              and carteira.estado(proc) in PROCESSOS_DA_LISTA else "")}


# Prazo de um processo que o advogado marcou como «Não é meu»: o Lex não lê e nada espera dele
# (a mesma regra da fila de leitura, em `prazos_banco`). `{p}` é o apelido da tabela prazo.
_NAO_E_MEU = ("EXISTS (SELECT 1 FROM processo pr WHERE pr.numero = {p}.numero AND "
              + prazos_banco._DESCARTADO_SQL + ")")


OBSERVACAO_NA_APRESENTACAO = "Este prazo tem uma observação"
# Na apresentação só passa o que está nesta lista. Texto livre (do Lex, do Diário ou digitado)
# nunca está aqui: a máscara por nomes conhecidos não alcança «A autora Maria». Campo novo da tela
# fica de fora até alguém decidir que ele é seguro.
_SEGUROS_NA_APRESENTACAO = frozenset((
    "id", "numero", "numero_formatado", "estado", "estado_rotulo", "e_aviso", "tipo", "a_conferir",
    "fatal", "fatal_br", "fatal_legivel", "fatal_maior", "escolheu_maior", "fatal_informada",
    "fatal_alternativa_br", "fatal_sem_suspensao_rotulo", "fatal_alternativa_sem_suspensao_rotulo",
    "trabalho", "trabalho_br", "outro_dia", "atrasado", "urgente", "selo_processo", "versao",
    "dias", "unidade", "unidade_rotulo", "dobro", "disponibilizacao_br", "ciencia", "ciencia_br",
    "passos", "alternativa_passos", "com_suspensao", "versao_calendario", "origem_rotulo",
    "ato_da_lei", "artigo_da_lei", "codigo_ato", "fonte_da_conta", "dias_na_fila", "coberto_por",
    "lex_vai_ler", "processo_nao_e_meu", "pode_conferir", "pode_cumprir", "pode_ler_de_novo",
    "pode_escolher_fatal", "pode_informar_ciencia", "inicio_informado"))


def para_apresentacao(d: dict) -> dict:
    """A linha, a grade ou o detalhe de um prazo como o modo apresentação pode mostrar: sem o
    texto livre. O ato vira o da lista fechada (se o prazo tem código) ou «Prazo»; motivo e alerta
    viram um aviso fixo; trecho, ressalvas, avisos, órgão, link e cliente não vão."""
    seguro = {k: v for k, v in d.items() if k in _SEGUROS_NA_APRESENTACAO}
    legal = prazos_legais.PRAZOS.get(d.get("codigo_ato"))
    seguro.update(
        ato=legal.ato if legal else "Prazo", ato_gravado="", cliente="", trecho="", ressalvas=[],
        avisos=[], alternativa=bool(d.get("alternativa")), alternativa_avisos=[],
        publicacao_orgao="", publicacao_link="",
        motivo=OBSERVACAO_NA_APRESENTACAO if d.get("motivo") else "",
        alerta=OBSERVACAO_NA_APRESENTACAO if d.get("alerta") else "")
    return seguro


def lista(conn: sqlite3.Connection, hoje: date, cals: _Calendarios | None = None) -> dict:
    """Atrasados, Hoje, Esta semana e Depois só têm o que o advogado conferiu. O que espera o
    clique fica em «A conferir», em dois grupos: `a_conferir_proximos` (vence hoje ou depois, o
    mais perto primeiro, e o sem data no fim) e `a_conferir_vencidos` (o mais recente primeiro),
    de modo que uma pilha de vencidos nunca esconde o que vence hoje. `a_conferir` é os dois
    juntos, os próximos antes. «A definir» tem o que não tem data e o que tem data que não se lê.

    Não aparecem: a intimação do PJe que uma publicação já cobre (`pareado_com`, que o pareamento
    refaz sempre) e, em qualquer processo marcado como «Não é meu», o que não tem data (espera do
    Lex e «a definir», avisos do PJe inclusive). Só o que está sem data some, nunca um prazo com
    data."""
    grupos = {"atrasados": [], "hoje": [], "semana": [], "depois": [], "a_conferir": [],
              "a_conferir_proximos": [], "a_conferir_vencidos": [], "a_definir": []}
    domingo = hoje + timedelta(days=6 - hoje.weekday())
    cals = cals or _Calendarios(conn)
    for r in conn.execute(
            "SELECT * FROM prazo WHERE estado IN ('aguardando_lex', 'a_conferir', 'a_definir', "
            "'conferido') AND (estado IN ('a_conferir', 'conferido') OR (pareado_com IS NULL AND "
            f"NOT {_NAO_E_MEU.format(p='prazo')})) ORDER BY fatal IS NULL, fatal, id").fetchall():
        linha = _linha(r, hoje, cals)
        fatal = _ler(r["fatal"])
        if r["estado"] in _SEM_DATA:
            grupos["a_definir"].append(linha)
        elif fatal is None and (r["fatal"] or r["estado"] == "conferido"):
            linha["motivo"] = linha["motivo"] or _SEM_DATA_LEGIVEL
            grupos["a_definir"].append(linha)
        elif r["estado"] == "a_conferir":
            grupos["a_conferir_vencidos" if fatal and fatal < hoje
                   else "a_conferir_proximos"].append(linha)
        else:
            chave = ("atrasados" if fatal < hoje else "hoje" if fatal == hoje
                     else "semana" if fatal <= domingo else "depois")
            grupos[chave].append(linha)
    grupos["a_conferir_vencidos"].reverse()
    grupos["a_conferir"] = grupos["a_conferir_proximos"] + grupos["a_conferir_vencidos"]
    return grupos


def grade(conn: sqlite3.Connection, ref: date, hoje: date, vista: str) -> dict:
    """Semana ou mês em grade simples: cada dia com a data de trabalho e a fatal."""
    if vista == "semana":
        comeco = ref - timedelta(days=ref.weekday())
        fim = comeco + timedelta(days=6)
        anterior, proximo = ref - timedelta(days=7), ref + timedelta(days=7)
        titulo = f"Semana de {comeco.strftime('%d/%m')} a {fim.strftime('%d/%m/%Y')}"
    else:
        primeiro = ref.replace(day=1)
        seguinte = (primeiro + timedelta(days=32)).replace(day=1)
        comeco = primeiro - timedelta(days=primeiro.weekday())
        ultimo = seguinte - timedelta(days=1)
        fim = ultimo + timedelta(days=6 - ultimo.weekday())
        anterior, proximo = (primeiro - timedelta(days=1)).replace(day=1), seguinte
        titulo = f"{_MESES[ref.month - 1].capitalize()} de {ref.year}"
    itens: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM prazo WHERE estado IN ('a_conferir', 'conferido') AND "
                          "fatal IS NOT NULL AND ((fatal BETWEEN ? AND ?) OR (trabalho BETWEEN ? "
                          "AND ?)) ORDER BY id", (comeco.isoformat(), fim.isoformat(),
                                                  comeco.isoformat(), fim.isoformat())).fetchall():
        base = {"id": r["id"], "ato": r["ato"] or "Prazo", "codigo_ato": r["codigo_ato"],
                "numero_formatado": formatar_numero_cnj(r["numero"]),
                "a_conferir": r["estado"] == "a_conferir"}
        if r["trabalho"] and r["trabalho"] != r["fatal"]:
            itens.setdefault(r["trabalho"], []).append({**base, "tipo": "trabalho"})
        itens.setdefault(r["fatal"], []).append({**base, "tipo": "fatal"})
    dias = []
    d = comeco
    while d <= fim:
        dias.append({"data": d.isoformat(), "numero_do_dia": d.day, "e_hoje": d == hoje,
                     "fora_do_mes": vista == "mes" and d.month != ref.month,
                     "itens": itens.get(d.isoformat(), [])})
        d += timedelta(days=1)
    return {"titulo": titulo, "anterior": anterior.isoformat(), "proximo": proximo.isoformat(),
            "dias": dias}


def _perto(conn: sqlite3.Connection, hoje: date, cals: _Calendarios) -> list:
    """Os prazos com data que ainda não passaram e vencem antes do horizonte: os únicos que
    podem ser urgentes, e os únicos que precisam de calendário."""
    return conn.execute(
        "SELECT numero, origem_tipo, origem_ref, estado, fatal FROM prazo WHERE estado IN "
        f"('a_conferir', 'conferido') AND {_LEGIVEL.format(c='fatal')} AND fatal >= ? AND "
        "fatal < ?", (hoje.isoformat(), cals.horizonte(hoje))).fetchall()


def _contar(conn: sqlite3.Connection, sql_resto: str, *args) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM prazo WHERE {sql_resto}", args).fetchone()[0]


_DATADO = f"estado IN ('a_conferir', 'conferido') AND {_LEGIVEL.format(c='fatal')}"


def _plural(n: int, um: str, varios: str) -> str:
    return f"1 {um}" if n == 1 else f"{n} {varios}"


def faixa(conn: sqlite3.Connection, hoje: date, cals: _Calendarios | None = None) -> dict | None:
    """A faixa do topo. Cada número é um grupo da aba, que o advogado abre: o conferido que vence
    logo, o que ainda se confere e vence logo, o outro que ainda se confere (data de hoje em
    diante), o que está sem data (o mesmo «A definir»), o atrasado conferido e o vencido que ainda
    se confere. O vencido nunca aparece antes do que vence logo."""
    cals = cals or _Calendarios(conn)
    hoje_iso = hoje.isoformat()
    atrasados = _contar(conn, f"estado = 'conferido' AND {_LEGIVEL.format(c='fatal')} AND "
                              "fatal < ?", hoje_iso)
    vencidos = _contar(conn, f"estado = 'a_conferir' AND {_LEGIVEL.format(c='fatal')} AND "
                             "fatal < ?", hoje_iso)
    a_conferir = _contar(conn, "estado = 'a_conferir' AND (fatal IS NULL OR "
                               f"({_LEGIVEL.format(c='fatal')} AND fatal >= ?))", hoje_iso)
    # o grupo «A definir» da aba: sem data (sem o que o PJe já cobre e o «Não é meu») e data ilegível
    sem_data = _contar(
        conn, f"(estado IN {_SEM_DATA_SQL} AND pareado_com IS NULL AND "
              f"NOT {_NAO_E_MEU.format(p='prazo')}) OR ({_ILEGIVEL_NO_PRAZO} AND "
              "estado IN ('a_conferir', 'conferido'))")
    perto = _perto(conn, hoje, cals)
    vencem = [r["fatal"] for r in perto if r["estado"] == "conferido" and _urgente(r, hoje, cals)]
    urgentes = sum(1 for r in perto if r["estado"] == "a_conferir" and _urgente(r, hoje, cals))
    partes = []
    if vencem:
        ate = max(vencem)
        quando = ("hoje" if ate == hoje_iso else "amanhã" if ate == (
            hoje + timedelta(days=1)).isoformat() else _dia_mes(ate))
        if quando == "hoje":
            partes.append("1 prazo vence hoje" if len(vencem) == 1
                          else f"{len(vencem)} prazos vencem hoje")
        else:
            partes.append(f"1 prazo vence até {quando}" if len(vencem) == 1
                          else f"{len(vencem)} prazos vencem até {quando}")
    if urgentes:
        partes.append(f"{urgentes} a conferir com data urgente")
    if a_conferir - urgentes:
        partes.append(f"{a_conferir - urgentes} a conferir")
    if sem_data:
        partes.append(f"{sem_data} sem data")
    if atrasados:
        partes.append(_plural(atrasados, "atrasado", "atrasados"))
    if vencidos:
        partes.append(_plural(vencidos, "vencido a conferir", "vencidos a conferir"))
    return {"texto": " · ".join(partes)} if partes else None


def contador_nav(conn: sqlite3.Connection, hoje: date, cals: _Calendarios | None = None) -> int:
    """Os que vencem dentro do limite de urgência; os atrasados entram, e o que ainda falta
    conferir também, e o que tem data que não se lê."""
    cals = cals or _Calendarios(conn)
    atrasados = _contar(conn, f"{_DATADO} AND fatal < ?", hoje.isoformat())
    ilegiveis = _contar(conn, _ILEGIVEL_NO_PRAZO + " AND estado IN ('a_conferir', 'conferido')")
    return atrasados + ilegiveis + sum(_urgente(r, hoje, cals) for r in _perto(conn, hoje, cals))


def selo_do_cartao(conn: sqlite3.Connection, demanda_id: int, hoje: date,
                   cals: _Calendarios | None = None) -> dict | None:
    """Um selo só: o que ainda falta decidir vem primeiro; sem nada a decidir, a data que vence
    primeiro. Não conta como «a definir» a intimação que uma publicação já cobre, nem o que
    está sem data num processo marcado como «Não é meu» (nada espera ninguém)."""
    linhas = conn.execute(
        "SELECT p.numero, p.origem_tipo, p.origem_ref, p.estado, p.fatal, p.trabalho, "
        f"p.pareado_com, {_NAO_E_MEU.format(p='p')} AS nao_e_meu FROM prazo p "
        "WHERE p.demanda_id = ? ORDER BY p.fatal IS NULL, p.fatal, p.id", (demanda_id,)).fetchall()
    linhas = [r for r in linhas
              if not (r["estado"] in _SEM_DATA and (r["pareado_com"] or r["nao_e_meu"]))]
    if not linhas:
        return None
    cals = cals or _Calendarios(conn)
    estados = {r["estado"] for r in linhas}
    com_data = [r for r in linhas if r["estado"] in prazos_banco.COM_DATA]
    lidos = [r for r in com_data if _ler(r["fatal"])]
    alerta = any(_urgente(r, hoje, cals) for r in com_data)
    if "a_conferir" in estados:
        return {"texto": "Prazo a conferir", "tom": "alerta" if alerta else "aviso"}
    if estados & set(_SEM_DATA):
        return {"texto": "Prazo a definir", "tom": "alerta" if alerta else "aviso"}
    if len(lidos) < len(com_data):
        return {"texto": "Prazo com data ilegível", "tom": "alerta"}
    if lidos:
        r = lidos[0]
        texto = f"Fatal {_dia_mes(r['fatal'])}"
        if r["trabalho"] and r["trabalho"] != r["fatal"]:
            texto += f" · trabalho {_dia_mes(r['trabalho'])}"
        return {"texto": texto, "tom": "alerta" if alerta else "neutro"}
    if estados & {"sem_prazo", "descartado"} and "cumprido" not in estados:
        return {"texto": "Sem prazo", "tom": "neutro"}
    return None


def aviso_de_arquivar(conn: sqlite3.Connection, numeros: list[str], hoje: date) -> str:
    """O que está aberto nos processos que vão ser arquivados: quantos prazos, o próximo, o
    atrasado mais antigo e quantos estão sem data. A confirmação antes do clique usa esta mesma
    frase, dentro de um texto entre aspas simples: ela só leva números e datas. Fora o que a aba Prazos também esconde (a
    intimação coberta e o que está sem data em processo «Não é meu»)."""
    abertos = atrasados = sem_data = ilegiveis = 0
    proximo = desde = None
    for i in range(0, len(numeros), 400):
        parte = list(numeros[i:i + 400])
        aberta = (f"numero IN ({_marcas(parte)}) AND estado IN ('aguardando_lex', 'a_conferir', "
                  "'a_definir', 'conferido') AND (estado IN ('a_conferir', 'conferido') OR "
                  f"(pareado_com IS NULL AND NOT {_NAO_E_MEU.format(p='prazo')}))")
        r = conn.execute(
            f"SELECT COUNT(*), "
            f"SUM(estado IN ('aguardando_lex', 'a_definir')), "
            f"SUM({_ILEGIVEL_NO_PRAZO} AND estado IN ('a_conferir', 'conferido')), "
            f"SUM({_DATADO} AND fatal < ?), "
            f"MIN(CASE WHEN {_DATADO} AND fatal < ? THEN fatal END), "
            f"MIN(CASE WHEN {_DATADO} AND fatal >= ? THEN fatal END) "
            f"FROM prazo WHERE {aberta}",
            (hoje.isoformat(),) * 3 + tuple(parte)).fetchone()
        abertos += r[0]
        sem_data += r[1] or 0
        ilegiveis += r[2] or 0
        atrasados += r[3] or 0
        desde = min(filter(None, (desde, r[4])), default=None)
        proximo = min(filter(None, (proximo, r[5])), default=None)
    if not abertos:
        return ""
    partes = []
    if proximo:
        partes.append(f"próximo {_dia_mes(proximo)}")
    if atrasados:
        partes.append(f"{_plural(atrasados, 'atrasado', 'atrasados')} desde {_dia_mes(desde)}")
    if sem_data:
        partes.append(f"{sem_data} sem data")
    if ilegiveis:
        partes.append(f"{ilegiveis} com data ilegível")
    detalhes = f": {'; '.join(partes)}" if partes else ""
    return (f"{_plural(abertos, 'prazo aberto', 'prazos abertos')}{detalhes}. "
            "Continuam na aba Prazos.")


class _SemSuspensao(prazos_banco._Contexto):
    """A conta de sempre, com o calendário sem os dias de suspensão. Só a suspensão sai: o
    recesso e os feriados continuam fora da conta. Não é a data do réu preso, da Maria da Penha,
    da urgência ou dos alimentos: se o recesso vale para eles é decisão do advogado."""

    def __init__(self, conn: sqlite3.Connection, numero: str):
        super().__init__(conn, numero)
        self._sem: dict = {}

    def calendario(self, p) -> prazos.Calendario:
        cal = super().calendario(p)
        if id(cal) not in self._sem:
            self._sem[id(cal)] = dataclasses.replace(cal, suspensao={})
        return self._sem[id(cal)]


def _sem_suspensao(conn: sqlite3.Connection, r: dict) -> dict | None:
    """A conta deste prazo sem os dias de suspensão, pela mesma conta de verdade
    (`prazos_banco`), ou None se ela não sai. Só lê."""
    try:
        return prazos_banco._conta(conn, r, _SemSuspensao(conn, r["numero"]))
    except (ValueError, TypeError, OverflowError):
        return None


def _lados_sem_suspensao(fatal, alternativa, sem_a, sem_b) -> tuple:
    """Dá a cada data da tela a sua data sem a suspensão. A conta devolve as duas na ordem dela;
    aqui o par é pelas datas: a suspensão só atrasa, então a data sem ela nunca vem depois da
    que a tela mostra, e entre os pares possíveis fica o de menor deslocamento."""
    if not alternativa:
        return sem_a, None
    sem_b = sem_b or sem_a
    pares = [(sem_a, sem_b), (sem_b, sem_a)]
    dias = [_ler(x) for x in (fatal, alternativa, sem_a, sem_b)]
    if None in dias:
        return pares[0]
    base, outra = dias[0], dias[1]

    def custo(par):
        return abs((_ler(par[0]) - base).days) + abs((_ler(par[1]) - outra).days)

    validos = [p for p in pares if p[0] <= fatal and p[1] <= alternativa] or pares
    return min(validos, key=custo)


def _rotulo_sem_suspensao(iso: str | None) -> str:
    if not iso:
        return ""
    return (f"Sem a suspensão de fim de ano: {_br(iso)}. O recesso e os feriados continuam fora "
            "desta conta; em caso de réu preso, Maria da Penha, urgência ou alimentos, confira.")


def _origem(r: dict) -> tuple[str, str, str]:
    """De onde vieram os dias: o rótulo e, se vieram da lista fechada, o ato e o artigo dela.
    Data fatal digitada vale no lugar da conta: nada da lista aparece ao lado dela. Dias sem
    origem gravada são os que o advogado preencheu à mão pela primeira vez."""
    if r["fatal_informada"]:
        return "Data fatal informada por você", "", ""
    origem = r["origem_dos_dias"] or ("manual" if r["dias"] else "")
    legal = prazos_legais.PRAZOS.get(r["codigo_ato"]) if origem == "lei" else None
    return (ROTULOS_DE_ORIGEM.get(origem, ""), legal.ato if legal else "",
            legal.artigo if legal else "")


def _fonte_da_conta(r: dict) -> str:
    """De onde vem o que a tela mostra, para ela dizer só o que é verdade: `digitada` (a data
    fatal foi digitada), `aviso` (intimação do PJe, sem ato), `lex` (o Lex leu a publicação:
    há trecho e os dias vieram do texto, da decisão ou da lei) ou `manual` (o resto)."""
    if r["fatal_informada"]:
        return "digitada"
    if r["origem_tipo"] == "aviso" and not r["ato"]:
        return "aviso"
    if r["trecho"] and r["origem_dos_dias"] in ("texto", "decisao", "lei"):
        return "lex"
    return "manual"


def _dias_na_fila(conn: sqlite3.Connection, r: dict, hoje: date) -> int:
    """Há quantos dias a linha espera o Lex: desde a criação (`criado_em`) ou, se for depois, desde
    o último «Ler de novo» (o evento), que a põe de volta na fila."""
    ultimo = conn.execute(
        "SELECT MAX(quando) FROM evento WHERE tipo = 'prazo_ler_de_novo' AND numero = ? AND "
        "json_extract(dados, '$.prazo') = ?", (r["numero"], r["id"])).fetchone()[0]
    return min(_dias_desde(r["criado_em"], hoje), _dias_desde(ultimo, hoje)) if ultimo else (
        _dias_desde(r["criado_em"], hoje))


def _opcoes_de_trabalho(r: dict, cals: _Calendarios) -> list[dict]:
    """D-0 a D-3 do prazo; a escolhida é a que dá a data de trabalho gravada."""
    fatal = _ler(r["fatal"])
    if fatal is None or r["estado"] not in prazos_banco.COM_DATA:
        return []
    cal = cals.de(r)
    try:
        datas = [(d, prazos.data_de_trabalho(fatal, d, cal)) for d in prazos_banco.D_VALIDOS]
    except (ValueError, OverflowError):
        return []
    return [{"d": d, "data_br": prazos.dia_br(dia), "escolhido": r["trabalho"] == dia.isoformat()}
            for d, dia in datas]


def detalhe(conn: sqlite3.Connection, prazo_id: int, hoje: date) -> dict:
    """Tudo da tela de conferir um prazo. Data que não se lê aparece como está escrita."""
    r = prazos_banco.obter(conn, prazo_id)
    cals = _Calendarios(conn)
    calculo = _json(r["calculo"], {})
    alternativa = calculo.get("alternativa") if isinstance(calculo.get("alternativa"), dict) else None
    pub = None
    if r["origem_tipo"] == "publicacao" and str(r["origem_ref"]).isdigit():
        pub = conn.execute("SELECT data_disponibilizacao, orgao, link FROM publicacao WHERE id = ?",
                           (int(r["origem_ref"]),)).fetchone()
    link = (pub["link"] or "").strip() if pub else ""
    nao_e_meu = prazos_banco.processo_descartado(conn, r["numero"])
    linha = _linha(r, hoje, cals)
    avisos = _lista(calculo.get("avisos"))
    avisos_da_outra = _lista((alternativa or {}).get("avisos"))
    com_suspensao = prazos.AVISO_SUSPENSAO in avisos or prazos.AVISO_SUSPENSAO in avisos_da_outra
    sem = _sem_suspensao(conn, r) if com_suspensao and r["estado"] in prazos_banco.COM_DATA else None
    sem_fatal = sem_outra = None
    if sem is not None:
        sem_fatal, sem_outra = _lados_sem_suspensao(
            r["fatal"], r["fatal_alternativa"], sem["fatal"], sem["fatal_alternativa"])
    if r["estado"] == "aguardando_lex" and nao_e_meu:
        linha["motivo"] = _MOTIVO_NAO_SEU
    origem_rotulo, ato_da_lei, artigo_da_lei = _origem(r)
    if r["estado"] == "aguardando_lex":
        linha["dias_na_fila"] = _dias_na_fila(conn, r, hoje)
    return {**r, **linha, "ato_gravado": r["ato"], "fonte_da_conta": _fonte_da_conta(r),  # o que está gravado, sem o «Prazo» de vazio
            "passos": _lista(calculo.get("passos")), "avisos": avisos,
            "alternativa": alternativa, "alternativa_passos": _lista((alternativa or {}).get("passos")),
            "alternativa_avisos": avisos_da_outra,
            "ressalvas": _json(r["ressalvas"], []),
            "com_suspensao": com_suspensao, "fatal_sem_suspensao": sem_fatal,
            "fatal_sem_suspensao_br": _br(sem_fatal),
            "fatal_sem_suspensao_rotulo": _rotulo_sem_suspensao(sem_fatal),
            "fatal_alternativa_sem_suspensao_br": _br(sem_outra),
            "fatal_alternativa_sem_suspensao_rotulo": _rotulo_sem_suspensao(sem_outra),
            "origem_rotulo": origem_rotulo, "ato_da_lei": ato_da_lei,
            "artigo_da_lei": artigo_da_lei,
            "unidade_rotulo": ROTULOS_DE_UNIDADE.get(r["unidade"], ""),
            "disponibilizacao_br": _br(r["disponibilizacao"]), "ciencia_br": _br(r["ciencia"]),
            "publicacao_orgao": pub["orgao"] if pub and pub["orgao"] else "",
            # só endereço http ou https vira link (nada de javascript: vindo do tribunal)
            "publicacao_link": link if _LIGACAO.fullmatch(link) else "",
            "coberto_por": r["pareado_com"],
            "opcoes_d": _opcoes_de_trabalho(r, cals),
            "outro_dia": r["d_escolhido"] == prazos_banco.OUTRO_DIA,
            "escolheu_maior": bool(r["fatal_maior"]),
            "pode_escolher_fatal": bool(r["fatal_alternativa"])
            and r["estado"] in prazos_banco.COM_DATA,
            "pode_conferir": r["estado"] == "a_conferir" and bool(r["fatal"]),
            "pode_cumprir": r["estado"] in prazos_banco.ABERTOS,
            "processo_nao_e_meu": nao_e_meu,
            "lex_vai_ler": r["estado"] == "aguardando_lex" and not nao_e_meu,
            # num processo que não é do advogado a leitura ficaria parada: não se oferece
            "pode_ler_de_novo": r["origem_tipo"] == "publicacao" and not nao_e_meu
            and r["estado"] in ("a_definir", "sem_prazo", "a_conferir", "descartado"),
            "e_aviso": r["origem_tipo"] == "aviso",
            "pode_informar_ciencia": r["origem_tipo"] == "aviso"
            and r["estado"] not in ("cumprido", "descartado"),
            "lista_fechada": [{"codigo": p.codigo, "ato": p.ato, "dias": p.dias,
                               "unidade": ROTULOS_DE_UNIDADE.get(p.unidade, ""), "artigo": p.artigo}
                              for p in prazos_legais.PRAZOS.values()]}


def dias_do_calendario(conn: sqlite3.Connection, tribunal: str = "", ano: int | None = None) -> list[dict]:
    """Os dias da tabela, com a fonte e se ainda valem (`ativo`): os de todos e os do tribunal
    escolhido."""
    sql, args = "SELECT * FROM dia_forense WHERE 1 = 1", []
    if tribunal:
        sql += " AND tribunal IN ('', ?)"
        args.append(tribunal.strip().upper())
    if ano:
        sql += " AND substr(data, 1, 4) = ?"
        args.append(str(ano))
    return [{**dict(r), "ativo": bool(r["ativo"]), "data_br": _br(r["data"]),
             "tipo_rotulo": ROTULOS_DE_DIA[r["tipo"]],
             "origem_rotulo": ROTULOS_DE_ORIGEM_DO_DIA.get(r["origem"], "")}
            for r in conn.execute(sql + " ORDER BY data, tribunal, comarca, tipo", args)]


def agrupar_dias(dias: list[dict]) -> list[dict]:
    """Só para a tela: os dias seguidos com o mesmo tribunal, comarca, tipo, descrição, fonte e
    origem viram uma linha «de tal dia a tal dia». Nada muda na tabela: cada linha traz os dias
    que a formam, e cada dia segue com os seus botões. O dia retirado fica sozinho na linha dele e
    quebra o grupo. Cada linha: `dias` (em ordem de data), `grupo` (mais de uma data), `de_br` e
    `ate_br`."""
    def chave(d):
        return (d["tribunal"], d["comarca"], d["tipo"], d["descricao"], d["fonte"], d["origem"])

    linhas = []  # (primeira data, ordem de chegada, dias)
    soltos = [(i, d) for i, d in enumerate(dias) if not d["ativo"]]
    ativos = sorted(((i, d) for i, d in enumerate(dias) if d["ativo"]),
                    key=lambda x: (chave(x[1]), x[1]["data"]))
    for _, mesmos in groupby(ativos, key=lambda x: chave(x[1])):
        corrida: list[tuple[int, dict]] = []
        for i, d in mesmos:
            if corrida and date.fromisoformat(d["data"]) != (
                    date.fromisoformat(corrida[-1][1]["data"]) + timedelta(days=1)):
                linhas.append(corrida)
                corrida = []
            corrida.append((i, d))
        linhas.append(corrida)
    linhas += [[x] for x in soltos]
    linhas.sort(key=lambda corrida: (corrida[0][1]["data"], corrida[0][0]))
    return [{"dias": [d for _, d in c], "grupo": len(c) > 1,
             "de_br": _br(c[0][1]["data"]), "ate_br": _br(c[-1][1]["data"])} for c in linhas]


def lembrete_calendario(conn: sqlite3.Connection, hoje: date, siglas: list[str]) -> str:
    """A partir de novembro, lembra de cadastrar o calendário do ano seguinte."""
    if hoje.month < 11:
        return ""
    ano = hoje.year + 1
    faltam = [s for s in siglas if ano not in prazos_banco.carregar_calendario(conn, s).anos_tribunal]
    if not faltam:
        return ""
    return f"Falta cadastrar o calendário de {ano}: {', '.join(faltam)}."
