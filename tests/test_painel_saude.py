import painel
from nucleo import versao

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_saude_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.setattr("nucleo.trabalhador.BATIDA", str(tmp_path / "batida"))
    resposta = painel.app.test_client().get("/saude")
    assert resposta.status_code == 200
    assert resposta.get_json() == {"ok": True, "versao": versao.atual(),
                                   "trabalhador": "desconhecido",
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
