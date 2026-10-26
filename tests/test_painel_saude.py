import painel
from nucleo import versao

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_saude_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    monkeypatch.setattr("nucleo.trabalhador.BATIDA", str(tmp_path / "batida"))
    resposta = painel.app.test_client().get("/saude")
    assert resposta.status_code == 200
    assert resposta.get_json() == {"ok": True, "versao": versao.atual(),
                                   "trabalhador": "desconhecido", "cofre": "sem_chave",
                                   "horas_desde_varredura": None}


def test_saude_com_banco_inacessivel(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path))  # é uma pasta, não um arquivo
    resposta = painel.app.test_client().get("/saude")
    assert resposta.status_code == 503
    assert resposta.get_json()["ok"] is False


def test_saude_e_publica_e_exportar_exige_login(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    cliente = painel.app.test_client()
    assert cliente.get("/saude").status_code == 200
    resposta = cliente.get("/carteira/exportar")
    assert resposta.status_code == 302 and "/login" in resposta.headers["Location"]


def test_exportar_entrega_a_planilha(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    resposta = painel.app.test_client().get("/carteira/exportar")
    assert resposta.status_code == 200 and resposta.mimetype == XLSX
    assert 'filename="Carteira.xlsx"' in resposta.headers["Content-Disposition"]


def test_saude_informa_horas_desde_a_ultima_varredura(tmp_path, monkeypatch):
    from nucleo import banco, eventos
    caminho = str(tmp_path / "b.db")
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr("nucleo.trabalhador.BATIDA", str(tmp_path / "batida"))
    conn = banco.conectar(caminho)
    with conn:
        eventos.registrar(conn, "varredura_concluida", None, {})
    conn.close()
    dados = painel.app.test_client().get("/saude").get_json()
    assert dados["ok"] is True
    assert dados["horas_desde_varredura"] is not None and dados["horas_desde_varredura"] < 0.1


def test_saude_cofre_sem_chave_nao_derruba_o_painel(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    resposta = painel.app.test_client().get("/saude")
    assert resposta.status_code == 200
    assert resposta.get_json()["ok"] is True
    assert resposta.get_json()["cofre"] == "sem_chave"


def test_saude_cofre_ok_com_a_chave(tmp_path, monkeypatch):
    from nucleo import cofre_servidor
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "cofre.key"))
    cofre_servidor.criar_chave()
    assert painel.app.test_client().get("/saude").get_json()["cofre"] == "ok"


def test_saude_cofre_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    dados = painel.app.test_client().get("/saude").get_json()
    assert dados["ok"] is True and dados["cofre"] == "local"


def test_painel_carrega_o_cofre_fora_do_modo_local(tmp_path, monkeypatch):
    from nucleo import banco, cofre_servidor, credenciais
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "cofre.key"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.setattr(credenciais, "_definidas", set())
    monkeypatch.delenv("PJE_CPF", raising=False)
    monkeypatch.delenv("PJE_SENHA_TJMT", raising=False)
    cofre_servidor.criar_chave()
    conn = banco.conectar()
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMT", "1grau", "abc")
    conn.close()
    painel._carregar_credenciais_do_cofre()
    import os
    assert os.environ["PJE_CPF"] == "11111111111"
    assert os.environ["PJE_SENHA_TJMT"] == "abc"


def test_painel_nao_carrega_o_cofre_no_modo_local(tmp_path, monkeypatch):
    from nucleo import credenciais
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    chamadas = []
    monkeypatch.setattr(credenciais, "carregar_no_ambiente", lambda c: chamadas.append(c))
    painel._carregar_credenciais_do_cofre()
    assert chamadas == []


def test_painel_falha_ao_carregar_o_cofre_nao_impede_o_painel(tmp_path, monkeypatch):
    from nucleo import banco, cofre_servidor, credenciais
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "cofre.key"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    cofre_servidor.criar_chave()

    def quebra(conn):
        raise RuntimeError("banco travado")
    monkeypatch.setattr(credenciais, "carregar_no_ambiente", quebra)
    painel._carregar_credenciais_do_cofre()  # só registra no log
