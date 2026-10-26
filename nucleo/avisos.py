"""Intimações pendentes do PJe. Vêm de consultarAvisosPendentes, que só lista:
nada aqui abre o teor (consultarTeorComunicacao) nem registra ciência.

Nada aqui faz commit: quem chama agrupa numa transação (`with conn:`)."""

from __future__ import annotations

import sqlite3
from datetime import date

from nucleo import eventos

_TIPOS = {"INT": "Intimação", "CIT": "Citação", "NOT": "Notificação", "VIS": "Vista"}


def rotulo_do_tipo(tipo: str | None) -> str:
    tipo = (tipo or "").strip()
    return _TIPOS.get(tipo.upper(), tipo) or "Comunicação"


def _digitos(valor) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def _data_iso(valor) -> str:
    s = _digitos(valor)[:8]
    try:
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8])).isoformat()
    except ValueError:
        return ""


def ler_aviso(obj, instancia: str) -> dict | None:
    """Converte um aviso do MNI; None se faltar o id ou o número do processo."""
    id_aviso = str(getattr(obj, "idAviso", "") or "").strip()
    processo = getattr(obj, "processo", None)
    numero = _digitos(getattr(processo, "numero", ""))
    if not id_aviso or len(numero) != 20:
        return None
    orgao = getattr(getattr(processo, "orgaoJulgador", None), "nomeOrgao", "") or ""
    return {"id": id_aviso, "instancia": instancia, "numero": numero,
            "tipo_comunicacao": str(getattr(obj, "tipoComunicacao", "") or ""),
            "data_disponibilizacao": _data_iso(getattr(obj, "dataDisponibilizacao", "")),
            "orgao": str(orgao)}


def sincronizar(conn: sqlite3.Connection, instancia: str, avisos: list[dict], *,
                encerrar: bool = True) -> list[dict]:
    """Grava a lista COMPLETA de uma consulta bem-sucedida de uma instância e
    devolve os avisos novos. Os que estavam pendentes e sumiram da lista viram
    encerrados (ciência dada no PJe ou tácita) — só com encerrar=True: se a
    lista veio com intimação ilegível, ela não é completa e nada se encerra."""
    agora = eventos.agora()
    novos: list[dict] = []
    vistos: set[str] = set()
    for a in avisos:
        vistos.add(a["id"])
        cursor = conn.execute(
            "INSERT OR IGNORE INTO aviso (instancia, id, numero, tipo_comunicacao, "
            "data_disponibilizacao, orgao, visto_em, pendente) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (instancia, a["id"], a["numero"], a["tipo_comunicacao"],
             a["data_disponibilizacao"], a["orgao"], agora))
        if cursor.rowcount:
            novos.append(a)
            eventos.registrar(conn, "aviso_detectado", a["numero"],
                              {"id": a["id"], "instancia": instancia,
                               "tipo": a["tipo_comunicacao"]})
        else:
            conn.execute("UPDATE aviso SET pendente = 1 WHERE instancia = ? AND id = ?",
                         (instancia, a["id"]))
    if not encerrar:
        return novos
    for linha in conn.execute("SELECT id, numero FROM aviso WHERE instancia = ? AND pendente = 1",
                              (instancia,)).fetchall():
        if linha["id"] not in vistos:
            conn.execute("UPDATE aviso SET pendente = 0 WHERE instancia = ? AND id = ?",
                         (instancia, linha["id"]))
            eventos.registrar(conn, "aviso_encerrado", linha["numero"],
                              {"id": linha["id"], "instancia": instancia})
    return novos
