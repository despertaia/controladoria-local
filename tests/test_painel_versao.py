"""Versão no painel: rodapé, /novidades, faixa de versão nova (só no modo local, fora
do modo apresentação) e /saude com a versão. Sem rede: a versão publicada vem do cache
preenchido no teste."""

import pytest

import painel
import painel_local
from nucleo import banco, versao


@pytest.fixture
def c(tmp_path, monkeypatch):
    caminho = str(tmp_path / "c.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_configurado", True)  # modo local já configurado
    return painel.app.test_client()


def _maior() -> str:
    return f"{versao._tupla(versao.atual())[0] + 1}.0.0"


def _publicada(v: str) -> None:
    versao.versao_publicada(lambda url, timeout: v)


def _html(c, caminho="/"):
    r = c.get(caminho)
    assert r.status_code == 200, caminho
    return r.get_data(as_text=True)


def test_rodape_mostra_a_versao_e_o_link_das_novidades(c):
    html = _html(c, "/carteira")
    assert f"versão {versao.atual()}" in html
    assert 'href="/novidades">Novidades</a>' in html


def test_pagina_de_novidades(c):
    html = _html(c, "/novidades")
    assert "O que mudou na Controladoria" in html
    for secao in versao.novidades():
        assert f"Versão {secao['versao']}" in html
        for item in secao["itens"]:
            assert item in html
    assert "Leia a íntegra da publicação dentro do painel." in html
    assert html.index("Versão 1.2.0") < html.index("Versão 1.1.0") < html.index("Versão 1.0.0")
    assert "a sua</span>" in html  # marca a versão instalada


def test_faixa_de_versao_nova_no_modo_local(c, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    _publicada(_maior())
    html = _html(c, "/carteira")
    assert 'class="faixa-versao"' in html
    assert f"Nova versão {_maior()} disponível." in html
    assert "Peça ao Claude: «Atualize a Controladoria do Lex Lab»." in html
    assert 'href="/novidades">ver novidades</a>' in html


def test_faixa_nao_aparece_na_producao_nem_sem_versao_maior(c, monkeypatch):
    _publicada(_maior())
    assert 'class="faixa-versao"' not in _html(c, "/carteira")  # produção (VPS)
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    versao.limpar_cache()
    _publicada(versao.atual())
    assert 'class="faixa-versao"' not in _html(c, "/carteira")
    versao.limpar_cache()  # GitHub sem resposta
    assert 'class="faixa-versao"' not in _html(c, "/carteira")


def test_faixa_nao_aparece_no_modo_apresentacao(c, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    _publicada(_maior())
    with c.session_transaction() as s:
        s["apresentacao"] = True
    assert 'class="faixa-versao"' not in _html(c, "/carteira")


def test_request_dispara_a_verificacao_em_segundo_plano_sem_ir_a_rede(c, monkeypatch):
    chamadas = []
    monkeypatch.setattr(versao, "verificar_em_segundo_plano", lambda: chamadas.append(1))

    def proibido(url, timeout):
        raise AssertionError("o request não pode consultar o GitHub")
    monkeypatch.setattr(versao, "_obter_padrao", proibido)
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    _html(c, "/carteira")
    assert chamadas


def test_saude_inclui_a_versao(c):
    r = c.get("/saude")
    assert r.status_code == 200
    assert r.get_json()["versao"] == versao.atual() != ""
