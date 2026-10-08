"""Tela "Acesso do Lex" no modo local: colar o código do `claude setup-token`, guardar
no cofre sem nunca reexibir, testar o acesso e apagar."""

import pytest

import painel
import painel_local
from nucleo import banco, cofre
from ponte_mac import executor
from ponte_mac import tela_acesso
from tests.cofre_falso import cofre_falso  # noqa: F401

CODIGO = "sk-ant-oat01-" + "Ab3_-x" * 15


@pytest.fixture
def c(tmp_path, monkeypatch, cofre_falso):  # noqa: F811
    caminho = str(tmp_path / "c.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setenv("CONTROLADORIA_CASA_BANCA", str(tmp_path))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_configurado", True)  # passa da barreira
    monkeypatch.setattr(executor, "localizar_claude", lambda env: "/usr/local/bin/claude")
    cli = painel.app.test_client()
    cli.cofre = cofre_falso
    return cli


def _guardado(c):
    return c.cofre.itens.get((cofre.SERVICO, "lex_claude"))


def _pagina(c):
    r = c.get("/configuracoes/lex")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_fora_do_modo_local_nao_ha_tela(c, monkeypatch):
    monkeypatch.delenv("CONTROLADORIA_LOCAL")
    html = _pagina(c)
    assert "Acesso do Lex" not in html and "Conexão com o Mac" in html
    assert "Gerar chave" in html
    for rota in ("/configuracoes/lex/acesso", "/configuracoes/lex/acesso/testar",
                 "/configuracoes/lex/acesso/apagar"):
        assert c.post(rota, data={"codigo": CODIGO}).status_code == 404
    assert _guardado(c) is None


def test_tela_mostra_situacao_e_instrucao(c):
    html = _pagina(c)
    assert "Ligar o Lex ao seu plano do Claude" in html
    assert "Claude Code instalado" in html
    assert "Falta conectar o Lex" in html and "Conectar o Lex" in html
    assert "Pasta da Banca encontrada" in html
    assert "claude setup-token" in html and "Outra forma" in html  # o caminho antigo, recolhido
    assert "Gerar chave" not in html and "Chave da ponte" not in html  # automática no local
    assert "Lex neste computador" in html
    assert html.index("acesso-lex") < html.index("titulo-automatico")  # conexão no topo


def test_tela_sem_claude_instalado(c, monkeypatch):
    monkeypatch.setattr(executor, "localizar_claude", lambda env: None)
    assert "instalamos ao conectar" in _pagina(c)


def test_salvar_guarda_no_cofre_e_nunca_reexibe(c):
    r = c.post("/configuracoes/lex/acesso", data={"codigo": CODIGO})
    assert r.status_code == 302 and r.headers["Location"].endswith("/configuracoes/lex#acesso-lex")
    assert CODIGO not in r.get_data(as_text=True)
    assert _guardado(c) == CODIGO
    html = _pagina(c)
    assert "Acesso do Lex guardado" in html and "Acesso ao plano guardado" in html
    assert CODIGO not in html and CODIGO[:20] not in html
    assert "Trocar acesso" in html


def test_salvar_junta_a_linha_quebrada_pelo_terminal(c):
    quebrado = f"  {CODIGO[:30]}\r\n{CODIGO[30:]}\n"
    c.post("/configuracoes/lex/acesso", data={"codigo": quebrado})
    assert _guardado(c) == CODIGO


@pytest.mark.parametrize("ruim", ["", "sk-ant-api03-" + "x" * 40, "sk-ant-oat01-curto",
                                  "sk-ant-oat01-" + "x" * 30 + "!", "minha senha"])
def test_salvar_recusa_formato_sem_ecoar(c, ruim):
    r = c.post("/configuracoes/lex/acesso", data={"codigo": ruim})
    assert r.status_code == 302 and _guardado(c) is None
    html = _pagina(c)
    assert "começa com sk-ant-oat" in html
    if ruim:
        assert ruim not in html


def test_salvar_cofre_recusou(c, monkeypatch):
    monkeypatch.setattr(c.cofre, "set_password", lambda *a: None)  # aceita e descarta
    c.post("/configuracoes/lex/acesso", data={"codigo": CODIGO})
    html = _pagina(c)
    assert "recusou guardar" in html and CODIGO not in html


def test_salvar_exige_csrf(c):
    painel.app.config["CSRF_EXIGIDO"] = True
    r = c.post("/configuracoes/lex/acesso", data={"codigo": CODIGO})
    assert r.status_code in (302, 400) and _guardado(c) is None
    c.get("/configuracoes/lex")
    with c.session_transaction() as sessao:
        token = sessao["csrf"]
    c.post("/configuracoes/lex/acesso", data={"codigo": CODIGO, "csrf": token})
    assert _guardado(c) == CODIGO


def test_modo_apresentacao_bloqueia(c):
    with c.session_transaction() as sessao:
        sessao["apresentacao"] = True
    c.post("/configuracoes/lex/acesso", data={"codigo": CODIGO})
    assert _guardado(c) is None


@pytest.mark.parametrize("resultado,frase", [
    (True, "Acesso conferido"), (False, "O Claude recusou o acesso"),
    (None, "Não deu para conferir agora")])
def test_testar_usa_a_sonda_da_ponte(c, monkeypatch, tmp_path, resultado, frase):
    chamadas = []

    def validar(token, casa, tempo=300):
        chamadas.append((token, casa, tempo))
        return resultado

    monkeypatch.setattr(executor, "validar_acesso", validar)
    c.post("/configuracoes/lex/acesso", data={"codigo": CODIGO})
    r = c.post("/configuracoes/lex/acesso/testar")
    assert r.status_code == 302
    assert chamadas == [(CODIGO, tmp_path, tela_acesso.TEMPO_TESTE_S)]
    html = _pagina(c)
    assert frase in html and CODIGO not in html


def test_testar_sem_acesso_guardado_usa_o_login(c, monkeypatch):
    chamadas = []
    monkeypatch.setattr(executor, "validar_acesso",
                        lambda token, casa, tempo=300: chamadas.append(token) or True)
    c.post("/configuracoes/lex/acesso/testar")
    assert chamadas == [None]


def test_testar_sem_claude(c, monkeypatch):
    monkeypatch.setattr(executor, "localizar_claude", lambda env: None)
    monkeypatch.setattr(executor, "validar_acesso", lambda *a, **k: pytest.fail("não testa"))
    c.post("/configuracoes/lex/acesso/testar")
    assert "Claude Code não foi encontrado" in _pagina(c)


def test_apagar(c):
    c.post("/configuracoes/lex/acesso", data={"codigo": CODIGO})
    c.post("/configuracoes/lex/acesso/apagar")
    assert _guardado(c) is None
    assert "Acesso apagado" in _pagina(c)


def test_codigo_valido():
    assert tela_acesso.codigo_valido(f" {CODIGO}\n") == CODIGO
    assert tela_acesso.codigo_valido("sk-ant-oat01 " + "x" * 30) == "sk-ant-oat01" + "x" * 30
    assert tela_acesso.codigo_valido("sk-ant-oat01-" + "é" * 30) is None
