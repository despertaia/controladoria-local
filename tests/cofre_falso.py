"""Cofre em memória para os testes (nunca toca nas Chaves/Gerenciador de Credenciais)."""

import pytest

keyring = pytest.importorskip("keyring")
from keyring.backend import KeyringBackend  # noqa: E402


class CofreEmMemoria(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.itens: dict[tuple[str, str], str] = {}

    def get_password(self, servico, nome):
        return self.itens.get((servico, nome))

    def set_password(self, servico, nome, valor):
        self.itens[(servico, nome)] = valor

    def delete_password(self, servico, nome):
        self.itens.pop((servico, nome))


@pytest.fixture
def cofre_falso():
    anterior = keyring.get_keyring()
    falso = CofreEmMemoria()
    keyring.set_keyring(falso)
    yield falso
    keyring.set_keyring(anterior)
