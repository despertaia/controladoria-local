"""Configurações do Lex: chave automática, estado do Mac e a chave da ponte."""

import logging
from datetime import datetime, timedelta

import pytest

import painel
from nucleo import banco, config, eventos, ponte


@pytest.fixture
def c(tmp_path, monkeypatch):
    caminho = str(tmp_path / "c.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    cli = painel.app.test_client()
    cli.caminho = caminho
    return cli


def _conn(c):
    return banco.conectar(c.caminho)


def _gravar(c, chave, valor):
    conn = _conn(c)
    with conn:
        config.gravar(conn, chave, valor)
    conn.close()


def _antes(minutos):
    return (datetime.now(eventos.FUSO) - timedelta(minutes=minutos)).isoformat(timespec="seconds")


def _pagina(c):
    r = c.get("/configuracoes/lex")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_pagina_sem_chave_e_sem_contato(c):
    html = _pagina(c)
    assert "Nunca conectado" in html
    assert "Nenhuma chave gerada" in html
    assert "Gerar chave" in html
    assert "Revogar" not in html


def test_pagina_mostra_estado_do_lex_automatico(c):
    assert 'aria-checked="false"' in _pagina(c)
    conn = _conn(c)
    with conn:
        config.definir_lex_automatico(conn, True)
    conn.close()
    html = _pagina(c)
    assert 'aria-checked="true"' in html
    assert 'name="voltar" value="/configuracoes/lex"' in html


def test_alternar_lex_automatico_volta_para_a_pagina(c):
    r = c.post("/lex/automatico", data={"ligado": "1", "voltar": "/configuracoes/lex"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/configuracoes/lex")
    assert 'aria-checked="true"' in _pagina(c)


def test_conectado_ha_poucos_minutos(c):
    _gravar(c, ponte.ULTIMO_CONTATO, _antes(3))
    html = _pagina(c)
    assert "Conectado há 3 min" in html


def test_sem_contato_desde_quando_passou_do_limite(c):
    _gravar(c, ponte.ULTIMO_CONTATO, "2026-10-05T14:30:00-04:00")
    html = _pagina(c)
    assert "Sem contato desde 05/10 14:30" in html
    assert "Conectado" not in html


def test_gerar_chave_mostra_uma_vez_e_so_no_post(c):
    r = c.post("/configuracoes/lex/chave")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    corpo = r.get_data(as_text=True)
    conn = _conn(c)
    try:
        achadas = [t for t in corpo.replace('"', " ").split() if ponte.chave_valida(conn, t)]
    finally:
        conn.close()
    assert len(achadas) == 1
    chave = achadas[0]
    assert "readonly" in corpo and "Copiar" in corpo
    assert "python -m ponte_mac guardar-chave" in corpo
    assert chave not in _pagina(c)
    assert chave not in c.get("/").get_data(as_text=True)
    html = _pagina(c)
    assert "Chave gerada em" in html and "Revogar" in html and "Gerar nova chave" in html


def test_gerar_chave_nova_invalida_a_antiga(c):
    corpo1 = c.post("/configuracoes/lex/chave").get_data(as_text=True)
    corpo2 = c.post("/configuracoes/lex/chave").get_data(as_text=True)
    conn = _conn(c)
    try:
        def vale(corpo):
            return [t for t in corpo.replace('"', " ").split() if ponte.chave_valida(conn, t)]
        assert vale(corpo1) == [] and len(vale(corpo2)) == 1
    finally:
        conn.close()


def test_chave_nao_vai_para_log_nem_flash_nem_evento(c, caplog):
    with caplog.at_level(logging.DEBUG):
        corpo = c.post("/configuracoes/lex/chave").get_data(as_text=True)
    conn = _conn(c)
    try:
        chave = next(t for t in corpo.replace('"', " ").split() if ponte.chave_valida(conn, t))
        eventos_texto = str([tuple(r) for r in conn.execute("SELECT * FROM evento")])
    finally:
        conn.close()
    assert chave not in caplog.text and chave not in eventos_texto
    with c.session_transaction() as s:
        assert chave not in str(dict(s)) and not s.get("_flashes")


def test_revogar_invalida_a_chave(c):
    corpo = c.post("/configuracoes/lex/chave").get_data(as_text=True)
    conn = _conn(c)
    chave = next(t for t in corpo.replace('"', " ").split() if ponte.chave_valida(conn, t))
    conn.close()
    r = c.post("/configuracoes/lex/revogar")
    assert r.status_code == 302 and r.headers["Location"].endswith("/configuracoes/lex")
    conn = _conn(c)
    try:
        assert not ponte.chave_valida(conn, chave)
    finally:
        conn.close()
    assert c.post("/ponte/proximo", headers={"Authorization": f"Bearer {chave}"}).status_code == 401
    html = _pagina(c)
    assert "Nenhuma chave gerada" in html and "Chave revogada." in html


def test_modo_apresentacao_bloqueia_as_chaves_e_esconde_botoes(c):
    c.post("/apresentacao")
    for caminho in ("/configuracoes/lex/chave", "/configuracoes/lex/revogar"):
        r = c.post(caminho)
        assert r.status_code == 403
        assert "Indisponível no modo apresentação." in r.get_data(as_text=True)
    conn = _conn(c)
    try:
        assert config.ler(conn, ponte.CHAVE_HASH) == ""
    finally:
        conn.close()
    html = _pagina(c)
    assert "Gerar chave" not in html and "Revogar" not in html
    assert "modo apresentação" in html.lower()


def test_link_discreto_no_topo(c):
    assert 'href="/configuracoes/lex"' in c.get("/carteira").get_data(as_text=True)


def test_posts_das_chaves_exigem_csrf(c):
    painel.app.config["CSRF_EXIGIDO"] = True
    for caminho in ("/configuracoes/lex/chave", "/configuracoes/lex/revogar"):
        assert c.post(caminho).status_code == 302
    conn = _conn(c)
    try:
        assert config.ler(conn, ponte.CHAVE_HASH) == ""
    finally:
        conn.close()
    c.get("/configuracoes/lex")
    with c.session_transaction() as s:
        token = s["csrf"]
    r = c.post("/configuracoes/lex/chave", data={"csrf": token})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"


# --- onda final -------------------------------------------------------------------

def test_conectado_agora(c):
    _gravar(c, ponte.ULTIMO_CONTATO, _antes(0))
    html = _pagina(c)
    assert "Conectado agora" in html and "Conectado há 0 min" not in html


def test_mac_sem_acesso_ao_plano(c):
    _gravar(c, ponte.ULTIMO_CONTATO, _antes(1))
    _gravar(c, ponte.CLAUDE_OK, "0")
    _gravar(c, ponte.CLAUDE_OK_EM, _antes(1))
    assert "Mac conectado, sem acesso ao plano do Lex" in _pagina(c)
    _gravar(c, ponte.CLAUDE_OK_EM, _antes(30))  # aviso velho não vale
    assert "sem acesso ao plano" not in _pagina(c)


def test_tentativas_com_chave_invalida(c):
    assert "chave inválida" not in _pagina(c)
    hora = datetime.now(eventos.FUSO).replace(microsecond=0)
    _gravar(c, ponte.CHAVE_INVALIDA_N, "4")
    _gravar(c, ponte.CHAVE_INVALIDA_EM, hora.isoformat())
    assert f"Tentativas com chave inválida: 4, última às {hora.strftime('%H:%M')}" in _pagina(c)


def test_pagina_da_chave_avisa_e_traz_o_comando_completo(c):
    corpo = c.post("/configuracoes/lex/chave").get_data(as_text=True)
    assert "Não recarregue esta página: recarregar gera outra chave" in corpo
    assert ".venv/bin/python -m ponte_mac guardar-chave" in corpo
    assert "cd ~/Projetos/controladoria &amp;&amp;" in corpo
