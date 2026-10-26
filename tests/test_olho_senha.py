"""Botão "olho" (mostrar/ocultar senha): static/senha.js é incluído por todas as telas com
campo de senha, que continuam com type="password" e o mesmo name (sem JS, nada muda)."""

import pathlib
import re

import pytest

import painel
import painel_local
from nucleo import banco
from tests.cofre_falso import cofre_falso  # noqa: F401

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = re.compile(r'<script src="/static/senha\.js"></script>')
CAMPO = re.compile(r'<input type="password"[^>]*>')


@pytest.fixture
def c(tmp_path, monkeypatch, cofre_falso):  # noqa: F811
    caminho = str(tmp_path / "c.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setenv("CONTROLADORIA_CASA_BANCA", str(tmp_path))
    for var in ("PJE_SENHA", "TJMT_SENHA", "CONTROLADORIA_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_configurado", True)
    return painel.app.test_client()


def _nomes(html):
    return re.findall(r'name="([^"]+)"', " ".join(CAMPO.findall(html)))


def _confere(html, nomes):
    assert SCRIPT.search(html), "página sem static/senha.js"
    assert _nomes(html) == nomes
    for campo in CAMPO.findall(html):  # nenhum campo foi trocado por text no HTML
        assert 'type="password"' in campo
    assert "campo-senha" not in html and "olho-senha" not in html  # criados só pelo JS


def test_script_e_servido_e_cria_o_botao_acessivel(c):
    r = c.get("/static/senha.js")
    assert r.status_code == 200
    js = r.get_data(as_text=True)
    for trecho in ("campo-senha", "olho-senha", 'type = \'button\'', "aria-pressed",
                   "Mostrar senha", "Ocultar senha", "'submit'", "setSelectionRange"):
        assert trecho in js
    css = c.get("/static/style.css").get_data(as_text=True)
    assert ".olho-senha" in css and "width: 36px" in css and ":focus-visible" in css


def test_login(c, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "x")
    _confere(c.get("/login").get_data(as_text=True), ["senha"])


def test_conectar_pje(c):
    r = c.get("/conectar-pje")
    assert r.status_code == 200
    _confere(r.get_data(as_text=True), ["senha"])


def test_configuracoes_tribunais(c):
    r = c.get("/configuracoes/tribunais")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert SCRIPT.search(html) and _nomes(html) and set(_nomes(html)) == {"senha"}
    assert 'autocomplete="new-password"' in html


def test_configurar_modo_local(c, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    r = c.get("/configurar")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    nomes = _nomes(html)
    assert SCRIPT.search(html) and nomes
    assert all(n.startswith(("senha_", "senha_2grau_")) for n in nomes)
    assert any(n.startswith("senha_2grau_") for n in nomes)


def test_acesso_lex_local(c, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    r = c.get("/configuracoes/lex")
    assert r.status_code == 200
    _confere(r.get_data(as_text=True), ["codigo"])


def test_toda_senha_dos_templates_vem_de_pagina_que_estende_a_base():
    """O script mora no base.html: um template com campo de senha precisa estendê-lo
    (ou ser incluído por quem estende)."""
    for t in (RAIZ / "templates").glob("*.html"):
        texto = t.read_text(encoding="utf-8")
        if 'type="password"' in texto and not t.name.startswith("_"):
            assert '{% extends "base.html" %}' in texto, t.name
    assert "static', filename='senha.js'" in (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
