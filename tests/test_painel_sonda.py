import pytest

import painel
from captura.mni_client import CredencialInvalidaError, ServicoIndisponivelError


class _Cliente:
    def __init__(self, efeito):
        self.efeito = efeito

    def consultar_avisos_pendentes(self):
        if isinstance(self.efeito, Exception):
            raise self.efeito
        return self.efeito

    def consultar_processo(self, *args, **kwargs):
        raise AssertionError("a sonda de login deve usar avisos pendentes")


@pytest.mark.parametrize("efeito,esperado", [
    ([], True),
    (CredencialInvalidaError("recusado"), False),
    (ServicoIndisponivelError("fora do ar"), True),
])
def test_sonda_de_login_usa_avisos_pendentes(monkeypatch, efeito, esperado):
    monkeypatch.setattr(painel, "obter_cliente", lambda inst, cpf, senha: _Cliente(efeito))
    assert painel._credencial_valida_no_pje("12345678901", "s") is esperado
