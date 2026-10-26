"""Cofre do sistema para os segredos da instalação local (senha do PJe, chave de
sessão): Gerenciador de Credenciais no Windows, Chaves no macOS, via `keyring`.

Nada aqui loga, mostra ou devolve em mensagem de erro o VALOR de um segredo; só o
nome. O `keyring` é importado na hora do uso: a produção (VPS) não o instala e
nunca chama este módulo."""

from __future__ import annotations

import logging

SERVICO = "br.com.despertaia.controladoria"

log = logging.getLogger(__name__)


class CofreError(RuntimeError):
    """O cofre do sistema não aceitou guardar ou apagar um segredo."""


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise CofreError("O cofre do sistema não está disponível neste computador "
                         "(falta o componente keyring). Reinstale a Controladoria.") from exc
    return keyring


def ler(nome: str) -> str | None:
    """Valor guardado, ou None se não houver (ou se o cofre não responder)."""
    try:
        valor = _keyring().get_password(SERVICO, nome)
    except Exception as exc:  # noqa: BLE001 — backend nulo, cofre trancado, keyring ausente
        log.warning("Cofre: não foi possível ler %s (%s).", nome, type(exc).__name__)
        return None
    return valor or None


def gravar(nome: str, valor: str) -> None:
    """Guarda o segredo e confere lendo de volta (o backend nulo aceita e descarta).
    Valor vazio apaga."""
    if not valor:
        apagar(nome)
        return
    keyring = _keyring()
    try:
        keyring.set_password(SERVICO, nome, valor)
        conferido = keyring.get_password(SERVICO, nome)
    except Exception as exc:  # noqa: BLE001 — qualquer falha do backend vira erro claro
        raise CofreError(f"O cofre do sistema recusou guardar “{nome}” "
                         f"({type(exc).__name__}).") from exc
    if conferido != valor:
        raise CofreError(f"O cofre do sistema não guardou “{nome}”: nenhum cofre "
                         "utilizável foi encontrado neste computador.")


def apagar(nome: str) -> None:
    """Remove o segredo; não ter nada guardado não é erro."""
    keyring = _keyring()
    try:
        if keyring.get_password(SERVICO, nome) is None:
            return
        keyring.delete_password(SERVICO, nome)
    except Exception as exc:  # noqa: BLE001
        raise CofreError(f"O cofre do sistema recusou apagar “{nome}” "
                         f"({type(exc).__name__}).") from exc
