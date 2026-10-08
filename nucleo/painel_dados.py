"""Consultas de leitura para as telas do painel (cockpit, carteira e
processo). Nada aqui grava no banco nem fala com o tribunal."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta

from markupsafe import Markup, escape

from captura.processo_parser import formatar_numero_cnj
from nucleo import avisos, carteira, config, tarefas_fila, tribunal
from nucleo import ponte as nucleo_ponte
from nucleo import quadro as nucleo_quadro
from nucleo.carteira import data_de_referencia, listar, situacao

LIMITE_TEXTO_PUBLICACAO = 280
_TONS = {
    carteira.ATIVO: "ok",
    carteira.ATUA_PELO_DJEN: "aviso",
    carteira.SEM_ACESSO: "alerta",
}
_INSTANCIAS = {"1grau": "1º grau", "2grau": "2º grau"}


def __getattr__(nome: str):
    # `painel_dados.FUSO`: o fuso do tribunal atual, lido a cada uso.
    if nome == "FUSO":
        return tribunal.fuso()
    raise AttributeError(f"module {__name__!r} has no attribute {nome!r}")


def agora_cuiaba() -> datetime:
    """Agora no fuso do tribunal do escritório (o nome é da época só-TJMT)."""
    return datetime.now(tribunal.fuso())


def hoje_cuiaba() -> date:
    return agora_cuiaba().date()


def tom_da_situacao(rotulo: str) -> str:
    """Tom do selo na tela: ok, aviso, alerta ou neutro (a conferir, arquivado)."""
    return _TONS.get(rotulo, "neutro")


def selo_citacao(status: str | None) -> str:
    """Tom do selo de uma citação do Citation Gate: ok, alerta ou aviso (desconhecido)."""
    st = str(status or "").strip().lower()
    if st.startswith(("verificada", "verified")) or st in ("ok", "aprovada", "aprovado"):
        return "ok"
    if any(t in st for t in ("nao", "não", "not", "diverg", "falh", "reprov")):
        return "alerta"
    return "aviso"


def _data(aaaammdd: str | None) -> date | None:
    s = "".join(c for c in str(aaaammdd or "") if c.isdigit())[:8]
    try:
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def data_br(aaaammdd: str | None) -> str:
    d = _data(aaaammdd)
    return d.strftime("%d/%m/%Y") if d else ""


def dias_desde(aaaammdd: str | None, hoje: date) -> int | None:
    d = _data(aaaammdd)
    return (hoje - d).days if d else None


def valor_em_reais(texto: str | None) -> float:
    """'R$ 1.500,50' → 1500.5; vazio ou ilegível → 0.0."""
    s = (texto or "").replace("R$", "").strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "." in s and len(s.rsplit(".", 1)[1]) != 3:
        pass  # ponto como separador decimal: "1500.50"
    else:
        s = s.replace(".", "")  # ponto de milhar: "1.500"
    try:
        return float(s)
    except ValueError:
        return 0.0


def formatar_compacto(valor: float) -> str:
    # Arredonda antes de escolher a unidade: 999.500 vira "1,0 mi", não "1000 mil".
    if round(valor / 1_000) >= 1_000:
        return f"R$ {valor / 1_000_000:.1f} mi".replace(".", ",")
    if round(valor) >= 1_000:
        return f"R$ {valor / 1_000:.0f} mil"
    return f"R$ {valor:.0f}"


def saudacao(agora: datetime) -> str:
    if agora.hour < 12:
        return "Bom dia"
    if agora.hour < 18:
        return "Boa tarde"
    return "Boa noite"


def primeiro_nome(nome_completo: str) -> str:
    partes = (nome_completo or "").split()
    return partes[0].capitalize() if partes else ""


def kpis(conn: sqlite3.Connection, hoje: date) -> dict:
    ativa = listar(conn, "ativa")
    dias = [dias_desde(data_de_referencia(p), hoje) for p in ativa]
    corte = (hoje - timedelta(days=30)).isoformat()
    return {
        "ativos": len(ativa),
        "a_conferir": len(listar(conn, "a_conferir")),
        "movimentaram_7d": sum(1 for d in dias if d is not None and d <= 7),
        "parados_90d": sum(1 for d in dias if d is None or d > 90),
        "publicacoes_30d": conn.execute(
            "SELECT COUNT(*) FROM publicacao WHERE data_disponibilizacao >= ? "
            "AND numero IN (SELECT numero FROM processo "
            f"WHERE {carteira.FILTRO_ATIVA})",
            (corte,)).fetchone()[0],
        "valor_em_causa": formatar_compacto(sum(valor_em_reais(p["valor_causa"]) for p in ativa)),
    }


def por_tribunal(conn: sqlite3.Connection) -> list[dict]:
    contagem: dict[str, int] = {}
    for p in listar(conn, "ativa"):
        contagem[p["tribunal"]] = contagem.get(p["tribunal"], 0) + 1
    maior = max(contagem.values(), default=0)
    return [{"tribunal": t, "quantidade": n,
             "percentual": round(100 * n / maior) if maior else 0}
            for t, n in sorted(contagem.items(), key=lambda x: (-x[1], x[0]))]


def por_situacao(conn: sqlite3.Connection) -> list[dict]:
    """Resumo para o card "Situação da carteira" do Cockpit: as linhas dos ativos
    somam sempre o total de ativos; "A conferir" fica por último."""
    rotulos = [situacao(p) for p in listar(conn, "ativa")]
    sem_acesso = rotulos.count(carteira.SEM_ACESSO)
    linhas = [
        {"rotulo": "Conferidos no PJe", "quantidade": rotulos.count(carteira.ATIVO), "tom": "ok"},
        {"rotulo": "Só pelo DJEN",
         "quantidade": rotulos.count(carteira.ATUA_PELO_DJEN), "tom": "aviso"},
    ]
    if sem_acesso:
        linhas.append({"rotulo": carteira.SEM_ACESSO, "quantidade": sem_acesso, "tom": "alerta"})
    linhas.append({"rotulo": "A conferir", "quantidade": len(listar(conn, "a_conferir")),
                   "tom": "neutro"})
    return linhas


def ultima_varredura(conn: sqlite3.Connection) -> dict | None:
    """A última varredura agendada (ou "Varrer agora"), com os avisos de falha."""
    linha = conn.execute(
        "SELECT quando, tipo, dados FROM evento "
        "WHERE tipo IN ('varredura_concluida', 'varredura_falhou') "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if linha is None:
        return None
    q = linha["quando"]
    dados = json.loads(linha["dados"] or "{}")
    falhou = linha["tipo"] == "varredura_falhou"
    erros = [dados.get("erro", "erro desconhecido")] if falhou else list(dados.get("erros", []))
    return {"quando": f"{q[8:10]}/{q[5:7]}/{q[0:4]} {q[11:16]}", "erros": erros,
            "falhou": falhou}


def atividade_recente(conn: sqlite3.Connection, limite: int = 12) -> list[dict]:
    ativos = {p["numero"]: p for p in listar(conn, "ativa")}
    de_ativos = f"numero IN (SELECT numero FROM processo WHERE {carteira.FILTRO_ATIVA})"
    itens: list[dict] = []
    for r in conn.execute("SELECT numero, data_disponibilizacao, tipo, orgao FROM publicacao "
                          f"WHERE {de_ativos} "
                          "ORDER BY data_disponibilizacao DESC, id DESC LIMIT ?", (limite,)):
        descricao = " · ".join(x for x in (r["tipo"] or "Publicação", r["orgao"] or "") if x)
        itens.append({"data": r["data_disponibilizacao"].replace("-", ""),
                      "numero": r["numero"], "tipo": "publicacao", "texto": descricao})
    for r in conn.execute("SELECT numero, data_ordenavel, texto FROM andamento "
                          f"WHERE {de_ativos} "
                          "ORDER BY data_ordenavel DESC LIMIT ?", (limite,)):
        itens.append({"data": r["data_ordenavel"][:8], "numero": r["numero"],
                      "tipo": "andamento", "texto": r["texto"]})
    itens.sort(key=lambda i: (i["data"], i["tipo"] == "andamento"), reverse=True)
    resultado = itens[:limite]
    for item in resultado:
        item["data_br"] = data_br(item["data"])
        item["numero_formatado"] = formatar_numero_cnj(item["numero"])
        item["cliente"] = ativos[item["numero"]]["cliente"] or ""
    return resultado


def _limpar_texto(texto: str, limite: int = LIMITE_TEXTO_PUBLICACAO) -> str:
    sem_tags = re.sub(r"<[a-zA-Z/!][^>]*>", " ", texto or "")
    limpo = re.sub(r"\s+", " ", html.unescape(sem_tags)).strip()
    if len(limpo) > limite:
        limpo = limpo[:limite].rstrip() + "…"
    return limpo


LIMITE_INTEGRA = 60_000
AVISO_INTEGRA_CORTADA = ("[texto cortado: a publicação passa de 60 mil caracteres; "
                         "o restante está no site do tribunal]")
_INTEGRA_ESCONDIDOS = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_INTEGRA_COMENTARIO = re.compile(r"<!--.*?-->", re.DOTALL)
_INTEGRA_QUEBRA = re.compile(r"<br\s*/?\s*>|</\s*(?:p|div|li|tr)\s*>|<(?:p|div)\b[^>]*>",
                             re.IGNORECASE)
_INTEGRA_TAG = re.compile(r"<[a-zA-Z/!?][^>]*>")


def texto_integral(texto_html: str | None, limite: int = LIMITE_INTEGRA) -> list[str]:
    """A publicação do DJEN como parágrafos de texto puro (o template escapa cada um;
    o HTML do DJEN nunca vai cru para a tela). <br>, </p>, </div>, </li> e </tr>
    quebram o parágrafo; script e style somem com o conteúdo; as demais tags somem;
    entidades viram caracteres; espaços colapsados; parágrafos vazios fora. Texto
    sem nenhuma tag (há tribunal que manda texto puro): cada linha é um parágrafo.
    Acima de `limite` caracteres, corta e termina com AVISO_INTEGRA_CORTADA."""
    bruto = str(texto_html or "")[:limite * 4]  # entrada absurda não trava a página
    bruto = _INTEGRA_COMENTARIO.sub(" ", _INTEGRA_ESCONDIDOS.sub(" ", bruto))
    if _INTEGRA_TAG.search(bruto):
        bruto = _INTEGRA_TAG.sub(" ", _INTEGRA_QUEBRA.sub("\n", bruto))
    paragrafos: list[str] = []
    total = 0
    for linha in html.unescape(bruto).split("\n"):
        limpa = re.sub(r"\s+", " ", linha).strip()
        if not limpa:
            continue
        if total + len(limpa) > limite:
            resto = limite - total
            if resto > 0:
                paragrafos.append(limpa[:resto].rstrip() + "…")
            paragrafos.append(AVISO_INTEGRA_CORTADA)
            break
        paragrafos.append(limpa)
        total += len(limpa)
    return paragrafos


_POLOS_ROTULO = (("ativo", "polo ativo"), ("passivo", "polo passivo"), ("", "polo não informado"))


def partes_publicadas(conn: sqlite3.Connection, numero: str) -> list[dict]:
    """Partes que o DJEN pôs como destinatárias nas publicações do processo (da mais
    recente para a mais antiga, sem repetir nome), agrupadas por polo:
    [{"rotulo": "polo ativo", "nomes": [...], "separador": ""}, …]. `separador` é o
    que vai antes do grupo na frase ("×" entre os polos, "·" antes do sem polo)."""
    por_polo: dict[str, list[str]] = {"ativo": [], "passivo": [], "": []}
    vistos: set[str] = set()
    for r in conn.execute("SELECT partes_json FROM publicacao WHERE numero = ? "
                          "ORDER BY data_disponibilizacao DESC, id DESC", (numero,)):
        try:
            partes = json.loads(r["partes_json"] or "[]")
        except ValueError:
            continue
        for parte in partes if isinstance(partes, list) else []:
            if not isinstance(parte, dict):
                continue
            nome = " ".join(str(parte.get("nome") or "").split())
            polo = parte.get("polo") if parte.get("polo") in por_polo else ""
            if nome and nome.casefold() not in vistos:
                vistos.add(nome.casefold())
                por_polo[polo].append(nome)
    grupos = []
    for polo, rotulo in _POLOS_ROTULO:
        if por_polo[polo]:
            separador = "" if not grupos else ("·" if polo == "" else "×")
            grupos.append({"rotulo": rotulo, "nomes": por_polo[polo], "separador": separador})
    return grupos


def monitoramento(p) -> dict:
    """Decisão do advogado sobre o processo, para o bloco "Este processo é seu?":
    estado "confirmado" (ele disse que é), "oab" (a OAB dele consta), "arquivado",
    "descartado" (ele disse que não é) ou "a_conferir" (mostra a pergunta)."""
    if p is None:
        return {"estado": "", "confirmado_br": "", "descartado_br": ""}
    chaves = p.keys()
    confirmado = p["confirmado_em"] if "confirmado_em" in chaves else None
    descartado = p["descartado_em"] if "descartado_em" in chaves else None
    if confirmado:
        estado = "confirmado"
    elif p["advogado_atua"] == 1:
        estado = "oab"
    elif p["arquivado"]:
        estado = "arquivado"
    elif descartado:
        estado = "descartado"
    else:
        estado = "a_conferir"
    return {"estado": estado, "confirmado_br": data_br(str(confirmado or "")[:10]),
            "descartado_br": data_br(str(descartado or "")[:10])}


def _link_seguro(link: str | None) -> str:
    """Só links http(s) vão para o href; qualquer outro esquema (javascript:) vira vazio."""
    link = str(link or "").strip()
    return link if link.lower().startswith(("http://", "https://")) else ""


def linha_do_tempo(conn: sqlite3.Connection, numero: str) -> list[dict]:
    itens: list[dict] = []
    for r in conn.execute("SELECT data_ordenavel, texto FROM andamento WHERE numero = ?",
                          (numero,)):
        d = r["data_ordenavel"]
        itens.append({"ordem": d.ljust(14, "0"), "data_br": data_br(d),
                      "hora": f"{d[8:10]}:{d[10:12]}" if len(d) >= 12 else "",
                      "tipo": "andamento", "texto": r["texto"], "link": "", "integra": []})
    for r in conn.execute("SELECT data_disponibilizacao, tipo, orgao, texto, link "
                          "FROM publicacao WHERE numero = ?", (numero,)):
        d = r["data_disponibilizacao"].replace("-", "")
        texto = (_limpar_texto(r["texto"])
                 or " · ".join(x for x in (r["tipo"], r["orgao"]) if x)
                 or "Publicação no DJEN")
        itens.append({"ordem": d.ljust(14, "0"), "data_br": data_br(d), "hora": "",
                      "tipo": "publicacao", "texto": texto, "link": _link_seguro(r["link"]),
                      "integra": texto_integral(r["texto"])})
    for r in conn.execute("SELECT data_disponibilizacao, visto_em, tipo_comunicacao, pendente "
                          "FROM aviso WHERE numero = ?", (numero,)):
        d = (r["data_disponibilizacao"] or r["visto_em"][:10]).replace("-", "")
        estado = "aguardando ciência" if r["pendente"] else "não está mais pendente"
        itens.append({"ordem": d.ljust(14, "0"), "data_br": data_br(d), "hora": "",
                      "tipo": "aviso", "link": "", "integra": [],
                      "texto": f"{avisos.rotulo_do_tipo(r['tipo_comunicacao'])} no PJe · {estado}"})
    itens.sort(key=lambda i: i["ordem"], reverse=True)
    return itens


def _linha_da_carteira(p, hoje: date) -> dict:
    referencia = data_de_referencia(p)
    rotulo = carteira.ARQUIVADO if p["arquivado"] else situacao(p)
    andamento = (p["ultimo_andamento_data"] or "")[:8]
    publicacao = (p["ultima_publicacao_data"] or "")[:8]
    if publicacao and publicacao > andamento:
        ultimo_texto = "Publicação no DJEN"
    else:
        ultimo_texto = p["ultimo_andamento_texto"] or ""
    return {
        "numero": p["numero"], "numero_formatado": formatar_numero_cnj(p["numero"]),
        "tribunal": p["tribunal"], "orgao": p["orgao_julgador"] or "",
        "cliente": p["cliente"] or "", "parte_contraria": p["parte_contraria"] or "",
        "ultimo_texto": ultimo_texto, "referencia_br": data_br(referencia),
        "dias_parado": dias_desde(referencia, hoje),
        "situacao": rotulo, "tom": tom_da_situacao(rotulo),
    }


def linhas_da_carteira(conn: sqlite3.Connection, aba: str, hoje: date) -> list[dict]:
    return [_linha_da_carteira(p, hoje) for p in listar(conn, aba)]


def normalizar_texto(texto: str | None) -> str:
    """Minúsculas e sem acento, para comparar nomes sem se importar com a grafia."""
    decomposto = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def _nomes_das_partes(partes_json: str | None) -> list[str]:
    try:
        polos = json.loads(partes_json or "[]")
    except ValueError:
        return []
    nomes: list[str] = []
    for polo in polos if isinstance(polos, list) else []:
        if not isinstance(polo, dict):
            continue
        nomes.extend(str(n) for n in polo.get("nomes") or [])
        nomes.extend(str(i.get("nome") or "") for i in polo.get("integrantes") or []
                     if isinstance(i, dict))
    return nomes


def buscar_na_carteira(conn: sqlite3.Connection, termo: str, hoje: date) -> list[dict]:
    """Processos da carteira (inclusive arquivados e a conferir) em que o termo
    aparece no cliente, na parte contrária ou em qualquer parte do processo.
    Ativos primeiro; em cada grupo, o de movimento mais recente primeiro."""
    alvo = normalizar_texto(termo).strip()
    if not alvo:
        return []
    achados = []
    for p in conn.execute("SELECT * FROM processo"):
        textos = [p["cliente"], p["parte_contraria"], *_nomes_das_partes(p["partes_json"])]
        if any(alvo in normalizar_texto(t) for t in textos):
            ativo = bool((p["advogado_atua"] == 1 or p["confirmado_em"]) and not p["arquivado"])
            achados.append((ativo, data_de_referencia(p) or "", p))
    achados.sort(key=lambda a: a[2]["numero"])
    achados.sort(key=lambda a: (a[0], a[1]), reverse=True)
    return [_linha_da_carteira(p, hoje) for _, _, p in achados]


def numeros_na_carteira(conn: sqlite3.Connection, numeros: list[str]) -> set[str]:
    """Quais destes números já existem no banco."""
    if not numeros:
        return set()
    marcas = ", ".join("?" for _ in numeros)
    return {r["numero"] for r in conn.execute(
        f"SELECT numero FROM processo WHERE numero IN ({marcas})", list(numeros))}


def filtrar(linhas: list[dict], busca: str = "", tribunal: str = "") -> list[dict]:
    termo = busca.strip().lower()
    digitos = "".join(c for c in termo if c.isdigit())
    so_numero = not any(c.isalpha() for c in termo) and len(digitos) >= 4
    resultado = []
    for linha in linhas:
        if tribunal and linha["tribunal"] != tribunal:
            continue
        if termo:
            alvo = " ".join((linha["numero_formatado"], linha["cliente"],
                             linha["parte_contraria"], linha["orgao"])).lower()
            if termo not in alvo and not (so_numero and digitos in linha["numero"]):
                continue
        resultado.append(linha)
    return resultado


def tribunais(linhas: list[dict]) -> list[str]:
    return sorted({linha["tribunal"] for linha in linhas})


def contagens(conn: sqlite3.Connection) -> dict:
    return {aba: len(listar(conn, aba)) for aba in ("ativa", "a_conferir", "descartados")}


def processo_detalhe(conn: sqlite3.Connection, numero: str) -> dict | None:
    p = conn.execute("SELECT * FROM processo WHERE numero = ?", (numero,)).fetchone()
    if p is None:
        return None
    rotulo = carteira.ARQUIVADO if p["arquivado"] else situacao(p)
    return {
        "monitoramento": monitoramento(p),
        "partes_publicadas": ([] if p["cliente"] or p["parte_contraria"]
                              else partes_publicadas(conn, numero)),
        "numero": numero, "numero_formatado": formatar_numero_cnj(numero),
        "tribunal": p["tribunal"], "instancia_rotulo": _INSTANCIAS.get(p["instancia"] or "", ""),
        "orgao_julgador": p["orgao_julgador"] or "", "valor_causa": p["valor_causa"] or "",
        "cliente": p["cliente"] or "", "parte_contraria": p["parte_contraria"] or "",
        "polo_cliente": p["polo_cliente"] or "",
        "situacao": rotulo, "tom": tom_da_situacao(rotulo),
        "partes": json.loads(p["partes_json"] or "[]"),
        "linha_do_tempo": linha_do_tempo(conn, numero),
        "erro": p["erro_sincronizacao"] or "",
        "cartoes_lex": cartoes_com_lex(conn, numero),
    }


def cartoes_com_lex(conn: sqlite3.Connection, numero: str) -> list[dict]:
    """Cartões do processo que já passaram pelo Lex (têm execução), do mais novo para o
    mais antigo, para o link ao cartão aberto na página do processo."""
    linhas = conn.execute(
        "SELECT d.id, d.coluna, d.lex_squad, d.lex_squad_nome, d.referencia_em, "
        "(SELECT e.squad FROM execucao_lex e WHERE e.demanda_id = d.id "
        " AND e.resultado = 'pronto' ORDER BY e.n DESC LIMIT 1) AS squad_pronto, "
        "(SELECT e.squad_nome FROM execucao_lex e WHERE e.demanda_id = d.id "
        " AND e.resultado = 'pronto' ORDER BY e.n DESC LIMIT 1) AS nome_pronto "
        "FROM demanda d WHERE d.numero = ? AND EXISTS (SELECT 1 FROM execucao_lex e "
        "WHERE e.demanda_id = d.id) ORDER BY d.id DESC", (numero,)).fetchall()
    cartoes = []
    for r in linhas:
        slug = r["squad_pronto"] or r["lex_squad"]
        nome = r["nome_pronto"] or r["lex_squad_nome"]
        cartoes.append({"id": r["id"], "coluna_titulo": nucleo_quadro.COLUNAS.get(r["coluna"], ""),
                        "squad_rotulo": nome_do_squad(nome, slug) if slug or nome else "",
                        "data_br": data_br(r["referencia_em"])})
    return cartoes


LIMITE_TRECHO_CARTAO = 240
_TEXTOS_EVENTO = {
    "demanda_criada": "novo cartão",
    "demanda_item_juntado": "novidade juntada ao cartão",
    "autos_iniciados": "baixando autos",
    "autos_pedidos_de_novo": "download dos autos pedido de novo",
    "autos_falharam": "falha ao baixar os autos",
    "aviso_detectado": "intimação nova no PJe",
    "varredura_falhou": "varredura falhou",
}
# Texto de "Atividade agora" por (de, para, por); None vale para qualquer um.
_TEXTOS_MOVIMENTO = {
    ("lex", "autos", "mac"): "Lex não escolheu o tipo de peça",
    ("lex", "autos", None): "tirado do Lex",
    ("protocolado", "revisao", None): "protocolo desfeito",
    ("lex", "revisao", "advogado"): "ajuste desistido; a peça anterior voltou à revisão",
    ("lex", "revisao", None): "peça pronta para a sua revisão",
    ("revisao", "lex", None): "devolvido ao Lex com ajuste",
    ("autos", "lex", "sistema"): "enviado ao Lex automaticamente",
    ("autos", "lex", None): "enviado ao Lex",
    ("resolvida", "autos", None): "reaberto",
    ("chegou", "resolvida", None): "não é seu; saiu do quadro",
    ("revisao", "protocolado", None): "marcado como protocolado",
    (None, "acao", None): "marcado: precisa de ação",
    (None, "acompanhar", None): "marcado: só acompanhar",
    (None, "autos", None): "autos baixados",
    (None, "resolvida", None): "resolvido",
    (None, "chegou", None): "voltou para a triagem",
}


def texto_do_movimento(de: str | None, para: str | None, por: str | None) -> str:
    """O texto mais específico para o movimento: (de, para, por), depois (de, para),
    depois só o destino."""
    for chave in ((de, para, por), (de, para, None), (None, para, None)):
        if chave in _TEXTOS_MOVIMENTO:
            return _TEXTOS_MOVIMENTO[chave]
    return "cartão movido"


LIMITE_NOTA = 100_000
LIMITE_GATE = 1_000_000
PERIODOS = ("30d", "12m", "tudo")


def versao(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM evento").fetchone()[0]


def chegou_ha(data_iso: str, hoje: date) -> str:
    try:
        dias = (hoje - date.fromisoformat(data_iso[:10])).days
    except ValueError:
        return ""
    return "hoje" if dias <= 0 else "ontem" if dias == 1 else f"há {dias} dias"


def formatar_duracao(minutos: float | None) -> str:
    """14 → "14 min"; 95 → "1 h 35 min"; 120 → "2 h"; sem valor → ""."""
    if minutos is None:
        return ""
    total = max(0, round(minutos))
    horas, resto = divmod(total, 60)
    if not horas:
        return f"{resto} min"
    return f"{horas} h {resto} min" if resto else f"{horas} h"


def _momento(texto: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(texto or "")
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=tribunal.fuso())


def _minutos_entre(inicio: str | None, fim: str | None) -> int | None:
    a, b = _momento(inicio), _momento(fim)
    if a is None or b is None:
        return None
    return max(0, round((b - a).total_seconds() / 60))


def _execucoes(conn: sqlite3.Connection, demanda_id: int) -> list:
    return conn.execute("SELECT * FROM execucao_lex WHERE demanda_id = ? ORDER BY n",
                        (demanda_id,)).fetchall()


def _cronometro(d, execucoes: list) -> dict:
    """Tempo total = primeira execução pronta − chegada da novidade ao painel;
    tempo do Lex = soma das execuções prontas. `pronta` é a última execução pronta."""
    prontas = [e for e in execucoes if e["resultado"] == "pronto"]
    total = (_minutos_entre(d["criada_em"], prontas[0]["terminada_em"]) if prontas else None)
    lex = sum(_minutos_entre(e["iniciada_em"], e["terminada_em"]) or 0 for e in prontas)
    return {"pronta": prontas[-1] if prontas else None, "primeira": prontas[0] if prontas else None,
            "total_minutos": total, "lex_minutos": lex}


# --- nomes de squad e frases de falha -----------------------------------------------

_ACENTOS_SQUAD = {
    "contestacao": "contestação", "declaracao": "declaração", "replica": "réplica",
    "peticao": "petição", "apelacao": "apelação", "impugnacao": "impugnação",
    "manifestacao": "manifestação", "execucao": "execução", "sentenca": "sentença",
    "acao": "ação",
}


def humanizar_squad(slug: str | None) -> str:
    """"replica-a-contestacao" → "Réplica à contestação"; "EMBARGOS-DE-DECLARACAO" →
    "Embargos de declaração". Hífens e sublinhados viram espaço, acentos pelo dicionário,
    só a primeira letra maiúscula."""
    palavras = [p for p in re.split(r"[-_\s]+", str(slug or "").strip().lower()) if p]
    palavras = [_ACENTOS_SQUAD.get(p, p) for p in palavras]
    for i in range(len(palavras) - 1):
        if palavras[i] == "a" and palavras[i + 1] == "contestação":
            palavras[i] = "à"
    texto = " ".join(palavras)
    return texto[:1].upper() + texto[1:]


def nome_do_squad(nome: str | None, slug: str | None) -> str:
    """O nome que o Mac mandou (o `name` do squad.yaml) ou, na falta, o slug humanizado."""
    return " ".join(str(nome or "").split()) or humanizar_squad(slug)


# Na instalação local (CONTROLADORIA_LOCAL=1) o Lex roda no próprio computador do advogado,
# Windows ou Mac: as frases não falam em "Mac". A ordem importa (as longas primeiro).
_SEM_MAC = (
    ("o Mac perdeu o acesso ao plano do Lex",
     "o Lex perdeu o acesso ao plano do Claude (conecte de novo em Configurações)"),
    ("Mac sem acesso ao plano do Lex", "falta conectar o Lex ao plano do Claude"),
    ("aguardando o Mac", "aguardando o Lex começar"),
    ("o Mac parou de responder", "o Lex parou de responder (computador desligado?)"),
    ("no Mac", "neste computador"),
    ("ao Mac", "ao Lex"),
    ("erro no Mac", "erro no Lex"),
)


def sem_mac(texto):
    """A frase com "Mac" trocado pelo que vale na instalação local; fora dela, igual."""
    if not texto or os.getenv("CONTROLADORIA_LOCAL", "").strip() != "1":
        return texto
    for antigo, novo in _SEM_MAC:
        texto = texto.replace(antigo, novo)
    return texto


# Detalhes conhecidos do Mac → frase fixa (o detalhe cru fica só no histórico). Cobre
# todas as frases de `ponte_mac/laco.py::_FRASES_DE_ERRO`; a ordem importa (a recusa do
# painel pode citar "pacote inválido" na mensagem).
_FRASES_DO_DETALHE = (
    ("painel recusou o pacote", "o painel recusou a peça enviada"),
    ("acesso ao plano do lex no mac foi recusado", "o Mac perdeu o acesso ao plano do Lex"),
    ("acesso do claude no mac foi recusado", "o Mac perdeu o acesso ao plano do Lex"),  # Mac antigo
    ("manifesto do pacote é de outro run", "a peça encontrada não era desta execução"),
    ("terminou sem a linha final", "o Lex parou antes de concluir"),
    ("squad não é deste caso", "o Lex achou uma peça de outro caso e a recusou"),
    ("pacote da peça não encontrado", "o Lex terminou, mas a peça não apareceu na pasta do caso"),
    ("pacote não encontrado", "o Lex terminou, mas a peça não apareceu na pasta do caso"),
    ("não gerou pacote novo", "o Lex terminou sem gerar uma peça nova para este caso"),
    ("citation gate", "a peça veio sem o relatório de conferência das citações"),
    ("autos indisponíveis", "os autos não puderam ser entregues ao Lex"),
    ("pacote inválido", "o pedido chegou incompleto ao Mac"),
    ("ajuste", "o ajuste não encontrou a peça anterior no Mac"),
    ("claude code não foi encontrado", "o Claude Code não está instalado no computador do Lex"),
    ("erro no mac", "erro no Mac"),
)
_FRASES_DO_MOTIVO = {
    "limite": "o limite do plano acabou; o Lex tenta de novo sozinho depois da pausa",
    "tempo": "o Lex passou do tempo máximo sem terminar a peça",
    "sem_tipo": "o Lex não teve segurança sobre o tipo de peça",
    "reserva_vencida": "o Mac parou de responder",
}
FRASE_FALHA_DESCONHECIDA = "o Lex não terminou a peça (detalhe técnico no histórico)"


def frase_da_falha(motivo: str | None, detalhe: str | None = "") -> str:
    return sem_mac(_frase_da_falha(motivo, detalhe))


def _frase_da_falha(motivo: str | None, detalhe: str | None = "") -> str:
    """Frase fixa, em português, para a falha do Lex: pelo motivo (limite, tempo, sem
    tipo) ou por padrões conhecidos do detalhe; desconhecido vira a frase genérica.
    `motivo` aceita tanto o motivo da ponte (erro, tempo…) quanto o resultado gravado
    na execução (falhou, tempo, limite…)."""
    if motivo in _FRASES_DO_MOTIVO:
        return _FRASES_DO_MOTIVO[motivo]
    baixo = str(detalhe or "").lower()
    for padrao, frase in _FRASES_DO_DETALHE:
        if padrao in baixo:
            return frase
    return FRASE_FALHA_DESCONHECIDA


SEM_TIPO_TITULO = "O Lex não escolheu o tipo de peça"
SEM_TIPO_PADRAO = "escreva a orientação e mande de novo"


def explicacao_sem_tipo(detalhe: str | None) -> str:
    """A explicação do Lex guardada no sem_tipo; sem ela (ou a frase antiga, fixa), o
    pedido de orientação."""
    texto = " ".join(str(detalhe or "").split())
    if not texto or texto.lower().startswith("o lex não teve segurança"):
        return SEM_TIPO_PADRAO
    return texto


def reserva_vencida(d, agora: datetime) -> bool:
    """Cartão reservado/trabalhando cuja reserva já venceu (ou nem tem data): o Mac
    parou de responder e o advogado pode tirá-lo do Lex."""
    if d["coluna"] != "lex" or d["lex_estado"] not in ("reservado", "trabalhando"):
        return False
    ate = _momento(d["lex_reservado_ate"])
    return ate is None or ate < agora


def _ultimo_motivo(execucoes: list) -> str:
    fechadas = [e for e in execucoes if e["terminada_em"] is not None]
    return fechadas[-1]["resultado"] if fechadas else ""


def _rotulo_do_lex(conn: sqlite3.Connection, d, execucoes: list,
                   agora: datetime | None) -> str:
    return sem_mac(_rotulo_do_lex_bruto(conn, d, execucoes, agora))


def _rotulo_do_lex_bruto(conn: sqlite3.Connection, d, execucoes: list,
                         agora: datetime | None) -> str:
    estado = d["lex_estado"]
    agora = agora or agora_cuiaba()
    if estado == "na_fila":
        if not nucleo_ponte.mac_conectado(conn, agora):
            return "aguardando o Mac"
        if nucleo_ponte.mac_sem_plano(conn, agora):
            return "na fila · Mac sem acesso ao plano do Lex"
        return "na fila do Lex"
    if estado in ("reservado", "trabalhando"):
        if reserva_vencida(d, agora):
            return "o Mac parou de responder"
        aberta = next((e for e in reversed(execucoes) if e["terminada_em"] is None), None)
        inicio = _momento(aberta["iniciada_em"]) if aberta else None
        partes = ["Lex trabalhando" + (
            f" há {max(0, round((agora - inicio).total_seconds() / 60))} min" if inicio else "")]
        if d["lex_squad"] or d["lex_squad_nome"]:
            partes.append(nome_do_squad(d["lex_squad_nome"], d["lex_squad"]))
        if d["lex_etapa"]:
            partes.append(d["lex_etapa"])
        return " · ".join(partes)
    if estado == "pausado_limite":
        return "pausado: limite do plano"
    if estado == "falhou":
        return "falhou: " + frase_da_falha(_ultimo_motivo(execucoes), d["lex_detalhe"])
    if estado == "sem_tipo":
        return "o Lex precisa da sua orientação"
    if estado == "pronto":
        return "peça pronta"
    return ""


def _cartao(conn: sqlite3.Connection, d, hoje: date, agora: datetime | None = None) -> dict:
    itens = conn.execute("SELECT tipo, ref, data FROM demanda_item WHERE demanda_id = ? "
                         "ORDER BY data DESC, rowid DESC", (d["id"],)).fetchall()
    origens = []
    for tipo in ("publicacao", "aviso"):
        if any(i["tipo"] == tipo for i in itens):
            origens.append("DJEN" if tipo == "publicacao" else "PJe")
    trecho = ""
    for item in itens:  # a novidade mais recente que tiver texto
        if item["tipo"] == "publicacao":
            p = conn.execute("SELECT texto, tipo, orgao FROM publicacao WHERE id = ?",
                             (int(item["ref"]),)).fetchone()
            if p:
                trecho = (_limpar_texto(p["texto"], LIMITE_TRECHO_CARTAO)
                          or " · ".join(x for x in (p["tipo"], p["orgao"]) if x))
        else:
            instancia, _, id_aviso = item["ref"].partition(":")
            a = conn.execute("SELECT tipo_comunicacao, pendente FROM aviso "
                             "WHERE instancia = ? AND id = ?", (instancia, id_aviso)).fetchone()
            rotulo = avisos.rotulo_do_tipo(a["tipo_comunicacao"] if a else "")
            if a is None or a["pendente"]:
                trecho = f"{rotulo} no PJe aguardando ciência"
            else:
                trecho = (f"{rotulo} no PJe · ciência já registrada "
                          "(o prazo pode estar correndo)")
        if trecho:
            break
    execucoes = _execucoes(conn, d["id"])
    cron = _cronometro(d, execucoes)
    pronta = cron["pronta"]
    agora = agora or agora_cuiaba()
    return {
        "tem_execucao": bool(execucoes),
        "reserva_vencida": reserva_vencida(d, agora),
        "squad_rotulo": (nome_do_squad(d["lex_squad_nome"], d["lex_squad"])
                         if d["lex_squad"] or d["lex_squad_nome"] else ""),
        "id": d["id"], "numero": d["numero"], "numero_formatado": formatar_numero_cnj(d["numero"]),
        "cliente": d["cliente"] or "", "parte_contraria": d["parte_contraria"] or "",
        "orgao": d["orgao_julgador"] or "", "origem": " + ".join(origens) or "DJEN",
        "data_br": data_br(d["referencia_em"]), "chegou_ha": chegou_ha(d["referencia_em"], hoje),
        "trecho": trecho, "n_itens": len(itens), "coluna": d["coluna"],
        "partes_publicadas": ([] if d["cliente"] or d["parte_contraria"]
                              else partes_publicadas(conn, d["numero"])),
        "autos_estado": d["autos_estado"], "autos_detalhe": d["autos_detalhe"],
        "lex_estado": d["lex_estado"], "lex_rotulo": _rotulo_do_lex(conn, d, execucoes, agora),
        "lex_orientacao": d["lex_orientacao"], "lex_detalhe": d["lex_detalhe"],
        "sem_tipo_explicacao": (explicacao_sem_tipo(d["lex_detalhe"])
                                if d["lex_estado"] == "sem_tipo" else ""),
        "citacoes_falhas": pronta["citacoes_falhas"] if pronta else None,
        "gate_status": pronta["gate_status"] if pronta else "",
        "tempo_total": formatar_duracao(cron["total_minutos"]),
        "tempo_lex": formatar_duracao(cron["lex_minutos"]) if pronta else "",
    }


def quadro(conn: sqlite3.Connection, hoje: date, agora: datetime | None = None) -> dict:
    agora = agora or agora_cuiaba()
    colunas = []
    for chave in nucleo_quadro.COLUNAS_VISIVEIS:
        linhas = conn.execute(
            "SELECT d.*, p.cliente, p.parte_contraria, p.orgao_julgador FROM demanda d "
            "LEFT JOIN processo p ON p.numero = d.numero WHERE d.coluna = ? "
            "ORDER BY d.referencia_em, d.id", (chave,)).fetchall()
        colunas.append({"chave": chave, "titulo": nucleo_quadro.COLUNAS[chave],
                        "cartoes": [_cartao(conn, d, hoje, agora) for d in linhas]})
    return {"colunas": colunas, "versao": versao(conn),
            "na_triagem": len(colunas[0]["cartoes"]),
            "lex_automatico": config.lex_automatico(conn),
            "mac_sem_plano": nucleo_ponte.mac_sem_plano(conn, agora),
            "varredura": {"ativa": tarefas_fila.ativa(conn, "varredura"),
                          "ultima": ultima_varredura(conn)}}


def atividade_agora(conn: sqlite3.Connection, limite: int = 8) -> list[dict]:
    tipos = tuple(_TEXTOS_EVENTO) + ("demanda_movida", "varredura_concluida")
    marcas = ", ".join("?" for _ in tipos)
    itens = []
    for e in conn.execute(f"SELECT quando, tipo, numero, dados FROM evento "
                          f"WHERE tipo IN ({marcas}) ORDER BY quando DESC, id DESC LIMIT ?",
                          (*tipos, limite)).fetchall():
        dados = json.loads(e["dados"] or "{}")
        if e["tipo"] == "demanda_movida":
            texto = texto_do_movimento(dados.get("de"), dados.get("para"), dados.get("por"))
        elif e["tipo"] == "varredura_concluida":
            novidades = dados.get("publicacoes_novas", 0) + dados.get("avisos_novos", 0)
            texto = f"varredura concluída · {novidades} novidade(s)"
        else:
            texto = _TEXTOS_EVENTO[e["tipo"]]
        q = e["quando"]
        itens.append({"hora": q[11:16], "data_br": f"{q[8:10]}/{q[5:7]}/{q[0:4]}",
                      "texto": texto, "numero": e["numero"] or "",
                      "numero_formatado": formatar_numero_cnj(e["numero"]) if e["numero"] else ""})
    return itens


# --- cartão aberto (Fase 3) -------------------------------------------------------

_RESULTADOS_TEXTO = {
    "pronto": "peça pronta", "falhou": "falhou", "limite": "limite do plano",
    "sem_tipo": "sem segurança no tipo de peça", "tempo": "tempo esgotado",
    "reserva_vencida": "o Mac parou de responder", "": "em andamento",
}


def _data_hora_br(iso: str | None) -> str:
    m = _momento(iso)
    return m.strftime("%d/%m/%Y %H:%M") if m else ""


def _texto_do_evento(tipo: str, dados: dict, com_detalhe: bool = True) -> str | None:
    return sem_mac(_texto_do_evento_bruto(tipo, dados, com_detalhe))


def _texto_do_evento_bruto(tipo: str, dados: dict, com_detalhe: bool = True) -> str | None:
    if tipo == "demanda_movida":
        if (dados.get("de"), dados.get("para")) == ("protocolado", "revisao"):
            return "protocolo desfeito; de volta à sua revisão"
        if (dados.get("de"), dados.get("para"), dados.get("por")) == ("lex", "revisao",
                                                                         "advogado"):
            return "você desistiu do ajuste; a peça anterior voltou à sua revisão"
        para = nucleo_quadro.COLUNAS.get(dados.get("para"), "outra coluna")
        quem = {"advogado": "por você", "mac": "pelo Lex", "sistema": "automaticamente",
                "trabalhador": "pelo sistema"}.get(dados.get("por"), "")
        return f"movido para “{para}”" + (f" {quem}" if quem else "")
    if tipo == "lex_enfileirado":
        return ("ajuste pedido ao Lex" if dados.get("modo") == "ajuste"
                else "enviado à fila do Lex")
    if tipo == "lex_iniciado":
        return f"Lex começou a trabalhar (tentativa {dados.get('n', 1)})"
    if tipo == "lex_pronto":
        nome = (nome_do_squad(dados.get("squad_nome"), dados.get("squad"))
                if dados.get("squad") or dados.get("squad_nome") else "")
        extra = f" · {nome}" if nome else ""
        aviso = f" ({dados['detalhe']})" if dados.get("detalhe") and com_detalhe else ""
        return (f"Lex terminou a peça{extra} · {formatar_duracao(dados.get('minutos_lex', 0))}"
                f"{aviso}")
    if tipo == "lex_falhou":
        motivo = {"limite": "limite do plano", "tempo": "tempo esgotado",
                  "sem_tipo": "sem segurança no tipo de peça"}.get(dados.get("motivo"), "erro")
        detalhe = f": {dados['detalhe']}" if dados.get("detalhe") and com_detalhe else ""
        return f"Lex não concluiu ({motivo}){detalhe}"
    if tipo == "lex_reserva_vencida":
        return "o Mac parou de responder; cartão devolvido à fila"
    if tipo == "lex_liberado_apos_limite":
        return "fim da pausa do limite; de volta à fila do Lex"
    if tipo == "lex_pedido_de_novo":
        return "nova tentativa pedida ao Lex"
    if tipo == "protocolado":
        return "marcado como protocolado"
    return _TEXTOS_EVENTO.get(tipo)


_EVENTO_QUE_SUBSTITUI_MOVIMENTO = {"lex": "lex_enfileirado", "protocolado": "protocolado"}


def _historico(conn: sqlite3.Connection, d) -> list[dict]:
    """Eventos do cartão em ordem. O movimento para "Lex" ou "Protocolado" também
    registra um evento específico; mostra só este, para não repetir o mesmo passo."""
    eventos_do_cartao = []
    for e in conn.execute("SELECT quando, tipo, dados FROM evento WHERE numero = ? ORDER BY id",
                          (d["numero"],)).fetchall():
        try:
            dados = json.loads(e["dados"] or "{}")
        except ValueError:
            continue
        if isinstance(dados, dict) and dados.get("demanda") == d["id"]:
            eventos_do_cartao.append((e["quando"], e["tipo"], dados))
    itens = []
    for i, (quando, tipo, dados) in enumerate(eventos_do_cartao):
        seguinte = eventos_do_cartao[i + 1][1] if i + 1 < len(eventos_do_cartao) else None
        substituto = _EVENTO_QUE_SUBSTITUI_MOVIMENTO.get(dados.get("para"))
        if tipo == "demanda_movida" and substituto and substituto == seguinte:
            continue
        texto = _texto_do_evento(tipo, dados)
        if texto:
            # `texto_curto`: sem o detalhe livre do Mac (modo apresentação).
            itens.append({"quando": _data_hora_br(quando), "texto": texto,
                          "texto_curto": _texto_do_evento(tipo, dados, com_detalhe=False)})
    return itens


def _ler_pacote(pasta: str) -> dict:
    """O que o Mac devolveu: PDF, Word, nota ao revisor e o relatório do Citation Gate."""
    def _arquivo(nome: str) -> str:
        return os.path.join(pasta, nome)

    nota = ""
    try:
        with open(_arquivo("nota-ao-revisor.md"), "rb") as f:
            nota = f.read(LIMITE_NOTA).decode("utf-8", errors="replace")
    except OSError:
        pass
    try:
        with open(_arquivo("citation-gate.json"), "rb") as f:
            bruto = f.read(LIMITE_GATE + 1)
        # Acima do limite não se lê (JSON cortado seria inválido): conta como vazio.
        gate = json.loads(bruto.decode("utf-8")) if len(bruto) <= LIMITE_GATE else {}
    except (OSError, ValueError, RecursionError):  # ValueError cobre UnicodeDecodeError
        gate = {}
    gate = gate if isinstance(gate, dict) else {}

    def _lista(chave: str) -> list:
        valor = gate.get(chave)
        return valor if isinstance(valor, list) else []

    citacoes = [{"title": str(c.get("title") or ""), "status": str(c.get("status") or ""),
                 "source_url": _link_seguro(c.get("source_url"))}
                for c in _lista("citations") if isinstance(c, dict)]
    pendencias = [{"marcador": str(p.get("marcador") or ""), "onde": str(p.get("onde") or ""),
                   "diligencia": str(p.get("diligencia") or "")}
                  for p in _lista("pendencias_do_profissional") if isinstance(p, dict)]
    return {"tem_pdf": os.path.isfile(_arquivo("peca.pdf")),
            "tem_docx": os.path.isfile(_arquivo("peca.docx")),
            "tem_termo": os.path.isfile(_arquivo("termo-de-conferencia.docx")),
            "nota": nota, "citacoes": citacoes, "pendencias": pendencias,
            "gate_status": str(gate.get("gate_status") or "")}


def _pasta_da_execucao(execucao, pecas_dir: str) -> str:
    # Sempre dentro de `pecas_dir`: o caminho gravado no banco só vale se estiver lá.
    esperada = os.path.join(pecas_dir, str(execucao["demanda_id"]), str(execucao["n"]))
    gravada = execucao["pasta"] or ""
    if gravada and os.path.realpath(gravada) == os.path.realpath(esperada):
        return gravada
    return esperada


def publicacoes_do_cartao(conn: sqlite3.Connection, demanda_id: int) -> list[dict]:
    """As publicações do cartão, da mais recente para a mais antiga, com o trecho e a
    íntegra em parágrafos (texto puro) para ler no painel."""
    pubs = []
    for r in conn.execute(
            "SELECT p.* FROM demanda_item i JOIN publicacao p ON p.id = CAST(i.ref AS INTEGER) "
            "WHERE i.demanda_id = ? AND i.tipo = 'publicacao' "
            "ORDER BY p.data_disponibilizacao DESC, p.id DESC", (demanda_id,)).fetchall():
        pubs.append({"id": r["id"], "data_br": data_br(r["data_disponibilizacao"]),
                     "tipo": r["tipo"] or "", "orgao": r["orgao"] or "",
                     "trecho": _limpar_texto(r["texto"], LIMITE_TRECHO_CARTAO),
                     "integra": texto_integral(r["texto"]), "link": _link_seguro(r["link"])})
    return pubs


def demanda_detalhe(conn: sqlite3.Connection, demanda_id: int, hoje: date,
                    pecas_dir: str, agora: datetime | None = None) -> dict | None:
    d = conn.execute(
        "SELECT d.*, p.cliente, p.parte_contraria, p.orgao_julgador FROM demanda d "
        "LEFT JOIN processo p ON p.numero = d.numero WHERE d.id = ?", (demanda_id,)).fetchone()
    if d is None:
        return None
    processo = conn.execute("SELECT * FROM processo WHERE numero = ?", (d["numero"],)).fetchone()
    execucoes = _execucoes(conn, demanda_id)
    cron = _cronometro(d, execucoes)
    pronta = cron["pronta"]
    peca = None
    if pronta:
        peca = {"n": pronta["n"], "squad": pronta["squad"],
                "squad_rotulo": nome_do_squad(pronta["squad_nome"], pronta["squad"])
                if pronta["squad"] or pronta["squad_nome"] else "",
                "citacoes_total": pronta["citacoes_total"],
                "citacoes_falhas": pronta["citacoes_falhas"],
                **_ler_pacote(_pasta_da_execucao(pronta, pecas_dir))}
        peca["gate_status"] = pronta["gate_status"] or peca["gate_status"]
    cartao = _cartao(conn, d, hoje, agora)
    # "Desistir do ajuste": há peça pronta anterior e o Lex está parado no ajuste.
    pode_desistir = bool(pronta and d["coluna"] == "lex" and d["lex_ajuste"]
                         and d["lex_estado"] in nucleo_quadro.LEX_PARADO)
    lex_frase = ""
    if d["lex_estado"] == "falhou" or (d["lex_estado"] == "pausado_limite"):
        lex_frase = frase_da_falha(_ultimo_motivo(execucoes), d["lex_detalhe"])
    elif d["lex_estado"] == "sem_tipo":
        lex_frase = f"{SEM_TIPO_TITULO}: {cartao['sem_tipo_explicacao']}"
    return {
        "monitoramento": monitoramento(processo),
        "partes_publicadas": ([] if d["cliente"] or d["parte_contraria"]
                              else partes_publicadas(conn, d["numero"])),
        "publicacoes": publicacoes_do_cartao(conn, demanda_id),
        "sem_tipo_titulo": SEM_TIPO_TITULO,
        "sem_tipo_explicacao": cartao["sem_tipo_explicacao"],
        "reserva_vencida": cartao["reserva_vencida"], "pode_desistir": pode_desistir,
        "lex_frase": lex_frase, "squad_rotulo": cartao["squad_rotulo"] or (
            peca["squad_rotulo"] if peca else ""),
        "id": d["id"], "numero": d["numero"], "numero_formatado": cartao["numero_formatado"],
        "cliente": cartao["cliente"], "parte_contraria": cartao["parte_contraria"],
        "orgao": cartao["orgao"], "origem": cartao["origem"], "data_br": cartao["data_br"],
        "chegou_ha": cartao["chegou_ha"], "trecho": cartao["trecho"],
        "coluna": d["coluna"], "coluna_titulo": nucleo_quadro.COLUNAS.get(d["coluna"], d["coluna"]),
        "orientacao": d["lex_orientacao"], "ajuste": d["lex_ajuste"],
        "squad": d["lex_squad"] or (pronta["squad"] if pronta else ""),
        "lex_estado": cartao["lex_estado"], "lex_rotulo": cartao["lex_rotulo"],
        "lex_detalhe": d["lex_detalhe"],
        "execucoes": [{"n": e["n"], "modo": e["modo"], "inicio": _data_hora_br(e["iniciada_em"]),
                       "fim": _data_hora_br(e["terminada_em"]),
                       "resultado": e["resultado"],
                       "resultado_rotulo": sem_mac(_RESULTADOS_TEXTO.get(e["resultado"], e["resultado"])),
                       "minutos": _minutos_entre(e["iniciada_em"], e["terminada_em"]),
                       "squad": e["squad"],
                       "squad_rotulo": nome_do_squad(e["squad_nome"], e["squad"])
                       if e["squad"] or e["squad_nome"] else "",
                       # Detalhe cru só no histórico; aqui, a frase fixa da falha.
                       "frase": frase_da_falha(e["resultado"], e["detalhe"])
                       if e["resultado"] in ("falhou", "tempo") else "",
                       "detalhe": e["detalhe"]} for e in execucoes],
        "cronometro": {"tempo_total": formatar_duracao(cron["total_minutos"]),
                       "tempo_lex": formatar_duracao(cron["lex_minutos"]) if pronta else "",
                       "total_minutos": cron["total_minutos"],
                       "lex_minutos": cron["lex_minutos"] if pronta else None},
        "pronta": peca,
        "historico": _historico(conn, d),
    }


# --- Resultados -------------------------------------------------------------------

def resultados(conn: sqlite3.Connection, periodo: str,
               agora: datetime | None = None) -> dict:
    """Números da aba Resultados. Uma peça = um cartão com ao menos uma execução
    pronta, contada na data da primeira. `periodo`: 30d, 12m ou tudo."""
    if periodo not in PERIODOS:
        periodo = "30d"
    agora = agora or agora_cuiaba()
    corte = {"30d": agora - timedelta(days=30), "12m": agora - timedelta(days=365),
             "tudo": None}[periodo]
    pecas = []
    for d in conn.execute("SELECT * FROM demanda WHERE id IN (SELECT demanda_id FROM execucao_lex "
                          "WHERE resultado = 'pronto') ORDER BY id").fetchall():
        cron = _cronometro(d, _execucoes(conn, d["id"]))
        primeira = cron["primeira"]
        fim = _momento(primeira["terminada_em"])
        if fim is None or (corte is not None and fim < corte):
            continue
        pecas.append({"fim": fim, "slug": primeira["squad"] or "",
                      "squad_nome": primeira["squad_nome"] or "",
                      "total": cron["total_minutos"], "lex": cron["lex_minutos"],
                      "falhas": primeira["citacoes_falhas"]})
    # Agrupa pelo slug; exibe o nome mais recente que o Mac mandou (ou o slug humanizado).
    nomes: dict[str, str] = {}
    for p in sorted(pecas, key=lambda p: p["fim"]):
        if p["squad_nome"] or p["slug"] not in nomes:
            nomes[p["slug"]] = nome_do_squad(p["squad_nome"], p["slug"]) or "Não informado"
    for p in pecas:
        p["squad"] = nomes[p["slug"]]
    totais = [p["total"] for p in pecas if p["total"] is not None]
    media_total = sum(totais) / len(totais) if totais else None
    media_lex = sum(p["lex"] for p in pecas) / len(pecas) if pecas else None
    por_mes: dict[str, int] = {}
    por_tipo: dict[str, int] = {}
    for p in pecas:
        mes = p["fim"].strftime("%Y-%m")
        por_mes[mes] = por_mes.get(mes, 0) + 1
        por_tipo[p["slug"]] = por_tipo.get(p["slug"], 0) + 1
    melhores = sorted((p for p in pecas if p["total"] is not None),
                      key=lambda p: (p["total"], p["fim"]))[:3]
    limpas = sum(1 for p in pecas if p["falhas"] == 0)
    return {
        "periodo": periodo,
        "tempo_medio_total": formatar_duracao(media_total),
        "tempo_medio_total_minutos": None if media_total is None else round(media_total),
        "tempo_medio_lex": formatar_duracao(media_lex),
        "tempo_medio_lex_minutos": None if media_lex is None else round(media_lex),
        "pecas_por_mes": sorted(por_mes.items()),
        "pecas_por_tipo": sorted(((nomes[slug], q) for slug, q in por_tipo.items()),
                                 key=lambda x: (-x[1], x[0])),
        "melhores": [{"squad": p["squad"], "minutos": p["total"],
                      "texto": formatar_duracao(p["total"]),
                      "data": p["fim"].strftime("%d/%m/%Y")} for p in melhores],
        "citacoes_de_primeira": round(100 * limpas / len(pecas)) if pecas else None,
        "total_pecas": len(pecas),
    }


# --- nota ao revisor: markdown mínimo e seguro (V5) -------------------------------

_MD_TITULO = re.compile(r"^(#{1,3})\s+(.*)$")
_MD_ITEM = re.compile(r"^[-*+]\s+(.*)$")
_MD_ITEM_NUM = re.compile(r"^\d{1,3}[.)]\s+(.*)$")


_MD_CODIGO = re.compile(r"`([^`]*)`")
_MD_NEGRITO = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")  # linear: sem `.+?`
_MD_ITALICO = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")
_MD_MARCA = re.compile(r"\x00(\d+)\x00")


def _md_em_linha(texto: str) -> str:
    """Escapa o texto e só então aplica `código`, **negrito** e *itálico* (também
    **`código` em negrito**). Nada vira link: endereços ficam como texto."""
    codigos: list[str] = []

    def guardar(m: re.Match) -> str:
        codigos.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codigos) - 1}\x00"

    parte = _MD_CODIGO.sub(guardar, str(escape(str(texto).replace("\x00", ""))))
    parte = _MD_NEGRITO.sub(r"<strong>\1</strong>", parte)
    parte = _MD_ITALICO.sub(r"<em>\1</em>", parte)
    return _MD_MARCA.sub(lambda m: codigos[int(m.group(1))], parte)


def markdown_minimo(texto: str | None) -> Markup:
    """Markdown mínimo para a nota ao revisor: títulos (#, ##, ###), **negrito**,
    *itálico*, `código`, blocos ``` (viram <pre><code>), listas (-, *, 1.) e parágrafos.
    Tudo é escapado antes; não sai link clicável nem HTML do texto."""
    blocos: list[str] = []
    paragrafo: list[str] = []
    lista: tuple[str, list[str]] | None = None
    codigo: list[str] | None = None  # dentro de um bloco ```

    def fechar_paragrafo():
        if paragrafo:
            blocos.append("<p>" + " ".join(_md_em_linha(x) for x in paragrafo) + "</p>")
            paragrafo.clear()

    def fechar_lista():
        nonlocal lista
        if lista:
            tag, itens = lista
            blocos.append(f"<{tag}>" + "".join(f"<li>{i}</li>" for i in itens) + f"</{tag}>")
            lista = None

    def fechar_codigo():
        nonlocal codigo
        if codigo is not None:
            blocos.append("<pre><code>" + str(escape("\n".join(codigo))) + "</code></pre>")
            codigo = None

    for bruta in str(texto or "").replace("\r\n", "\n").split("\n"):
        linha = bruta.strip()
        if linha.startswith("```"):
            if codigo is None:
                fechar_paragrafo()
                fechar_lista()
                codigo = []
            else:
                fechar_codigo()
            continue
        if codigo is not None:
            codigo.append(bruta.rstrip())
            continue
        if not linha:
            fechar_paragrafo()
            fechar_lista()
            continue
        titulo = _MD_TITULO.match(linha)
        item = _MD_ITEM.match(linha)
        numerado = _MD_ITEM_NUM.match(linha)
        if titulo:
            fechar_paragrafo()
            fechar_lista()
            nivel = len(titulo.group(1)) + 2  # # → h3 (a página já tem h1 e h2)
            blocos.append(f"<h{nivel}>{_md_em_linha(titulo.group(2))}</h{nivel}>")
        elif item or numerado:
            fechar_paragrafo()
            tag = "ul" if item else "ol"
            if lista and lista[0] != tag:
                fechar_lista()
            if lista is None:
                lista = (tag, [])
            lista[1].append(_md_em_linha((item or numerado).group(1)))
        else:
            fechar_lista()
            paragrafo.append(linha)
    fechar_paragrafo()
    fechar_lista()
    fechar_codigo()  # bloco sem a cerca de fechamento: fecha no fim
    return Markup("".join(blocos))
