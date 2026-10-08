"""Registro imutável do que acontece com cada processo (só acrescenta).

O quadro, o cronômetro e os Resultados das próximas fases derivam daqui.
`registrar` não faz commit: quem chama agrupa numa transação (`with conn:`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from nucleo import tribunal


def __getattr__(nome: str):
    # `eventos.FUSO` é o fuso do tribunal atual (Cuiabá no TJMT), lido a cada uso:
    # o lançador local pode trocar o tribunal no ambiente depois do import.
    if nome == "FUSO":
        return tribunal.fuso()
    raise AttributeError(f"module {__name__!r} has no attribute {nome!r}")


def agora() -> str:
    """Momento atual no fuso do tribunal (Cuiabá no TJMT), ISO 8601 com deslocamento
    (ex.: 2026-10-05T06:00:00-04:00), para não depender do fuso do servidor."""
    return datetime.now(tribunal.fuso()).isoformat(timespec="seconds")


def registrar(conn: sqlite3.Connection, tipo: str, numero: str | None = None,
              dados: dict | None = None, quando: str | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO evento (quando, tipo, numero, dados) VALUES (?, ?, ?, ?)",
        (quando or agora(), tipo, numero, json.dumps(dados or {}, ensure_ascii=False)))
    return cursor.lastrowid


def listar(conn: sqlite3.Connection, numero: str | None = None) -> list[dict]:
    sql = "SELECT id, quando, tipo, numero, dados FROM evento"
    argumentos: tuple = ()
    if numero is not None:
        sql += " WHERE numero = ?"
        argumentos = (numero,)
    sql += " ORDER BY id"
    return [{"id": r["id"], "quando": r["quando"], "tipo": r["tipo"],
             "numero": r["numero"], "dados": json.loads(r["dados"])}
            for r in conn.execute(sql, argumentos)]
