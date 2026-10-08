"""Regra de arquivamento pelos códigos nacionais de movimento (TPU/CNJ).

246 = Arquivado Definitivamente (confirmado no PJe/TJMT); 22 = Baixa Definitiva.
893 = Desarquivamento; 849 = Reativação. Juntadas depois do arquivamento (ex.:
certidão) não reabrem o processo; só reativação/desarquivamento ou uma
publicação nova no DJEN mais de DIAS_PARA_REABRIR dias depois do arquivamento
(publicações logo após arquivar costumam ser o próprio arquivamento ou custas).
"""

from __future__ import annotations

from datetime import date

CODIGOS_ARQUIVAMENTO = frozenset({246, 22})
CODIGOS_REATIVACAO = frozenset({893, 849})
DIAS_PARA_REABRIR = 30


def _so_digitos(valor: str | None) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def data_arquivamento(andamentos: list[dict]) -> str | None:
    """AAAAMMDD do último arquivamento não seguido de reativação; None se ativo."""
    ultimo_arquivamento = ultima_reativacao = ""
    for andamento in andamentos:
        codigo = andamento.get("codigo")
        quando = str(andamento.get("data_ordenavel") or "")
        if codigo in CODIGOS_ARQUIVAMENTO:
            ultimo_arquivamento = max(ultimo_arquivamento, quando)
        elif codigo in CODIGOS_REATIVACAO:
            ultima_reativacao = max(ultima_reativacao, quando)
    if ultimo_arquivamento and ultimo_arquivamento > ultima_reativacao:
        return ultimo_arquivamento[:8]
    return None


def _data(aaaammdd: str) -> date | None:
    try:
        return date(int(aaaammdd[0:4]), int(aaaammdd[4:6]), int(aaaammdd[6:8]))
    except ValueError:
        return None


def esta_arquivado(data_arq: str | None, ultima_publicacao: str | None) -> bool:
    """Arquivado, salvo se houve publicação mais de DIAS_PARA_REABRIR dias
    depois do arquivamento."""
    if not data_arq:
        return False
    arquivamento = _data(_so_digitos(data_arq)[:8])
    publicacao = _data(_so_digitos(ultima_publicacao)[:8])
    if arquivamento is None or publicacao is None:
        return True
    return (publicacao - arquivamento).days <= DIAS_PARA_REABRIR
