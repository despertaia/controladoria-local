"""Telas com o tribunal do escritório = TJMG (CONTROLADORIA_TRIBUNAL): textos com
a sigla certa, PJe só para processos do TJMG e nada de 2º grau."""

import pytest

import painel
from nucleo import banco, painel_dados
from scripts import banco_demo

NOVO_TJMG = "50012348020248130024"
NOVO_TJMT = "00000013920248110041"


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    caminho = tmp_path / "demo.db"
    conn = banco.conectar(str(caminho))
    banco_demo.gerar(conn, painel_dados.hoje_cuiaba())
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(caminho))
    monkeypatch.setenv("CARTEIRA_ADVOGADO_NOME", "MARIA EXEMPLO DA SILVA")
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "TJMG")
    return painel.app.test_client()


def test_numero_do_tjmg_vai_ao_pje(cliente):
    html = cliente.get(f"/buscar?q={NOVO_TJMG}").get_data(as_text=True)
    assert "Consultar no PJe" in html and "Adicionar à carteira" not in html


def test_numero_do_tjmt_fica_fora_e_a_tela_fala_tjmg(cliente):
    html = cliente.get(f"/buscar?q={NOVO_TJMT}").get_data(as_text=True)
    assert "Este processo não é do TJMG; os movimentos chegam pelo DJEN." in html
    assert "Consultar no PJe" not in html


def test_processo_do_tjmt_sem_botoes_do_pje(cliente):
    html = cliente.get(f"/processo/{banco_demo._tjmt(1)}").get_data(as_text=True)
    assert "Atualizar do PJe" not in html and "Baixar do" not in html


def test_processo_do_tjmg_oferece_so_a_1a_instancia(cliente, monkeypatch):
    monkeypatch.setattr(painel.cache, "ler", lambda n: {"numero": n, "documentos": [],
                                                        "andamentos": [], "partes": []})
    html = cliente.get(f"/processo/{NOVO_TJMG}").get_data(as_text=True)
    assert "Baixar do TJMG" in html and "Baixar do TJMT" not in html
    assert "na 1ª instância" in html and "2ª instância" not in html


def test_atualizar_processo_de_fora_avisa_com_a_sigla(cliente):
    resposta = cliente.post(f"/atualizar/{NOVO_TJMT}", follow_redirects=True)
    assert "Este processo não é do TJMG" in resposta.get_data(as_text=True)


def test_baixar_lote_sem_2o_grau(cliente):
    html = cliente.get("/baixar-lote").get_data(as_text=True)
    assert "2ª instância" not in html and "/2grau/" not in html


def test_filtro_de_publicacoes_comeca_pelo_tjmg(cliente):
    assert painel._tribunais_busca()[0] == "TJMG"
    assert "TJMT" in painel._tribunais_busca()


def test_no_tjmt_continua_igual(cliente, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "TJMT")
    assert painel._tribunais_busca() == painel._TRIBUNAIS_BUSCA
    html = cliente.get("/baixar-lote").get_data(as_text=True)
    assert "2ª instância" in html
