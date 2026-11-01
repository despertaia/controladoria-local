"""Arquivar, desarquivar, excluir e restaurar da lixeira (1.5.0): o estado do processo
(carteira), os cartões do quadro e a busca de autos na fila, numa chamada só, para um
processo ou um lote. Nada aqui faz commit: quem chama agrupa numa transação
(`with conn:`). Nada é apagado; os autos e a pasta do caso não são tocados."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from nucleo import carteira, quadro

ACOES = ("arquivar", "desarquivar", "excluir", "restaurar")
LIMITE_LOTE = 200
MENSAGEM_LEX = "Espere o Lex terminar ou desista do pedido."
# Ação → (função da carteira, motivo dos cartões, tira do quadro?)
_PASSOS = {
    "arquivar": (carteira.arquivar, quadro.MOTIVO_ARQUIVADO, True),
    "excluir": (carteira.excluir, quadro.MOTIVO_EXCLUIDO, True),
    "desarquivar": (carteira.desarquivar, quadro.MOTIVO_ARQUIVADO, False),
    "restaurar": (carteira.restaurar_da_lixeira, quadro.MOTIVO_EXCLUIDO, False),
}
_FEITO = {
    "arquivar": ("Arquivado. Ele fica em «Arquivados» e volta sozinho se sair publicação nova.",
                 "{n} processos arquivados. Eles ficam em «Arquivados»."),
    "desarquivar": ("Desarquivado: o processo voltou para a carteira.",
                    "{n} processos desarquivados."),
    "excluir": ("Foi para a lixeira. Dá para restaurar por 30 dias.",
                "{n} processos foram para a lixeira. Dá para restaurar por 30 dias."),
    "restaurar": ("Restaurado: o processo voltou para onde estava.",
                  "{n} processos restaurados."),
}
_NADA = {
    "arquivar": "Nada mudou: este processo já estava arquivado ou na lixeira.",
    "desarquivar": "Nada mudou: este processo não estava arquivado.",
    "excluir": "Nada mudou: este processo já estava na lixeira.",
    "restaurar": "Nada mudou: este processo não estava na lixeira.",
}


@dataclass
class Resultado:
    feitos: list[str] = field(default_factory=list)
    sem_mudanca: list[str] = field(default_factory=list)
    com_lex: list[str] = field(default_factory=list)
    cartoes: int = 0


class LoteGrande(ValueError):
    """Mais de LIMITE_LOTE processos de uma vez: nada é feito."""


def aplicar(conn: sqlite3.Connection, acao: str, numeros: list[str]) -> Resultado:
    """Aplica a ação a cada número (repetidos contam uma vez). Arquivar e excluir pulam o
    processo com cartão em "Lex minutando" (vai para `com_lex`); tirar do quadro cancela a
    busca de autos que ainda está na fila."""
    if acao not in ACOES:
        raise ValueError(f"ação desconhecida: {acao!r}")
    numeros = list(dict.fromkeys(numeros))
    if len(numeros) > LIMITE_LOTE:
        raise LoteGrande(f"Escolha até {LIMITE_LOTE} processos de uma vez.")
    funcao, motivo, tira = _PASSOS[acao]
    r = Resultado()
    for numero in numeros:
        if tira and quadro.lex_trabalhando(conn, numero):
            r.com_lex.append(numero)
            continue
        if not funcao(conn, numero):
            r.sem_mudanca.append(numero)
            continue
        r.feitos.append(numero)
        if tira:
            r.cartoes += len(quadro.tirar_do_quadro(conn, numero, motivo=motivo))
        else:
            r.cartoes += len(quadro.devolver_ao_quadro(conn, numero, motivo=motivo))
    return r


def deu_certo(r: Resultado) -> bool:
    """Falhou só quando nada foi feito porque o Lex trabalha no processo."""
    return bool(r.feitos) or not r.com_lex


def mensagem(acao: str, r: Resultado) -> str:
    """Frase curta para a tela: o que foi feito, os cartões e quem ficou de fora."""
    if not r.feitos and not r.sem_mudanca and len(r.com_lex) == 1:
        return f"O Lex está trabalhando neste processo. {MENSAGEM_LEX}"
    partes = []
    n = len(r.feitos)
    if n:
        um, varios = _FEITO[acao]
        partes.append(um if n == 1 else varios.format(n=n))
    if r.cartoes:
        if acao in ("arquivar", "excluir"):
            partes.append("O cartão saiu do quadro." if r.cartoes == 1
                          else "Os cartões saíram do quadro.")
        else:
            partes.append("O cartão voltou ao quadro." if r.cartoes == 1
                          else "Os cartões voltaram ao quadro.")
    if r.com_lex:
        k = len(r.com_lex)
        partes.append((f"1 processo ficou de fora: o Lex está trabalhando nele. {MENSAGEM_LEX}"
                       if k == 1 else
                       f"{k} processos ficaram de fora: o Lex está trabalhando neles. "
                       f"{MENSAGEM_LEX}"))
    if not partes:
        partes.append(_NADA[acao] if len(r.sem_mudanca) <= 1 else "Nada mudou.")
    return " ".join(partes)
