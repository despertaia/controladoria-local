"""Arquivar e lixeira no painel (1.5.0): rotas de um processo, do lote e do cartão."""

import pathlib
import re

import pytest

import painel
from nucleo import banco, quadro

A = "00000010220248110041"
B = "00000020220248110041"
JSON = {"Accept": "application/json"}


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    caminho = str(tmp_path / "a.db")
    conn = banco.conectar(caminho)
    for numero, nome in ((A, "MARIA FICTÍCIA"), (B, "JOÃO FICTÍCIO")):
        conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado, cliente) "
                     "VALUES (?, 'TJMT', 1, 0, ?)", (numero, nome))
    quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
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


def _estado(cliente, numero):
    return tuple(_sql(cliente, "SELECT arquivado_por_voce_em IS NOT NULL, excluido_em IS NOT NULL "
                               "FROM processo WHERE numero = ?", numero)[0])


def _html(cliente, url="/carteira"):
    r = cliente.get(url)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_arquivar_um_pela_pagina_do_processo(cliente):
    r = cliente.post("/carteira/arquivar", data={"numero": A, "voltar": f"/processo/{A}"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/processo/{A}")
    assert _estado(cliente, A) == (1, 0)
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "resolvida"
    assert "Arquivado. Ele fica em «Arquivados»" in _html(cliente, f"/processo/{A}")


def test_lote_arquiva_e_desarquiva(cliente):
    r = cliente.post("/carteira/arquivar", data={"numero": [A, B], "voltar": "/carteira"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/carteira")
    assert _estado(cliente, A) == _estado(cliente, B) == (1, 0)
    assert "2 processos arquivados" in _html(cliente)
    cliente.post("/carteira/desarquivar", data={"numero": [A, B]})
    assert _estado(cliente, A) == _estado(cliente, B) == (0, 0)
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "chegou"


def test_excluir_e_restaurar_da_lixeira(cliente):
    cliente.post("/carteira/excluir", data={"numero": A})
    assert _estado(cliente, A) == (0, 1)
    cliente.post("/carteira/lixeira/restaurar", data={"numero": A})
    assert _estado(cliente, A) == (0, 0)


def test_lote_de_mais_de_200_e_recusado_inteiro(cliente):
    numeros = [f"{i:07d}0220248110041" for i in range(1, 202)]  # inclui A e B
    r = cliente.post("/carteira/arquivar", data={"numero": numeros, "voltar": "/carteira"})
    assert r.status_code == 302
    assert _estado(cliente, A) == _estado(cliente, B) == (0, 0)
    assert "Escolha até 200 processos de uma vez." in _html(cliente)


def test_sem_numero_valido(cliente):
    cliente.post("/carteira/arquivar", data={"numero": "123", "voltar": "/carteira"})
    assert "Escolha pelo menos um processo." in _html(cliente)


def test_cartao_do_quadro_em_json(cliente):
    r = cliente.post("/carteira/arquivar", data={"numero": A}, headers=JSON)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert "O cartão saiu do quadro." in r.get_json()["mensagem"]


def test_lex_trabalhando_recusa_em_json(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'trabalhando'")
    r = cliente.post("/carteira/excluir", data={"numero": A}, headers=JSON)
    assert r.status_code == 409
    assert r.get_json()["erro"] == (
        "O Lex está trabalhando neste processo. Espere o Lex terminar ou desista do pedido.")
    assert _estado(cliente, A) == (0, 0)


@pytest.mark.parametrize("rota, antes, depois", [
    ("/carteira/arquivar", None, (1, 0)),
    ("/carteira/excluir", None, (0, 1)),
    ("/carteira/desarquivar", "UPDATE processo SET arquivado_por_voce_em = 'x'", (0, 0)),
    ("/carteira/lixeira/restaurar", "UPDATE processo SET excluido_em = 'x'", (0, 0)),
])
def test_rotas_exigem_csrf(cliente, rota, antes, depois):
    if antes:
        _sql(cliente, antes)
    inicial = _estado(cliente, A)
    painel.app.config["CSRF_EXIGIDO"] = True
    assert cliente.post(rota, data={"numero": A}).status_code == 302
    assert _estado(cliente, A) == inicial
    cliente.get("/carteira")
    with cliente.session_transaction() as sessao:
        token = sessao["csrf"]
    assert cliente.post(rota, data={"numero": A, "csrf": token}).status_code == 302
    assert _estado(cliente, A) == depois


def test_modo_apresentacao_bloqueia_as_acoes(cliente):
    cliente.post("/apresentacao")
    for rota in ("/carteira/arquivar", "/carteira/desarquivar", "/carteira/excluir",
                 "/carteira/lixeira/restaurar"):
        assert cliente.post(rota, data={"numero": A}).status_code == 403
    assert _estado(cliente, A) == (0, 0)


def test_carteira_aceita_as_abas_novas(cliente):
    cliente.post("/carteira/arquivar", data={"numero": A})
    cliente.post("/carteira/excluir", data={"numero": B})
    assert "MARIA FICTÍCIA" in _html(cliente, "/carteira?aba=arquivados")
    html = _html(cliente, "/carteira?aba=lixeira")
    assert "JOÃO FICTÍCIO" in html and "MARIA FICTÍCIA" not in html
    html = _html(cliente, "/carteira")
    assert "MARIA FICTÍCIA" not in html and "JOÃO FICTÍCIO" not in html


def test_adicionar_nao_tira_da_lixeira_nem_do_arquivo(cliente):
    cliente.post("/carteira/excluir", data={"numero": A})
    r = cliente.post("/carteira/adicionar", data={"numero": A})
    assert r.headers["Location"].endswith(f"/processo/{A}")
    assert _estado(cliente, A) == (0, 1)
    assert "Este processo está arquivado ou na lixeira" in _html(cliente, f"/processo/{A}")


def test_cockpit_e_processo_continuam_abrindo(cliente):
    cliente.post("/carteira/arquivar", data={"numero": A})
    cliente.post("/carteira/excluir", data={"numero": B})
    assert _html(cliente, "/")
    assert _html(cliente, f"/processo/{A}") and _html(cliente, f"/processo/{B}")


def test_quadro_mudou_durante_a_acao_nao_aplica_nada(cliente, monkeypatch):
    """Um movimento concorrente do cartão (MovimentoInvalido) não vira erro 500 e a
    transação inteira é desfeita."""
    def _falha(*args, **kwargs):
        raise quadro.MovimentoInvalido("cartão mudou")
    monkeypatch.setattr(quadro, "tirar_do_quadro", _falha)
    r = cliente.post("/carteira/arquivar", data={"numero": A}, headers=JSON)
    assert r.status_code == 409
    assert r.get_json() == {"ok": False, "erro": "O quadro mudou enquanto você agia. Tente de novo."}
    assert _estado(cliente, A) == (0, 0)
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "chegou"
    r = cliente.post("/carteira/arquivar", data={"numero": A, "voltar": "/carteira"})
    assert r.status_code == 302
    assert "O quadro mudou enquanto você agia" in _html(cliente)
    assert _estado(cliente, A) == (0, 0)


# --- telas ---------------------------------------------------------------------------

def test_carteira_tem_caixas_e_barra_de_lote_que_muda_com_a_aba(cliente):
    html = _html(cliente)
    assert 'id="lote"' in html and 'form="lote" name="numero"' in html
    assert 'formaction="/carteira/arquivar"' in html and 'formaction="/carteira/excluir"' in html
    assert 'formaction="/carteira/desarquivar"' not in html
    cliente.post("/carteira/arquivar", data={"numero": A})
    html = _html(cliente, "/carteira?aba=arquivados")
    assert 'formaction="/carteira/desarquivar"' in html and 'formaction="/carteira/excluir"' in html
    assert "por você" in html
    cliente.post("/carteira/excluir", data={"numero": B})
    html = _html(cliente, "/carteira?aba=lixeira")
    assert 'formaction="/carteira/lixeira/restaurar"' in html and "faltam 30 dias" in html
    assert 'formaction="/carteira/arquivar"' not in html


def test_abas_novas_so_aparecem_com_conteudo(cliente):
    html = _html(cliente)
    assert "aba=arquivados" not in html and "aba=lixeira" not in html
    cliente.post("/carteira/arquivar", data={"numero": A})
    html = _html(cliente)
    assert "aba=arquivados" in html and "aba=lixeira" not in html


def test_lote_tem_confirmacao_e_some_na_apresentacao(cliente):
    html = _html(cliente)
    assert "window.confirm(" in html and 'data-verbo="Arquivar"' in html
    cliente.post("/apresentacao")
    html = _html(cliente)
    assert 'id="lote"' not in html and 'form="lote" name="numero"' not in html


def test_caixa_desligada_com_o_lex_trabalhando(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'trabalhando'")
    html = _html(cliente)
    assert re.search(rf'value="{A}"[^>]*disabled', html)


def test_pagina_do_processo_tem_botoes_faixa_e_confirmacao(cliente):
    html = _html(cliente, f"/processo/{A}")
    assert 'action="/carteira/arquivar"' in html and 'action="/carteira/excluir"' in html
    assert "return confirm(" in html
    cliente.post("/carteira/arquivar", data={"numero": A})
    html = _html(cliente, f"/processo/{A}")
    assert "Arquivado por você em" in html and 'action="/carteira/desarquivar"' in html
    cliente.post("/carteira/excluir", data={"numero": A})
    html = _html(cliente, f"/processo/{A}")
    assert "Na lixeira: some em 30 dias" in html
    assert 'action="/carteira/lixeira/restaurar"' in html
    assert 'action="/carteira/arquivar"' not in html
    cliente.post("/apresentacao")
    assert 'action="/carteira/lixeira/restaurar"' not in _html(cliente, f"/processo/{A}")


def test_pagina_do_processo_com_o_lex_desliga_os_botoes(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'trabalhando'")
    html = _html(cliente, f"/processo/{A}")
    assert "Espere o Lex terminar ou desista do pedido." in html
    assert re.search(r'action="/carteira/arquivar".*?<button[^>]*disabled', html, re.S)


def test_cartao_do_quadro_tem_menu_com_confirmacao(cliente):
    html = _html(cliente, "/quadro/fragmento")
    assert 'class="cartao-menu"' in html
    assert ">Arquivar processo</button>" in html and ">Excluir processo</button>" in html
    assert "return confirm('Arquivar este processo?" in html
    cliente.post("/apresentacao")
    assert "cartao-menu" not in _html(cliente, "/quadro/fragmento")


def test_quadro_js_respeita_o_cancelar_da_confirmacao():
    js = (pathlib.Path(painel.__file__).parent / "static" / "quadro.js").read_text(encoding="utf-8")
    assert "e.defaultPrevented" in js


def test_quadro_js_mantem_o_menu_do_cartao_aberto_na_troca_do_quadro():
    js = (pathlib.Path(painel.__file__).parent / "static" / "quadro.js").read_text(encoding="utf-8")
    assert "details.cartao-menu[open]" in js and "details.cartao-menu'" in js


def test_cockpit_carteira_e_processo_mostram_quem_voltou(cliente):
    from nucleo import eventos
    conn = banco.conectar(cliente.caminho)
    with conn:
        eventos.registrar(conn, "processo_voltou", A,
                          {"de": "arquivado", "publicacao": 1, "data": "2026-10-05"})
    conn.close()
    html = _html(cliente, "/")
    assert "1 processo(s) voltaram por publicação nova" in html and "/carteira?aba=ativa" in html
    assert "Voltou: publicação de 05/10" in _html(cliente)
    assert "Voltou: publicação de 05/10" in _html(cliente, f"/processo/{A}")


def test_selo_voltou_nao_aparece_em_processo_arquivado_de_novo(cliente):
    from nucleo import eventos
    conn = banco.conectar(cliente.caminho)
    with conn:
        eventos.registrar(conn, "processo_voltou", A,
                          {"de": "arquivado", "publicacao": 1, "data": "2026-10-05"})
    conn.close()
    cliente.post("/carteira/arquivar", data={"numero": A})
    assert "Voltou" not in _html(cliente, "/carteira?aba=arquivados")
    assert "Voltou" not in _html(cliente, f"/processo/{A}")
    cliente.post("/carteira/excluir", data={"numero": A})
    assert "Voltou" not in _html(cliente, "/carteira?aba=lixeira")
    assert "Voltou" not in _html(cliente, f"/processo/{A}")


def test_grupo_mostra_o_estado_do_processo(cliente, tmp_path, monkeypatch):
    from captura import grupos
    monkeypatch.setattr(grupos, "PASTA_GRUPOS", str(tmp_path / "grupos"))
    g = grupos.criar("Bancos", "", [A, B])
    cliente.post("/carteira/excluir", data={"numero": B})
    html = _html(cliente, f"/grupos/{g['slug']}")
    assert html.count("Na lixeira") == 1 and "Arquivado por você" not in html
