"""Fila de tarefas demoradas (varredura e download de autos), no banco.

O site e o agendador só pedem; o trabalhador (trabalhador.py) executa uma por
vez. Nada aqui faz commit: quem chama agrupa numa transação (`with conn:`)."""

from __future__ import annotations

import sqlite3

from nucleo import eventos

TIPOS = ("varredura", "autos")


def ativa(conn: sqlite3.Connection, tipo: str, ref: str = "") -> bool:
    return conn.execute(
        "SELECT 1 FROM tarefa WHERE tipo = ? AND ref = ? AND estado IN ('na_fila', 'rodando') "
        "LIMIT 1", (tipo, ref)).fetchone() is not None


def pedir(conn: sqlite3.Connection, tipo: str, ref: str = "",
          por: str = "advogado") -> int | None:
    """Põe a tarefa na fila. None se já há uma igual na fila ou rodando."""
    if tipo not in TIPOS:
        raise ValueError(f"tipo de tarefa desconhecido: {tipo!r}")
    if ativa(conn, tipo, ref):
        return None
    return conn.execute(
        "INSERT INTO tarefa (tipo, ref, estado, pedida_em, pedida_por) "
        "VALUES (?, ?, 'na_fila', ?, ?)", (tipo, ref, eventos.agora(), por)).lastrowid


def pegar_proxima(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """A tarefa mais antiga da fila, já marcada como rodando."""
    linha = conn.execute(
        "SELECT id FROM tarefa WHERE estado = 'na_fila' ORDER BY id LIMIT 1").fetchone()
    if linha is None:
        return None
    cursor = conn.execute(
        "UPDATE tarefa SET estado = 'rodando', iniciada_em = ? "
        "WHERE id = ? AND estado = 'na_fila'", (eventos.agora(), linha["id"]))
    if not cursor.rowcount:
        return None
    return conn.execute("SELECT * FROM tarefa WHERE id = ?", (linha["id"],)).fetchone()


def concluir(conn: sqlite3.Connection, tarefa_id: int, erro: str = "") -> None:
    conn.execute("UPDATE tarefa SET estado = ?, terminada_em = ?, erro = ? WHERE id = ?",
                 ("falhou" if erro else "ok", eventos.agora(), erro, tarefa_id))


def cancelar(conn: sqlite3.Connection, tipo: str, ref: str) -> bool:
    """Cancela a tarefa que ainda está na fila (a que já começou segue)."""
    return conn.execute(
        "UPDATE tarefa SET estado = 'cancelada', terminada_em = ? "
        "WHERE tipo = ? AND ref = ? AND estado = 'na_fila'",
        (eventos.agora(), tipo, ref)).rowcount > 0


def reabrir_interrompidas(conn: sqlite3.Connection) -> int:
    """Ao iniciar o trabalhador: o que ficou "rodando" (reinício no meio) volta à fila."""
    return conn.execute("UPDATE tarefa SET estado = 'na_fila', iniciada_em = NULL "
                        "WHERE estado = 'rodando'").rowcount
