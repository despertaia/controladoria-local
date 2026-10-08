"""Tela /configurar e a barreira do modo local (CONTROLADORIA_LOCAL=1). Sem rede: a
sonda do PJe é trocada por um dublê."""

import os

import pytest

import painel
import painel_local
from nucleo import ajustes_locais, banco, cofre
from tests.cofre_falso import cofre_falso  # noqa: F401

VARIAVEIS = ("CONTROLADORIA_TRIBUNAL", "PJE_CPF", "PJE_SENHA", "PJE_SENHA_2GRAU",
             "CARTEIRA_ADVOGADO_NOME", "CARTEIRA_OAB_NUMERO", "CARTEIRA_OAB_UF")


@pytest.fixture
def c(tmp_path, monkeypatch, cofre_falso):  # noqa: F811
    caminho = str(tmp_path / "c.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    for var in VARIAVEIS:  # salvar() escreve em os.environ: o monkeypatch desfaz
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_configurado", False)
    cli = painel.app.test_client()
    cli.caminho = caminho
    cli.sondas = []
    cli.resposta_sonda = {}

    def sonda_falsa(cred, instancias, **_k):
        cli.sondas.append((dict(cred), [i[0] for i in instancias]))
        return {i[0]: cli.resposta_sonda.get(i[0], ("ok", "Login aceito pelo PJe."))
                for i in instancias}
    monkeypatch.setattr(painel_local, "sondar_login", sonda_falsa)
    return cli


def _form(**extra):
    dados = {"tribunal": "TJMT", "cpf": "123.456.789-01", "senha": "Senha-Um-9", "senha_2grau": "",
             "advogado_nome": "Maria  da Silva", "oab_numero": "12.345", "oab_uf": "MT"}
    dados.update(extra)
    return dados


def _tarefas(c):
    conn = banco.conectar(c.caminho)
    try:
        return [tuple(r) for r in conn.execute("SELECT tipo, pedida_por FROM tarefa")]
    finally:
        conn.close()


def test_modo_local_sem_configuracao_leva_tudo_para_configurar(c):
    for caminho in ("/", "/carteira", "/configuracoes/lex"):
        r = c.get(caminho)
        assert r.status_code == 302 and r.headers["Location"].endswith("/configurar")
    assert c.get("/saude").status_code in (200, 503)
    assert c.get("/static/style.css").status_code == 200
    r = c.get("/configurar")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Configuração do escritório" in html
    assert "cofre deste computador" in html
    assert 'value="TJMT"' in html and 'value="TJMG"' in html


def test_producao_nao_muda_nada(c, monkeypatch):
    monkeypatch.delenv("CONTROLADORIA_LOCAL")
    assert c.get("/").status_code == 200
    assert c.get("/configurar").status_code == 404
    assert "Configuração do escritório" not in c.get("/configuracoes/lex").get_data(as_text=True)
    assert c.post("/configurar", data=_form()).status_code == 404
    assert cofre.ler("pje_senha") is None and c.sondas == []


def test_post_ok_grava_enfileira_varredura_e_vai_ao_cockpit(c):
    r = c.post("/configurar", data=_form(senha_2grau="Senha-Dois-9"))
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert c.sondas == [({"cpf": "12345678901", "senha": "Senha-Um-9", "senha_2grau": "Senha-Dois-9"},
                         ["1grau", "2grau"])]
    assert cofre.ler("pje_senha") == "Senha-Um-9" and cofre.ler("pje_senha_2grau") == "Senha-Dois-9"
    assert ajustes_locais.ler() == {"tribunal": "TJMT", "tribunais": ["TJMT"],
                                    "advogado_nome": "Maria da Silva",
                                    "oab_numero": "12345", "oab_uf": "MT"}
    assert os.environ["PJE_SENHA"] == "Senha-Um-9" and os.environ["CARTEIRA_OAB_UF"] == "MT"
    assert _tarefas(c) == [("varredura", "configuracao")]
    html = c.get("/").get_data(as_text=True)  # já configurado: o Cockpit abre
    assert "Configuração guardada" in html


def test_senha_recusada_nao_grava(c):
    c.resposta_sonda = {"2grau": ("recusada", "O PJe recusou o CPF ou a senha.")}
    r = c.post("/configurar", data=_form(senha_2grau="errada"))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "recusou o login na 2ª instância" in html
    assert "Nada foi gravado" in html
    assert cofre.ler("pje_senha") is None and ajustes_locais.ler() == {}
    assert _tarefas(c) == []
    assert "errada" not in html and 'value="Senha-Um-9"' not in html  # senha nunca volta à tela
    assert c.get("/").status_code == 302


def test_tribunal_fora_do_ar_grava_com_aviso(c):
    c.resposta_sonda = {"1grau": ("indisponivel", "Não foi possível baixar o WSDL")}
    r = c.post("/configurar", data=_form(tribunal="TJMG", oab_uf="MG", senha_2grau="ignorada"))
    assert r.status_code == 302
    assert c.sondas[0] == ({"cpf": "12345678901", "senha": "Senha-Um-9"}, ["1grau"])
    assert cofre.ler("pje_senha_2grau") is None  # TJMG não tem 2º grau
    html = c.get("/").get_data(as_text=True)
    assert "A carteira pelo DJEN já funciona" in html
    assert "aviso-aviso" in html
    assert _tarefas(c) == [("varredura", "configuracao")]


@pytest.mark.parametrize("campo,valor,mensagem", [
    ("cpf", "123", "11 dígitos"),
    ("senha", "", "Informe a senha do PJe"),
    ("advogado_nome", " ", "como sai no DJEN"),
    ("oab_numero", "abc", "número da OAB"),
    ("oab_uf", "XX", "UF da OAB"),
])
def test_validacao_nao_chama_a_sonda_nem_grava(c, campo, valor, mensagem):
    r = c.post("/configurar", data=_form(**{campo: valor}))
    assert r.status_code == 200 and mensagem in r.get_data(as_text=True)
    assert c.sondas == [] and cofre.ler("pje_cpf") is None


def test_edicao_com_senha_vazia_mantem_a_guardada(c):
    c.post("/configurar", data=_form(senha_2grau="Senha-Dois-9"))
    html = c.get("/configurar").get_data(as_text=True)
    assert "guardada ✓" in html and "Senha-Um-9" not in html and "Senha-Dois-9" not in html
    assert 'value="12345678901"' in html and "Voltar ao Cockpit" in html
    r = c.post("/configurar", data=_form(senha="", advogado_nome="Maria S."))
    assert r.status_code == 302
    assert c.sondas[-1][0] == {"cpf": "12345678901", "senha": "Senha-Um-9", "senha_2grau": "Senha-Dois-9"}
    assert cofre.ler("pje_senha") == "Senha-Um-9" and ajustes_locais.ler()["advogado_nome"] == "Maria S."


def test_trocar_cpf_exige_digitar_a_senha_de_novo(c):
    c.post("/configurar", data=_form())
    r = c.post("/configurar", data=_form(cpf="98765432100", senha=""))
    assert "digite a senha do PJe de novo" in r.get_data(as_text=True)
    assert cofre.ler("pje_cpf") == "12345678901"


def test_erro_do_cofre_mostra_aviso_e_nao_enfileira(c, monkeypatch):
    def recusa(*_a, **_k):
        raise cofre.CofreError("O cofre do sistema recusou guardar “pje_cpf” (KeyringError).")
    monkeypatch.setattr(painel_local.ajustes_locais, "salvar", recusa)
    r = c.post("/configurar", data=_form())
    assert r.status_code == 200 and "cofre deste computador" in r.get_data(as_text=True)
    assert _tarefas(c) == []


def test_link_na_tela_de_configuracoes_em_modo_local(c):
    c.post("/configurar", data=_form())
    html = c.get("/configuracoes/lex").get_data(as_text=True)
    assert 'href="/configurar"' in html


def test_csrf_exigido_no_post(c):
    painel.app.config["CSRF_EXIGIDO"] = True
    r = c.post("/configurar", data=_form())
    assert r.status_code == 302 and c.sondas == [] and cofre.ler("pje_senha") is None
    c.get("/configurar")
    with c.session_transaction() as sessao:
        token = sessao["csrf"]
    r = c.post("/configurar", data={**_form(), "csrf": token})
    assert r.status_code == 302 and cofre.ler("pje_senha") == "Senha-Um-9"


def test_barreira_deixa_a_ponte_livre(monkeypatch, tmp_path):
    import painel_local
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setattr(painel_local, "_ja_configurado", lambda: False)
    import painel
    with painel.app.test_request_context("/ponte/proximo", method="POST"):
        assert painel_local._exigir_configuracao() is None
    with painel.app.test_request_context("/"):
        assert painel_local._exigir_configuracao() is not None


def test_sem_tribunal_guarda_so_o_advogado_e_a_busca_no_djen_funciona(c):
    """O caso do José (mentoria de 2026-10-07): sem senha do PJe, nome e OAB bastam."""
    r = c.post("/configurar", data={"advogado_nome": "José  Campos", "oab_numero": "9.876",
                                     "oab_uf": "SP", "cpf": "", "csrf": ""})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert c.sondas == []  # nada a testar no PJe
    assert ajustes_locais.ler() == {"tribunais": [], "advogado_nome": "José Campos",
                                    "oab_numero": "9876", "oab_uf": "SP"}
    assert ajustes_locais.configurado() is True and ajustes_locais.pje_ligado() is False
    assert cofre.ler("pje_cpf") is None
    assert _tarefas(c) == [("varredura", "configuracao")]
    html = c.get("/").get_data(as_text=True)
    assert "Configuração guardada" in html and "publicações do Diário (DJEN) já são buscadas" in html


def test_primeira_tela_nao_traz_tribunal_marcado(c):
    html = c.get("/configurar").get_data(as_text=True)
    assert "marcar-tribunal\" checked" not in html and "opcional" in html
    assert html.index("titulo-advogado") < html.index("titulo-pje")


def test_cpf_digitado_sem_tribunal_e_ignorado(c):
    r = c.post("/configurar", data=_form(tribunal="", senha=""))
    assert r.status_code == 302 and c.sondas == [] and cofre.ler("pje_cpf") is None


def test_tribunal_marcado_sem_senha_explica_que_pode_desmarcar(c):
    r = c.post("/configurar", data=_form(senha=""))
    html = r.get_data(as_text=True)
    assert "Informe a senha do PJe do TJMT" in html and "desmarque" in html
    assert c.sondas == [] and ajustes_locais.ler() == {}


def test_desmarcar_todos_os_tribunais_apaga_cpf_e_senhas(c):
    c.post("/configurar", data=_form(senha_2grau="Senha-Dois-9"))
    assert cofre.ler("pje_senha_TJMT") == "Senha-Um-9"
    r = c.post("/configurar", data=_form(tribunal="", senha=""))
    assert r.status_code == 302
    for nome in ("pje_cpf", "pje_senha", "pje_senha_2grau", "pje_senha_TJMT",
                 "pje_senha_2grau_TJMT"):
        assert cofre.ler(nome) is None, nome
    assert ajustes_locais.ler()["tribunais"] == []
    assert "PJE_SENHA" not in os.environ


def test_consulta_ao_pje_sem_senha_leva_a_configuracao(c):
    c.post("/configurar", data=_form(tribunal="", senha=""))
    with painel.app.test_request_context("/"):
        r = painel._sem_credencial_pje(painel.CredencialPJeAusente("sem"))
    assert r.status_code == 302 and r.headers["Location"].endswith("/configurar")
