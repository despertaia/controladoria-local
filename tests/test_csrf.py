import pathlib
import re

import pytest

import painel

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    painel.app.config["CSRF_EXIGIDO"] = True
    return painel.app.test_client()


def _token(cliente):
    cliente.get("/carteira")
    with cliente.session_transaction() as sessao:
        return sessao["csrf"]


def _na_carteira(numero):
    from nucleo import banco
    conn = banco.conectar()
    try:
        return conn.execute("SELECT 1 FROM processo WHERE numero = ?", (numero,)).fetchone()
    finally:
        conn.close()


MENSAGEM_400 = "Sessão expirada ou formulário inválido. Recarregue a página."


def test_post_sem_token_e_recusado(cliente):
    r = cliente.post("/carteira/adicionar", data={"numero": "1" * 20})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert _na_carteira("1" * 20) is None
    assert MENSAGEM_400 in cliente.get("/").get_data(as_text=True)


def test_post_com_token_no_campo_passa(cliente):
    r = cliente.post("/carteira/adicionar", data={"numero": "1" * 20, "csrf": _token(cliente)})
    assert r.status_code == 302


def test_post_com_token_no_cabecalho_passa(cliente):
    r = cliente.post("/carteira/adicionar", data={"numero": "1" * 20},
                     headers={"X-CSRF": _token(cliente)})
    assert r.status_code == 302


def test_token_errado_e_recusado(cliente):
    _token(cliente)
    r = cliente.post("/carteira/adicionar", data={"numero": "1" * 20, "csrf": "outro"})
    assert r.status_code == 302 and _na_carteira("1" * 20) is None


def test_token_com_acento_e_recusado_sem_erro_interno(cliente):
    _token(cliente)
    r = cliente.post("/carteira/adicionar", data={"numero": "1" * 20, "csrf": "é"})
    assert r.status_code == 302 and _na_carteira("1" * 20) is None
    r = cliente.post("/varrer", headers={"X-CSRF": "é", "Accept": "application/json"})
    assert r.status_code == 400


def test_400_volta_para_a_pagina_de_origem_do_mesmo_site(cliente):
    _token(cliente)
    r = cliente.post("/carteira/adicionar", data={"numero": "1" * 20},
                     headers={"Referer": "http://localhost/carteira?aba=conferir"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/carteira?aba=conferir")
    r = cliente.post("/carteira/adicionar", data={"numero": "1" * 20},
                     headers={"Referer": "https://outro.example/carteira"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert "outro.example" not in r.headers["Location"]


def test_400_em_json_para_o_quadro(cliente):
    r = cliente.post("/demanda/1/mover", data={"para": "acao"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 400
    assert r.get_json() == {"ok": False, "erro": MENSAGEM_400}


def test_login_com_acento_nao_da_erro_interno(cliente, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    monkeypatch.setattr(painel, "PAINEL_USUARIO", "daniel")
    cliente.get("/login")
    with cliente.session_transaction() as sessao:
        token = sessao["csrf"]
    r = cliente.post("/login", data={"usuario": "josé", "senha": "senhá", "csrf": token})
    assert r.status_code == 200 and "Usuário ou senha incorretos." in r.get_data(as_text=True)
    r = cliente.post("/login", data={"usuario": "daniel", "senha": "segredo", "csrf": token})
    assert r.status_code == 302


def test_pagina_tem_meta_com_o_token(cliente):
    html = cliente.get("/carteira").get_data(as_text=True)
    assert re.search(r'<meta name="csrf" content="[^"]{20,}">', html)


@pytest.mark.parametrize("caminho", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_todo_formulario_post_tem_o_campo(caminho):
    texto = caminho.read_text(encoding="utf-8")
    for form in re.findall(r"<form[^>]*method=\"post\"[^>]*>(.*?)</form>", texto, re.S | re.I):
        assert 'name="csrf"' in form, f"formulário POST sem csrf em {caminho.name}"
    for achado in re.finditer(r"fetch\(", texto):
        trecho = texto[achado.start():achado.start() + 600]
        if re.search(r"method:\s*['\"]POST['\"]", trecho):
            assert "X-CSRF" in trecho, f"fetch POST sem X-CSRF em {caminho.name}"
