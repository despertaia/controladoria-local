"""Incluir processos num grupo já criado e retirar um processo do grupo (pedido do
mentorado Tiago, 2026-10-26)."""

import pytest

import painel
from captura import grupos
from captura.processo_parser import _dv_esperado, formatar_numero_cnj


def _cnj(seq: int, ano="2024", jtr="811", origem="0041") -> str:
    """Número CNJ com o dígito verificador certo."""
    base = f"{seq:07d}00{ano}{jtr}{origem}"
    dv = _dv_esperado(base)
    return f"{seq:07d}{dv:02d}{ano}{jtr}{origem}"


A, B, C, D = (_cnj(n) for n in (1, 2, 3, 4))
DE_OUTRO_ESTADO = _cnj(5, jtr="826")  # TJSP
DV_ERRADO = A[:7] + f"{(int(A[7:9]) + 1) % 100:02d}" + A[9:]


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(grupos, "PASTA_GRUPOS", str(tmp_path / "grupos"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    for var in ("CONTROLADORIA_TRIBUNAIS", "CONTROLADORIA_TRIBUNAL", "CONTROLADORIA_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    cli = painel.app.test_client()
    cli.grupo = grupos.criar("Bancos", "", [A, B])
    return cli


def _mensagens(c, slug):
    return c.get(f"/grupos/{slug}").get_data(as_text=True)


def test_armazenamento_incluir_e_retirar(tmp_path, monkeypatch):
    monkeypatch.setattr(grupos, "PASTA_GRUPOS", str(tmp_path / "g"))
    g = grupos.criar("X", "", [A])
    assert grupos.incluir(g["slug"], [B, A, B, C]) == {"entraram": [B, C], "ja_estavam": [A]}
    assert grupos.ler(g["slug"])["numeros"] == [A, B, C]
    assert grupos.retirar(g["slug"], B) is True
    assert grupos.ler(g["slug"])["numeros"] == [A, C]
    assert grupos.retirar(g["slug"], B) is False
    assert grupos.incluir("nao-existe", [A]) is None and grupos.retirar("nao-existe", A) is False
    assert [p.name for p in (tmp_path / "g").iterdir()] == [f"{g['slug']}.json"]  # sem sobras


def test_incluir_varios_confere_e_explica_o_que_ficou_de_fora(c):
    slug = c.grupo["slug"]
    colado = "\n".join([formatar_numero_cnj(C), D, A, DE_OUTRO_ESTADO, DV_ERRADO, "123", C])
    r = c.post(f"/grupos/{slug}/incluir", data={"numeros": colado})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/grupos/{slug}")
    assert grupos.ler(slug)["numeros"] == [A, B, C, D]
    html = _mensagens(c, slug)
    assert "2 processo(s) entraram no grupo" in html
    assert "Ficaram de fora 4" in html
    assert f"{formatar_numero_cnj(A)} (já estava no grupo)" in html
    assert "dígito verificador inválido" in html
    assert "não é do TJMT" in html
    assert "123 (não tem 20 dígitos)" in html


def test_incluir_so_repetidos_nao_grava_nada(c):
    slug = c.grupo["slug"]
    c.post(f"/grupos/{slug}/incluir", data={"numeros": f"{A}, {B}"})
    assert grupos.ler(slug)["numeros"] == [A, B]
    html = _mensagens(c, slug)
    assert "Nenhum processo novo entrou no grupo" in html and "Ficaram de fora 2" in html


def test_incluir_sem_numero(c):
    slug = c.grupo["slug"]
    c.post(f"/grupos/{slug}/incluir", data={"numeros": "  \n nada aqui "})
    assert "Cole ao menos um número" in _mensagens(c, slug)
    assert grupos.ler(slug)["numeros"] == [A, B]


def test_lista_longa_de_fora_e_resumida(c):
    slug = c.grupo["slug"]
    c.post(f"/grupos/{slug}/incluir", data={"numeros": "\n".join(str(n) for n in range(1, 16))})
    html = _mensagens(c, slug)
    assert "Ficaram de fora 15" in html and "e mais 5." in html


def test_retirar_pela_rota(c):
    slug = c.grupo["slug"]
    r = c.post(f"/grupos/{slug}/retirar", data={"numero": formatar_numero_cnj(A)})
    assert r.status_code == 302 and grupos.ler(slug)["numeros"] == [B]
    html = _mensagens(c, slug)
    assert "retirado do grupo" in html and "continuam guardados" in html
    c.post(f"/grupos/{slug}/retirar", data={"numero": A})
    assert "Esse processo não está no grupo" in _mensagens(c, slug)


def test_grupo_inexistente_da_404(c):
    assert c.post("/grupos/nao-existe/incluir", data={"numeros": A}).status_code == 404
    assert c.post("/grupos/nao-existe/retirar", data={"numero": A}).status_code == 404


def test_pagina_do_grupo_tem_o_campo_e_os_botoes(c):
    html = _mensagens(c, c.grupo["slug"])
    assert "Incluir processos no grupo" in html and 'name="numeros"' in html
    assert html.count("Retirar do grupo") == 2
    assert "event.stopPropagation()" in html  # o clique no botão não abre o processo
