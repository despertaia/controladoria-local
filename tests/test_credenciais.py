from captura import credenciais


def test_senha_da_instancia_usa_senha_propria_do_2grau():
    cred = {"cpf": "12345678901", "senha": "s1", "senha_2grau": "s2"}
    assert credenciais.senha_da_instancia(cred, "2grau") == "s2"
    assert credenciais.senha_da_instancia(cred, "1grau") == "s1"


def test_senha_da_instancia_cai_na_principal_sem_senha_do_2grau():
    cred = {"cpf": "12345678901", "senha": "s1"}
    assert credenciais.senha_da_instancia(cred, "2grau") == "s1"


def test_credencial_do_env_le_as_duas_senhas(monkeypatch):
    monkeypatch.setenv("TJMT_CPF", "12345678901")
    monkeypatch.setenv("TJMT_SENHA", "s1")
    monkeypatch.setenv("TJMT_SENHA_2GRAU", "s2")
    assert credenciais.credencial_do_env() == {
        "cpf": "12345678901", "senha": "s1", "senha_2grau": "s2"}


def test_credencial_do_env_sem_senha_do_2grau(monkeypatch):
    monkeypatch.setenv("TJMT_CPF", "12345678901")
    monkeypatch.setenv("TJMT_SENHA", "s1")
    monkeypatch.delenv("TJMT_SENHA_2GRAU", raising=False)
    assert credenciais.credencial_do_env() == {"cpf": "12345678901", "senha": "s1"}


def test_credencial_do_env_em_modo_sessao_devolve_none(monkeypatch):
    monkeypatch.setenv("TJMT_SENHA", "")
    assert credenciais.credencial_do_env() is None
