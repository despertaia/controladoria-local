"""
Guarda credenciais do PJe (CPF + senha) APENAS em memória do processo, por
sessão, com expiração (TTL). Nunca grava em disco nem no cookie.

Modelo "caminho 2": o usuário informa a senha do PJe quando vai usar; o servidor
a mantém só enquanto necessário e a descarta. Como o gunicorn roda com 1 worker
(processo único), um dicionário em memória é compartilhado entre as threads de
requisição e o worker de segundo plano. Reinício do serviço apaga tudo — o
usuário simplesmente reconecta.
"""

from __future__ import annotations

import secrets
import threading
import time

from nucleo import tribunal

# Tempo de vida da credencial em memória (renovado a cada uso). Padrão 12h.
TTL_SEGUNDOS = 12 * 60 * 60

_lock = threading.Lock()
_creds: dict[str, dict] = {}  # token -> {"cpf", "senha", "expira_em"}


def novo_token() -> str:
    return secrets.token_urlsafe(24)


def guardar(token: str, cpf: str, senha: str) -> None:
    with _lock:
        _creds[token] = {
            "cpf": cpf,
            "senha": senha,
            "expira_em": time.monotonic() + TTL_SEGUNDOS,
        }


def obter(token: str | None) -> dict | None:
    """Retorna {'cpf','senha'} se houver credencial válida; renova o TTL.
    Retorna None se ausente ou expirada (e remove a expirada)."""
    if not token:
        return None
    with _lock:
        c = _creds.get(token)
        if c is None:
            return None
        if time.monotonic() > c["expira_em"]:
            _creds.pop(token, None)
            return None
        c["expira_em"] = time.monotonic() + TTL_SEGUNDOS  # renova no uso
        return {"cpf": c["cpf"], "senha": c["senha"]}


def limpar(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _creds.pop(token, None)


def credencial_do_env(sigla: str | None = None) -> dict | None:
    """Credencial gravada no ambiente (modo armazenado) do tribunal `sigla` (padrão,
    o principal): PJE_CPF e PJE_SENHA_<SIGLA>/PJE_SENHA_2GRAU_<SIGLA>; a do principal
    cai em PJE_SENHA/PJE_SENHA_2GRAU e nas TJMT_* da produção. Retorna None se
    não houver senha — aí a senha vem da sessão (caminho 2)."""
    senha = tribunal.senha(sigla)
    if not senha:
        return None
    cred = {"cpf": tribunal.cpf(), "senha": senha}
    senha_2grau = tribunal.senha_2grau(sigla)
    if senha_2grau:
        cred["senha_2grau"] = senha_2grau
    return cred


def senha_da_instancia(cred: dict, instancia: str) -> str:
    """Senha a usar na instância. No TJMT o 1º e o 2º grau têm logins
    separados: usa 'senha_<instancia>' se existir, senão a senha principal."""
    return cred.get(f"senha_{instancia}") or cred["senha"]
