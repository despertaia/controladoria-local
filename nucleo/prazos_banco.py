"""O prazo no banco (1.6.0): calendário, estados, correção e eventos. A conta é de
`nucleo/prazos.py`. Estados: aguardando_lex → a_conferir | a_definir | sem_prazo → conferido
→ cumprido; mais descartado. Só `conferir`, um clique do advogado, leva a `conferido`. Nada
é apagado, e toda mudança registra evento.

Nada aqui faz commit numa transação que o chamador abriu (`with conn:`). A única exceção é
`isolado`: se não havia transação aberta, ele abre uma, e é ele quem a fecha."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from nucleo import config, eventos, leitura, prazos, prazos_legais

log = logging.getLogger(__name__)

ESTADOS = ("aguardando_lex", "a_conferir", "a_definir", "sem_prazo", "conferido", "cumprido",
           "descartado")
ABERTOS = ("aguardando_lex", "a_conferir", "a_definir", "conferido")
COM_DATA = ("a_conferir", "conferido")
ESTADOS_DE_ENTRADA = ("aguardando_lex", "a_conferir", "a_definir", "sem_prazo", "descartado")
OUTRO_DIA = -1
D_VALIDOS = (0, 1, 2, 3)
MAX_DIAS = 365
LIMITE_ATO = 120
ORIGENS_DO_LEX = ("decisao", "texto")
# horas e meses não entram na conta automática: só valem com a data fatal informada
UNIDADES_FORA_DA_CONTA = tuple(u for u in leitura.UNIDADES if u not in prazos.UNIDADES_DA_CONTA)
CHAVE_VERSAO = "prazos_calendario_versao"
CHAVE_D_PADRAO = "prazos_d_padrao"


class PrazoInvalido(ValueError):
    """Pedido incoerente com o estado do prazo ou dado mal formado (a mensagem vai à tela)."""


# --- calendário ------------------------------------------------------------------------

# O vocabulário fechado de vara e foro que pode vir antes do nome da comarca. Qualquer outra
# palavra antes (Lagoa DA Prata, Nova Cuiabá) quer dizer que o nome é de outra cidade: não
# casa, e a falta de casamento erra para a data mais cedo.
_VOCABULARIO = frozenset((
    "VARA VARAS COMARCA FORO JUIZADO JUIZADOS JUIZO ESPECIAL CIVEL CIVEIS CRIMINAL CRIMINAIS "
    "UNICA FAMILIA FAZENDA PUBLICA PUBLICAS SUCESSOES EXECUCOES FISCAIS INFANCIA JUVENTUDE "
    "EMPRESARIAL FEDERAL DE DA DO DAS DOS E").split())
_ORDINAL = re.compile(r"\d+[A-Z]{0,2}")
_TOKEN = re.compile(r"[A-Z0-9]+|[^A-Z0-9\s]")


def _sem_acento(texto: str | None) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(texto or ""))
                   if not unicodedata.combining(c)).upper()


def _palavras(texto: str | None) -> list[str]:
    """As palavras do nome, sem acento nem pontuação (del-Rei vira DEL REI)."""
    return re.findall(r"[A-Z0-9]+", _sem_acento(texto))


def _fichas(texto: str | None) -> list[str]:
    """Palavras e sinais do órgão. Hífen colado entre letras junta palavras (del-Rei); os
    demais sinais ficam como separador."""
    limpo = re.sub(r"(?<=[A-Z0-9])-(?=[A-Z0-9])", " ", _sem_acento(texto))
    return _TOKEN.findall(limpo)


def _eh_palavra(ficha: str) -> bool:
    return ficha[0].isalnum()


def comarca_casa(orgao: str | None, comarca: str | None) -> bool:
    """A comarca aparece, inteira, no texto do órgão julgador? Só vale se o que vem antes é
    nada, um separador ou vocabulário de vara («1ª Vara Cível de»), e o que vem depois é nada
    ou um separador («/MT», vírgula, parêntese). Do contrário, não casa."""
    alvo, fichas = _palavras(comarca), _fichas(orgao)
    n = len(alvo)
    if not n:
        return False
    for i in range(len(fichas) - n + 1):
        if fichas[i:i + n] != alvo:
            continue
        if i + n < len(fichas) and _eh_palavra(fichas[i + n]):
            continue
        j = i - 1
        while j >= 0 and _eh_palavra(fichas[j]) and (
                fichas[j] in _VOCABULARIO or _ORDINAL.fullmatch(fichas[j])):
            j -= 1
        if j < 0 or not _eh_palavra(fichas[j]):
            return True
    return False


def versao_calendario(conn: sqlite3.Connection) -> int:
    return int(config.ler(conn, CHAVE_VERSAO, "1"))


def carregar_calendario(conn: sqlite3.Connection, tribunal: str, orgao: str = "") -> prazos.Calendario:
    """Os dias que valem para o processo: os de todos, os do tribunal e os da comarca que
    casar com o órgão julgador. Sem casar, vale só o geral."""
    sigla = (tribunal or "").strip().upper()
    mapas: dict[str, dict] = {"sem_expediente": {}, "reduzido": {}, "suspensao": {}}
    anos_tribunal, anos_nacional, comarca_achada = set(), set(), ""
    for r in conn.execute("SELECT data, tribunal, comarca, tipo, descricao, origem FROM dia_forense "
                          "WHERE ativo = 1 AND tribunal IN ('', ?)", (sigla,)):
        if r["comarca"]:
            if not comarca_casa(orgao, r["comarca"]):
                continue
            comarca_achada = r["comarca"]
        d = date.fromisoformat(r["data"])
        mapas[r["tipo"]].setdefault(d, r["descricao"])
        if r["tribunal"] and not r["comarca"] and r["origem"] != "manual":
            anos_tribunal.add(d.year)
        if not r["tribunal"] and r["tipo"] == "sem_expediente" and r["origem"] == "lei":
            anos_nacional.add(d.year)
    return prazos.Calendario(tribunal=sigla, comarca=comarca_achada,
                             sem_expediente=mapas["sem_expediente"], reduzido=mapas["reduzido"],
                             suspensao=mapas["suspensao"], anos_tribunal=frozenset(anos_tribunal),
                             anos_nacional=frozenset(anos_nacional),
                             versao=versao_calendario(conn))


def _tribunal_e_orgao(conn: sqlite3.Connection, numero: str,
                      publicacao_id: int | None = None) -> tuple[str, str]:
    """Tribunal e órgão para o calendário e para a conta. O tribunal da publicação de onde o
    prazo veio decide, quando se sabe, e o órgão do ato vem antes do órgão do processo (F7). Sem
    ela, vale o do processo e, faltando, o da última publicação. O órgão do processo não serve a
    uma publicação de outro tribunal: sem órgão não casa comarca, e a data fica mais cedo."""
    do_ato, orgao_do_ato = "", ""
    if publicacao_id is not None:
        ato = conn.execute("SELECT tribunal, orgao FROM publicacao WHERE id = ? AND numero = ?",
                           (publicacao_id, numero)).fetchone()
        if ato:
            do_ato, orgao_do_ato = (ato["tribunal"] or "").strip(), (ato["orgao"] or "").strip()
    if do_ato and orgao_do_ato:
        return do_ato, orgao_do_ato
    p = conn.execute("SELECT tribunal, orgao_julgador FROM processo WHERE numero = ?",
                     (numero,)).fetchone()
    tribunal, orgao = (p["tribunal"] or "", p["orgao_julgador"] or "") if p else ("", "")
    if do_ato and tribunal.strip().upper() != do_ato.upper():
        return do_ato, ""
    orgao = orgao_do_ato or orgao
    if not tribunal or not orgao:
        pub = conn.execute("SELECT tribunal, orgao FROM publicacao WHERE numero = ? "
                           "ORDER BY data_disponibilizacao DESC, id DESC LIMIT 1",
                           (numero,)).fetchone()
        if pub:
            tribunal, orgao = tribunal or pub["tribunal"] or "", orgao or pub["orgao"] or ""
    return tribunal, orgao


def calendario_do_processo(conn: sqlite3.Connection, numero: str) -> prazos.Calendario:
    return carregar_calendario(conn, *_tribunal_e_orgao(conn, numero))


def _tribunal_e_orgao_do_prazo(conn: sqlite3.Connection, p: dict) -> tuple[str, str]:
    ref = p["origem_ref"] if p["origem_tipo"] == "publicacao" and str(p["origem_ref"]).isdigit() \
        else None
    return _tribunal_e_orgao(conn, p["numero"], int(ref) if ref is not None else None)


def calendario_do_prazo(conn: sqlite3.Connection, p: dict) -> prazos.Calendario:
    """O calendário deste prazo: pelo tribunal e o órgão da publicação de onde veio, se houver."""
    return carregar_calendario(conn, *_tribunal_e_orgao_do_prazo(conn, p))


def d_padrao(conn: sqlite3.Connection) -> int:
    """O D-x padrão do escritório (sem escolha, a fatal)."""
    try:
        d = int(config.ler(conn, CHAVE_D_PADRAO, "0"))
    except ValueError:
        return 0
    return d if d in D_VALIDOS else 0


def definir_d_padrao(conn: sqlite3.Connection, d: int, por: str = "advogado") -> None:
    if d not in D_VALIDOS:
        raise PrazoInvalido("Escolha D-0, D-1, D-2 ou D-3.")
    config.gravar(conn, CHAVE_D_PADRAO, str(d))
    eventos.registrar(conn, "prazo_d_padrao", None, {"d": d, "por": por})


# --- leitura e gravação do prazo -------------------------------------------------------

def obter(conn: sqlite3.Connection, prazo_id: int) -> dict:
    r = conn.execute("SELECT * FROM prazo WHERE id = ?", (prazo_id,)).fetchone()
    if r is None:
        raise PrazoInvalido("Prazo não encontrado.")
    return dict(r)


def abrir(conn: sqlite3.Connection, numero: str, demanda_id: int | None, origem_tipo: str,
          origem_ref: str, disponibilizacao: str | None, *, estado: str = "aguardando_lex",
          motivo: str = "", ordem: int = 0) -> int | None:
    if estado not in ESTADOS_DE_ENTRADA:
        raise PrazoInvalido("Um prazo novo não pode nascer conferido nem cumprido.")
    agora = eventos.agora()
    cur = conn.execute(
        "INSERT OR IGNORE INTO prazo (numero, demanda_id, origem_tipo, origem_ref, ordem, estado, "
        "motivo, disponibilizacao, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (numero, demanda_id, origem_tipo, origem_ref, ordem, estado, motivo, disponibilizacao,
         agora, agora))
    if not cur.rowcount:
        return None
    eventos.registrar(conn, "prazo_aberto", numero, {"prazo": cur.lastrowid, "origem": origem_tipo,
                                                     "ref": origem_ref})
    return cur.lastrowid


def _momento(conn: sqlite3.Connection, prazo_id: int) -> str:
    """O `atualizado_em` de uma mudança: sempre depois do que a linha já tinha, mesmo em duas
    mudanças no mesmo instante. A versão que uma tela mostrou (`fatal|atualizado_em`) só vale se
    toda mudança a move."""
    novo = eventos.agora_fino()
    r = conn.execute("SELECT atualizado_em FROM prazo WHERE id = ?", (prazo_id,)).fetchone()
    try:
        antigo = datetime.fromisoformat(r[0])
        atual = datetime.fromisoformat(novo)
        if atual <= antigo:
            return (antigo + timedelta(microseconds=1)).isoformat(timespec="microseconds")
    except (TypeError, ValueError):
        pass  # linha sem momento legível: vale o de agora
    return novo


def _gravar(conn: sqlite3.Connection, prazo_id: int, campos: dict) -> None:
    campos = {**campos, "atualizado_em": _momento(conn, prazo_id)}
    conn.execute(f"UPDATE prazo SET {', '.join(f'{c} = ?' for c in campos)} WHERE id = ?",
                 (*campos.values(), prazo_id))


def _data_valida(texto, rotulo: str) -> str:
    try:
        return date.fromisoformat(str(texto)).isoformat()
    except ValueError:
        raise PrazoInvalido(f"{rotulo} inválida.") from None


DATA_MINIMA = date(2000, 1, 1)
ANOS_A_FRENTE = 5  # limite de bom senso para uma data digitada, não prazo de lei


def _hoje_do_tribunal(hoje: date | None) -> date:
    if hoje is None:
        from nucleo import painel_dados  # import tardio
        hoje = painel_dados.hoje_cuiaba()
    return hoje


def _limite_a_frente(hoje: date) -> date:
    return date(hoje.year + ANOS_A_FRENTE, hoje.month, min(hoje.day, 28))


def _data_no_alcance(texto, rotulo: str, hoje: date | None) -> str:
    """Data digitada: legível e entre 01/01/2000 e cinco anos adiante. Fora disso quase sempre
    é erro de digitação, e a conta daria uma data absurda."""
    data = date.fromisoformat(_data_valida(texto, rotulo))
    limite = _limite_a_frente(_hoje_do_tribunal(hoje))
    if data < DATA_MINIMA or data > limite:
        raise PrazoInvalido(f"{rotulo} fora do alcance: use uma data entre "
                            f"{prazos.dia_br(DATA_MINIMA)} e {prazos.dia_br(limite)}.")
    return data.isoformat()


MSG_DISPONIBILIZACAO_ILEGIVEL = ("A data de disponibilização desta publicação não se lê. "
                                 "Informe a data fatal direto.")
MSG_CONTA_FORA = "Não foi possível fazer a conta com estes dados. Confira a unidade, os dias e as datas."
# Mensagens de `PrazoForaDaConta` que só trazem texto e números nossos; as outras citam o valor
# guardado e nunca vão à tela.
_CONTA_QUE_SE_MOSTRA = (prazos.MSG_FORA_DO_ALCANCE, "Sem data de disponibilização nem de início.")


def _ilegivel(texto) -> bool:
    try:
        date.fromisoformat(str(texto))
        return False
    except ValueError:
        return True


def _data_guardada_ilegivel(p: dict) -> str:
    """Por que a conta não leu este prazo, em frase fixa: qual das datas guardadas não se lê."""
    if p.get("fatal_informada") and _ilegivel(p["fatal_informada"]):
        return "A data fatal informada não se lê. Informe a data fatal de novo."
    if p.get("ciencia") and _ilegivel(p["ciencia"]):
        return "A data da ciência guardada não se lê. Informe a data fatal direto."
    if p.get("inicio_informado") and _ilegivel(p["inicio_informado"]):
        return "A data de início guardada não se lê. Informe a data fatal direto."
    if p.get("disponibilizacao") and _ilegivel(p["disponibilizacao"]):
        return MSG_DISPONIBILIZACAO_ILEGIVEL
    return MSG_CONTA_FORA


def _conta_para_a_tela(conn: sqlite3.Connection, p: dict) -> dict:
    """`_conta` para quem vai mostrar o erro ao advogado: só frase nossa, nunca o valor guardado
    nem o texto de uma exceção do Python."""
    try:
        return _conta(conn, p)
    except prazos.PrazoForaDaConta as exc:
        texto = str(exc)
        if texto in _CONTA_QUE_SE_MOSTRA or "limite da conta automática" in texto:
            raise PrazoInvalido(texto) from None
        raise PrazoInvalido(_data_guardada_ilegivel(p)) from None
    except (ValueError, TypeError, OverflowError):
        raise PrazoInvalido(_data_guardada_ilegivel(p)) from None


def _trabalho(conn: sqlite3.Connection, p: dict, fatal: date, cal: prazos.Calendario) -> str:
    d = p["d_escolhido"]
    if d == OUTRO_DIA and p["trabalho"]:
        return min(p["trabalho"], fatal.isoformat())
    if d not in D_VALIDOS:
        d = d_padrao(conn)
    return prazos.data_de_trabalho(fatal, d, cal).isoformat()


def _conta(conn: sqlite3.Connection, p: dict, ctx: "_Contexto | None" = None) -> dict:
    """Os campos da conta deste prazo, com o calendário de agora. `ctx`: o que um pareamento
    já carregou (calendários, intimações, contas repetidas), para não refazer por prazo."""
    ctx = ctx or _Contexto(conn, p["numero"])
    cal = ctx.calendario(p)
    if p["fatal_informada"]:
        fatal = date.fromisoformat(p["fatal_informada"])
        return {"publicacao": None, "inicio": None, "fatal": fatal.isoformat(),
                "fatal_alternativa": None, "fatal_maior": 0,
                "trabalho": _trabalho(conn, p, fatal, cal), "versao_calendario": cal.versao,
                "calculo": json.dumps({"passos": [f"Data fatal informada por você: "
                                                  f"{prazos.dia_br(fatal)}"],
                                       "avisos": [], "versao": cal.versao}, ensure_ascii=False)}
    dobro = bool(p["dobro"])
    nota = ""
    if p["origem_tipo"] == "aviso":
        # a data do aviso é a do envio, não a de uma publicação: a conta parte da ciência (ou
        # do início informado), e nada passa da ciência que acontece sozinha
        informada = date.fromisoformat(p["ciencia"]) if p["ciencia"] else None
        inicio = date.fromisoformat(p["inicio_informado"]) if p["inicio_informado"] else None
        sem_data = informada is None and _pendente_do_aviso(conn, p["origem_ref"]) == 0
        c, nota = _contar_do_aviso(cal, _dia_ou_none(p["disponibilizacao"]), informada, sem_data,
                                   p["dias"], p["unidade"], dobro, inicio)
    elif p["ciencia"]:
        c = prazos.contar_da_ciencia(date.fromisoformat(p["ciencia"]), p["dias"], p["unidade"],
                                     cal, dobro=dobro)
    elif p["inicio_informado"]:
        c = prazos.contar(None, p["dias"], p["unidade"], cal, dobro=dobro,
                          inicio=date.fromisoformat(p["inicio_informado"]))
    else:
        c = prazos.contar(date.fromisoformat(p["disponibilizacao"]), p["dias"], p["unidade"], cal,
                          dobro=dobro)
    calculo = c.como_dict()
    if nota:
        calculo["avisos"].append(nota)
    campos = {"publicacao": c.publicacao.isoformat() if c.publicacao else None,
              "inicio": c.inicio.isoformat(), "fatal": c.fatal.isoformat(),
              "fatal_alternativa": None, "trabalho": _trabalho(conn, p, c.fatal, cal),
              "versao_calendario": cal.versao}
    return _com_a_outra_data(conn, p, c, cal, campos, calculo, ctx)



# --- ações do advogado -----------------------------------------------------------------

def conferir(conn: sqlite3.Connection, prazo_id: int, por: str = "advogado") -> None:
    """O clique de «Conferir»: o único caminho até `conferido`."""
    p = obter(conn, prazo_id)
    if p["estado"] != "a_conferir" or not p["fatal"]:
        raise PrazoInvalido("Este prazo não está esperando conferência.")
    _gravar(conn, prazo_id, {"estado": "conferido", "conferido_em": eventos.agora(), "alerta": ""})
    eventos.registrar(conn, "prazo_conferido", p["numero"],
                      {"prazo": prazo_id, "fatal": p["fatal"], "por": por})


_CAMPOS_DA_CORRECAO = ("ato", "dias", "unidade", "inicio_informado", "dobro", "fatal_informada",
                       "ciencia", "fatal", "estado", "codigo_ato", "origem_dos_dias", "trecho",
                       "fixado_pelo_juiz", "ressalvas")


def _ressalvas_do(p: dict) -> list[str]:
    try:
        lista = json.loads(p["ressalvas"] or "[]")
    except ValueError:
        return []
    return [r for r in lista if isinstance(r, str)] if isinstance(lista, list) else []


def _ressalva_da_lista(codigo: str) -> str:
    legal = prazos_legais.PRAZOS.get(codigo)
    return legal.ressalva if legal else ""


def corrigir(conn: sqlite3.Connection, prazo_id: int, *, ato: str | None = None,
             dias: int | None = None, unidade: str | None = None, inicio: str | None = None,
             dobro: bool | None = None, fatal_informada: str | None = None,
             ciencia: str | None = None, codigo: str | None = None,
             por: str = "advogado", hoje: date | None = None) -> None:
    """Preencher à mão ou corrigir: refaz a conta e deixa o prazo «a conferir», com o que
    mudou no registro. Quem não veio fica como está. Com `codigo` da lista fechada, o ato,
    os dias e a unidade vêm dela. Prazo em horas ou meses só com a data fatal informada,
    porque a conta automática não os trata."""
    p = obter(conn, prazo_id)
    if p["estado"] == "cumprido":
        raise PrazoInvalido("Este prazo já foi cumprido. Desfaça antes de corrigir.")
    if unidade is not None and unidade not in leitura.UNIDADES:
        # vale também com data fatal informada, quando a conta não lê a unidade: não se guarda lixo
        raise PrazoInvalido("Escolha dias úteis, dias corridos, horas ou meses.")
    novo = dict(p)
    ressalvas = _ressalvas_do(p)
    if codigo:
        legal = prazos_legais.PRAZOS.get(codigo)
        if legal is None:
            raise PrazoInvalido("Ato fora da lista.")
        fatal_que_vale = p["fatal_informada"] if fatal_informada is None else fatal_informada
        if fatal_que_vale:
            raise PrazoInvalido("Este prazo tem data fatal informada, que vale no lugar da conta. "
                                "Limpe a data fatal para usar um ato da lista.")
        anterior = _ressalva_da_lista(p["codigo_ato"])
        ressalvas = [r for r in ressalvas if r != anterior]
        if legal.ressalva and legal.ressalva not in ressalvas:
            ressalvas.append(legal.ressalva)
        # a lista manda: o trecho da decisão e o «fixado pelo juiz» contradiriam os dias dela
        novo.update(ato=legal.ato, dias=legal.dias, unidade=legal.unidade, codigo_ato=legal.codigo,
                    origem_dos_dias="lei", trecho="", fixado_pelo_juiz=0)
        ato, dias, unidade = None, None, None  # o formulário manda os três preenchidos: vale a lista
    if ato is not None:
        novo["ato"] = " ".join(str(ato).split())[:LIMITE_ATO]
        if not novo["ato"]:
            raise PrazoInvalido("Diga o nome do ato.")
    if dias is not None:
        novo["dias"] = dias
    if unidade is not None:
        novo["unidade"] = unidade
    legal_atual = prazos_legais.PRAZOS.get(novo["codigo_ato"])
    if novo["codigo_ato"] and (
            (ato is not None and (legal_atual is None or novo["ato"] != legal_atual.ato))
            or (dias is not None and (legal_atual is None or dias != legal_atual.dias))
            or (unidade is not None and (legal_atual is None or unidade != legal_atual.unidade))):
        # a fonte tem de continuar verdadeira: mudou à mão, deixa de ser o prazo da lista
        ressalvas = [r for r in ressalvas if r != _ressalva_da_lista(novo["codigo_ato"])]
        novo.update(codigo_ato="", origem_dos_dias="manual")
    elif (not novo["codigo_ato"] and novo["origem_dos_dias"] in ORIGENS_DO_LEX
          and ((dias is not None and dias != p["dias"])
               or (unidade is not None and unidade != p["unidade"]))):
        novo["origem_dos_dias"] = "manual"  # o número agora é do advogado; trecho e ressalvas ficam
    novo["ressalvas"] = json.dumps(ressalvas, ensure_ascii=False)
    if inicio is not None:
        novo["inicio_informado"] = (_data_no_alcance(inicio, "Data de início", hoje)
                                    if inicio else None)
    if dobro is not None:
        novo["dobro"] = int(bool(dobro))
    if fatal_informada is not None:
        novo["fatal_informada"] = (_data_no_alcance(fatal_informada, "Data fatal", hoje)
                                   if fatal_informada else None)
    if ciencia is not None:
        novo["ciencia"] = _data_no_alcance(ciencia, "Data da ciência", hoje) if ciencia else None
        if novo["ciencia"]:
            # no aviso vale também «não antes do envio»; na publicação, só o futuro
            _conferir_dia_da_ciencia(novo["ciencia"], p["disponibilizacao"]
                                     if p["origem_tipo"] == "aviso" else None, hoje)
    if novo["fatal_informada"]:
        # horas e meses digitados agora, junto com a data fatal, valem: é o único jeito de
        # guardá-los. Com a data fatal já informada antes, dias novos continuam recusados.
        horas_agora = (not p["fatal_informada"] and bool(fatal_informada)
                       and novo["unidade"] in UNIDADES_FORA_DA_CONTA)
        if horas_agora and dias is not None and (
                not isinstance(dias, int) or not 1 <= dias <= MAX_DIAS):
            raise PrazoInvalido("Informe os dias, de 1 a 365.")
        if (dias is not None and not horas_agora) or ciencia or inicio or dobro:
            raise PrazoInvalido("Este prazo tem data fatal informada, que vale no lugar da conta. "
                                "Limpe a data fatal para corrigir dias, início, ciência ou dobro.")
    elif novo["ciencia"] and inicio:
        raise PrazoInvalido("Este prazo conta pela ciência, que vale no lugar da data de início. "
                            "Limpe a ciência para usar a data de início.")
    if not novo["fatal_informada"]:
        if not novo["unidade"]:
            raise PrazoInvalido("Informe se os dias são úteis ou corridos.")
        if novo["unidade"] not in prazos.UNIDADES_DA_CONTA:
            raise PrazoInvalido("Prazo em horas ou meses: informe a data fatal.")
        if not isinstance(novo["dias"], int) or not 1 <= novo["dias"] <= MAX_DIAS:
            raise PrazoInvalido("Informe os dias, de 1 a 365.")
        if not (novo["disponibilizacao"] or novo["inicio_informado"] or novo["ciencia"]):
            raise PrazoInvalido("Informe a data de início ou a da ciência.")
    novo["ato"] = novo["ato"] or "Prazo"
    conta = _conta_para_a_tela(conn, novo)
    novo.update(conta, estado="a_conferir", motivo="", alerta=_alerta_da_escolha(p, conta),
                leitura_reservada_ate=None, conferido_em=None)
    antes = {c: p[c] for c in _CAMPOS_DA_CORRECAO}
    depois = {c: novo[c] for c in _CAMPOS_DA_CORRECAO}
    _gravar(conn, prazo_id, {c: novo[c] for c in novo if c not in ("id", "atualizado_em")})
    eventos.registrar(conn, "prazo_corrigido", p["numero"],
                      {"prazo": prazo_id, "por": por, "antes": antes, "depois": depois})
    parear_processo(conn, p["numero"], ALERTA_PAREAMENTO)


def descartar(conn: sqlite3.Connection, prazo_id: int, por: str = "advogado") -> bool:
    """«Não há prazo aqui». O registro fica; o prazo só sai das listas e da agenda. Devolve se
    mudou (já descartado: nada se escreve)."""
    p = obter(conn, prazo_id)
    if p["estado"] == "cumprido":
        raise PrazoInvalido("Este prazo já foi cumprido.")
    if p["estado"] == "descartado":
        return False  # repetir não muda nada: sem escrita e sem evento
    _gravar(conn, prazo_id, {"estado": "descartado", "leitura_reservada_ate": None, "alerta": "",
                             "conferido_em": None})
    eventos.registrar(conn, "prazo_descartado", p["numero"],
                      {"prazo": prazo_id, "de": p["estado"], "por": por})
    parear_processo(conn, p["numero"], ALERTA_PAREAMENTO)
    return True


def escolher_trabalho(conn: sqlite3.Connection, prazo_id: int, d: int | None = None,
                      data: str | None = None, por: str = "advogado") -> None:
    """D-0 a D-3 em um clique, ou «Outro dia» (nunca depois da fatal)."""
    p = obter(conn, prazo_id)
    if not p["fatal"] or p["estado"] not in COM_DATA:
        raise PrazoInvalido("Este prazo ainda não tem data fatal.")
    if data:
        escolhida = _data_valida(data, "Data de trabalho")
        if escolhida > p["fatal"]:
            raise PrazoInvalido("A data de trabalho não pode passar da fatal.")
        campos = {"trabalho": escolhida, "d_escolhido": OUTRO_DIA}
    else:
        if d not in D_VALIDOS:
            raise PrazoInvalido("Escolha D-0, D-1, D-2 ou D-3.")
        cal = calendario_do_prazo(conn, p)
        campos = {"trabalho": prazos.data_de_trabalho(date.fromisoformat(p["fatal"]), d,
                                                      cal).isoformat(), "d_escolhido": d}
    _gravar(conn, prazo_id, campos)
    eventos.registrar(conn, "prazo_data_de_trabalho", p["numero"],
                      {"prazo": prazo_id, "trabalho": campos["trabalho"], "por": por})


def alternar_dobro(conn: sqlite3.Connection, prazo_id: int, ligado: bool,
                   por: str = "advogado") -> bool:
    """Liga ou desliga o dobro, que nunca é automático. A conta refeita pede conferência.
    Devolve se mudou: o dobro que já está como se pede não toca no prazo conferido."""
    p = obter(conn, prazo_id)
    if p["estado"] not in COM_DATA or p["fatal_informada"]:
        raise PrazoInvalido("Este prazo não tem conta para refazer.")
    if bool(ligado) == bool(p["dobro"]):
        return False
    novo = {**p, "dobro": int(bool(ligado))}
    conta = _conta_para_a_tela(conn, novo)
    campos = {"dobro": novo["dobro"], **conta, "estado": "a_conferir", "conferido_em": None,
              "alerta": _alerta_da_escolha(p, conta)}
    _gravar(conn, prazo_id, campos)
    eventos.registrar(conn, "prazo_dobro", p["numero"],
                      {"prazo": prazo_id, "ligado": bool(ligado), "fatal": campos["fatal"],
                       "por": por})
    return True


def cumprir(conn: sqlite3.Connection, prazo_id: int, por: str = "advogado",
            pelo_cartao: bool = False, parear: bool = True) -> None:
    p = obter(conn, prazo_id)
    if p["estado"] not in ABERTOS:
        raise PrazoInvalido("Este prazo não está em aberto.")
    _gravar(conn, prazo_id, {"estado": "cumprido", "cumprido_de": p["estado"],
                             "cumprido_em": eventos.agora(), "leitura_reservada_ate": None,
                             "cumprido_pelo_cartao": int(pelo_cartao)})
    eventos.registrar(conn, "prazo_cumprido", p["numero"], {"prazo": prazo_id, "por": por})
    if parear:
        parear_processo(conn, p["numero"], ALERTA_PAREAMENTO)


def desfazer_cumprido(conn: sqlite3.Connection, prazo_id: int, por: str = "advogado",
                      parear: bool = True, ctx: "_Contexto | None" = None) -> None:
    p = obter(conn, prazo_id)
    if p["estado"] != "cumprido":
        raise PrazoInvalido("Este prazo não está cumprido.")
    estado = p["cumprido_de"] or "a_conferir"
    _gravar(conn, prazo_id, {"estado": estado, "cumprido_de": None, "cumprido_em": None,
                             "cumprido_pelo_cartao": 0})
    eventos.registrar(conn, "prazo_cumprido_desfeito", p["numero"], {"prazo": prazo_id, "por": por})
    if estado in COM_DATA and p["fatal"] and not p["fatal_informada"]:
        # o calendário pode ter mudado enquanto estava cumprido: não volta com data velha
        recontar_seguro(conn, {**p, "estado": estado, "cumprido_de": None},
                        _alerta_ao_reabrir(conn, p), ctx)
    if parear:
        parear_processo(conn, p["numero"], ALERTA_PAREAMENTO)


def _alerta_ao_reabrir(conn: sqlite3.Connection, p: dict) -> str:
    """Por que a data mudou enquanto o prazo estava cumprido. Só o calendário muda sozinho; se o
    processo tem intimação do PJe, a causa pode ser outra, e o texto não afirma."""
    tem_aviso = conn.execute("SELECT 1 FROM prazo WHERE numero = ? AND origem_tipo = 'aviso' "
                             "AND estado != 'descartado'", (p["numero"],)).fetchone()
    if p["versao_calendario"] != versao_calendario(conn) and not tem_aviso:
        return ALERTA_CALENDARIO
    return ALERTA_REABERTO


def cumprir_do_cartao(conn: sqlite3.Connection, demanda_id: int) -> int:
    """O cartão foi a «Protocolado»: os prazos com data que o advogado já pode ver (a conferir
    ou conferido) viram cumpridos. O que ainda espera leitura ou está sem data continua
    aberto: nenhum humano viu uma data para eles, e a publicação ainda precisa ser lida."""
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM prazo WHERE demanda_id = ? AND estado IN ({_marcas(COM_DATA)}) ORDER BY id",
        (demanda_id, *COM_DATA)).fetchall()]
    for prazo_id in ids:
        cumprir(conn, prazo_id, por="protocolado", pelo_cartao=True, parear=False)
    if ids:  # uma vez só: o pareamento é do processo inteiro e roda dentro do movimento do cartão
        _parear_do_cartao(conn, demanda_id, ALERTA_PAREAMENTO)
    return len(ids)


def contar_sem_data_do_cartao(conn: sqlite3.Connection, demanda_id: int) -> int:
    """Quantos prazos do cartão seguem abertos sem data (esperando leitura ou «a definir»). Os
    de um processo que o advogado marcou como «não é meu» não esperam ninguém: não entram."""
    return conn.execute(
        "SELECT COUNT(*) FROM prazo WHERE demanda_id = ? "
        "AND estado IN ('aguardando_lex', 'a_definir') "
        f"AND NOT EXISTS (SELECT 1 FROM processo pr WHERE pr.numero = prazo.numero "
        f"AND {_DESCARTADO_SQL})", (demanda_id,)).fetchone()[0]


def desfazer_do_cartao(conn: sqlite3.Connection, demanda_id: int) -> int:
    """«Desfazer» do Protocolado: só volta o que o Protocolado marcou."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM prazo WHERE demanda_id = ? AND estado = 'cumprido' "
        "AND cumprido_pelo_cartao = 1 ORDER BY id", (demanda_id,)).fetchall()]
    d = conn.execute("SELECT numero FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    ctx = _Contexto(conn, d["numero"]) if d is not None else None
    for prazo_id in ids:
        desfazer_cumprido(conn, prazo_id, por="protocolado", parear=False, ctx=ctx)
    if ids:
        _parear_do_cartao(conn, demanda_id, ALERTA_PAREAMENTO)
    return len(ids)


def _marcas(valores) -> str:
    return ", ".join("?" for _ in valores)
# --- o calendário muda -----------------------------------------------------------------

TIPOS_DE_DIA = ("sem_expediente", "reduzido", "suspensao")
_ORIGENS_DE_DIA = ("ato", "manual")


def recontar(conn: sqlite3.Connection, p: dict, alerta_texto: str,
             ctx: "_Contexto | None" = None) -> bool:
    """Refaz a conta de um prazo com data. Se a fatal mudou, o advogado é avisado: o que ele
    conferiu foi outra data, então o prazo volta a pedir conferência. Devolve se mudou."""
    campos = _conta(conn, p, ctx)
    desfeita = _escolha_desfeita(p, campos)
    mudou = campos["fatal"] != p["fatal"] or desfeita
    if not mudou and p["alerta"] == ALERTA_SEM_CONTA:
        campos["alerta"] = ""  # a conta voltou a sair: o aviso de «refaça» não vale mais
    if mudou:
        campos.update(estado="a_conferir", conferido_em=None, alerta=_alerta_da_mudanca(
            p, campos["fatal"], alerta_texto, desfeita))
        eventos.registrar(conn, "prazo_recalculado", p["numero"],
                          {"prazo": p["id"], "de": p["fatal"], "para": campos["fatal"],
                           "estado_antes": p["estado"]})
    elif all(p[c] == v for c, v in campos.items()):
        return False  # nada mudou: sem escrita, sem mexer em quem mexeu por último
    _gravar(conn, p["id"], campos)
    return mudou


ALERTA_CALENDARIO = "O calendário mudou: a data fatal era {de} e agora é {para}."
ALERTA_REABERTO = ("A data fatal mudou enquanto o prazo estava cumprido: era {de} e agora é "
                   "{para}.")
ALERTA_SEM_CONTA = "A data não pôde ser refeita; refaça a conta."


def recontar_seguro(conn: sqlite3.Connection, p: dict, alerta_texto: str,
                    ctx: "_Contexto | None" = None) -> bool:
    """Como `recontar`, mas um prazo cuja conta não sai (dado ruim) não trava ninguém: fica com
    a data antiga, volta a `a_conferir` e avisa. Devolve se o prazo pede atenção do advogado."""
    try:
        return recontar(conn, p, alerta_texto, ctx)
    except (ValueError, TypeError) as exc:
        if p["estado"] == "a_conferir" and p["alerta"] == ALERTA_SEM_CONTA:
            return True  # já está avisado: não escreve nem registra de novo a cada pareamento
        _gravar(conn, p["id"], {"estado": "a_conferir", "conferido_em": None,
                                "alerta": ALERTA_SEM_CONTA})
        eventos.registrar(conn, "prazo_recalculo_falhou", p["numero"],
                          {"prazo": p["id"], "estado_antes": p["estado"], "erro": str(exc)[:200]})
        return True


def recalcular_abertos(conn: sqlite3.Connection) -> int:
    """Refaz a conta dos prazos com data. Quem tem data informada à mão não muda de data nem de
    estado (só a data de trabalho acompanha o calendário). Devolve quantos pedem atenção."""
    atencao = 0
    for r in conn.execute(f"SELECT * FROM prazo WHERE estado IN ({_marcas(COM_DATA)}) "
                          "ORDER BY id", COM_DATA).fetchall():
        atencao += recontar_seguro(conn, dict(r), ALERTA_CALENDARIO)
    return atencao


def _calendario_mudou(conn: sqlite3.Connection) -> int:
    config.gravar(conn, CHAVE_VERSAO, str(versao_calendario(conn) + 1))
    return recalcular_abertos(conn)


def _tribunal_conhecido(conn: sqlite3.Connection, sigla: str) -> bool:
    """A sigla já aparece no calendário, nos processos ou nas publicações."""
    return conn.execute(
        "SELECT 1 FROM dia_forense WHERE tribunal = ? UNION "
        "SELECT 1 FROM processo WHERE UPPER(tribunal) = ? UNION "
        "SELECT 1 FROM publicacao WHERE UPPER(tribunal) = ? LIMIT 1",
        (sigla, sigla, sigla)).fetchone() is not None


def comarca_casa_com_algum(conn: sqlite3.Connection, tribunal: str, comarca: str) -> bool:
    """Algum órgão julgador (do processo ou de publicação) do tribunal traz o nome da comarca."""
    sigla = (tribunal or "").strip().upper()
    filtro = "AND UPPER(tribunal) = ?" if sigla else ""
    args = (sigla,) if sigla else ()
    for tabela, coluna in (("processo", "orgao_julgador"), ("publicacao", "orgao")):
        for r in conn.execute(f"SELECT DISTINCT {coluna} FROM {tabela} WHERE {coluna} IS NOT NULL "
                              f"{filtro}", args):
            if comarca_casa(r[0], comarca):
                return True
    return False


def incluir_dia(conn: sqlite3.Connection, data: str, tribunal: str, comarca: str, tipo: str,
                descricao: str, fonte: str, origem: str = "manual",
                por: str = "advogado") -> tuple[int, int]:
    """Inclui a data no calendário (ou reativa e atualiza a mesma, se estava desativada).
    Devolve (id da linha, quantos prazos mudaram de data)."""
    data = _data_no_alcance(data, "Data", None)
    descricao, fonte = " ".join(str(descricao or "").split()), " ".join(str(fonte or "").split())
    if tipo not in TIPOS_DE_DIA:
        raise PrazoInvalido("Escolha o tipo do dia.")
    if not descricao or not fonte:
        raise PrazoInvalido("Diga o que é o dia e a fonte.")
    if origem not in _ORIGENS_DE_DIA:
        raise PrazoInvalido("Origem inválida.")
    tribunal = (tribunal or "").strip().upper()
    comarca = " ".join(str(comarca or "").split())
    if tribunal and not _tribunal_conhecido(conn, tribunal):
        raise PrazoInvalido("Tribunal desconhecido. Use a sigla de um tribunal que o sistema já "
                            "conhece, ou deixe em branco para todos.")
    conn.execute(
        "INSERT INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem, ativo, "
        "atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?) "
        "ON CONFLICT (data, tribunal, comarca, tipo) DO UPDATE SET descricao = excluded.descricao, "
        "fonte = excluded.fonte, origem = excluded.origem, ativo = 1, "
        "atualizado_em = excluded.atualizado_em",
        (data, tribunal, comarca, tipo, descricao, fonte, origem, eventos.agora()))
    dia_id = conn.execute("SELECT id FROM dia_forense WHERE data = ? AND tribunal = ? AND "
                          "comarca = ? AND tipo = ?", (data, tribunal, comarca, tipo)).fetchone()[0]
    eventos.registrar(conn, "dia_forense_incluido", None,
                      {"dia": dia_id, "data": data, "tribunal": tribunal, "comarca": comarca,
                       "tipo": tipo, "por": por})
    return dia_id, _calendario_mudou(conn)


def _dia(conn: sqlite3.Connection, dia_id: int) -> sqlite3.Row:
    d = conn.execute("SELECT * FROM dia_forense WHERE id = ?", (dia_id,)).fetchone()
    if d is None:
        raise PrazoInvalido("Dia não encontrado.")
    return d


def corrigir_dia(conn: sqlite3.Connection, dia_id: int, *, tipo: str | None = None,
                 descricao: str | None = None, fonte: str | None = None,
                 por: str = "advogado") -> int:
    d = _dia(conn, dia_id)
    novo_tipo = tipo or d["tipo"]
    if novo_tipo not in TIPOS_DE_DIA:
        raise PrazoInvalido("Escolha o tipo do dia.")
    novo_nome = " ".join(str(descricao).split()) if descricao is not None else d["descricao"]
    nova_fonte = " ".join(str(fonte).split()) if fonte is not None else d["fonte"]
    if not novo_nome or not nova_fonte:
        raise PrazoInvalido("Diga o que é o dia e a fonte.")
    try:
        conn.execute("UPDATE dia_forense SET tipo = ?, descricao = ?, fonte = ?, atualizado_em = ? "
                     "WHERE id = ?", (novo_tipo, novo_nome, nova_fonte, eventos.agora(), dia_id))
    except sqlite3.IntegrityError:
        raise PrazoInvalido("Já há este dia com esse tipo.") from None
    eventos.registrar(conn, "dia_forense_corrigido", None,
                      {"dia": dia_id, "antes": {"tipo": d["tipo"], "descricao": d["descricao"],
                                                "fonte": d["fonte"]},
                       "depois": {"tipo": novo_tipo, "descricao": novo_nome, "fonte": nova_fonte},
                       "por": por})
    return _calendario_mudou(conn)


def desativar_dia(conn: sqlite3.Connection, dia_id: int, por: str = "advogado") -> int:
    """Tira o dia da conta sem apagar a linha."""
    d = _dia(conn, dia_id)
    if not d["ativo"]:
        return 0
    conn.execute("UPDATE dia_forense SET ativo = 0, atualizado_em = ? WHERE id = ?",
                 (eventos.agora(), dia_id))
    eventos.registrar(conn, "dia_forense_desativado", None,
                      {"dia": dia_id, "data": d["data"], "por": por})
    return _calendario_mudou(conn)


# --- a fila de leituras do Lex ---------------------------------------------------------

RESERVA_LEITURA_MINUTOS = 10
TENTATIVAS_DE_LEITURA = 3
MOTIVOS_DE_FALHA = ("erro", "tempo", "limite", "acesso")
_NAO_GASTAM_TENTATIVA = ("limite", "acesso")
# O limite do plano não conta, mas uma publicação não pode segurar a fila para sempre (o texto
# dela pode induzir a resposta): a cada 4 limites registrados na linha, um conta como tentativa.
# Com 3 tentativas são 12 rodadas, umas 6 horas, mais que um limite de 5 horas.
LIMITES_POR_TENTATIVA = 4
_CAMPOS_DO_ITEM = ("ato", "codigo_ato", "dias", "unidade", "rito", "origem_dos_dias", "trecho",
                   "fixado_pelo_juiz", "motivo")
_MOTIVO_NAO_GRAVOU = "A resposta do Lex não pôde ser gravada. Preencha à mão ou peça para ler de novo."
# O que «ler de novo» limpa em toda linha da publicação: nada do que veio da leitura anterior
# nem do que o advogado pôs sobre ela sobrevive à nova leitura (o que havia fica no evento).
_ZERO_DA_LEITURA = {
    "motivo": "", "alerta": "", "ato": "", "codigo_ato": "", "dias": None, "unidade": "",
    "rito": "", "origem_dos_dias": "", "trecho": "", "fixado_pelo_juiz": 0, "dobro": 0,
    "publicacao": None, "inicio": None, "inicio_informado": None, "ciencia": None,
    "fatal": None, "fatal_alternativa": None, "fatal_maior": 0, "fatal_informada": None,
    "trabalho": None, "d_escolhido": None, "calculo": "{}", "versao_calendario": None,
    "ressalvas": "[]", "tentativas": 0, "leitura_reservada_ate": None, "pareado_com": None,
    "conferido_em": None}
_ANTES_DE_LER_DE_NOVO = ("estado", "ato", "codigo_ato", "dias", "unidade", "rito", "dobro",
                         "ciencia", "inicio_informado", "fatal_informada", "fatal", "trabalho",
                         "d_escolhido", "origem_dos_dias", "motivo")


def _utc(momento: datetime) -> str:
    """O instante em UTC, no mesmo formato sempre: comparar texto só vale assim."""
    return momento.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _texto_de(conn: sqlite3.Connection, p: dict) -> tuple[str, bool]:
    """O texto da publicação de onde o prazo veio, e se o original passou do limite."""
    ref = str(p["origem_ref"])
    if not (ref.isascii() and ref.isdigit() and len(ref) <= 18):
        return "", False
    r = conn.execute("SELECT texto FROM publicacao WHERE id = ?", (int(ref),)).fetchone()
    bruto = r["texto"] if r else ""
    return leitura.texto_da_publicacao(bruto), leitura.bruto_longo_demais(bruto)


def _sem_leitura(conn: sqlite3.Connection, p: dict, motivo: str) -> None:
    _gravar(conn, p["id"], {"estado": "a_definir", "motivo": motivo, "leitura_reservada_ate": None})
    eventos.registrar(conn, "prazo_lido", p["numero"], {"prazo": p["id"], "estado": "a_definir"})


def proxima_leitura(conn: sqlite3.Connection, agora: datetime) -> dict | None:
    """Reserva a próxima publicação que espera leitura: as dos últimos `JANELA_FILA_DIAS` dias
    primeiro, da mais antiga para a mais nova, e depois o acumulado, na mesma ordem. Sem texto (ou com texto demais
    para um pedido curto), vira «a definir» sem gastar o Lex. A reserva é um UPDATE
    condicional: dois leitores nunca levam a mesma publicação."""
    if not config.ler(conn, CHAVE_ALCANCE):
        criar_dos_cartoes_abertos(conn)  # a primeira leitura não espera uma varredura
    agora_utc = _utc(agora)
    recentes_desde = (agora.date() - timedelta(days=JANELA_FILA_DIAS)).isoformat()
    while True:
        r = conn.execute(
            "SELECT * FROM prazo WHERE estado = 'aguardando_lex' AND origem_tipo = 'publicacao' "
            "AND (leitura_reservada_ate IS NULL OR leitura_reservada_ate < ?) "
            f"AND NOT EXISTS (SELECT 1 FROM processo pr WHERE pr.numero = prazo.numero "
            f"AND {_DESCARTADO_SQL}) "
            "ORDER BY CASE WHEN disponibilizacao >= ? THEN 0 ELSE 1 END, disponibilizacao, id "
            "LIMIT 1", (agora_utc, recentes_desde)).fetchone()
        if r is None:
            return None
        p = dict(r)
        texto, longo = _texto_de(conn, p)
        if not texto or longo or len(texto) > leitura.LIMITE_TEXTO:
            _sem_leitura(conn, p, "Publicação sem texto. Abra no tribunal e preencha à mão."
                         if not texto else
                         "Publicação longa demais para a leitura automática. Preencha à mão.")
            continue
        ate = _utc(agora + timedelta(minutes=RESERVA_LEITURA_MINUTOS))
        cur = conn.execute(
            "UPDATE prazo SET leitura_reservada_ate = ?, atualizado_em = ? WHERE id = ? "
            "AND estado = 'aguardando_lex' AND (leitura_reservada_ate IS NULL "
            "OR leitura_reservada_ate < ?)", (ate, eventos.agora_fino(), p["id"], agora_utc))
        if not cur.rowcount:
            continue
        return {"id": p["id"], "pedido": leitura.montar_pedido(texto), "reserva": ate}


def _gravar_item(conn: sqlite3.Connection, p: dict, item: dict) -> None:
    campos = {c: item[c] for c in _CAMPOS_DO_ITEM}
    campos["fixado_pelo_juiz"] = int(item["fixado_pelo_juiz"])
    campos.update(estado=item["estado"], leitura_reservada_ate=None, tentativas=0,
                  ressalvas=json.dumps(item["ressalvas"], ensure_ascii=False))
    motivo = ""
    if item["estado"] == "a_conferir":
        tribunal, _ = _tribunal_e_orgao_do_prazo(conn, p)
        motivo = prazos.fora_da_conta(p["numero"], tribunal, item["rito"])
    if not motivo and item["estado"] == "a_conferir":
        try:
            campos.update(_conta(conn, {**p, **campos}))
        except prazos.PrazoForaDaConta as exc:
            motivo = str(exc)
        except (ValueError, TypeError, OverflowError):
            motivo = "A data de disponibilização está ausente ou fora do alcance. Preencha à mão."
    if motivo:  # o número recusado não fica guardado
        campos.update(estado="a_definir", motivo=motivo, dias=None, unidade="")
    _gravar(conn, p["id"], campos)


def _linha_da_ordem(conn: sqlite3.Connection, p: dict, ordem: int) -> dict:
    """A linha do prazo `ordem` da mesma publicação: nova, ou a descartada num ler de novo."""
    novo = abrir(conn, p["numero"], p["demanda_id"], p["origem_tipo"], p["origem_ref"],
                 p["disponibilizacao"], ordem=ordem)
    if novo is not None:
        return obter(conn, novo)
    r = conn.execute("SELECT * FROM prazo WHERE origem_tipo = ? AND origem_ref = ? AND ordem = ?",
                     (p["origem_tipo"], p["origem_ref"], ordem)).fetchone()
    return dict(r)


def _espera_leitura(conn: sqlite3.Connection, prazo_id: int) -> dict:
    p = obter(conn, prazo_id)
    if p["origem_tipo"] != "publicacao":
        raise PrazoInvalido("Este prazo não vem de uma publicação.")
    if p["estado"] != "aguardando_lex":
        raise PrazoInvalido("Este prazo não espera mais a leitura.")
    return p


def aplicar_leitura(conn: sqlite3.Connection, prazo_id: int, bruta: str,
                    reserva: str | None = None) -> str:
    """Confere a resposta do Lex e grava: uma linha por prazo. Devolve o estado da primeira.
    Se o prazo já não espera leitura (o advogado preencheu), nada muda. A trava de escrita vem
    antes da conferência do estado: uma correção não entra entre ela e a gravação. Com `reserva`
    (a que `proxima_leitura` entregou), a resposta de um leitor cuja reserva venceu e passou
    adiante é recusada quieta, como em `falhar_leitura`, e devolve o estado que está lá."""
    abriu = not conn.in_transaction
    if abriu:
        conn.execute("BEGIN IMMEDIATE")
    try:
        p = _espera_leitura(conn, prazo_id)
    except PrazoInvalido:
        if abriu:
            conn.rollback()
        raise
    if reserva is not None and p["leitura_reservada_ate"] != reserva:
        if abriu:
            conn.rollback()
        return p["estado"]
    texto, longo = _texto_de(conn, p)
    if longo or len(texto) > leitura.LIMITE_TEXTO:
        r = {"erro": "Publicação longa demais para a leitura automática. Preencha à mão.",
             "sem_prazo": False, "itens": []}
    else:
        try:
            r = leitura.validar_resposta(bruta, texto)
        except Exception:  # qualquer surpresa na conferência acaba em «a definir», nunca em prazo
            r = {"erro": "A resposta do Lex não pôde ser conferida. Preencha à mão ou peça para "
                         "ler de novo.", "sem_prazo": False, "itens": []}
    if r["erro"] or r["sem_prazo"]:
        estado = "a_definir" if r["erro"] else "sem_prazo"
        _gravar(conn, prazo_id, {"estado": estado, "motivo": r["erro"],
                                 "leitura_reservada_ate": None})
        eventos.registrar(conn, "prazo_lido", p["numero"], {"prazo": prazo_id, "estado": estado})
        parear_processo(conn, p["numero"], ALERTA_PAREAMENTO)
        return estado
    lidas = []
    for ordem, item in enumerate(r["itens"]):
        linha = p if ordem == 0 else _linha_da_ordem(conn, p, ordem)
        if linha["estado"] not in ("aguardando_lex", "descartado"):
            continue
        try:
            _gravar_item(conn, linha, item)
        except (sqlite3.Error, ValueError, TypeError, OverflowError):
            _gravar(conn, linha["id"], {"estado": "a_definir", "motivo": _MOTIVO_NAO_GRAVOU,
                                        "dias": None, "unidade": "", "leitura_reservada_ate": None})
        lidas.append(linha["id"])
    for id_ in lidas:
        eventos.registrar(conn, "prazo_lido", p["numero"],
                          {"prazo": id_, "estado": obter(conn, id_)["estado"]})
    parear_processo(conn, p["numero"], ALERTA_PAREAMENTO)
    return obter(conn, prazo_id)["estado"]


def falhar_leitura(conn: sqlite3.Connection, prazo_id: int, motivo: str,
                   reserva: str | None = None) -> None:
    """O Lex não respondeu. Limite do plano e acesso recusado não contam: a leitura espera o
    Lex voltar (o limite deixa um evento na linha, e a cada `LIMITES_POR_TENTATIVA` deles conta
    uma tentativa: nenhuma publicação segura a fila para sempre). Erro e tempo contam; na
    terceira, «a definir». Com `reserva` (a que
    `proxima_leitura` entregou), a falha de um leitor cuja reserva já venceu e passou a outro
    não gasta a tentativa do novo."""
    if motivo not in MOTIVOS_DE_FALHA:
        raise PrazoInvalido("Motivo desconhecido.")
    if not conn.in_transaction:  # a trava vem antes de olhar: uma correção não se perde
        conn.execute("BEGIN IMMEDIATE")
    p = obter(conn, prazo_id)
    if p["estado"] != "aguardando_lex":
        raise PrazoInvalido("Este prazo não espera mais a leitura.")
    if reserva is not None and p["leitura_reservada_ate"] != reserva:
        return
    conta = False
    if motivo == "limite":
        eventos.registrar(conn, "prazo_leitura_limite", p["numero"], {"prazo": prazo_id})
        conta = _limites_desde_a_ultima_leitura(conn, p) % LIMITES_POR_TENTATIVA == 0
    if motivo in _NAO_GASTAM_TENTATIVA and not conta:
        _gravar(conn, prazo_id, {"leitura_reservada_ate": None})
        return
    n = p["tentativas"] + 1
    campos = {"tentativas": n, "leitura_reservada_ate": None}
    if n >= TENTATIVAS_DE_LEITURA:
        campos.update(estado="a_definir",
                      motivo=f"O Lex não conseguiu ler depois de {n} tentativas. "
                             "Preencha à mão ou peça para ler de novo.")
    _gravar(conn, prazo_id, campos)
    eventos.registrar(conn, "prazo_leitura_falhou", p["numero"],
                      {"prazo": prazo_id, "motivo": motivo, "tentativas": n})


def _limites_desde_a_ultima_leitura(conn: sqlite3.Connection, p: dict) -> int:
    """Quantas vezes o Lex respondeu «limite» para esta linha depois do último «ler de novo»."""
    desde = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM evento WHERE tipo = 'prazo_ler_de_novo' AND numero = ? "
        "AND json_extract(dados, '$.prazo') = ?", (p["numero"], p["id"])).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM evento WHERE tipo = 'prazo_leitura_limite' AND numero = ? "
        "AND json_extract(dados, '$.prazo') = ? AND id > ?",
        (p["numero"], p["id"], desde)).fetchone()[0]


def ler_de_novo(conn: sqlite3.Connection, prazo_id: int, por: str = "advogado") -> None:
    """«Ler de novo»: a publicação volta para a fila. Toda linha dela é zerada (nada da leitura
    anterior, nem do que o advogado pôs sobre ela, passa para a nova), e o que havia fica no
    evento. A primeira espera o Lex; as outras ficam descartadas até a nova leitura."""
    p = obter(conn, prazo_id)
    if p["origem_tipo"] != "publicacao" or p["estado"] not in ("a_definir", "sem_prazo",
                                                               "a_conferir", "descartado"):
        raise PrazoInvalido("Este prazo não pode ser lido de novo.")
    linhas = [dict(r) for r in conn.execute(
        "SELECT * FROM prazo WHERE origem_tipo = ? AND origem_ref = ? ORDER BY ordem",
        (p["origem_tipo"], p["origem_ref"])).fetchall()]
    if any(r["estado"] in ("conferido", "cumprido") for r in linhas):
        raise PrazoInvalido("Outro prazo desta publicação já foi conferido. Corrija à mão.")
    for r in linhas:
        if r["ordem"] == 0:
            estado = "aguardando_lex"
        else:
            estado = "descartado"
        _gravar(conn, r["id"], {**_ZERO_DA_LEITURA, "estado": estado})
        eventos.registrar(conn, "prazo_ler_de_novo", p["numero"],
                          {"prazo": r["id"], "por": por,
                           "antes": {c: r[c] for c in _ANTES_DE_LER_DE_NOVO}})
    parear_processo(conn, p["numero"], ALERTA_PAREAMENTO)


# --- de onde nascem os prazos ----------------------------------------------------------

CHAVE_ALCANCE = "prazos_alcance_em"
_COLUNAS_FECHADAS = ("resolvida", "protocolado")
# Janela da FILA de leitura, não prazo legal: o que foi publicado nos últimos dias é lido antes
# do acumulado, para um backlog grande não enterrar a publicação de hoje.
JANELA_FILA_DIAS = 30
# «Não é meu»: o processo foi descartado e o advogado não atua nele (mesma regra de
# `quadro.processo_elegivel`: `processo.descartado_em` preenchido e `advogado_atua` diferente
# de 1). Arquivado e lixeira não entram: esses processos mantêm os prazos.
_DESCARTADO_SQL = "pr.descartado_em IS NOT NULL AND (pr.advogado_atua IS NULL OR pr.advogado_atua != 1)"
_contador_de_ponto = 0


class TransacaoPerdida(RuntimeError):
    """A transação do chamador sumiu no meio do trabalho dos prazos: o que ele tinha feito
    foi junto. Não se engole: quem chamou não pode dizer que deu certo."""


@contextmanager
def isolado(conn: sqlite3.Connection):
    """O trabalho dos prazos corre à parte do resto da transação: se falhar, só ele é
    desfeito (ponto de salvamento) e o erro sobe. Se `conn.in_transaction` é falso, abre uma
    (BEGIN IMMEDIATE) e fecha: comita no sucesso, desfaz na falha. Atenção: o Python só abre
    transação na primeira escrita, então dentro de um `with conn:` que ainda não escreveu nada
    `in_transaction` é falso, e o que o `isolado` fez fica comitado mesmo que o `with` depois
    desfaça. Com uma transação já aberta, não comita nada. Se nem o desfazer funcionar, a
    transação do chamador já se foi: sobe `TransacaoPerdida`."""
    global _contador_de_ponto
    _contador_de_ponto += 1
    ponto = f"prazos_{_contador_de_ponto}"
    abriu = not conn.in_transaction
    if abriu:
        # IMMEDIATE: a trava de escrita vem já, para o leitor que vem depois não ficar com
        # um instantâneo velho e levar «database is locked» na primeira escrita.
        conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"SAVEPOINT {ponto}")
    try:
        yield
    except BaseException:
        try:
            if abriu:
                conn.rollback()
            else:
                conn.execute(f"ROLLBACK TO {ponto}")
                conn.execute(f"RELEASE {ponto}")
        except sqlite3.Error as exc:
            raise TransacaoPerdida("a transação de fora se perdeu") from exc
        raise
    try:
        conn.execute(f"RELEASE {ponto}")
        if abriu:
            conn.commit()
    except sqlite3.Error as exc:
        raise TransacaoPerdida("a transação de fora se perdeu") from exc


def sem_quebrar(conn: sqlite3.Connection, o_que: str, trabalho, padrao=None):
    """Roda `trabalho()` isolado. Uma falha dos prazos nunca derruba a varredura nem o
    movimento do cartão: fica no registro técnico (só o tipo do erro) e devolve `padrao`.
    `TransacaoPerdida` sobe: o resto do trabalho do chamador se foi."""
    try:
        with isolado(conn):
            return trabalho()
    except TransacaoPerdida:
        raise
    except Exception as exc:  # noqa: BLE001 - por desenho: prazo não pode quebrar o quadro
        log.warning("prazos: falhou ao %s (%s)", o_que, type(exc).__name__)
        return padrao


def processo_descartado(conn: sqlite3.Connection, numero: str) -> bool:
    return conn.execute(f"SELECT 1 FROM processo pr WHERE pr.numero = ? AND {_DESCARTADO_SQL}",
                        (numero,)).fetchone() is not None


def _abrir_do_item(conn: sqlite3.Connection, numero: str, demanda_id: int, tipo: str, ref: str,
                   data_iso: str) -> int | None:
    if tipo not in ("publicacao", "aviso") or processo_descartado(conn, numero):
        return None
    envio = str(data_iso)[:10]
    if tipo == "publicacao":
        return abrir(conn, numero, demanda_id, "publicacao", ref, envio)
    # Intimação do PJe: sem texto, «a definir». Nunca se abre o teor para ler (pode registrar
    # ciência); o advogado abre no PJe e informa.
    linha = _linha_do_aviso(conn, ref)
    # a data do aviso é a do envio que o PJe informou; o cartão pode ter usado outra para se
    # ordenar. Sem data de envio, o prazo não inventa uma.
    dia = _dia_ou_none(linha["data_disponibilizacao"] if linha else data_iso)
    saiu = linha is not None and linha["pendente"] == 0
    if dia is None:
        motivo = MOTIVO_SEM_ENVIO
    elif saiu:
        motivo = _CIENCIA_REGISTRADA
    else:
        motivo = _motivo_do_aviso(dia)
    novo = abrir(conn, numero, demanda_id, "aviso", ref, dia.isoformat() if dia else None,
                 estado="a_definir", motivo=motivo)
    if novo is not None:
        parear_processo(conn, numero, ALERTA_CIENCIA_DESCONHECIDA if saiu else ALERTA_INTIMACAO)
    return novo


def criar_para_item(conn: sqlite3.Connection, numero: str, demanda_id: int, tipo: str, ref: str,
                    data_iso: str) -> int | None:
    """A novidade entrou num cartão: abre o prazo dela. Todo cartão passa por aqui
    (`quadro.adicionar_item`), com a janela de dias e a elegibilidade já decididas; só o
    processo descartado fica sem prazo. Se abrir o prazo falhar, o cartão segue sem ele e a
    próxima varredura (`criar_dos_cartoes_abertos`) cria."""
    return sem_quebrar(conn, "abrir o prazo", lambda: _abrir_do_item(
        conn, numero, demanda_id, tipo, ref, data_iso))


def criar_dos_cartoes_abertos(conn: sqlite3.Connection) -> int:
    """Varredura dos cartões abertos (toda coluna menos Resolvido e Protocolado): a publicação
    (e a intimação do PJe, esteja ou não ainda na lista de pendentes) deles que ainda não tem nenhuma linha de prazo, em
    qualquer estado, ganha uma. Idempotente:
    roda no fim de toda varredura e recupera o que uma falha deixou para trás ou o cartão que
    voltou de arquivado. Linha que falha é tentada de novo na próxima. Devolve quantos
    prazos abriu. A marca `prazos_alcance_em` só diz que a primeira rodada já foi (a fila de
    leitura usa para não esperar uma varredura)."""
    try:
        itens = conn.execute(
            "SELECT i.tipo, i.ref, i.data, d.id, d.numero FROM demanda_item i "
            "JOIN demanda d ON d.id = i.demanda_id "
            f"WHERE d.coluna NOT IN ({_marcas(_COLUNAS_FECHADAS)}) "
            "AND i.tipo IN ('publicacao', 'aviso') "
            "AND NOT EXISTS (SELECT 1 FROM prazo z WHERE z.origem_tipo = i.tipo "
            "AND z.origem_ref = i.ref) "
            "AND NOT EXISTS (SELECT 1 FROM processo pr WHERE pr.numero = d.numero "
            f"AND {_DESCARTADO_SQL}) ORDER BY i.data, i.ref",
            _COLUNAS_FECHADAS).fetchall()
    except sqlite3.Error as exc:
        log.warning("prazos: falhou ao listar os cartões abertos (%s)", type(exc).__name__)
        return 0
    criados, falhou = 0, False
    for r in itens:
        try:
            with isolado(conn):
                criados += _abrir_do_item(conn, r["numero"], r["id"], r["tipo"], r["ref"],
                                          r["data"]) is not None
        except TransacaoPerdida:
            raise
        except Exception as exc:  # noqa: BLE001
            falhou = True
            log.warning("prazos: falhou ao abrir um prazo da varredura (%s)", type(exc).__name__)
    if not falhou:
        sem_quebrar(conn, "marcar a primeira rodada",
                    lambda: config.gravar(conn, CHAVE_ALCANCE, eventos.agora()))
    return criados


# --- intimação do PJe e a data que vence primeiro (spec, seção 3.5; F14, F15) --------------
#
# A regra que decide tudo aqui: a data errada para mais tarde é o dano. Vale a que vence
# primeiro; a mais tarde só aparece ao lado, e só um clique do advogado a escolhe, para as duas
# datas em que clicou.

AVISO_DUAS_DATAS = ("Há publicação no Diário e intimação no PJe neste processo, com datas "
                    "diferentes. A questão não está pacificada, e o sistema não sabe se são o "
                    "mesmo ato. Vale a que vence primeiro; você pode escolher a maior.")
ALERTA_INTIMACAO = "Chegou intimação no PJe neste processo: a data fatal era {de} e agora é {para}."
ALERTA_CIENCIA = "A ciência no PJe foi informada: a data fatal era {de} e agora é {para}."
ALERTA_PAREAMENTO = "A conta do cartão mudou: a data fatal era {de} e agora é {para}."
ALERTA_ESCOLHA = "A sua escolha da data maior foi desfeita: vale a que vence primeiro."
ALERTA_CIENCIA_DESCONHECIDA = (
    "A ciência já foi registrada no PJe e o dia não é conhecido: a conta parte do envio até "
    "você informar a data. A data fatal era {de} e agora é {para}.")
ALERTA_CONFIRME_DIA = ("A ciência já foi registrada no PJe: confirme o dia. Informe a data da "
                       "ciência para a conta.")
ALERTA_CIENCIA_ILEGIVEL = ("Uma intimação do PJe deste processo traz uma data que o sistema não "
                           "consegue ler. A conta usou a ciência automática; confira.")
NOTA_CIENCIA_ILEGIVEL = ("Uma intimação do PJe deste processo traz uma data ilegível; a conta "
                         "usou a ciência automática.")
_CIENCIA_REGISTRADA = "A ciência já foi registrada no PJe. Informe a data para a conta."
MOTIVO_SEM_ENVIO = "O PJe não informou a data de envio: confira e informe a ciência."
MSG_CIENCIA_ANTES_DO_ENVIO = "A ciência não pode ser anterior ao envio da intimação."
MSG_CIENCIA_NO_FUTURO = "A data da ciência não pode estar no futuro."


def _dia_ou_none(texto) -> date | None:
    """Uma data que veio de fora (o PJe, o banco): vazia, ausente ou ilegível é «sem data», nunca
    um erro cru."""
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None


def _linha_do_aviso(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    instancia, _, id_ = str(ref).partition(":")
    return conn.execute("SELECT data_disponibilizacao, pendente FROM aviso "
                        "WHERE instancia = ? AND id = ?", (instancia, id_)).fetchone()


def _pendente_do_aviso(conn: sqlite3.Connection, ref: str) -> int:
    linha = _linha_do_aviso(conn, ref)
    return 1 if linha is None else linha["pendente"]


class _Contexto:
    """O que um pareamento carrega uma vez só: os calendários, as intimações do processo e as
    contas que se repetem. Sem isto, cada publicação refaz tudo (era quadrático)."""

    def __init__(self, conn: sqlite3.Connection, numero: str):
        self.conn, self.numero = conn, numero
        self._calendarios: dict = {}
        self._contas: dict = {}
        self._avisos: list[dict] | None = None

    def calendario(self, p: dict) -> prazos.Calendario:
        chave = _tribunal_e_orgao_do_prazo(self.conn, p)
        if chave not in self._calendarios:
            self._calendarios[chave] = carregar_calendario(self.conn, *chave)
        return self._calendarios[chave]

    def avisos(self) -> list[dict]:
        """As intimações do PJe do processo que contam (todas menos as descartadas), com a
        situação delas na lista de pendentes. Duas buscas por número, nada de varredura."""
        if self._avisos is None:
            pendentes = {f"{r['instancia']}:{r['id']}": r["pendente"] for r in self.conn.execute(
                "SELECT instancia, id, pendente FROM aviso WHERE numero = ?", (self.numero,))}
            self._avisos = []
            for r in self.conn.execute(
                    "SELECT * FROM prazo WHERE numero = ? AND origem_tipo = 'aviso' "
                    "AND estado != 'descartado' ORDER BY id", (self.numero,)).fetchall():
                a = dict(r)
                a["pendente"] = pendentes.get(a["origem_ref"], 1)
                self._avisos.append(a)
        return self._avisos

    def conta_do_aviso(self, cal, *args):
        chave = (id(cal), *args)
        if chave not in self._contas:
            self._contas[chave] = _contar_do_aviso(cal, *args)
        return self._contas[chave]

    def aviso_para(self, cal, a: dict, p: dict) -> tuple[tuple | None, bool]:
        """A conta de uma intimação do PJe para os dias desta publicação: ((contagem, nota) ou
        None, se algum dado dela não se lê ou não se conta). Um aviso ruim nunca esconde os
        outros: o que não se conta cai na ciência automática, ou em nada se não houver envio."""
        chave = ("aviso", id(cal), a["id"], p["dias"], p["unidade"], bool(p["dobro"]))
        if chave not in self._contas:
            self._contas[chave] = self._calcular_aviso(cal, a, p)
        return self._contas[chave]

    def _calcular_aviso(self, cal, a, p):
        envio = _dia_ou_none(a["disponibilizacao"])
        informada = _dia_ou_none(a["ciencia"])
        inicio = _dia_ou_none(a["inicio_informado"])
        ruim = bool(a["ciencia"]) and informada is None
        if envio is None and informada is None and inicio is None:
            return None, ruim
        resto = (p["dias"], p["unidade"], bool(p["dobro"]))
        try:
            return _contar_do_aviso(cal, envio, informada, _sem_dia_da_ciencia(a), *resto,
                                    inicio), ruim
        except (ValueError, TypeError, OverflowError):
            pass
        # uma data informada que não se deixa contar: cai na ciência automática, e avisa
        try:
            return _contar_do_aviso(cal, envio, None, False, *resto, None), True
        except (ValueError, TypeError, OverflowError):
            # sem envio, a data informada era o único dado e não se conta; com envio, o que
            # falha é a conta da própria publicação, que quem a refaz já avisa
            return None, ruim or envio is None


def _motivo_do_aviso(envio: date) -> str:
    tacita = prazos.ciencia_tacita(envio, prazos_legais.DIAS_CIENCIA_TACITA)
    return ("Intimação no PJe, sem texto aqui: abra no PJe e preencha à mão. Se ninguém abrir, a "
            f"ciência acontece sozinha em {prazos.dia_br(tacita)}.")


def _contar_do_aviso(cal: prazos.Calendario, envio: date | None, informada: date | None,
                     sem_data: bool, dias: int, unidade: str, dobro: bool,
                     inicio: date | None = None) -> tuple[prazos.Contagem, str]:
    """A conta de uma intimação do PJe e a nota que explica quando o que foi informado não
    vale. Fica a de menor data FATAL (não a de menor data bruta) entre: a que parte da ciência
    informada (ou do início informado), a que parte da ciência que acontece sozinha, que é o
    teto de tudo, e, se a ciência já foi registrada e o dia não se sabe (`sem_data`), a que
    parte do envio, o mais cedo possível. Sem data de envio não há teto nem envio: conta do
    que foi informado."""
    if envio is None:
        if informada is not None:
            return prazos.contar_da_ciencia(informada, dias, unidade, cal, dobro=dobro), ""
        if inicio is not None:
            return prazos.contar(None, dias, unidade, cal, dobro=dobro, inicio=inicio), ""
        raise ValueError("Sem data de envio nem ciência: não há de onde contar.")
    tacita = prazos.ciencia_tacita(envio, prazos_legais.DIAS_CIENCIA_TACITA)
    base, rotulo, dia_base = None, "", None
    if informada is not None:
        base = prazos.contar_da_ciencia(informada, dias, unidade, cal, dobro=dobro)
        rotulo, dia_base = "Ciência informada em", informada
    elif inicio is not None:
        base = prazos.contar(None, dias, unidade, cal, dobro=dobro, inicio=inicio)
        rotulo, dia_base = "Início informado em", inicio
    da_envio = (prazos.contar_da_ciencia(envio, dias, unidade, cal, dobro=dobro, tacita=True)
                if sem_data and informada is None else None)
    da_tacita = prazos.contar_da_ciencia(tacita, dias, unidade, cal, dobro=dobro, tacita=True)
    melhor = None
    for candidata in (base, da_envio, da_tacita):  # empate: fica a primeira
        if candidata is not None and (melhor is None or candidata.fatal < melhor.fatal):
            melhor = candidata
    if melhor is da_envio and da_envio is not None:
        return melhor, ("A ciência já foi registrada no PJe e o dia não é conhecido: a conta "
                        f"parte do envio, {prazos.dia_br(envio)}, até você informar a data.")
    if melhor is da_tacita and base is not None:
        if dia_base > tacita:
            return melhor, (f"{rotulo} {prazos.dia_br(dia_base)}, depois do limite de ciência "
                            f"automática: a conta parte de {prazos.dia_br(tacita)}.")
        return melhor, (f"{rotulo} {prazos.dia_br(dia_base)}; a conta parte de "
                        f"{prazos.dia_br(tacita)}, que dá a data mais cedo.")
    return melhor, ""


def _sem_dia_da_ciencia(a: dict) -> bool:
    return a["pendente"] == 0 and not a["ciencia"]


def _relacionadas(avisos: list[dict], p: dict) -> list[dict]:
    """As intimações do PJe que a conta desta publicação tem de olhar: as do mesmo cartão, sem
    janela, e as dos outros cartões do processo cujo envio cai a até `JANELA_DIAS` da data da
    publicação (para um lado ou para o outro). Sem envio, vale a ciência informada como a data;
    sem nenhuma das duas, não entra entre cartões. Aviso descartado já não está na lista;
    cartão resolvido conta, porque a data mais cedo é o lado seguro."""
    from nucleo import quadro  # import tardio: quadro importa este módulo
    dispo = _dia_ou_none(p["disponibilizacao"])
    achados = []
    for a in avisos:
        if p["demanda_id"] is not None and a["demanda_id"] == p["demanda_id"]:
            achados.append(a)
            continue
        dia = _dia_ou_none(a["disponibilizacao"]) or _dia_ou_none(a["ciencia"])
        if dispo is not None and dia is not None and abs(
                (dia - dispo).days) <= quadro.JANELA_DIAS:
            achados.append(a)
    return achados


def _aviso_antigo_ja_avisado(p: dict) -> bool:
    """O calculo guardado já registra que o advogado foi avisado da data ilegível."""
    try:
        return bool(json.loads(p["calculo"] or "{}").get("ilegivel_avisado"))
    except (ValueError, AttributeError):
        return False


def _com_a_outra_data(conn: sqlite3.Connection, p: dict, c: prazos.Contagem,
                      cal: prazos.Calendario, campos: dict, calculo: dict,
                      ctx: _Contexto) -> dict:
    """Publicação com dias conhecidos e intimação do PJe no processo: as duas contas. Vale a
    que vence primeiro, entre todas as que se contam. A data maior só vale se o advogado a
    escolheu para ESTAS duas datas; qualquer mudança em uma delas, ou no fim da outra, desfaz
    a escolha."""
    if p["origem_tipo"] != "publicacao":
        return {**campos, "calculo": json.dumps(calculo, ensure_ascii=False)}
    melhor, ilegivel = None, False
    for a in _relacionadas(ctx.avisos(), p):
        par, ruim = ctx.aviso_para(cal, a, p)
        ilegivel = ilegivel or ruim
        if par is not None and (melhor is None or par[0].fatal < melhor[0].fatal):
            melhor = par
    if ilegivel:
        calculo["avisos"].append(NOTA_CIENCIA_ILEGIVEL)
        if _aviso_antigo_ja_avisado(p):
            calculo["ilegivel_avisado"] = True
    if melhor is None or melhor[0].fatal == c.fatal:
        return {**campos, "fatal_maior": 0, "calculo": json.dumps(calculo, ensure_ascii=False)}
    outra, nota = melhor
    menor, maior = sorted((c.fatal, outra.fatal))
    mesmas = {p["fatal"], p["fatal_alternativa"]} == {menor.isoformat(), maior.isoformat()}
    maior_vale = bool(p["fatal_maior"]) and mesmas
    usada, ao_lado = (maior, menor) if maior_vale else (menor, maior)
    calculo["avisos"].append(AVISO_DUAS_DATAS)
    alternativa = outra.como_dict()
    if nota:
        alternativa["avisos"].append(nota)
    calculo["alternativa"] = alternativa
    return {**campos, "fatal": usada.isoformat(), "fatal_alternativa": ao_lado.isoformat(),
            "fatal_maior": int(maior_vale), "trabalho": _trabalho(conn, p, usada, cal),
            "calculo": json.dumps(calculo, ensure_ascii=False)}


def _escolha_desfeita(p: dict, conta: dict) -> bool:
    """A conta refeita não mantém a data maior que o advogado tinha escolhido."""
    return bool(p["fatal_maior"]) and not conta.get("fatal_maior", 0)


def _alerta_da_mudanca(p: dict, fatal_novo: str, texto: str, desfeita: bool) -> str:
    """O alerta de uma conta refeita: o que mudou na data (se mudou) e, se for o caso, que a
    escolha da data maior foi desfeita. Data igual não diz «era X e agora é X»."""
    partes = []
    if fatal_novo != p["fatal"]:
        partes.append(texto.format(de=prazos.dia_br(date.fromisoformat(p["fatal"])),
                                   para=prazos.dia_br(date.fromisoformat(fatal_novo))))
    if desfeita:
        partes.append(ALERTA_ESCOLHA)
    return " ".join(partes)


def _alerta_da_escolha(p: dict, conta: dict) -> str:
    if not _escolha_desfeita(p, conta):
        return ""
    return _alerta_da_mudanca(p, conta["fatal"], "A data fatal era {de} e agora é {para}.", True)


def parear_processo(conn: sqlite3.Connection, numero: str,
                    alerta: str = ALERTA_INTIMACAO) -> None:
    """Refaz a conta dos prazos de publicação do processo (os que têm data e não foram
    informados à mão) e diz qual cobre cada intimação do PJe: só uma publicação que tem data
    e foi comparada com ela, com uma ciência que a conta conhece. O prazo do aviso
    coberto não aparece à parte em «A definir». Quem mudou de data volta a `a_conferir`, com
    `alerta`. Conta que não sai não trava ninguém (`recontar_seguro`). Cumprido e descartado
    ficam como estão. Tudo o que depende do processo é carregado uma vez."""
    ctx = _Contexto(conn, numero)
    cobertura: dict[int, int] = {}
    for r in conn.execute(
            f"SELECT * FROM prazo WHERE numero = ? AND origem_tipo = 'publicacao' "
            f"AND estado IN ({_marcas(COM_DATA)}) ORDER BY ordem, id",
            (numero, *COM_DATA)).fetchall():
        p = dict(r)
        if p["fatal_informada"]:
            continue  # a data veio da mão do advogado: não houve comparação, não cobre
        relacionadas = _relacionadas(ctx.avisos(), p)
        recontar_seguro(conn, p, alerta, ctx)
        cal = ctx.calendario(p)
        achadas = [(a, *ctx.aviso_para(cal, a, p)) for a in relacionadas]
        if any(ruim for _, _, ruim in achadas):
            _avisar_ciencia_ilegivel(conn, p["id"])
        for a, par, ruim in achadas:
            if par is not None and not ruim and not _sem_dia_da_ciencia(a):
                cobertura.setdefault(a["id"], p["id"])
    for r in conn.execute("SELECT id, pareado_com FROM prazo WHERE numero = ? "
                          "AND origem_tipo = 'aviso'", (numero,)).fetchall():
        novo = cobertura.get(r["id"])
        if novo != r["pareado_com"]:
            # a linha do aviso aparece ou some de «A definir»: é uma mudança do que se vê
            conn.execute("UPDATE prazo SET pareado_com = ?, atualizado_em = ? WHERE id = ?",
                         (novo, _momento(conn, r["id"]), r["id"]))


def _avisar_ciencia_ilegivel(conn: sqlite3.Connection, prazo_id: int) -> None:
    """Uma vez: a publicação foi contada com a ciência automática porque uma data informada
    não se lê ou não se conta. O calculo guarda que já avisou; a marca só vale enquanto o
    problema existir."""
    q = obter(conn, prazo_id)
    if q["estado"] not in COM_DATA or _aviso_antigo_ja_avisado(q):
        return
    try:
        calculo = json.loads(q["calculo"] or "{}")
    except ValueError:
        calculo = {}
    calculo["ilegivel_avisado"] = True
    alerta = f"{q['alerta']} {ALERTA_CIENCIA_ILEGIVEL}".strip()
    _gravar(conn, prazo_id, {"estado": "a_conferir", "conferido_em": None, "alerta": alerta,
                             "calculo": json.dumps(calculo, ensure_ascii=False)})
    eventos.registrar(conn, "prazo_ciencia_ilegivel", q["numero"], {"prazo": prazo_id})


def _parear_do_cartao(conn: sqlite3.Connection, demanda_id: int, alerta: str) -> None:
    d = conn.execute("SELECT numero FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    if d is not None:
        parear_processo(conn, d["numero"], alerta)


def parear_cartao(conn: sqlite3.Connection, demanda_id: int,
                  alerta: str = ALERTA_INTIMACAO) -> None:
    """O pareamento é do processo do cartão (`parear_processo`)."""
    _parear_do_cartao(conn, demanda_id, alerta)


def escolher_fatal(conn: sqlite3.Connection, prazo_id: int, maior: bool,
                   por: str = "advogado") -> bool:
    """«Usar a maior» ou «Usar a que vence primeiro». Não é conferir: pede o clique depois.
    Devolve se mudou (a mesma escolha de novo não escreve nem registra)."""
    p = obter(conn, prazo_id)
    if not p["fatal_alternativa"] or p["estado"] not in COM_DATA:
        raise PrazoInvalido("Este prazo não tem duas datas.")
    if bool(maior) == bool(p["fatal_maior"]):
        return False
    novo = {**p, "fatal_maior": int(bool(maior))}
    campos = _conta_para_a_tela(conn, novo)
    if campos["fatal"] != p["fatal"]:
        campos.update(estado="a_conferir", conferido_em=None, alerta="")
    _gravar(conn, prazo_id, campos)
    eventos.registrar(conn, "prazo_fatal_escolhida", p["numero"],
                      {"prazo": prazo_id, "maior": bool(maior), "fatal": campos["fatal"],
                       "por": por})
    return True


def _conferir_dia_da_ciencia(data: str, envio_texto, hoje: date | None) -> None:
    """A ciência informada não vale antes do envio (se o PJe o informou) nem depois de amanhã no
    fuso do tribunal (a folga é para quem está um fuso à frente)."""
    envio = _dia_ou_none(envio_texto)
    if data < DATA_MINIMA.isoformat():
        raise PrazoInvalido(f"Data da ciência fora do alcance: use uma data a partir de "
                            f"{prazos.dia_br(DATA_MINIMA)}.")
    if envio is not None and data < envio.isoformat():
        raise PrazoInvalido(MSG_CIENCIA_ANTES_DO_ENVIO)
    hoje = _hoje_do_tribunal(hoje)
    if data > (hoje + timedelta(days=1)).isoformat():
        raise PrazoInvalido(MSG_CIENCIA_NO_FUTURO)


def informar_ciencia(conn: sqlite3.Connection, prazo_id: int, data: str,
                     por: str = "advogado", hoje: date | None = None) -> None:
    """O advogado abriu a intimação no PJe e diz o dia. A conta usa a menor data fatal entre a
    ciência informada e a que acontece sozinha. Não vale data antes do envio (se o PJe o
    informou) nem depois de amanhã no fuso do tribunal (a folga é para quem está um fuso à
    frente)."""
    p = obter(conn, prazo_id)
    if p["origem_tipo"] != "aviso":
        raise PrazoInvalido("Este prazo não é de intimação do PJe.")
    if p["estado"] in ("cumprido", "descartado"):
        raise PrazoInvalido("Este prazo já está encerrado.")
    data = _data_valida(data, "Data da ciência")
    envio = _dia_ou_none(p["disponibilizacao"])
    _conferir_dia_da_ciencia(data, p["disponibilizacao"], hoje)
    campos = {"ciencia": data}
    informada = date.fromisoformat(data)
    if p["estado"] == "a_definir":
        tacita = (prazos.ciencia_tacita(envio, prazos_legais.DIAS_CIENCIA_TACITA)
                  if envio is not None else None)
        if tacita is not None and informada > tacita:
            campos["motivo"] = (f"Ciência informada em {prazos.dia_br(informada)}, depois do "
                                "limite de ciência automática: a conta parte de "
                                f"{prazos.dia_br(tacita)}. Preencha o ato e os dias.")
        else:
            campos["motivo"] = (f"Ciência informada: {prazos.dia_br(informada)}. "
                                "Preencha o ato e os dias.")
    elif p["motivo"] == _CIENCIA_REGISTRADA:
        campos["motivo"] = ""
    _gravar(conn, prazo_id, campos)
    eventos.registrar(conn, "prazo_ciencia_informada", p["numero"],
                      {"prazo": prazo_id, "ciencia": data, "por": por})
    if p["estado"] in COM_DATA and not p["fatal_informada"]:
        # o aviso já tinha data, conferida com outra ciência
        recontar_seguro(conn, obter(conn, prazo_id), ALERTA_CIENCIA)
    parear_processo(conn, p["numero"], ALERTA_CIENCIA)


def atualizar_avisos(conn: sqlite3.Connection) -> int:
    """A intimação saiu da lista de pendentes (ciência dada no PJe) e o sistema não sabe o dia:
    o aviso pede a data, e a conta do processo parte do envio até o advogado informar. Aviso já
    preenchido à mão tem a conta refeita (sempre, a cada rodada) e pede conferência do dia, uma
    vez (a data só fica como está se veio de uma data fatal informada). A marca da linha só
    segura o alerta repetido: a conta e o pareamento do processo rodam sempre que há aviso
    fora da lista, seja qual for o estado dele (menos descartado). Repetida, não muda nada.
    Devolve quantos avisos foram marcados agora."""
    n = 0
    for r in conn.execute(
            "SELECT p.id, p.numero, p.estado, p.fatal_informada, p.motivo FROM prazo p JOIN aviso a "
            "ON a.numero = p.numero AND a.instancia || ':' || a.id = p.origem_ref "
            "WHERE p.origem_tipo = 'aviso' AND a.pendente = 0 AND p.ciencia IS NULL "
            "AND p.estado IN ('a_definir', 'a_conferir', 'conferido') "
            "AND p.disponibilizacao IS NOT NULL AND p.disponibilizacao != '' "
            "ORDER BY p.id").fetchall():
        marcado = r["motivo"] == _CIENCIA_REGISTRADA
        if r["estado"] == "a_definir":
            if not marcado:
                _gravar(conn, r["id"], {"motivo": _CIENCIA_REGISTRADA})
        else:
            if not r["fatal_informada"]:
                recontar_seguro(conn, obter(conn, r["id"]), ALERTA_CIENCIA_DESCONHECIDA)
            if not marcado:  # a data que o advogado viu partia de uma ciência que já aconteceu
                atual = obter(conn, r["id"])
                _gravar(conn, r["id"], {"motivo": _CIENCIA_REGISTRADA, "estado": "a_conferir",
                                        "conferido_em": None,
                                        "alerta": atual["alerta"] or ALERTA_CONFIRME_DIA})
        if not marcado:
            eventos.registrar(conn, "prazo_ciencia_registrada", r["numero"], {"prazo": r["id"]})
            n += 1
    numeros = [r["numero"] for r in conn.execute(
        "SELECT DISTINCT p.numero FROM prazo p JOIN aviso a "
        "ON a.numero = p.numero AND a.instancia || ':' || a.id = p.origem_ref "
        "WHERE p.origem_tipo = 'aviso' AND a.pendente = 0 AND p.estado != 'descartado' "
        "ORDER BY p.numero")]
    for numero in numeros:
        parear_processo(conn, numero, ALERTA_CIENCIA_DESCONHECIDA)
    return n
