"""Modo apresentação: mostrar o sistema de verdade sem expor clientes.

Nomes conhecidos do banco (clientes, partes contrárias, todos os nomes das partes e
advogados) viram nomes fictícios consistentes — o mesmo nome real vira sempre o mesmo
nome fictício — e os números CNJ ficam mascarados, preservando ano, justiça e tribunal.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import re
import sqlite3
import unicodedata

from markupsafe import escape

_PRIMEIROS = (
    "AURORA", "BENEDITO", "CELINA", "DARIO", "ELISA", "FABIANO", "GLÓRIA", "HEITOR",
    "IOLANDA", "JONAS", "LÍVIA", "MOACIR", "NAIR", "OSVALDO", "PIETRA", "QUIRINO",
    "ROSANA", "SAMUEL", "TELMA", "ULISSES", "VILMA", "WAGNER", "YARA", "ZENO",
    "ADELINO", "BRUNA", "CÍCERO", "DALVA", "EMÍLIO", "FLORA",
)
_SOBRENOMES = (
    "ARAGÃO", "BARRETO", "CAVALCANTI", "DUARTE", "ESTEVES", "FALCÃO", "GUSMÃO", "HOLANDA",
    "IGREJA", "JARDIM", "LACERDA", "MACEDO", "NOGUEIRA", "PASSOS", "QUEIROZ", "RANGEL",
    "SAMPAIO", "TAVARES", "UCHÔA", "VALADARES", "XAVIER", "ZANETTI", "AMARAL",
    "BITTENCOURT", "CORDEIRO", "DANTAS", "FONTES", "GUERRA", "LEMOS", "MOURA",
)
_PALAVRAS = (
    "AURORA", "BORDA", "CASCATA", "DELTA", "ESTRELA", "FAROL", "GIRASSOL", "IPÊ",
    "JATOBÁ", "LUAR", "MIRANTE", "NASCENTE", "ORVALHO", "PRISMA", "QUARTZO", "RIACHO",
    "SERENO", "TOPÁZIO", "VERTENTE", "ZÊNITE", "ALVORADA", "BRISA", "CEDRO", "DUNA",
)
_MARCAS_EMPRESA = {"LTDA", "LTDA.", "EIRELI", "ME", "BANCO", "S.A.", "S.A", "S/A"}
_MARCAS_ENTE = {"MUNICÍPIO", "MUNICIPIO", "ESTADO", "UNIÃO", "UNIAO"}

TEXTO_OCULTO = "texto oculto no modo apresentação"


def _normalizar(nome: str) -> str:
    return " ".join(str(nome or "").split()).upper()


# Sem chave do painel, a escolha sai de uma chave fixa (o painel sempre passa a chave
# derivada do segredo, para ninguém refazer o nome real a partir do fictício testando
# nomes candidatos).
_CHAVE_PADRAO = b"controladoria-apresentacao"


def _escolher(chave: str, salto: str, opcoes: tuple[str, ...],
              segredo: bytes | None = None) -> str:
    resumo = hmac.new(segredo or _CHAVE_PADRAO, f"{salto}|{chave}".encode(), "sha256").digest()
    return opcoes[int.from_bytes(resumo[:8], "big") % len(opcoes)]


def nome_ficticio(nome_real: str, chave: bytes | None = None) -> str:
    """Nome fictício determinístico (mesmo nome real e mesma chave → mesmo fictício, em
    qualquer página), escolhido por HMAC com a `chave` (o painel passa uma chave
    derivada do seu segredo). Empresas viram "EMPRESA <PALAVRA> LTDA"; entes públicos,
    "ENTE PÚBLICO <PALAVRA>"; pessoas, nome e dois sobrenomes fictícios."""
    texto = _normalizar(nome_real)
    palavras = set(texto.replace(",", " ").split())
    if palavras & _MARCAS_EMPRESA or "S.A." in texto or "S/A" in texto:
        return f"EMPRESA {_escolher(texto, 'empresa', _PALAVRAS, chave)} LTDA"
    if palavras & _MARCAS_ENTE:
        return f"ENTE PÚBLICO {_escolher(texto, 'ente', _PALAVRAS, chave)}"
    primeiro = _escolher(texto, "nome", _PRIMEIROS, chave)
    sobrenome = _escolher(texto, "sobrenome", _SOBRENOMES, chave)
    outro = _escolher(texto, "sobrenome2", _SOBRENOMES, chave)
    if outro == sobrenome:
        outro = _SOBRENOMES[(_SOBRENOMES.index(outro) + 1) % len(_SOBRENOMES)]
    return f"{primeiro} {sobrenome} {outro}"


_CNJ_FORMATADO = re.compile(r"(?<!\d)\d{7}-\d{2}\.(\d{4})\.(\d)\.(\d{2})\.\d{4}(?!\d)")
_CNJ_CRU = re.compile(r"(?<!\d)\d{9}(\d{4})(\d)(\d{2})\d{4}(?!\d)")


def _sete_digitos(digitos: str, chave: bytes | None) -> str:
    resumo = hmac.new(chave or _CHAVE_PADRAO, digitos.encode(), "sha256").digest()
    return f"{int.from_bytes(resumo[:8], 'big') % 10_000_000:07d}"


def mascarar_cnj(texto: str, chave: bytes | None = None) -> str:
    """Troca números CNJ (formatados ou em 20 dígitos) por NNNNNNN-••.AAAA.J.TR.••••:
    ano, justiça e tribunal preservados; os 7 primeiros dígitos são fictícios e
    estáveis (o mesmo número real dá sempre a mesma máscara)."""
    def trocar(m: re.Match) -> str:
        digitos = re.sub(r"\D", "", m.group(0))
        return (f"{_sete_digitos(digitos, chave)}-••.{m.group(1)}.{m.group(2)}."
                f"{m.group(3)}.••••")
    return _CNJ_CRU.sub(trocar, _CNJ_FORMATADO.sub(trocar, texto))


_UFS = ("AC|AL|AM|AP|BA|CE|DF|ES|GO|MA|MG|MS|MT|PA|PB|PE|PI|PR|RJ|RN|RO|RR|RS|SC|SE|SP|TO")
_NUMERO_OAB = r"\d{1,3}(?:\.\d{3})+|\d{3,8}"
# "OAB/MT 54321", "OAB MT nº 54.321-A", "oab/mt 54321"
_OAB_EXTENSO = re.compile(
    r"\bOAB\s*[/-]?\s*(" + _UFS + r")\s*(?:n[º°o.]*\s*)?(?:" + _NUMERO_OAB + r")"
    r"(?:\s*-?\s*[A-Z](?![\w]))?", re.IGNORECASE)
# "54321/MT", "54.321/MT", "13408/B"
_OAB_BARRA = re.compile(
    r"(?<![\w./-])(?:" + _NUMERO_OAB + r")\s*/\s*(" + _UFS + r"|[A-Z])(?![\w])", re.IGNORECASE)
# "MT0054321A", "MT12345-A", "MT-54321-A", "mt0054321a"
_OAB_COMPACTO = re.compile(
    r"(?<![\w#&-])(" + _UFS + r")(?:-)?(?:" + _NUMERO_OAB + r")(?:-?[A-Z])?(?![\w-])", re.IGNORECASE)
# "MT 54321" (só com a sigla em maiúsculas: "se 2024" é texto comum)
_OAB_ESPACO = re.compile(r"(?<![\w-])(" + _UFS + r")\s+(?:" + _NUMERO_OAB + r")(?:-[A-Z])?(?![\w-])")


# Formato do PJe (sigla + 7 dígitos + letra, "MT0054321A"): inconfundível, vale sozinho.
_OAB_PJE = re.compile(r"(?<![\w#&-])(" + _UFS + r")\d{7}[A-Z](?![\w-])", re.IGNORECASE)
# Os formatos soltos ("54321/MT", "MT 54321", "MT-54321-A"…) só valem perto de "OAB" ou
# de "adv"/"advogado(a)": sem isso, "MT 2024" ou "12345/SP" são texto comum.
_CONTEXTO_OAB = re.compile(r"\b(?:oab|adv)", re.IGNORECASE)
JANELA_CONTEXTO_OAB = 80


def _oab(m: re.Match) -> str:
    uf = (m.group(1) or "").upper()
    return f"OAB/{uf} •••••" if len(uf) == 2 else "OAB •••••"


def _oab_com_contexto(texto: str):
    """Contexto = "OAB"/"adv" até 80 caracteres antes, ou uma inscrição já mascarada
    nesse trecho (lista longa de advogados: cada número encadeia o seguinte)."""
    ultimo_fim = None  # fim da última inscrição mascarada nesta passada

    def trocar(m: re.Match) -> str:
        nonlocal ultimo_fim
        antes = texto[max(0, m.start() - JANELA_CONTEXTO_OAB):m.start()]
        encadeado = ultimo_fim is not None and m.start() - ultimo_fim <= JANELA_CONTEXTO_OAB
        if encadeado or _CONTEXTO_OAB.search(antes):
            ultimo_fim = m.end()
            return _oab(m)
        return m.group(0)
    return trocar


def mascarar_oab(texto: str) -> str:
    """Inscrições na OAB → "OAB/MT •••••" (ou "OAB •••••" sem a sigla do estado).
    Sempre: "OAB/MT 54321", "OAB MT nº 54.321-A" e o formato do PJe "MT0054321A". Os
    formatos soltos ("54321/MT", "54.321/MT", "MT-54321-A", "13408/B", "MT 54321") só
    com "OAB" ou "adv" pouco antes, para não mascarar "MT 2024" ou "12345/SP"."""
    texto = _OAB_EXTENSO.sub(_oab, texto)
    texto = _OAB_PJE.sub(_oab, texto)
    for padrao in (_OAB_BARRA, _OAB_COMPACTO, _OAB_ESPACO):
        texto = padrao.sub(_oab_com_contexto(texto), texto)
    return texto


def _nomes_do_json(valor, saida: list[str]) -> None:
    if isinstance(valor, dict):
        for chave, item in valor.items():
            if chave == "nome" and isinstance(item, str):
                saida.append(item)
            elif chave == "nomes" and isinstance(item, list):
                saida.extend(n for n in item if isinstance(n, str))
            else:
                _nomes_do_json(item, saida)
    elif isinstance(valor, list):
        for item in valor:
            _nomes_do_json(item, saida)


def nomes_conhecidos(conn: sqlite3.Connection) -> list[str]:
    """Clientes, partes contrárias, todos os nomes de `partes_json` (inclusive
    advogados) e as partes das publicações do DJEN, sem vazios nem repetidos, do maior
    para o menor."""
    brutos: list[str] = []
    for r in conn.execute("SELECT cliente, parte_contraria, partes_json FROM processo"):
        brutos.extend(n for n in (r["cliente"], r["parte_contraria"]) if n)
        try:
            _nomes_do_json(json.loads(r["partes_json"] or "[]"), brutos)
        except (ValueError, RecursionError):
            continue
    # Partes que o DJEN pôs nas publicações (processos sem dados do PJe).
    for r in conn.execute("SELECT DISTINCT partes_json FROM publicacao WHERE partes_json != '[]'"):
        try:
            _nomes_do_json(json.loads(r["partes_json"] or "[]"), brutos)
        except (ValueError, RecursionError):
            continue
    vistos: dict[str, str] = {}
    for nome in brutos:
        limpo = " ".join(nome.split())
        if limpo and limpo.casefold() not in vistos:
            vistos[limpo.casefold()] = limpo
    return sorted(vistos.values(), key=lambda n: (-len(n), n))


_ESPACO_HTML = re.compile(r"&nbsp;|&#160;|&#xa0;", re.IGNORECASE)


def _chave(texto: str) -> str:
    return " ".join(_ESPACO_HTML.sub(" ", texto).split()).casefold()


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto)
                   if not unicodedata.combining(c))


@functools.lru_cache(maxsize=8)
def _padrao(nomes: tuple[str, ...], chave: bytes | None = None):
    """Uma expressão (em forma de árvore de prefixos, para ficar rápida com centenas
    de nomes) e o mapa forma-encontrada → nome fictício."""
    trocas: dict[str, str] = {}
    for nome in nomes:
        if sum(c.isalnum() for c in nome) < 2:
            continue
        ficticio = nome_ficticio(nome, chave)
        for base in (nome, _sem_acento(nome)):
            for forma in (base, str(escape(base))):
                trocas.setdefault(_chave(forma), ficticio)
    if not trocas:
        return None, trocas
    arvore: dict = {}
    for forma in trocas:
        no = arvore
        for parte in forma.split(" "):
            no = no.setdefault(parte, {})
        no[""] = {}
    return re.compile(r"(?<!\w)" + _regex_da_arvore(arvore) + r"(?!\w)", re.IGNORECASE), trocas


_ESPACOS = r"(?:\s|&nbsp;|&#160;|&#xa0;)+"


def _regex_da_arvore(no: dict) -> str:
    """Alternativas a partir de um nó da árvore (cada nó é uma palavra inteira,
    separada da seguinte por espaço flexível). Quando um nome termina num nó que
    também continua ("MARIA SOUZA" e "MARIA SOUZA LIMA"), o resto é opcional e
    guloso: o nome mais longo ganha."""
    ramos = []
    for palavra, filho in sorted(((p, f) for p, f in no.items() if p),
                                 key=lambda kv: (-len(kv[0]), kv[0])):
        if not any(filho):
            ramos.append(re.escape(palavra))
        elif "" in filho:
            ramos.append(re.escape(palavra) + f"(?:{_ESPACOS}" + _regex_da_arvore(filho) + ")?")
        else:
            ramos.append(re.escape(palavra) + _ESPACOS + _regex_da_arvore(filho))
    return "(?:" + "|".join(ramos) + ")"


# Trechos técnicos que não são texto da página: scripts, metadados (o token CSRF) e
# campos escondidos de formulário. Ali nada é trocado (o número do processo já chega
# como apelido pelo painel).
_PROTEGIDOS = re.compile(
    r"(<script\b[^>]*>.*?</script\s*>|<meta\b[^>]*>"
    r"|<input\b(?=[^>]*\btype\s*=\s*[\"']?hidden\b)[^>]*>)",
    re.IGNORECASE | re.DOTALL)


def mascarar_html(html: str, nomes: list[str], chave: bytes | None = None,
                  chave_nomes: bytes | None = None) -> str:
    """Troca cada nome conhecido (também na forma escapada pelo Jinja, sem acento e sem
    diferenciar maiúsculas) pelo fictício e mascara números CNJ e de OAB — fora de
    scripts, metadados e campos escondidos. `chave` é a dos números CNJ; `chave_nomes`,
    a dos nomes fictícios."""
    padrao, trocas = _padrao(tuple(nomes), chave_nomes)

    def mascarar(trecho: str) -> str:
        if padrao is not None:
            trecho = padrao.sub(lambda m: trocas.get(_chave(m.group(0)), m.group(0)), trecho)
        return mascarar_oab(mascarar_cnj(trecho, chave))

    partes = _PROTEGIDOS.split(html)
    return "".join(p if i % 2 else mascarar(p) for i, p in enumerate(partes))
