"""A agenda de prazos em .ics (1.6.0). O calendário assinado vive fora do painel, então o
título é feito para não vazar nada: número do processo e nome do ato tirado da lista fechada.
O `ato` escrito à mão ou pelo Lex não entra, porque pode trazer nome de parte. Cliente, parte,
trecho, motivo, alerta, ressalvas e órgão nunca entram; a consulta pede só as colunas que o
evento usa. O número entra só se for feito de dígitos. Este módulo só lê: não grava nada ao montar a agenda."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

from captura.processo_parser import formatar_numero_cnj
from nucleo import config, eventos, prazos_banco, prazos_legais

CHAVE_HASH = "agenda_token_hash"
CHAVE_CRIADO = "agenda_token_criado_em"
LEMBRETE = "-PT15H"  # 9h do dia anterior (o evento de dia inteiro começa à meia-noite)
_OCTETOS = 75
_TAMANHO_MAXIMO_DO_TOKEN = 128  # o gerado tem 43; o que passa disso nem chega ao hash
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_ULTIMO_DIA = date(2100, 12, 31)  # bom senso: data fora de 2000..2100 é lixo, não prazo
# Tamanho do arquivo, não prazo de lei: o prazo conferido cuja data fatal passou há mais do que
# isto sai da agenda, para ela não crescer sem fim (5000 prazos davam 6 MB). O que ainda espera
# conferência fica, por mais velho que seja.
JANELA_DA_AGENDA_DIAS = 365
_BASE_DA_VERSAO = datetime(2026, 1, 1, tzinfo=timezone.utc)
_CONTROLE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

_DESCRICAO_A_CONFERIR = ("Prazo ainda não conferido no painel. A data pode estar errada: "
                         "confira no painel antes de agir.")
_DESCRICAO_CONFERIDO = "Confira sempre no painel antes de agir."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def gerar_token(conn: sqlite3.Connection) -> str:
    """Gera um token novo (o anterior deixa de valer) e o devolve uma única vez. Só o hash fica
    no banco, e o registro de eventos não leva o token."""
    token = secrets.token_urlsafe(32)
    config.gravar(conn, CHAVE_HASH, _hash(token))
    config.gravar(conn, CHAVE_CRIADO, eventos.agora())
    eventos.registrar(conn, "agenda_token_gerado", None, {})
    return token


def revogar_token(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM config WHERE chave IN (?, ?)", (CHAVE_HASH, CHAVE_CRIADO))
    eventos.registrar(conn, "agenda_token_revogado", None, {})


def tem_token(conn: sqlite3.Connection) -> bool:
    return bool(config.ler(conn, CHAVE_HASH))


def criado_em(conn: sqlite3.Connection) -> str:
    """Quando o endereço atual foi gerado (ISO, ou vazio)."""
    return config.ler(conn, CHAVE_CRIADO)


def token_valido(conn: sqlite3.Connection, enviado) -> bool:
    """Comparação em tempo constante, sobre o hash."""
    gravado = config.ler(conn, CHAVE_HASH)
    if (not gravado or not isinstance(enviado, str) or not enviado
            or len(enviado) > _TAMANHO_MAXIMO_DO_TOKEN):
        return False
    try:
        calculado = _hash(enviado).encode("ascii")
    except UnicodeEncodeError:  # caractere que não tem forma em UTF-8
        return False
    return hmac.compare_digest(calculado, gravado.encode("ascii", "replace"))


def _escapar(texto: str) -> str:
    """Valor de propriedade TEXT (RFC 5545, seção 3.3.11): barra, ponto e vírgula, vírgula e
    quebra de linha; os outros caracteres de controle saem."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = _CONTROLE.sub("", texto)
    return (texto.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\n", "\\n"))


def _dobrar(linha: str) -> list[str]:
    """Quebra a linha em até 75 octetos, sem partir um caractere; a continuação começa com um
    espaço (RFC 5545, seção 3.1)."""
    pedacos, atual, tamanho = [], "", 0
    for c in linha:
        n = len(c.encode("utf-8"))
        if tamanho + n > _OCTETOS:
            pedacos.append(atual)
            atual, tamanho = " ", 1
        atual += c
        tamanho += n
    return [*pedacos, atual]


def _dia(texto) -> date | None:
    """Uma data guardada como AAAA-MM-DD; o que não se lê (ou é absurdo) é None."""
    if not isinstance(texto, str) or not _ISO.fullmatch(texto):
        return None
    try:
        dia = date.fromisoformat(texto)
    except ValueError:
        return None
    return dia if prazos_banco.DATA_MINIMA <= dia <= _ULTIMO_DIA else None


def _momento(texto) -> datetime | None:
    """Um valor guardado que não se lê (ou que estoura ao ir para UTC) é None, nunca erro."""
    try:
        m = datetime.fromisoformat(str(texto))
        return m.replace(tzinfo=timezone.utc) if m.tzinfo is None else m.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _numero_no_titulo(numero) -> str:
    """O número do processo só entra se for só dígitos (formatado, se tiver 20): um texto que
    não é número pode ser um nome, e a agenda não leva nome."""
    numero = str(numero or "")
    return formatar_numero_cnj(numero) if numero.isascii() and numero.isdigit() else ""


def _ato_seguro(codigo_ato: str) -> str:
    legal = prazos_legais.PRAZOS.get(codigo_ato or "")
    return legal.ato if legal else "Prazo"


def _evento(p, tipo: str, dia: date, fatal: date, carimbo: str) -> list[str]:
    partes = [_ato_seguro(p["codigo_ato"]), _numero_no_titulo(p["numero"]), f"fatal {fatal:%d/%m}"]
    titulo = " · ".join(x for x in partes if x)
    if tipo == "trabalho":
        titulo = f"Trabalhar: {titulo}"
    conferido = p["estado"] == "conferido"
    if not conferido:
        titulo = f"[A CONFERIR] {titulo}"
    mudou = _momento(p["atualizado_em"])
    try:
        versao = [] if mudou is None else [
            f"SEQUENCE:{max(0, int((mudou - _BASE_DA_VERSAO).total_seconds()))}",
            f"LAST-MODIFIED:{mudou:%Y%m%dT%H%M%SZ}"]
    except (ValueError, OverflowError):
        versao = []
    return ["BEGIN:VEVENT", f"UID:prazo-{p['id']}-{tipo}@controladoria", f"DTSTAMP:{carimbo}",
            *versao,
            f"DTSTART;VALUE=DATE:{dia:%Y%m%d}",
            f"DTEND;VALUE=DATE:{dia + timedelta(days=1):%Y%m%d}",  # o fim é exclusivo
            f"SUMMARY:{_escapar(titulo)}",
            f"DESCRIPTION:{_escapar(_DESCRICAO_CONFERIDO if conferido else _DESCRICAO_A_CONFERIR)}",
            f"STATUS:{'CONFIRMED' if conferido else 'TENTATIVE'}", "TRANSP:TRANSPARENT",
            "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escapar(titulo)}",
            f"TRIGGER:{LEMBRETE}", "END:VALARM", "END:VEVENT"]


def montar_ics(conn: sqlite3.Connection, agora: datetime | None = None) -> str:
    """Um evento na data fatal e outro na de trabalho, se for outro dia. O que não foi
    conferido entra com «[A CONFERIR]»; cumprido, descartado e prazo sem data ficam de fora. A
    agenda acompanha o painel: o prazo com data entra qualquer que seja a marca do processo
    («Não é meu» inclusive). O conferido com fatal de mais de `JANELA_DA_AGENDA_DIAS` atrás
    também sai. Linha com data que não se lê sai sozinha: uma linha ruim nunca derruba a agenda
    inteira. Só datas, sem hora nem fuso."""
    momento = agora or datetime.now(timezone.utc)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    momento = momento.astimezone(timezone.utc)
    carimbo = momento.strftime("%Y%m%dT%H%M%SZ")
    mais_antigo = momento.date() - timedelta(days=JANELA_DA_AGENDA_DIAS)
    linhas = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Lex Lab//Controladoria//PT",
              "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Prazos",
              "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for p in conn.execute(
            "SELECT id, numero, estado, codigo_ato, fatal, trabalho, atualizado_em FROM prazo "
            "WHERE estado IN ('a_conferir', 'conferido') AND fatal IS NOT NULL "
            "ORDER BY fatal, id").fetchall():
        fatal = _dia(p["fatal"])
        if fatal is None or (p["estado"] == "conferido" and fatal < mais_antigo):
            continue
        trabalho = _dia(p["trabalho"])
        linhas += _evento(p, "fatal", fatal, fatal, carimbo)
        # o trabalho nunca passa da fatal; no mesmo dia, o evento é um só
        if trabalho is not None and trabalho < fatal:
            linhas += _evento(p, "trabalho", trabalho, fatal, carimbo)
    linhas.append("END:VCALENDAR")
    return "".join(f"{pedaco}\r\n" for linha in linhas for pedaco in _dobrar(linha))
