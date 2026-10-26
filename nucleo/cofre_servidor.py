"""Cofre das senhas dos tribunais na VPS: Fernet com a chave num arquivo root-only
fora do banco e do repositório (o backup diário do banco não leva senha legível).
Nada aqui loga ou devolve em erro o texto cifrado/decifrado."""

from __future__ import annotations

import os
from pathlib import Path

CAMINHO_PADRAO = "/etc/controladoria/cofre.key"


class CofreServidorError(RuntimeError):
    """Chave ausente, ilegível ou que não abre o que foi cifrado."""


def caminho_da_chave() -> Path:
    return Path(os.getenv("CONTROLADORIA_CHAVE_COFRE", "").strip() or CAMINHO_PADRAO)


def pronto() -> bool:
    try:
        return caminho_da_chave().is_file()
    except OSError:
        return False


def criar_chave(caminho: Path | None = None) -> Path:
    from cryptography.fernet import Fernet
    caminho = Path(caminho or caminho_da_chave())
    if caminho.exists():
        return caminho
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(Fernet.generate_key())
    return caminho


def _fernet():
    from cryptography.fernet import Fernet
    try:
        return Fernet(caminho_da_chave().read_bytes().strip())
    except (OSError, ValueError):
        raise CofreServidorError(
            f"A chave do cofre não está disponível ({caminho_da_chave()}).") from None


def cifrar(texto: str) -> str:
    return _fernet().encrypt(texto.encode("utf-8")).decode("ascii")


def decifrar(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise CofreServidorError("A chave do cofre não abre esta credencial.") from None
