"""Configurações › Tribunais: CPF, senha de cada instância, teste de login, selos e a
faixa de aviso no Cockpit. O PJe nunca é chamado: `conexao.testar` é sempre simulado."""

import pytest

import painel
from tests.cofre_falso import cofre_falso  # noqa: F401
from nucleo import banco, conexao, credenciais, cofre_servidor

CSRF = "token-de-teste"
CPF = "12345678901"
SENHA = "S3nhaUnica"


@pytest.fixture
def conn_painel(tmp_path, monkeypatch):
    """Banco do painel e a chave do cofre num arquivo temporário (modo VPS)."""
    caminho = str(tmp_path / "t.db")
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "cofre.key"))
    cofre_servidor.criar_chave()
    conn = banco.conectar(caminho)
    yield conn
    conn.close()
    credenciais._definidas.clear()


@pytest.fixture
def cliente_logado(conn_painel, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    cliente = painel.app.test_client()
    with cliente.session_transaction() as sessao:
        sessao["logado"] = True
    return cliente


def _guardar_cpf(cliente, cpf="111.111.111-11"):
    return cliente.post("/configuracoes/tribunais/cpf", data={"cpf": cpf, "csrf": CSRF},
                        follow_redirects=True)


def _salvar(cliente, senha=SENHA, sigla="TJMT", instancia="1grau", **extra):
    dados = {"sigla": sigla, "instancia": instancia, "senha": senha, "csrf": CSRF, **extra}
    return cliente.post("/configuracoes/tribunais/salvar", data=dados, follow_redirects=True)


def _simular(monkeypatch, estado, msg="x"):
    monkeypatch.setattr("nucleo.conexao.testar", lambda s, i, c, p: (estado, msg))


def _html(r):
    return r.get_data(as_text=True)


def test_salvar_so_com_login_aceito(cliente_logado, monkeypatch, conn_painel):
    monkeypatch.setattr("nucleo.conexao.testar", lambda s, i, c, p: ("recusada", "x"))
    cliente_logado.post("/configuracoes/tribunais/cpf", data={"cpf": "111.111.111-11", "csrf": CSRF})
    r = cliente_logado.post("/configuracoes/tribunais/salvar",
                            data={"sigla": "TJMT", "instancia": "1grau", "senha": "errada",
                                  "csrf": CSRF},
                            follow_redirects=True)
    assert "Nada foi guardado" in r.get_data(as_text=True)
    assert credenciais.ler(conn_painel, "TJMT", "1grau") is None
    monkeypatch.setattr("nucleo.conexao.testar", lambda s, i, c, p: ("ok", "Login aceito pelo PJe."))
    r = cliente_logado.post("/configuracoes/tribunais/salvar",
                            data={"sigla": "TJMT", "instancia": "1grau", "senha": "certa",
                                  "csrf": CSRF},
                            follow_redirects=True)
    assert "Senha do TJMT (1ª instância) guardada. Login aceito pelo PJe." in _html(r)
    assert credenciais.ler(conn_painel, "TJMT", "1grau")["senha"] == "certa"
    estado = conn_painel.execute("SELECT estado FROM conexao_tribunal WHERE sigla = 'TJMT' "
                                 "AND instancia = '1grau'").fetchone()
    assert estado["estado"] == "ok"


def test_tela_nunca_mostra_senha_nem_cpf_inteiro(cliente_logado, monkeypatch, conn_painel):
    _simular(monkeypatch, "ok", "Login aceito pelo PJe.")
    _guardar_cpf(cliente_logado, CPF)
    _salvar(cliente_logado)
    r = cliente_logado.get("/configuracoes/tribunais")
    html = _html(r)
    assert r.status_code == 200
    assert SENHA not in html and CPF not in html
    assert "final 01" in html
    assert "Conectado" in html and "conferido às" in html
    assert 'type="password"' in html and 'autocomplete="new-password"' in html
    assert "Trocar senha" in html and "Testar agora" in html and "Remover" in html
    # Nenhum flash nem a sessão guardam o segredo.
    with cliente_logado.session_transaction() as sessao:
        assert SENHA not in str(dict(sessao)) and CPF not in str(dict(sessao))


def test_fora_do_ar_pede_confirmacao_e_guarda_mesmo_assim(cliente_logado, monkeypatch, conn_painel):
    _simular(monkeypatch, "fora", "tempo esgotado")
    _guardar_cpf(cliente_logado)
    r = _salvar(cliente_logado)
    assert "O PJe do TJMT não respondeu agora" in _html(r)
    assert "Guardar mesmo assim" in _html(r)
    assert credenciais.ler(conn_painel, "TJMT", "1grau") is None
    r = _salvar(cliente_logado, mesmo_assim="1")
    assert "Senha guardada sem teste: o PJe do TJMT não respondeu. A próxima varredura confere." in _html(r)
    assert credenciais.ler(conn_painel, "TJMT", "1grau")["senha"] == SENHA
    linha = next(s for s in conexao.situacao(conn_painel)
                 if (s["sigla"], s["instancia"]) == ("TJMT", "1grau"))
    assert linha["selo"] == "instavel"
    assert "Instável" in _html(cliente_logado.get("/configuracoes/tribunais"))


def test_faixa_no_cockpit_com_senha_recusada(cliente_logado, conn_painel):
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMT", "1grau", SENHA)
        conexao.registrar(conn_painel, "TJMT", "1grau", "recusada", "login recusado")
    html = _html(cliente_logado.get("/"))
    assert "foi recusada" in html
    assert 'href="/configuracoes/tribunais"' in html
    assert "Abrir Configurações › Tribunais" in html


def test_sem_aviso_sem_faixa(cliente_logado):
    assert 'class="faixa-alerta"' not in _html(cliente_logado.get("/"))


def test_tribunal_fora_da_lista_e_recusado(cliente_logado, monkeypatch, conn_painel):
    chamadas = []
    monkeypatch.setattr("nucleo.conexao.testar", lambda *a: chamadas.append(a) or ("ok", "x"))
    _guardar_cpf(cliente_logado)
    r = _salvar(cliente_logado, sigla="TRF1")
    assert "Tribunal ou instância desconhecidos" in _html(r)
    r = _salvar(cliente_logado, sigla="TJMG", instancia="2grau")
    assert "Tribunal ou instância desconhecidos" in _html(r)
    assert chamadas == []
    assert credenciais.cadastradas(conn_painel) == set()
    for rota in ("testar", "remover"):
        r = cliente_logado.post(f"/configuracoes/tribunais/{rota}",
                                data={"sigla": "TRF1", "instancia": "1grau", "csrf": CSRF},
                                follow_redirects=True)
        assert "Tribunal ou instância desconhecidos" in _html(r)


def test_apresentacao_bloqueia(cliente_logado, conn_painel):
    with cliente_logado.session_transaction() as sessao:
        sessao["apresentacao"] = True
    assert cliente_logado.get("/configuracoes/tribunais").status_code == 403
    for rota in ("cpf", "salvar", "testar", "remover"):
        r = cliente_logado.post(f"/configuracoes/tribunais/{rota}",
                                data={"cpf": CPF, "sigla": "TJMT", "instancia": "1grau",
                                      "senha": SENHA, "csrf": CSRF})
        assert r.status_code == 403
    assert credenciais.cpf_guardado(conn_painel) == ""
    # a faixa de conexão também some na apresentação
    with conn_painel:
        credenciais.gravar_cpf(conn_painel, CPF)
        credenciais.gravar(conn_painel, "TJMT", "1grau", SENHA)
        conexao.registrar(conn_painel, "TJMT", "1grau", "recusada")
    assert "foi recusada" not in _html(cliente_logado.get("/"))


def test_exige_login(conn_painel, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    anonimo = painel.app.test_client()
    r = anonimo.get("/configuracoes/tribunais")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    r = anonimo.post("/configuracoes/tribunais/cpf", data={"cpf": CPF})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_cpf_so_digitos_e_exige_11(cliente_logado, conn_painel):
    r = _guardar_cpf(cliente_logado, "123.456")
    assert "CPF inválido: use os 11 números." in _html(r)
    assert credenciais.cpf_guardado(conn_painel) == ""
    _guardar_cpf(cliente_logado, "123.456.789-01")
    assert credenciais.cpf_guardado(conn_painel) == CPF
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "guardado (final 01)" in html and "Trocar" in html and "Guardar CPF" not in html


def test_sem_cpf_mostra_campo_e_nao_guarda_senha(cliente_logado, monkeypatch, conn_painel):
    _simular(monkeypatch, "ok")
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "Guardar CPF" in html and "Não cadastrado" in html
    r = _salvar(cliente_logado)
    assert "Guarde primeiro o seu CPF." in _html(r)
    assert credenciais.cadastradas(conn_painel) == set()


def test_senha_vazia(cliente_logado, monkeypatch, conn_painel):
    _simular(monkeypatch, "ok")
    _guardar_cpf(cliente_logado)
    r = _salvar(cliente_logado, senha="")
    assert "Digite a senha." in _html(r)
    assert credenciais.cadastradas(conn_painel) == set()


def test_testar_agora_registra_o_resultado(cliente_logado, monkeypatch, conn_painel):
    r = cliente_logado.post("/configuracoes/tribunais/testar",
                            data={"sigla": "TJMT", "instancia": "1grau", "csrf": CSRF},
                            follow_redirects=True)
    assert "Nada guardado para testar." in _html(r)
    _simular(monkeypatch, "ok")
    _guardar_cpf(cliente_logado)
    _salvar(cliente_logado)
    _simular(monkeypatch, "recusada", "O PJe recusou o login.")
    r = cliente_logado.post("/configuracoes/tribunais/testar",
                            data={"sigla": "TJMT", "instancia": "1grau", "csrf": CSRF},
                            follow_redirects=True)
    assert "recusou a senha do TJMT (1ª instância)" in _html(r)
    linha = next(s for s in conexao.situacao(conn_painel)
                 if (s["sigla"], s["instancia"]) == ("TJMT", "1grau"))
    assert linha["selo"] == "recusada"
    assert "Senha recusada" in _html(cliente_logado.get("/configuracoes/tribunais"))


def test_remover_apaga_e_zera_o_estado(cliente_logado, monkeypatch, conn_painel):
    _simular(monkeypatch, "ok")
    _guardar_cpf(cliente_logado)
    _salvar(cliente_logado)
    _simular(monkeypatch, "recusada")
    cliente_logado.post("/configuracoes/tribunais/testar",
                        data={"sigla": "TJMT", "instancia": "1grau", "csrf": CSRF})
    r = cliente_logado.post("/configuracoes/tribunais/remover",
                            data={"sigla": "TJMT", "instancia": "1grau", "csrf": CSRF},
                            follow_redirects=True)
    assert "Senha do TJMT (1ª instância) removida." in _html(r)
    assert credenciais.ler(conn_painel, "TJMT", "1grau") is None
    estado = conn_painel.execute("SELECT estado FROM conexao_tribunal WHERE sigla = 'TJMT' "
                                 "AND instancia = '1grau'").fetchone()
    assert estado["estado"] == "nao_cadastrado"
    # guardar de novo sem varredura: nada de aviso velho
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMT", "1grau", "outra")
    assert conexao.avisos(conn_painel) == []


def test_selo_guardada_ainda_nao_conferida(cliente_logado, conn_painel):
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMT", "2grau", SENHA)
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "Guardada · ainda não conferida" in html


def test_detalhe_e_escapado_e_curto(cliente_logado, conn_painel):
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMT", "1grau", SENHA)
        conexao.registrar(conn_painel, "TJMT", "1grau", "fora",
                          "<script>alert(1)</script> http://x.test/" + "a" * 300)
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html
    assert "a" * 130 not in html


def test_rodape_da_vps(cliente_logado):
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "No seu painel, as senhas ficam cifradas no servidor" in html
    assert "não abre intimação nem conta como ciência" in html
    assert 'name="csrf"' in html


def test_modo_local_guarda_no_cofre_do_computador(tmp_path, monkeypatch, cofre_falso):
    import painel_local
    caminho = str(tmp_path / "l.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_ja_configurado", lambda: True)
    _simular(monkeypatch, "ok", "Login aceito pelo PJe.")
    cliente = painel.app.test_client()
    _guardar_cpf(cliente, CPF)
    r = _salvar(cliente, instancia="2grau")
    assert "Senha do TJMT (2ª instância) guardada." in _html(r)
    conn = banco.conectar(caminho)
    assert credenciais.ler(conn, "TJMT", "2grau")["senha"] == SENHA
    assert SENHA in cofre_falso.itens.values()  # a senha foi para o cofre, não para o banco
    assert conn.execute("SELECT COUNT(*) FROM credencial").fetchone()[0] == 0
    conn.close()
    html = _html(cliente.get("/configuracoes/tribunais"))
    assert "As senhas ficam no cofre deste computador" in html
    assert "cifradas no servidor" not in html
    assert SENHA not in html and CPF not in html and "final 01" in html


# --- chave do cofre perdida ou trocada --------------------------------------------

AVISO_NAO_ABRE = ("Algumas senhas guardadas não abrem com a chave atual do cofre. "
                  "Digite-as de novo.")


def _trocar_a_chave(tmp_path, monkeypatch, nome="nova.key"):
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / nome))
    cofre_servidor.criar_chave()


def test_senha_que_nao_abre_libera_regravar(cliente_logado, monkeypatch, conn_painel, tmp_path):
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMG", "1grau", "senha-velha")
    _trocar_a_chave(tmp_path, monkeypatch)
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert AVISO_NAO_ABRE in html
    assert "Digite a senha de novo" in html
    assert "O cofre de senhas não está disponível" not in html
    assert " disabled" not in html  # CPF e senhas liberados para regravar
    # o CPF também não abre: o campo aparece para digitar de novo
    assert 'id="form-cpf"' in html and "guardado (final" not in html
    _simular(monkeypatch, "ok", "Login aceito pelo PJe.")
    _guardar_cpf(cliente_logado)
    r = _salvar(cliente_logado, sigla="TJMG", senha="senha-nova")
    assert "Senha do TJMG (1ª instância) guardada." in _html(r)
    assert credenciais.ler(conn_painel, "TJMG", "1grau")["senha"] == "senha-nova"
    assert credenciais.estado_do_cofre(conn_painel) == "ok"
    assert AVISO_NAO_ABRE not in _html(cliente_logado.get("/configuracoes/tribunais"))


def test_testar_senha_que_nao_abre_pede_para_digitar(cliente_logado, monkeypatch, conn_painel,
                                                      tmp_path):
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMG", "1grau", "senha-velha")
    _trocar_a_chave(tmp_path, monkeypatch)
    _guardar_cpf(cliente_logado)
    r = cliente_logado.post("/configuracoes/tribunais/testar",
                            data={"sigla": "TJMG", "instancia": "1grau"}, follow_redirects=True)
    assert "não abre com a chave atual do cofre" in _html(r)


def test_sem_chave_desabilita_e_avisa(cliente_logado, monkeypatch, conn_painel, tmp_path):
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMT", "1grau", SENHA)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "sumiu.key"))
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "O cofre de senhas não está disponível neste servidor" in html
    assert " disabled" in html
    assert AVISO_NAO_ABRE not in html


def test_faixa_quando_o_cofre_nao_abre(cliente_logado, monkeypatch, conn_painel, tmp_path):
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMT", "1grau", SENHA)
    _trocar_a_chave(tmp_path, monkeypatch)
    html = _html(cliente_logado.get("/"))
    assert conexao.AVISO_COFRE in html
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "sumiu.key"))
    assert conexao.AVISO_COFRE in _html(cliente_logado.get("/"))


def test_saude_informa_o_estado_do_cofre(cliente_logado, monkeypatch, conn_painel, tmp_path):
    assert cliente_logado.get("/saude").get_json()["cofre"] == "ok"
    _guardar_cpf(cliente_logado)
    with conn_painel:
        credenciais.gravar(conn_painel, "TJMT", "1grau", SENHA)
    _trocar_a_chave(tmp_path, monkeypatch)
    dados = cliente_logado.get("/saude").get_json()
    assert dados["cofre"] == "nao_abre" and dados["ok"] is True
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "sumiu.key"))
    dados = cliente_logado.get("/saude").get_json()
    assert dados["cofre"] == "sem_chave" and dados["ok"] is True


# --- modo local: o cofre do computador recusa ---------------------------------------

@pytest.fixture
def local_com_cofre(tmp_path, monkeypatch, cofre_falso):
    import painel_local
    caminho = str(tmp_path / "l.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_ja_configurado", lambda: True)
    _simular(monkeypatch, "ok", "Login aceito pelo PJe.")
    yield painel.app.test_client(), cofre_falso
    credenciais._definidas.clear()


def _cofre_recusa(cofre, monkeypatch):
    def recusa(*a):
        raise RuntimeError("cofre trancado")
    monkeypatch.setattr(cofre, "set_password", recusa)
    monkeypatch.setattr(cofre, "delete_password", recusa)


def test_local_cofre_recusa_guardar_cpf_e_senha(local_com_cofre, monkeypatch):
    cliente, cofre = local_com_cofre
    _guardar_cpf(cliente, CPF)
    _cofre_recusa(cofre, monkeypatch)
    r = _guardar_cpf(cliente, "22222222222")
    assert r.status_code == 200
    assert "O cofre deste computador não aceitou guardar. Nada foi guardado." in _html(r)
    r = _salvar(cliente)
    assert r.status_code == 200
    assert "O cofre deste computador não aceitou guardar. Nada foi guardado." in _html(r)
    assert SENHA not in _html(r)


def test_local_cofre_recusa_apagar(local_com_cofre, monkeypatch):
    cliente, cofre = local_com_cofre
    _guardar_cpf(cliente, CPF)
    _salvar(cliente)
    _cofre_recusa(cofre, monkeypatch)
    r = cliente.post("/configuracoes/tribunais/remover",
                     data={"sigla": "TJMT", "instancia": "1grau"}, follow_redirects=True)
    assert r.status_code == 200
    assert "O cofre deste computador não aceitou apagar." in _html(r)
    assert "removida" not in _html(r)


def test_local_testar_com_cofre_que_recusa_nao_quebra(local_com_cofre, monkeypatch):
    from nucleo import cofre as nucleo_cofre
    cliente, _cofre = local_com_cofre

    def recusa(*a, **k):
        raise nucleo_cofre.CofreError("x")
    monkeypatch.setattr(credenciais, "ler", recusa)
    r = cliente.post("/configuracoes/tribunais/testar",
                     data={"sigla": "TJMT", "instancia": "1grau"}, follow_redirects=True)
    assert r.status_code == 200
    assert "O cofre deste computador" in _html(r)


def test_vps_remover_com_cofre_indisponivel(cliente_logado, monkeypatch, conn_painel):
    def quebra(*a):
        raise cofre_servidor.CofreServidorError("x")
    monkeypatch.setattr(credenciais, "apagar", quebra)
    r = cliente_logado.post("/configuracoes/tribunais/remover",
                            data={"sigla": "TJMT", "instancia": "1grau"}, follow_redirects=True)
    assert r.status_code == 200
    assert "O cofre de senhas não está disponível neste servidor." in _html(r)


def test_troca_do_cpf_fica_escondida_ate_trocar_e_sem_js(cliente_logado, conn_painel):
    import re
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    # sem CPF: o formulário aparece direto, fora de <details>
    assert re.search(r'<form[^>]*id="form-cpf"', html)
    assert '<details class="trocar-cpf"' not in html
    _guardar_cpf(cliente_logado)
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "guardado (final 11)" in html
    trecho = html[html.index('<details class="trocar-cpf"'):]
    trecho = trecho[:trecho.index("</details>")]
    assert re.search(r"<summary[^>]*>Trocar</summary>", trecho)
    assert 'id="form-cpf"' in trecho and "Guardar novo CPF" in trecho
    assert not re.search(r'<form[^>]*id="form-cpf"[^>]*hidden', html)
    assert "getElementById('trocar-cpf')" not in html  # funciona sem JS


def test_titulo_da_pagina_e_configuracoes(cliente_logado):
    html = _html(cliente_logado.get("/configuracoes/tribunais"))
    assert "<h1>Configurações</h1>" in html
    assert "<h1>Configurações do Lex</h1>" not in html


def test_css_selo_do_tribunal_quebra_linha_sem_invadir_a_coluna():
    import pathlib
    import re
    css = (pathlib.Path(painel.app.static_folder) / "style.css").read_text(encoding="utf-8")
    m = re.search(r"\.linha-tribunal \.tribunal-identidade \.selo\s*\{([^}]*)\}", css)
    assert m and "white-space: normal" in m.group(1) and "max-width: 100%" in m.group(1)
    assert "min-width: 0" in re.search(r"\.linha-tribunal > \*\s*\{([^}]*)\}", css).group(1)
