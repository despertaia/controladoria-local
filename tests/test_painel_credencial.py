import pytest

import painel


@pytest.fixture
def chamadas(monkeypatch):
    """Substitui a criação de clientes MNI e registra (instância, cpf, senha)."""
    registro = []

    def falso_obter_cliente(instancia, cpf, senha):
        registro.append((instancia, cpf, senha))
        return object()

    monkeypatch.setattr(painel, "obter_cliente", falso_obter_cliente)
    monkeypatch.setenv("TJMT_CPF", "12345678901")
    monkeypatch.setenv("TJMT_SENHA", "senha1")
    monkeypatch.setenv("TJMT_SENHA_2GRAU", "senha2")
    return registro


def test_cliente_pje_usa_a_senha_de_cada_instancia(chamadas):
    with painel.app.test_request_context("/"):
        painel.cliente_pje("2grau")
        painel.cliente_pje("1grau")
    assert chamadas == [("2grau", "12345678901", "senha2"),
                        ("1grau", "12345678901", "senha1")]


def test_download_em_lote_prepara_cada_instancia_com_sua_senha(chamadas, monkeypatch):
    monkeypatch.setattr(painel, "baixar_processo_completo", lambda *a, **k: iter(()))
    cred = painel.credenciais.credencial_do_env()
    saida = "".join(painel._stream_download(["10088882920238110041"], cred))
    assert ("1grau", "12345678901", "senha1") in chamadas
    assert ("2grau", "12345678901", "senha2") in chamadas
    assert "Concluído" in saida
