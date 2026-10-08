"""Íntegra da publicação no painel, "Este processo é seu?" e partes da publicação."""

import json
import re

import pytest

import painel
from nucleo import banco, quadro
from nucleo import painel_dados as pd

TRF = "10088883420234013600"  # TRF1, fora do PJe que o sistema consulta
TEXTO_HTML = (
    "<p>PODER JUDICIÁRIO&nbsp;FEDERAL</p><p>Intime-se a parte <b>autora</b> para "
    "se manifestar em 15 dias.<br>Cumpra-se &amp; publique-se.</p>"
    "<script>alert('xss')</script><style>p{color:red}</style>"
    "<div>&lt;img src=x onerror=alert(1)&gt;</div>")
PARTES = [{"nome": "JOANA FICTÍCIA DE TESTE", "polo": "ativo"},
          {"nome": "PEDRO FICTÍCIO DE TESTE", "polo": "ativo"},
          {"nome": "AUTARQUIA FICTÍCIA FEDERAL", "polo": "passivo"}]


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    caminho = str(tmp_path / "i.db")
    conn = banco.conectar(caminho)
    conn.execute("INSERT INTO processo (numero, tribunal, fontes) VALUES (?, 'TRF1', '[\"djen\"]')",
                 (TRF,))
    for id_, dia, texto, partes in (
            (501, "2026-10-01", "<p>Despacho antigo: cite-se.</p>", PARTES[:1]),
            (502, "2026-10-05", TEXTO_HTML, PARTES)):
        conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, tipo, "
                     "orgao, texto, link, partes_json) VALUES (?, ?, 'TRF1', ?, 'Intimação', "
                     "'1ª VARA FEDERAL CÍVEL', ?, 'https://exemplo.invalido/doc', ?)",
                     (id_, TRF, dia, texto, json.dumps(partes, ensure_ascii=False)))
        quadro.adicionar_item(conn, TRF, "publicacao", str(id_), dia)
    conn.commit()
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    c = painel.app.test_client()
    c.caminho = caminho
    return c


def _sql(cliente, sql, *params):
    conn = banco.conectar(cliente.caminho)
    try:
        with conn:
            return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _html(cliente, url):
    r = cliente.get(url)
    assert r.status_code == 200
    return r.get_data(as_text=True)


# --- texto_integral ------------------------------------------------------------------

def test_texto_integral_quebras_tags_e_entidades():
    assert pd.texto_integral(TEXTO_HTML) == [
        "PODER JUDICIÁRIO FEDERAL",
        "Intime-se a parte autora para se manifestar em 15 dias.",
        "Cumpra-se & publique-se.",
        "<img src=x onerror=alert(1)>",  # texto (escapado pelo template), nunca tag
    ]


def test_texto_integral_li_tr_div_e_vazios():
    html = ("<ul><li>um</li><li>  dois   itens </li></ul><table><tr><td>a</td><td>b</td></tr>"
            "<tr><td>c</td></tr></table><div></div><p>&nbsp;</p><!-- comentário -->fim")
    assert pd.texto_integral(html) == ["um", "dois itens", "a b", "c", "fim"]


def test_texto_integral_remove_script_e_style_com_conteudo():
    paragrafos = pd.texto_integral("<SCRIPT type='x'>roubar()</SCRIPT>ok<style>.a{}</style>")
    assert paragrafos == ["ok"]


def test_texto_integral_texto_puro_quebra_por_linha():
    assert pd.texto_integral("Linha 1\r\n\r\n  Linha   2\n") == ["Linha 1", "Linha 2"]
    assert pd.texto_integral("") == [] and pd.texto_integral(None) == []


def test_texto_integral_corta_no_limite_com_aviso():
    paragrafos = pd.texto_integral("<p>" + "a" * 50 + "</p><p>" + "b" * 50 + "</p>", limite=70)
    assert paragrafos[0] == "a" * 50
    assert paragrafos[1] == "b" * 20 + "…"
    assert paragrafos[-1] == pd.AVISO_INTEGRA_CORTADA and "texto cortado" in paragrafos[-1]
    grande = pd.texto_integral("x" * (pd.LIMITE_INTEGRA * 10))
    assert sum(len(p) for p in grande[:-1]) <= pd.LIMITE_INTEGRA + 1
    assert grande[-1] == pd.AVISO_INTEGRA_CORTADA


# --- íntegra no cartão e na linha do tempo -----------------------------------------------

def test_cartao_mostra_as_publicacoes_com_a_integra(cliente):
    html = _html(cliente, "/demanda/1")
    assert "<h2>Publicações <span class=\"contador\">2</span></h2>" in html
    # Mais recente primeiro.
    secao = html[html.index("<h2>Publicações"):]
    assert secao.index("05/10/2026") < secao.index("01/10/2026")
    assert html.count('<details class="integra">') == 2
    assert "<summary>Ler a íntegra</summary>" in html
    assert "<p>Intime-se a parte autora para se manifestar em 15 dias.</p>" in html
    assert "<p>Despacho antigo: cite-se.</p>" in html
    assert "1ª VARA FEDERAL CÍVEL" in html and "Intimação" in html
    # Nada do HTML do DJEN chega cru.
    assert "<script>alert" not in html and "<b>autora</b>" not in html
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert ('<a href="https://exemplo.invalido/doc" target="_blank" rel="noopener noreferrer" '
            'class="link-discreto">abrir no site do tribunal</a>') in html


def test_cartao_sem_link_seguro_nao_mostra_o_link(cliente):
    _sql(cliente, "UPDATE publicacao SET link = 'javascript:alert(1)'")
    html = _html(cliente, "/demanda/1")
    assert "javascript:" not in html and "abrir no site do tribunal" not in html


def test_linha_do_tempo_troca_o_link_pela_integra(cliente):
    html = _html(cliente, f"/processo/{TRF}")
    assert html.count('<details class="integra">') == 2
    assert "<p>Cumpra-se &amp; publique-se.</p>" in html
    assert "ver publicação" not in html and "abrir no site do tribunal" in html
    assert "<script>alert" not in html


def test_integra_oculta_no_modo_apresentacao(cliente):
    cliente.post("/apresentacao")
    for url in ("/demanda/1", f"/processo/{TRF}"):
        html = _html(cliente, url)
        assert '<details class="integra">' in html and "texto oculto no modo apresentação" in html
        assert "Intime-se a parte" not in html and "Despacho antigo" not in html
        assert "abrir no site do tribunal" not in html


# --- partes da publicação ---------------------------------------------------------------

FRASE_PARTES = re.compile(r"Partes na publicação:</span>\s*(.*?)</p>", re.DOTALL)


def _frase(html):
    return re.sub(r"<[^>]+>", "", FRASE_PARTES.search(html).group(1)).split()


def test_partes_da_publicacao_no_cartao_e_no_processo(cliente):
    esperado = ("JOANA FICTÍCIA DE TESTE, PEDRO FICTÍCIO DE TESTE (polo ativo) × "
                "AUTARQUIA FICTÍCIA FEDERAL (polo passivo)").split()
    assert _frase(_html(cliente, "/demanda/1")) == esperado
    assert _frase(_html(cliente, f"/processo/{TRF}")) == esperado
    assert "Na publicação: JOANA FICTÍCIA DE TESTE" in _html(cliente, "/quadro/fragmento")


def test_partes_da_publicacao_some_com_dados_do_pje(cliente):
    _sql(cliente, "UPDATE processo SET cliente = 'MARIA CLIENTE'")
    assert "Partes na publicação" not in _html(cliente, "/demanda/1")
    assert "Partes na publicação" not in _html(cliente, f"/processo/{TRF}")


def test_partes_publicadas_agrupa_por_polo():
    conn = banco.conectar(":memory:")
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, partes_json) "
                 "VALUES (1, '1', 'TRF1', '2026-10-01', ?)",
                 (json.dumps([{"nome": "B", "polo": "passivo"}, {"nome": "C", "polo": ""}]),))
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, partes_json) "
                 "VALUES (2, '1', 'TRF1', '2026-10-02', ?)",
                 (json.dumps([{"nome": "A", "polo": "ativo"}, {"nome": "b", "polo": "ativo"}]),))
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, partes_json) "
                 "VALUES (3, '1', 'TRF1', '2026-10-03', 'lixo')")
    assert pd.partes_publicadas(conn, "1") == [
        {"rotulo": "polo ativo", "nomes": ["A", "b"], "separador": ""},
        {"rotulo": "polo não informado", "nomes": ["C"], "separador": "·"},
    ]
    assert pd.partes_publicadas(conn, "2") == []


def test_partes_da_publicacao_com_nomes_ficticios_no_modo_apresentacao(cliente):
    cliente.post("/apresentacao")
    for url in ("/demanda/1", f"/processo/{TRF}"):
        html = _html(cliente, url)
        assert "Partes na publicação" in html
        assert "JOANA FICTÍCIA DE TESTE" not in html and "AUTARQUIA FICTÍCIA FEDERAL" not in html


# --- "Este processo é seu?" ---------------------------------------------------------------

def test_pergunta_aparece_no_cartao_e_no_processo(cliente):
    for url in ("/demanda/1", f"/processo/{TRF}"):
        html = _html(cliente, url)
        assert "Este processo é seu?" in html
        assert 'action="/carteira/confirmar"' in html and "É meu, acompanhar" in html
        assert 'action="/carteira/descartar"' in html and "Não é meu" in html


def test_pergunta_some_quando_a_oab_consta(cliente):
    _sql(cliente, "UPDATE processo SET advogado_atua = 1")
    for url in ("/demanda/1", f"/processo/{TRF}"):
        html = _html(cliente, url)
        assert "Este processo é seu?" not in html and "confirmado por você" not in html


def test_e_meu_confirma_e_mostra_discreto(cliente):
    r = cliente.post("/carteira/confirmar", data={"numero": TRF, "voltar": "/demanda/1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/demanda/1")
    html = _html(cliente, "/demanda/1")
    assert "Confirmado: o processo está na sua carteira e segue monitorado." in html
    assert "Este processo é seu?" not in html
    assert re.search(r"Na sua carteira · confirmado por você em \d\d/\d\d/\d{4}", html)
    assert 'class="link-botao">Não é meu</button>' in html
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "chegou"
    carteira_html = _html(cliente, "/carteira?aba=ativa")
    assert "1008888-34.2023.4.01.3600" in carteira_html


def test_nao_e_meu_tira_o_cartao_do_quadro_e_desfazer_traz_de_volta(cliente):
    assert 'data-cartao="1"' in _html(cliente, "/quadro/fragmento")
    r = cliente.post("/carteira/descartar", data={"numero": TRF, "voltar": "/demanda/1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/demanda/1")
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "resolvida"
    assert 'data-cartao="1"' not in _html(cliente, "/quadro/fragmento")
    html = _html(cliente, "/demanda/1")
    assert 'O cartão saiu do quadro (foi para “Resolvido”).' in html
    assert "Você marcou este processo como não seu" in html and ">Desfazer</button>" in html
    assert "1008888-34.2023.4.01.3600" in _html(cliente, "/carteira?aba=descartados")
    # Desfazer = Restaurar (o mesmo da aba Descartados): volta para A conferir e para a triagem.
    r = cliente.post("/carteira/restaurar", data={"numero": TRF, "voltar": "/demanda/1"})
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "chegou"
    assert "O cartão voltou ao quadro." in _html(cliente, "/demanda/1")
    assert "Este processo é seu?" in _html(cliente, "/demanda/1")


def test_nao_e_meu_depois_e_meu_tambem_devolve_o_cartao(cliente):
    cliente.post("/carteira/descartar", data={"numero": TRF})
    cliente.post("/carteira/confirmar", data={"numero": TRF})
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "chegou"
    linha = _sql(cliente, "SELECT confirmado_em, descartado_em FROM processo")[0]
    assert linha[0] and linha[1] is None


def test_aba_a_conferir_tem_e_meu(cliente):
    html = _html(cliente, "/carteira?aba=a_conferir")
    assert 'action="/carteira/confirmar"' in html and ">É meu</button>" in html
    assert ">Descartar</button>" in html
    cliente.post("/carteira/confirmar", data={"numero": TRF, "voltar": "/carteira?aba=a_conferir"})
    assert "1008888-34.2023.4.01.3600" not in _html(cliente, "/carteira?aba=a_conferir")


def test_confirmar_pelo_apelido_no_modo_apresentacao(cliente):
    cliente.post("/apresentacao")
    html = _html(cliente, "/carteira?aba=a_conferir")
    apelido = re.search(r'name="numero" value="(p[0-9a-f]{12})"', html).group(1)
    cliente.post("/carteira/confirmar", data={"numero": apelido})
    assert _sql(cliente, "SELECT confirmado_em FROM processo")[0][0]


@pytest.mark.parametrize("rota, coluna, depois", [
    ("/carteira/confirmar", "confirmado_em", "chegou"),
    ("/carteira/descartar", "descartado_em", "resolvida"),
])
def test_decisoes_exigem_csrf(cliente, rota, coluna, depois):
    painel.app.config["CSRF_EXIGIDO"] = True
    r = cliente.post(rota, data={"numero": TRF, "voltar": "/demanda/1"})  # sem token
    assert r.status_code == 302
    assert _sql(cliente, f"SELECT {coluna} FROM processo")[0][0] is None
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "chegou"
    cliente.get("/carteira")
    with cliente.session_transaction() as sessao:
        token = sessao["csrf"]
    assert cliente.post(rota, data={"numero": TRF, "csrf": token}).status_code == 302
    assert _sql(cliente, f"SELECT {coluna} FROM processo")[0][0]
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == depois


def test_restaurar_exige_csrf(cliente):
    cliente.post("/carteira/descartar", data={"numero": TRF})
    painel.app.config["CSRF_EXIGIDO"] = True
    cliente.post("/carteira/restaurar", data={"numero": TRF})
    assert _sql(cliente, "SELECT descartado_em FROM processo")[0][0]
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "resolvida"
