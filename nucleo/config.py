"""Configuração do painel (tabela `config`: chave → valor em texto) e a chave do
Lex automático (Fase 3).

Nada aqui faz commit: quem chama agrupa numa transação (`with conn:`)."""

from __future__ import annotations

import sqlite3

from nucleo import eventos

LEX_AUTOMATICO = "lex_automatico"


def ler(conn: sqlite3.Connection, chave: str, padrao: str = "") -> str:
    r = conn.execute("SELECT valor FROM config WHERE chave = ?", (chave,)).fetchone()
    return padrao if r is None else r["valor"]


def gravar(conn: sqlite3.Connection, chave: str, valor: str) -> None:
    conn.execute("INSERT INTO config (chave, valor) VALUES (?, ?) "
                 "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor", (chave, valor))


def lex_automatico(conn: sqlite3.Connection) -> bool:
    return ler(conn, LEX_AUTOMATICO, "0") == "1"


def definir_lex_automatico(conn: sqlite3.Connection, ligado: bool,
                           por: str = "advogado") -> int:
    """Liga/desliga o envio automático ao Lex. Ao ligar, enfileira os cartões que já
    estão em "Autos baixados" (menos os que esperam orientação) e devolve quantos; ao desligar, devolve 0 (o que já
    está no Lex continua lá)."""
    from nucleo import quadro  # import tardio: quadro também lê esta chave
    gravar(conn, LEX_AUTOMATICO, "1" if ligado else "0")
    enfileirados = 0
    if ligado:
        # "sem_tipo" espera a orientação do advogado: não vai sozinho para o Lex.
        for r in conn.execute("SELECT id FROM demanda WHERE coluna = 'autos' "
                              "AND lex_estado != 'sem_tipo' ORDER BY id").fetchall():
            quadro.mover(conn, r["id"], "lex", "sistema")
            enfileirados += 1
    eventos.registrar(conn, "lex_automatico_ligado" if ligado else "lex_automatico_desligado",
                      None, {"por": por, "enfileirados": enfileirados})
    return enfileirados
