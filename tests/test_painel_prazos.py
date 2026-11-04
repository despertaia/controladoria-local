"""As rotas de prazo: ações, CSRF, login e modo apresentação (spec, seção 8)."""

import json
import pathlib
import re
import sqlite3
from datetime import date, datetime
from html.parser import HTMLParser

import pytest
from markupsafe import escape

import painel
from nucleo import (banco, eventos, painel_dados, ponte, prazos, prazos_banco, prazos_legais,
                    prazos_tela, tribunal)

A = "00012345620248110041"


@pytest.fixture
def web(tmp_path, monkeypatch):
    caminho = str(tmp_path / "b.db")
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel, "PAINEL_USUARIO", "")
    conn = banco.conectar(caminho)
    with conn:
        conn.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua, cliente) "
                     "VALUES (?, 'TJMT', '1ª Vara Cível de Cuiabá', 1, 'MARIA FICTÍCIA')", (A,))
        conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                     "VALUES (1, ?, 'TJMT', '2026-11-05', 'x')", (A,))
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "1", "2026-11-05")
        prazos_banco.corrigir(conn, pid, ato="Réplica", dias=15, unidade="uteis")
    conn.close()
    c = painel.app.test_client()
    c.pid, c.caminho = pid, caminho
    return c


def _p(c, pid=None):
    conn = banco.conectar(c.caminho)
    try:
        return prazos_banco.obter(conn, pid or c.pid)
    finally:
        conn.close()


def _token(c, pid=None):
    """O que a tela mandaria em `versao`: a data e o momento da última mudança do prazo."""
    conn = banco.conectar(c.caminho)
    try:
        return prazos_tela.versao_do_prazo(prazos_banco.obter(conn, pid or c.pid))
    finally:
        conn.close()


def _post(c, rota, data=None, versao=True):
    """POST de uma ação que age sobre o que está na tela: leva a versão que a tela mostrou
    (a do prazo da rota, ou a do `web.pid` se a rota não nomeia um)."""
    data = dict(data or {})
    if versao is True:
        achou = re.match(r"/prazos/(\d+)/", rota)
        data["versao"] = _token(c, int(achou.group(1)) if achou else None)
    return c.post(rota, data=data)


def _mensagem(c, resposta):
    assert resposta.status_code == 302
    with c.session_transaction() as s:
        return dict(s.get("_flashes", [])) if s.get("_flashes") else {}


def test_conferir(web):
    r = _post(web, f"/prazos/{web.pid}/conferir")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/prazos/{web.pid}")
    assert _p(web)["estado"] == "conferido"


def test_nada_vira_conferido_por_get(web):
    assert web.get(f"/prazos/{web.pid}/conferir").status_code == 405
    assert _p(web)["estado"] == "a_conferir"


def test_corrigir_com_os_campos_do_formulario(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={
        "ato": "Contestação", "dias": "5", "unidade": "uteis", "inicio": "2026-11-16"})
    p = _p(web)
    assert (p["ato"], p["dias"], p["inicio_informado"]) == ("Contestação", 5, "2026-11-16")
    assert p["fatal"] == "2026-11-23"  # 16, 17, 18, 19, 23 (20/11 é feriado)


def test_corrigir_pelo_codigo_da_lista(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"codigo": "ed_cpc"})
    assert _p(web)["ato"] == "Embargos de declaração" and _p(web)["dias"] == 5


def test_corrigir_com_dado_ruim_avisa_e_nao_muda(web):
    r = _post(web, f"/prazos/{web.pid}/corrigir", data={"dias": "muitos"})
    assert _mensagem(web, r).get("erro")
    assert _p(web)["dias"] == 15


def test_descartar_dobro_e_cumprido(web):
    _post(web, f"/prazos/{web.pid}/dobro", data={"ligado": "1"})
    assert _p(web)["dobro"] == 1
    _post(web, f"/prazos/{web.pid}/dobro", data={"ligado": "0"})
    assert _p(web)["dobro"] == 0
    _post(web, f"/prazos/{web.pid}/cumprido")
    assert _p(web)["estado"] == "cumprido"
    _post(web, f"/prazos/{web.pid}/desfazer-cumprido")
    assert _p(web)["estado"] == "a_conferir"
    _post(web, f"/prazos/{web.pid}/descartar")
    assert _p(web)["estado"] == "descartado"


def test_data_de_trabalho(web):
    _post(web, f"/prazos/{web.pid}/trabalho", data={"d": "2"})
    assert _p(web)["trabalho"] == "2026-11-26"
    _post(web, f"/prazos/{web.pid}/trabalho", data={"data": "2026-11-11"})
    assert _p(web)["trabalho"] == "2026-11-11"
    r = _post(web, f"/prazos/{web.pid}/trabalho", data={"data": "2026-12-31"})
    assert _mensagem(web, r).get("erro")
    assert _p(web)["trabalho"] == "2026-11-11"


def test_ler_de_novo(web):
    _post(web, f"/prazos/{web.pid}/ler-de-novo")
    assert _p(web)["estado"] == "aguardando_lex"


def test_prazo_desconhecido_avisa(web):
    r = web.post("/prazos/999/conferir")
    assert r.status_code == 302 and _mensagem(web, r).get("erro")


def test_voltar_so_para_dentro_do_site(web):
    r = _post(web, f"/prazos/{web.pid}/cumprido", data={"voltar": "https://fora.exemplo/"})
    assert r.headers["Location"].endswith(f"/prazos/{web.pid}")
    r = _post(web, f"/prazos/{web.pid}/desfazer-cumprido", data={"voltar": "/prazos"})
    assert r.headers["Location"].endswith("/prazos")


def test_configuracoes_padrao_e_dias(web):
    web.post("/configuracoes/prazos/padrao", data={"d": "2"})
    conn = banco.conectar(web.caminho)
    assert prazos_banco.d_padrao(conn) == 2
    conn.close()
    web.post("/configuracoes/prazos/dia", data={
        "data": "2026-11-10", "tribunal": "TJMT", "comarca": "", "tipo": "sem_expediente",
        "descricao": "Ponto facultativo", "fonte": "Portaria X"})
    assert _p(web)["fatal"] == "2026-12-01"
    assert "O calendário mudou" in _p(web)["alerta"]
    conn = banco.conectar(web.caminho)
    dia = conn.execute("SELECT id FROM dia_forense WHERE data = '2026-11-10' AND tribunal = 'TJMT'"
                       ).fetchone()[0]
    conn.close()
    web.post(f"/configuracoes/prazos/dia/{dia}/corrigir",
             data={"tipo": "reduzido", "descricao": "Até as 14h", "fonte": "Portaria X"})
    assert _p(web)["fatal"] == "2026-11-30"
    web.post(f"/configuracoes/prazos/dia/{dia}/desativar")
    assert _p(web)["fatal"] == "2026-11-30"
    conn = banco.conectar(web.caminho)
    assert conn.execute("SELECT COUNT(*) FROM dia_forense WHERE id = ?", (dia,)).fetchone()[0] == 1
    tipos = [e["tipo"] for e in eventos.listar(conn)]
    conn.close()
    assert {"dia_forense_incluido", "dia_forense_corrigido", "dia_forense_desativado"} <= set(tipos)


def test_arquivar_avisa_do_prazo_correndo(web):
    r = web.post("/carteira/arquivar", data={"numero": A})
    assert r.status_code == 302
    with web.session_transaction() as s:
        texto = " ".join(m for _, m in s["_flashes"])
    assert "1 prazo aberto: próximo 30/11" in texto and "Continuam na aba Prazos" in texto


ROTAS = ["/prazos/{id}/conferir", "/prazos/{id}/corrigir", "/prazos/{id}/descartar",
         "/prazos/{id}/trabalho", "/prazos/{id}/dobro", "/prazos/{id}/cumprido",
         "/prazos/{id}/desfazer-cumprido", "/prazos/{id}/fatal", "/prazos/{id}/ciencia",
         "/prazos/{id}/ler-de-novo", "/configuracoes/prazos/padrao", "/configuracoes/prazos/dia",
         "/configuracoes/prazos/dia/1/corrigir", "/configuracoes/prazos/dia/1/desativar"]


@pytest.mark.parametrize("rota", ROTAS)
def test_toda_acao_exige_csrf(web, rota):
    painel.app.config["CSRF_EXIGIDO"] = True
    r = web.post(rota.format(id=web.pid))
    assert r.status_code == 302
    assert _p(web)["estado"] == "a_conferir"
    with web.session_transaction() as s:
        assert any(painel.MENSAGEM_400 in m for _, m in s["_flashes"])


@pytest.mark.parametrize("rota", ROTAS)
def test_toda_acao_exige_login(web, rota, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    r = web.post(rota.format(id=web.pid))
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert _p(web)["estado"] == "a_conferir"


@pytest.mark.parametrize("rota", ROTAS)
def test_acoes_somem_no_modo_apresentacao(web, rota):
    with web.session_transaction() as s:
        s["apresentacao"] = True
    assert web.post(rota.format(id=web.pid)).status_code == 403
    assert _p(web)["estado"] == "a_conferir"


# --- rulings do controlador ------------------------------------------------------------


def _erros(c):
    with c.session_transaction() as s:
        return [m for cat, m in s.get("_flashes", []) if cat == "erro"]


def _oks(c):
    with c.session_transaction() as s:
        return [m for cat, m in s.get("_flashes", []) if cat == "ok"]


def _sql(c, sql, *args):
    conn = banco.conectar(c.caminho)
    try:
        with conn:
            return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_corrigir_so_manda_o_que_mudou(web):
    """O formulário completo, com campos iguais ao que já está, muda só o ato."""
    _post(web, f"/prazos/{web.pid}/corrigir", data={
        "ato": "Tréplica", "dias": "15", "unidade": "uteis", "inicio": "", "fatal_informada": "",
        "ciencia": "", "codigo": ""})
    p = _p(web)
    assert (p["ato"], p["dias"], p["fatal"]) == ("Tréplica", 15, "2026-11-30")
    assert not _erros(web)


def test_corrigir_sem_mudanca_avisa_e_nao_derruba_a_conferencia(web):
    _post(web, f"/prazos/{web.pid}/conferir")
    r = _post(web, f"/prazos/{web.pid}/corrigir", data={"ato": "Réplica", "dias": "15"})
    assert r.status_code == 302
    assert _erros(web)
    assert _p(web)["estado"] == "conferido"


def test_corrigir_com_data_fatal_informada_nao_pede_dias_de_novo(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"fatal_informada": "2026-12-04"})
    assert _p(web)["fatal"] == "2026-12-04" and _p(web)["fatal_informada"] == "2026-12-04"
    _post(web, f"/prazos/{web.pid}/corrigir", data={
        "ato": "Tréplica", "dias": "15", "unidade": "uteis", "fatal_informada": "2026-12-04"})
    assert not _erros(web)
    assert (_p(web)["ato"], _p(web)["fatal"]) == ("Tréplica", "2026-12-04")


def test_corrigir_dias_com_fatal_informada_e_recusado_com_a_mensagem(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"fatal_informada": "2026-12-04"})
    _post(web, f"/prazos/{web.pid}/corrigir", data={"dias": "10", "fatal_informada": "2026-12-04"})
    assert any("data fatal informada" in m for m in _erros(web))
    assert _p(web)["dias"] == 15


def test_unidade_invalida_e_recusada_mesmo_com_fatal_informada(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"fatal_informada": "2026-12-04"})
    antes = _p(web)
    r = _post(web, f"/prazos/{web.pid}/corrigir", data={"unidade": "semanas"})
    assert r.status_code == 302 and _erros(web)
    assert _p(web)["unidade"] == antes["unidade"]
    _post(web, f"/prazos/{web.pid}/corrigir", data={"unidade": "meses"})
    assert _p(web)["unidade"] == "meses"


def test_corrigir_passa_o_dia_de_hoje_do_tribunal(web, monkeypatch):
    from datetime import date
    visto = {}
    monkeypatch.setattr(painel.painel_dados, "hoje_cuiaba", lambda: date(2026, 11, 5))
    monkeypatch.setattr(painel, "PAINEL_USUARIO", "")
    monkeypatch.setattr(prazos_banco, "corrigir", lambda conn, pid, **kw: visto.update(kw))
    monkeypatch.setattr(prazos_banco, "informar_ciencia",
                        lambda conn, pid, data, **kw: visto.update(kw, ciencia=data))
    _post(web, f"/prazos/{web.pid}/corrigir", data={"ato": "Outro"})
    assert visto["hoje"] == date(2026, 11, 5) and visto["por"] == painel._por_quem()
    visto.clear()
    _post(web, f"/prazos/{web.pid}/ciencia", data={"data": "2026-11-04"})
    assert visto["hoje"] == date(2026, 11, 5) and visto["ciencia"] == "2026-11-04"


def test_ler_de_novo_em_processo_nao_e_meu_e_recusado(web):
    _sql(web, "UPDATE processo SET descartado_em = '2026-11-02', advogado_atua = 0")
    _post(web, f"/prazos/{web.pid}/ler-de-novo")
    assert any("Não é meu" in m for m in _erros(web))
    assert _p(web)["estado"] == "a_conferir"


def test_ler_de_novo_em_processo_arquivado_segue(web):
    _sql(web, "UPDATE processo SET descartado_em = '2026-11-02', advogado_atua = 1")
    _post(web, f"/prazos/{web.pid}/ler-de-novo")
    assert _p(web)["estado"] == "aguardando_lex"


@pytest.mark.parametrize("voltar", ["https://fora.exemplo/", "//fora.exemplo/x",
                                    "javascript:alert(1)", "/\\fora.exemplo", "/a\tb", ""])
def test_voltar_externo_cai_no_prazo(web, voltar):
    r = _post(web, f"/prazos/{web.pid}/cumprido", data={"voltar": voltar})
    assert r.headers["Location"].endswith(f"/prazos/{web.pid}")


def test_conferir_registra_quem_clicou(web, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_USUARIO", "dra.maria")
    _post(web, f"/prazos/{web.pid}/conferir")
    linhas = _sql(web, "SELECT dados FROM evento WHERE tipo = 'prazo_conferido'")
    assert len(linhas) == 1 and '"dra.maria"' in linhas[0][0]


def test_conferir_so_o_que_esta_esperando(web):
    _post(web, f"/prazos/{web.pid}/cumprido")
    _post(web, f"/prazos/{web.pid}/conferir")
    assert _erros(web) and _p(web)["estado"] == "cumprido"
    assert not _sql(web, "SELECT 1 FROM evento WHERE tipo = 'prazo_conferido'")


def test_conferir_no_modo_apresentacao_nem_pela_funcao_da_rota(web):
    """Mesmo que o filtro do modo falhe, a própria ação recusa."""
    with web.session_transaction() as s:
        s["apresentacao"] = True
    painel._INDISPONIVEIS_NA_APRESENTACAO.discard("prazo_conferir")
    try:
        r = _post(web, f"/prazos/{web.pid}/conferir")
    finally:
        painel._INDISPONIVEIS_NA_APRESENTACAO.add("prazo_conferir")
    assert r.status_code == 403 and _p(web)["estado"] == "a_conferir"


@pytest.mark.parametrize("erro", [RuntimeError("SEGREDO-DO-TEXTO"), KeyError("SEGREDO-DO-TEXTO"),
                                  sqlite3.OperationalError("database is locked SEGREDO-DO-TEXTO"),
                                  prazos_banco.TransacaoPerdida("SEGREDO-DO-TEXTO")])
def test_falha_inesperada_vira_mensagem_em_portugues_e_nada_muda(web, monkeypatch, erro):
    def quebra(conn, pid, **kw):
        conn.execute("UPDATE prazo SET ato = 'ESCRITO ANTES DE FALHAR' WHERE id = ?", (pid,))
        raise erro
    monkeypatch.setattr(prazos_banco, "corrigir", quebra)
    r = _post(web, f"/prazos/{web.pid}/corrigir", data={"ato": "Outro"})
    assert r.status_code == 302
    erros = _erros(web)
    assert erros and not any("SEGREDO" in m or "locked" in m for m in erros)
    assert _p(web)["ato"] == "Réplica"


def test_falha_inesperada_nas_rotas_do_calendario_tambem(web, monkeypatch):
    def quebra(conn, *a, **kw):
        raise RuntimeError("SEGREDO-DO-TEXTO")
    monkeypatch.setattr(prazos_banco, "definir_d_padrao", quebra)
    r = web.post("/configuracoes/prazos/padrao", data={"d": "1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/configuracoes/prazos")
    assert _erros(web) and not any("SEGREDO" in m for m in _erros(web))


def test_dia_do_calendario_diz_quantos_prazos_pedem_atencao(web):
    web.post("/configuracoes/prazos/dia", data={
        "data": "2026-11-10", "tribunal": "TJMT", "comarca": "", "tipo": "sem_expediente",
        "descricao": "Ponto facultativo", "fonte": "Portaria X"})
    assert any("1 prazo" in m and "atenção" in m for m in _oks(web))


def test_dia_sem_efeito_nos_prazos_nao_fala_de_atencao(web):
    web.post("/configuracoes/prazos/dia", data={
        "data": "2027-03-10", "tribunal": "TJMT", "comarca": "", "tipo": "sem_expediente",
        "descricao": "Ponto facultativo", "fonte": "Portaria X"})
    assert _oks(web) and not any("atenção" in m for m in _oks(web))


def test_dia_incluido_com_dado_ruim_avisa_e_nao_grava(web):
    web.post("/configuracoes/prazos/dia", data={
        "data": "2026-11-10", "tribunal": "TJMT", "tipo": "feriadao", "descricao": "X", "fonte": "Y"})
    assert _erros(web)
    assert not _sql(web, "SELECT 1 FROM dia_forense WHERE data = '2026-11-10'")


def test_refazer_as_datas_falhando_desfaz_o_dia_e_diz_isso(web, monkeypatch):
    """A conta dos prazos não saiu: o dia não fica pela metade (as datas estariam velhas)."""
    def quebra(conn):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(prazos_banco, "recalcular_abertos", quebra)
    r = web.post("/configuracoes/prazos/dia", data={
        "data": "2026-11-10", "tribunal": "TJMT", "comarca": "", "tipo": "sem_expediente",
        "descricao": "Ponto facultativo", "fonte": "Portaria X"})
    assert r.status_code == 302
    assert any("Nada foi alterado" in m for m in _erros(web))
    assert not _sql(web, "SELECT 1 FROM dia_forense WHERE data = '2026-11-10'")
    assert not _sql(web, "SELECT 1 FROM evento WHERE tipo = 'dia_forense_incluido'")
    assert _p(web)["fatal"] == "2026-11-30"


def test_arquivar_com_falha_no_aviso_arquiva_do_mesmo_jeito(web, monkeypatch):
    def quebra(conn, numeros, hoje):
        raise RuntimeError("SEGREDO-DO-TEXTO")
    monkeypatch.setattr(painel.prazos_tela, "aviso_de_arquivar", quebra)
    r = web.post("/carteira/arquivar", data={"numero": A})
    assert r.status_code == 302
    assert _sql(web, "SELECT arquivado_por_voce_em FROM processo")[0][0]
    with web.session_transaction() as s:
        texto = " ".join(m for _, m in s["_flashes"])
    assert "SEGREDO" not in texto and "Há prazo" not in texto


def test_arquivar_passa_o_dia_de_hoje_do_tribunal(web, monkeypatch):
    from datetime import date
    visto = []
    monkeypatch.setattr(painel.painel_dados, "hoje_cuiaba", lambda: date(2026, 11, 5))
    monkeypatch.setattr(painel.prazos_tela, "aviso_de_arquivar",
                        lambda conn, numeros, hoje: visto.append((numeros, hoje)) or "")
    web.post("/carteira/arquivar", data={"numero": A})
    assert visto == [([A], date(2026, 11, 5))]


def test_o_teor_do_pje_nunca_e_chamado_pelas_rotas():
    import re
    from pathlib import Path
    fonte = (Path(painel.__file__)).read_text(encoding="utf-8")
    assert not re.search(r"\.(consultarTeorComunicacao|confirmarRecebimento)\s*\(", fonte)


# --- fix round 1: a tela que o advogado viu ---------------------------------------------

MUDOU = "Este prazo mudou depois que você abriu a tela. Veja a data nova e confira de novo."
DIA_NOVO = {"data": "2026-11-10", "tribunal": "TJMT", "comarca": "", "tipo": "sem_expediente",
            "descricao": "Ponto facultativo", "fonte": "Portaria X"}


def _eventos(c, tipo):
    return _sql(c, "SELECT dados FROM evento WHERE tipo = ?", tipo)


def test_conferir_com_a_data_que_mudou_no_calendario_nao_confirma_a_data_nova(web):
    """A tela mostrava 30/11; o calendário levou para 01/12; a aba velha clica em Conferir."""
    velha = _token(web)
    web.post("/configuracoes/prazos/dia", data=DIA_NOVO)
    antes = _p(web)
    assert antes["fatal"] == "2026-12-01" and "O calendário mudou" in antes["alerta"]
    r = web.post(f"/prazos/{web.pid}/conferir", data={"versao": velha, "voltar": "/prazos"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/prazos/{web.pid}")
    assert MUDOU in _erros(web)
    depois = _p(web)
    assert (depois["estado"], depois["fatal"], depois["alerta"]) == ("a_conferir", "2026-12-01",
                                                                   antes["alerta"])
    assert not _eventos(web, "prazo_conferido")
    # com a tela nova, confere
    _post(web, f"/prazos/{web.pid}/conferir")
    assert _p(web)["estado"] == "conferido"


def test_nova_leitura_com_a_mesma_data_tambem_invalida_a_tela_velha(web):
    velha = _token(web)
    _sql(web, "UPDATE publicacao SET texto = ?",
         "Intimada para réplica no prazo de 15 (quinze) dias, apresentar réplica.")
    conn = banco.conectar(web.caminho)
    with conn:
        prazos_banco.ler_de_novo(conn, web.pid)
        prazos_banco.aplicar_leitura(conn, web.pid, json.dumps({"prazos": [{
            "ato": "Réplica", "dias": 15, "unidade": "uteis", "origem_dos_dias": "texto",
            "rito": "civel", "trecho": "prazo de 15 (quinze) dias, apresentar réplica",
            "fixado_pelo_juiz": False, "ressalvas": [], "natureza": "processual",
            "inicio_no_diario": True, "processo_suspenso": False}]}))
    conn.close()
    assert _p(web)["fatal"] == "2026-11-30" and _p(web)["estado"] == "a_conferir"
    web.post(f"/prazos/{web.pid}/conferir", data={"versao": velha})
    assert MUDOU in _erros(web)
    assert _p(web)["estado"] == "a_conferir"


def test_conferido_que_voltou_a_conferir_nao_e_conferido_de_novo_pela_aba_velha(web):
    aba_velha = _token(web)
    _post(web, f"/prazos/{web.pid}/conferir")
    assert _p(web)["estado"] == "conferido"
    depois_de_conferir = _token(web)
    conn = banco.conectar(web.caminho)
    with conn:  # alguém corrige com os mesmos números: a data é a mesma, o prazo volta a conferir
        prazos_banco.corrigir(conn, web.pid, dias=15, unidade="uteis", ato="Réplica")
    conn.close()
    assert _p(web)["estado"] == "a_conferir" and _p(web)["fatal"] == "2026-11-30"
    for versao in (aba_velha, depois_de_conferir):
        web.post(f"/prazos/{web.pid}/conferir", data={"versao": versao})
        assert MUDOU in _erros(web)
        assert _p(web)["estado"] == "a_conferir"
    assert len(_eventos(web, "prazo_conferido")) == 1


def test_duas_mudancas_seguidas_com_a_mesma_data_dao_versoes_diferentes(web):
    conn = banco.conectar(web.caminho)
    with conn:
        prazos_banco.corrigir(conn, web.pid, ato="Um")
        primeira = prazos_tela.versao_do_prazo(prazos_banco.obter(conn, web.pid))
        prazos_banco.corrigir(conn, web.pid, ato="Dois")
        segunda = prazos_tela.versao_do_prazo(prazos_banco.obter(conn, web.pid))
    conn.close()
    assert primeira != segunda


def test_pareamento_que_muda_a_linha_do_aviso_muda_o_momento_dela(web):
    conn = banco.conectar(web.caminho)
    with conn:
        conn.execute("INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, "
                     "data_disponibilizacao, visto_em, pendente) VALUES ('1grau', '9', ?, 'INT', "
                     "'2026-11-04', '2026-11-04T10:00:00-04:00', 1)", (A,))
        aviso = prazos_banco.abrir(conn, A, None, "aviso", "1grau:9", "2026-11-04")
        antes = prazos_banco.obter(conn, aviso)
        prazos_banco.parear_processo(conn, A)
        depois = prazos_banco.obter(conn, aviso)
    conn.close()
    assert antes["pareado_com"] != depois["pareado_com"]
    assert antes["atualizado_em"] != depois["atualizado_em"]


@pytest.mark.parametrize("rota,dados", [
    ("conferir", {}), ("corrigir", {"ato": "Outro"}), ("fatal", {"maior": "1"}),
    ("trabalho", {"d": "1"}), ("ciencia", {"data": "2026-11-04"}), ("dobro", {"ligado": "1"}),
    ("descartar", {}), ("cumprido", {}), ("desfazer-cumprido", {}), ("ler-de-novo", {})])
def test_acao_sem_a_versao_da_tela_e_recusada(web, rota, dados):
    antes = _p(web)
    r = web.post(f"/prazos/{web.pid}/{rota}", data={**dados, "voltar": "/prazos"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/prazos/{web.pid}")
    assert MUDOU in _erros(web)
    assert _p(web) == antes


@pytest.mark.parametrize("versao", ["", "lixo", "2026-11-30"])
def test_versao_errada_e_recusada(web, versao):
    antes = _p(web)
    web.post(f"/prazos/{web.pid}/conferir", data={"versao": versao})
    assert MUDOU in _erros(web) and _p(web) == antes


def test_formulario_velho_nao_apaga_o_que_mudou_depois(web):
    """A tela velha mandaria dias 10; quem corrigiu depois pôs 15 e a data fatal à mão."""
    web.post(f"/prazos/{web.pid}/corrigir", data={"versao": _token(web), "dias": "10"})
    velha = _token(web)
    conn = banco.conectar(web.caminho)
    with conn:
        prazos_banco.corrigir(conn, web.pid, fatal_informada="2026-12-04")
    conn.close()
    with web.session_transaction() as sessao:
        sessao.pop("_flashes", None)
    r = web.post(f"/prazos/{web.pid}/corrigir", data={
        "versao": velha, "ato": "Réplica", "dias": "15", "unidade": "uteis", "fatal_informada": ""})
    assert r.status_code == 302 and MUDOU in _erros(web)
    assert _p(web)["fatal_informada"] == "2026-12-04"
    assert not _oks(web) or "Prazo corrigido" not in " ".join(_oks(web))


def test_a_linha_e_a_tela_do_prazo_trazem_a_versao(web):
    conn = banco.conectar(web.caminho)
    try:
        hoje = date(2026, 11, 5)
        assert prazos_tela.detalhe(conn, web.pid, hoje)["versao"] == _token(web)
        achada = [l for g in prazos_tela.lista(conn, hoje).values() if isinstance(g, list)
                  for l in g if isinstance(l, dict) and l.get("id") == web.pid]
        assert achada and achada[0]["versao"] == _token(web)
    finally:
        conn.close()


# --- texto guardado nunca vai à tela ---------------------------------------------------


def test_data_guardada_que_nao_se_le_diz_uma_frase_fixa(web):
    sujo = "05/11/2026 <b>x</b> TEXTO-DA-PUBLICACAO"
    _sql(web, "UPDATE prazo SET disponibilizacao = ? WHERE id = ?", sujo, web.pid)
    for rota, dados in (("corrigir", {"dias": "10"}), ("dobro", {"ligado": "1"})):
        _post(web, f"/prazos/{web.pid}/{rota}", data=dados)
        erros = _erros(web)
        assert ("A data de disponibilização desta publicação não se lê. "
                "Informe a data fatal direto.") in erros, rota
        assert not any("TEXTO" in m or "Invalid" in m or "<b>" in m for m in erros)
    assert _p(web)["dias"] == 15 and _p(web)["dobro"] == 0


def test_valor_guardado_nao_aparece_na_mensagem_da_conta(web):
    _sql(web, "UPDATE prazo SET unidade = 'TEXTO-DA-PUBLICACAO' WHERE id = ?", web.pid)
    _post(web, f"/prazos/{web.pid}/corrigir", data={"dias": "10"})
    erros = _erros(web)
    assert erros and not any("TEXTO" in m for m in erros)


def test_data_guardada_ilegivel_na_escolha_da_fatal_e_do_dobro_nao_vaza(web):
    _sql(web, "UPDATE prazo SET estado = 'conferido', fatal_alternativa = '2026-12-04', "
              "disponibilizacao = 'TEXTO-DA-PUBLICACAO' WHERE id = ?", web.pid)
    _post(web, f"/prazos/{web.pid}/fatal", data={"maior": "1"})
    assert _erros(web) and not any("TEXTO" in m or "Invalid" in m for m in _erros(web))


# --- campos da correção -----------------------------------------------------------------


@pytest.mark.parametrize("ato", ["   ", "\t \u00a0 "])
def test_ato_em_branco_e_recusado(web, ato):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"ato": ato, "dias": "10"})
    assert _erros(web)
    assert (_p(web)["ato"], _p(web)["dias"]) == ("Réplica", 15)


def test_ato_do_formulario_pode_vir_junto_com_a_escolha_da_lista(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"ato": "", "codigo": "ed_cpc"})
    assert not _erros(web) and _p(web)["dias"] == 5


def test_horas_com_a_data_fatal_na_mesma_correcao_vale(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={
        "dias": "48", "unidade": "horas", "fatal_informada": "2026-11-09"})
    assert not _erros(web)
    p = _p(web)
    assert (p["dias"], p["unidade"], p["fatal"], p["fatal_informada"]) == (
        48, "horas", "2026-11-09", "2026-11-09")


def test_dias_com_data_fatal_na_mesma_correcao_sem_ser_horas_ou_meses_continua_recusado(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"dias": "10", "fatal_informada": "2026-11-09"})
    assert any("data fatal informada" in m for m in _erros(web))
    assert (_p(web)["dias"], _p(web)["fatal_informada"]) == (15, None)


def test_horas_com_data_fatal_ja_informada_continua_recusado(web):
    _post(web, f"/prazos/{web.pid}/corrigir", data={"fatal_informada": "2026-11-09"})
    _post(web, f"/prazos/{web.pid}/corrigir", data={"dias": "48", "unidade": "horas"})
    assert any("data fatal informada" in m for m in _erros(web))


@pytest.mark.parametrize("campo,valor", [
    ("fatal_informada", "1999-12-31"), ("fatal_informada", "2099-01-01"),
    ("inicio", "1999-12-31"), ("inicio", "2099-01-01"),
    ("ciencia", "1999-12-31"), ("ciencia", "2099-01-01")])
def test_datas_fora_do_alcance_sao_recusadas(web, campo, valor):
    _post(web, f"/prazos/{web.pid}/corrigir", data={campo: valor})
    assert _erros(web)
    assert _p(web)[{"inicio": "inicio_informado"}.get(campo, campo)] is None


def test_ciencia_no_futuro_numa_publicacao_e_recusada(web, monkeypatch):
    from datetime import date
    monkeypatch.setattr(painel.painel_dados, "hoje_cuiaba", lambda: date(2026, 11, 5))
    _post(web, f"/prazos/{web.pid}/corrigir", data={"ciencia": "2026-11-20"})
    assert any("futuro" in m for m in _erros(web))
    assert _p(web)["ciencia"] is None
    _post(web, f"/prazos/{web.pid}/corrigir", data={"ciencia": "2026-11-06"})
    assert _p(web)["ciencia"] == "2026-11-06"


@pytest.mark.parametrize("data", ["1999-12-31", "2099-01-01"])
def test_dia_do_calendario_fora_do_alcance_e_recusado(web, data):
    web.post("/configuracoes/prazos/dia", data={**DIA_NOVO, "data": data})
    assert _erros(web)
    assert not _sql(web, "SELECT 1 FROM dia_forense WHERE data = ?", data)


def test_tribunal_desconhecido_e_recusado(web):
    web.post("/configuracoes/prazos/dia", data={**DIA_NOVO, "tribunal": "TJXX"})
    assert _erros(web) and any("tribunal" in m.lower() for m in _erros(web))
    assert not _sql(web, "SELECT 1 FROM dia_forense WHERE tribunal = 'TJXX'")
    web.post("/configuracoes/prazos/dia", data={**DIA_NOVO, "tribunal": "tjmt"})
    assert _sql(web, "SELECT 1 FROM dia_forense WHERE tribunal = 'TJMT' AND data = '2026-11-10'")


def test_comarca_que_nao_casa_com_nada_entra_com_o_aviso(web):
    web.post("/configuracoes/prazos/dia", data={**DIA_NOVO, "comarca": "Lagoa da Prata"})
    assert _sql(web, "SELECT 1 FROM dia_forense WHERE comarca = 'Lagoa da Prata'")
    assert any("Lagoa da Prata" in m and "órgão julgador" in m for m in _oks(web))


def test_comarca_que_casa_com_um_processo_entra_sem_o_aviso(web):
    web.post("/configuracoes/prazos/dia", data={**DIA_NOVO, "comarca": "Cuiabá"})
    assert _sql(web, "SELECT 1 FROM dia_forense WHERE comarca = 'Cuiabá'")
    assert not any("órgão julgador" in m for m in _oks(web))


# --- repetir não muda nada --------------------------------------------------------------


def _quantos(c, tipo):
    return len(_eventos(c, tipo))


def test_dobro_repetido_nao_desfaz_a_conferencia(web):
    _post(web, f"/prazos/{web.pid}/conferir")
    _post(web, f"/prazos/{web.pid}/dobro", data={"ligado": "0"})
    assert _p(web)["estado"] == "conferido" and _quantos(web, "prazo_dobro") == 0
    assert any("Nada mudou" in m for m in _oks(web) + _erros(web))
    _post(web, f"/prazos/{web.pid}/dobro", data={"ligado": "1"})
    assert _p(web)["estado"] == "a_conferir" and _quantos(web, "prazo_dobro") == 1
    _post(web, f"/prazos/{web.pid}/dobro", data={"ligado": "1"})
    assert _quantos(web, "prazo_dobro") == 1


def test_descartar_duas_vezes_registra_uma(web):
    _post(web, f"/prazos/{web.pid}/descartar")
    _post(web, f"/prazos/{web.pid}/descartar")
    assert _quantos(web, "prazo_descartado") == 1
    assert any("Nada mudou" in m for m in _oks(web) + _erros(web))


def test_escolher_a_mesma_data_fatal_nao_diz_que_trocou(web):
    _sql(web, "UPDATE prazo SET fatal_alternativa = '2026-12-04', fatal_maior = 0 WHERE id = ?", web.pid)
    antes = _p(web)
    _post(web, f"/prazos/{web.pid}/fatal", data={"maior": "0"})
    assert _p(web) == antes and _quantos(web, "prazo_fatal_escolhida") == 0
    texto = " ".join(_oks(web) + _erros(web))
    assert "Nada mudou" in texto and "trocada" not in texto


# --- a transação -------------------------------------------------------------------------


def test_a_acao_trava_a_escrita_antes_de_ler(web, monkeypatch):
    """Ler antes do BEGIN deixa uma escrita de outro passar no meio e ser perdida pela gravação
    da linha inteira: a trava (BEGIN IMMEDIATE) vem antes da primeira leitura do prazo."""
    vistos = []
    original = banco.conectar
    versao = _token(web)

    def com_rastro(*a, **kw):
        conn = original(*a, **kw)
        conn.set_trace_callback(vistos.append)
        return conn
    monkeypatch.setattr(banco, "conectar", com_rastro)
    web.post(f"/prazos/{web.pid}/corrigir", data={"ato": "Outro", "versao": versao})
    sql = [v.strip().upper() for v in vistos]
    inicio = next(i for i, v in enumerate(sql) if v.startswith("BEGIN IMMEDIATE"))
    leitura = next(i for i, v in enumerate(sql) if "FROM PRAZO" in v)
    assert inicio < leitura, vistos
    assert _p(web)["ato"] == "Outro"


# --- telas -----------------------------------------------------------------------------

HOJE = date(2026, 11, 10)  # terça
B = "00099999920248110041"
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"
CSS = pathlib.Path(__file__).resolve().parent.parent / "static" / "style.css"


@pytest.fixture
def tela(web, monkeypatch):
    """O `web`, com o relógio do tribunal parado em HOJE (as telas mudam de grupo com a data).
    O contato do Lex usa o relógio de verdade: quem testa a conexão usa o `web` puro."""
    agora = datetime(2026, 11, 10, 12, 0, tzinfo=tribunal.fuso())
    monkeypatch.setattr(painel_dados, "agora_cuiaba", lambda: agora)
    assert painel_dados.hoje_cuiaba() == HOJE
    return web


def _texto(r):
    assert r.status_code == 200
    return r.get_data(as_text=True)


class _Forms(HTMLParser):
    """Os formulários de uma página como o navegador os enviaria: campos e valores de partida."""

    def __init__(self):
        super().__init__()
        self.forms, self._f, self._sel, self._opcoes = [], None, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._f = {"action": a.get("action", ""), "onsubmit": a.get("onsubmit", ""),
                       "campos": {}, "tipos": {}}
            self.forms.append(self._f)
        elif self._f is None:
            return
        elif tag == "input" and a.get("name"):
            if a.get("type") in ("radio", "checkbox") and "checked" not in a:
                return
            self._f["campos"][a["name"]] = a.get("value", "")
            self._f["tipos"][a["name"]] = a.get("type", "text")
        elif tag == "select" and a.get("name"):
            self._sel, self._opcoes = a["name"], []
        elif tag == "option" and self._sel:
            self._opcoes.append((a.get("value", ""), "selected" in a))

    def handle_endtag(self, tag):
        if tag == "select" and self._sel and self._f is not None:
            marcadas = [v for v, s in self._opcoes if s]
            self._f["campos"][self._sel] = marcadas[0] if marcadas else (
                self._opcoes[0][0] if self._opcoes else "")
            self._f["tipos"][self._sel] = "select"
            self._sel = None
        elif tag == "form":
            self._f = None


def _formularios(html_texto):
    p = _Forms()
    p.feed(html_texto)
    return p.forms


def _form(html_texto, termina_em, **tem):
    achados = [f for f in _formularios(html_texto) if f["action"].endswith(termina_em)
               and all(f["campos"].get(k) == v for k, v in tem.items())]
    assert achados, f"sem formulário {termina_em} {tem}"
    return achados[0]


def _enviar(c, form, **mudar):
    return c.post(form["action"], data={**form["campos"], **mudar})


def _outro_processo(c, numero=B):
    conn = banco.conectar(c.caminho)
    with conn:
        conn.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua, "
                     "cliente) VALUES (?, 'TJMT', '1ª Vara Cível de Cuiabá', 1, 'JOSÉ FICTÍCIO')",
                     (numero,))
    conn.close()


def _novo(c, ref, *, fatal, ato="Outro ato", conferir=False, numero=A):
    """Um prazo com a data fatal digitada (a conta não entra)."""
    conn = banco.conectar(c.caminho)
    with conn:
        conn.execute("INSERT OR IGNORE INTO publicacao (id, numero, tribunal, "
                     "data_disponibilizacao, texto) VALUES (?, ?, 'TJMT', '2026-11-05', 'x')",
                     (int(ref), numero))
        pid = prazos_banco.abrir(conn, numero, None, "publicacao", ref, "2026-11-05")
        prazos_banco.corrigir(conn, pid, ato=ato, unidade="horas", fatal_informada=fatal)
        if conferir:
            prazos_banco.conferir(conn, pid)
    conn.close()
    return pid


def _duas_datas(c):
    """Publicação e intimação do PJe no mesmo cartão, com datas diferentes."""
    _outro_processo(c)
    conn = banco.conectar(c.caminho)
    with conn:
        conn.execute("INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, "
                     "atualizada_em) VALUES (7, ?, 'autos', '2026-11-05', 'x', 'x')", (B,))
        conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                     "VALUES (7, ?, 'TJMT', '2026-11-05', 'x')", (B,))
        pub = prazos_banco.abrir(conn, B, 7, "publicacao", "7", "2026-11-05")
        prazos_banco.corrigir(conn, pub, ato="Contestação", dias=15, unidade="uteis")
        prazos_banco.conferir(conn, pub)
        conn.execute("INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, "
                     "data_disponibilizacao, visto_em, pendente) VALUES ('1grau', '9', ?, 'INT', "
                     "'2026-10-20', '2026-11-04T10:00:00-04:00', 1)", (B,))
        aviso = prazos_banco.abrir(conn, B, 7, "aviso", "1grau:9", "2026-10-20",
                                   estado="a_definir",
                                   motivo="Intimação no PJe, sem texto aqui: abra no PJe e "
                                          "preencha à mão.")
        prazos_banco.parear_processo(conn, B, prazos_banco.ALERTA_INTIMACAO)
    conn.close()
    return pub, aviso


def test_aba_prazos_lista(tela):
    t = _texto(tela.get("/prazos"))
    assert "A conferir" in t and "Réplica" in t and "30/11/2026" in t
    assert "0001234-56.2024.8.11.0041" in t and "MARIA FICTÍCIA" in t
    for grupo in ("Atrasados", "Hoje", "Esta semana", "Depois", "A definir"):
        assert grupo in t


@pytest.mark.parametrize("vista", ["semana", "mes"])
def test_aba_prazos_grades(tela, vista):
    t = _texto(tela.get(f"/prazos?vista={vista}&ref=2026-11-30"))
    assert "grade-dias" in t and "Réplica" in t and "dia-fatal" in t


def test_ref_ruim_nao_quebra(tela):
    assert tela.get("/prazos?vista=mes&ref=ontem").status_code == 200
    assert tela.get("/prazos?vista=outra").status_code == 200


def test_tela_de_conferir(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert "Preenchido por você, com a conta do sistema. Confira." in t
    assert "Sugestão do Lex" not in t  # os dias foram digitados à mão, não lidos pelo Lex
    assert "30/11/2026" in t and "dia 15" in t and "feriados municipais" in t
    for rotulo in ("Conferir", "Corrigir", "Não há prazo aqui", "D-0", "D-3", "Outro dia",
                   "Prazo em dobro", "Cumprido"):
        assert rotulo in t
    assert 'name="csrf"' in t


def test_prazo_desconhecido_e_404(tela):
    assert tela.get("/prazos/999").status_code == 404


def test_tela_de_prazo_a_definir_mostra_o_motivo(tela):
    conn = banco.conectar(tela.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    t = _texto(tela.get(f"/prazos/{pid}"))
    assert "O Lex ainda não leu" in t and "Conferir</button>" not in t


def test_menu_e_faixa_em_todas_as_paginas(tela):
    t = _texto(tela.get("/carteira"))
    assert 'href="/prazos"' in t and "faixa-prazos" in t and "1 a conferir" in t


def test_sem_prazos_nao_ha_faixa(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "vazio.db"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    t = _texto(painel.app.test_client().get("/carteira"))
    assert 'href="/prazos"' in t and "faixa-prazos" not in t


def test_telas_de_prazo_pedem_login(web, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    for rota in ("/prazos", f"/prazos/{web.pid}", "/configuracoes/prazos"):
        r = web.get(rota)
        assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_modo_apresentacao_esconde_cliente_trecho_e_acoes(tela):
    _sql(tela, "UPDATE prazo SET trecho = 'TRECHO SIGILOSO DA PUBLICAÇÃO' WHERE id = ?", tela.pid)
    with tela.session_transaction() as s:
        s["apresentacao"] = True
    for rota in ("/prazos", f"/prazos/{tela.pid}"):
        t = _texto(tela.get(rota))
        assert "MARIA FICTÍCIA" not in t and "TRECHO SIGILOSO" not in t
        assert f"/prazos/{tela.pid}/conferir" not in t and f"/prazos/{tela.pid}/cumprido" not in t
    t = _texto(tela.get("/configuracoes/prazos"))
    assert "/configuracoes/prazos/dia" not in t


def test_configuracoes_de_prazos(tela):
    t = _texto(tela.get("/configuracoes/prazos?tribunal=TJMT&ano=2026"))
    assert "Calendário forense" in t and "Portaria TJMT/PRES 1915/2025" in t
    assert "18/02/2026" in t and "Expediente reduzido" in t
    assert 'action="/configuracoes/prazos/dia"' in t and 'action="/configuracoes/prazos/padrao"' in t


def test_lembrete_do_calendario_a_partir_de_novembro(web, monkeypatch):
    monkeypatch.setattr(painel_dados, "hoje_cuiaba", lambda: date(2026, 11, 3))
    assert "Falta cadastrar o calendário de 2027" in _texto(web.get("/configuracoes/prazos"))
    monkeypatch.setattr(painel_dados, "hoje_cuiaba", lambda: date(2026, 10, 3))
    assert "Falta cadastrar" not in _texto(web.get("/configuracoes/prazos"))


def test_quadro_mostra_o_selo_e_a_confirmacao_avisa_do_prazo(tela):
    _sql(tela, "INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
               "VALUES (5, ?, 'chegou', '2026-11-05', 'x', 'x')", A)
    _sql(tela, "UPDATE prazo SET demanda_id = 5 WHERE id = ?", tela.pid)
    t = _texto(tela.get("/"))
    assert "Prazo a conferir" in t
    frase = "1 prazo aberto: próximo 30/11. Continuam na aba Prazos."
    assert frase in t
    assert frase in _texto(tela.get(f"/processo/{A}"))


# --- o que as telas prometem além do básico ---------------------------------------------


def test_proximos_antes_dos_vencidos_e_o_urgente_vem_marcado(tela):
    for i in range(3):
        _novo(tela, str(10 + i), fatal=f"2026-10-0{i + 2}", ato=f"Velho {i}")
    _novo(tela, "20", fatal="2026-11-11", ato="Urgente hoje")  # quarta: dentro do limite
    _novo(tela, "21", fatal="2026-12-20", ato="Longe")
    t = _texto(tela.get("/prazos"))
    assert (t.index("Urgente hoje") < t.index("Longe") < t.index("Vencidos a conferir")
            < t.index("Velho 2") < t.index("Velho 0"))  # o vencido mais recente primeiro
    linha = t[t.index("Urgente hoje"):t.index("Longe")]
    assert "Vence logo" in linha
    assert "Vence logo" not in t[t.index("Longe"):t.index("Vencidos a conferir")]
    assert "Atrasado" in t[t.index("Velho 2"):]


def test_toda_acao_sobre_o_prazo_leva_a_versao_que_a_tela_mostrou(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    versao = _token(tela)
    vistos = set()
    for f in _formularios(t):
        acao = f["action"].rsplit("/", 1)[-1]
        if acao in ("conferir", "corrigir", "trabalho", "dobro", "fatal", "ciencia", "descartar",
                    "cumprido", "ler-de-novo", "desfazer-cumprido"):
            assert f["campos"].get("versao") == versao, f["action"]
            vistos.add(acao)
    assert vistos >= {"conferir", "corrigir", "trabalho", "dobro", "descartar", "cumprido",
                      "ler-de-novo"}


def test_a_escolha_da_data_maior_e_a_ciencia_tambem_levam_a_versao(tela):
    pub, aviso = _duas_datas(tela)
    for pid, acao in ((pub, "fatal"), (aviso, "ciencia")):
        t = _texto(tela.get(f"/prazos/{pid}"))
        assert _form(t, f"/prazos/{pid}/{acao}")["campos"]["versao"] == _token(tela, pid)


def test_conferir_pelo_formulario_da_tela(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert _p(tela)["estado"] == "a_conferir"
    r = _enviar(tela, _form(t, f"/prazos/{tela.pid}/conferir"))
    assert r.status_code == 302 and _p(tela)["estado"] == "conferido"


def test_nada_chega_a_conferido_sem_o_clique(tela):
    for rota in ("/prazos", f"/prazos/{tela.pid}", "/carteira", "/configuracoes/prazos"):
        _texto(tela.get(rota))
    assert _p(tela)["estado"] == "a_conferir"
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert "autofocus" not in t


def test_o_formulario_de_corrigir_traz_o_ato_e_o_dobro_explicito(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    f = _form(t, f"/prazos/{tela.pid}/corrigir")
    assert f["campos"]["ato"] == "Réplica" and f["campos"]["dobro"] == "0"
    assert f["campos"]["dias"] == "15"
    # enviado como veio, nada muda: a tela diz isso e o prazo fica como estava
    antes = _p(tela)
    r = _enviar(tela, f)
    assert "Nada foi alterado" in str(_mensagem(tela, r))
    assert _p(tela) == antes
    # mudou só os dias: o ato e o dobro seguem
    _enviar(tela, f, dias="7")
    p = _p(tela)
    assert (p["dias"], p["ato"], p["dobro"]) == (7, "Réplica", 0)
    # o dobro vai escrito, sem depender de caixa marcada
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    _enviar(tela, _form(t, f"/prazos/{tela.pid}/corrigir"), dobro="1")
    assert _p(tela)["dobro"] == 1
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert _form(t, f"/prazos/{tela.pid}/corrigir")["campos"]["dobro"] == "1"


def test_a_data_sem_a_suspensao_aparece_ao_lado_com_o_rotulo(tela):
    _sql(tela, "INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
               "VALUES (3, ?, 'TJMT', '2026-12-18', 'x')", A)
    conn = banco.conectar(tela.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "3", "2026-12-18")
        prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="corridos")
    d = prazos_tela.detalhe(conn, pid, HOJE)
    conn.close()
    assert d["fatal_sem_suspensao_rotulo"]
    assert str(escape(d["fatal_sem_suspensao_rotulo"])) in _texto(tela.get(f"/prazos/{pid}"))
    assert "Sem a suspensão" not in _texto(tela.get(f"/prazos/{tela.pid}"))


def test_dias_da_lei_mostram_o_ato_e_o_artigo_da_lista_ao_lado(tela):
    legal = prazos_legais.PRAZOS["contestacao"]
    conn = banco.conectar(tela.caminho)
    with conn:
        prazos_banco.corrigir(conn, tela.pid, codigo="contestacao")
        prazos_banco.corrigir(conn, tela.pid, ato="Peça lida pelo Lex")
    conn.close()
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    # o ato mudou à mão: a origem deixa de ser a lei e o artigo some
    assert legal.artigo not in t.split("Corrigir")[0]
    _sql(tela, "UPDATE prazo SET ato = 'Peça lida pelo Lex', origem_dos_dias = 'lei', "
               "codigo_ato = 'contestacao' WHERE id = ?", tela.pid)
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert "Dias da lei" in t and legal.artigo in t and "Peça lida pelo Lex" in t
    assert str(escape(legal.ato)) in t


def test_data_digitada_nao_mostra_artigo_ao_lado(tela):
    legal = prazos_legais.PRAZOS["contestacao"]
    conn = banco.conectar(tela.caminho)
    with conn:
        prazos_banco.corrigir(conn, tela.pid, codigo="contestacao")
        prazos_banco.corrigir(conn, tela.pid, fatal_informada="2026-12-01")
    conn.close()
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert "Data fatal informada por você" in t
    corpo = t[t.index("<main"):t.index("</main>")]
    assert legal.artigo not in corpo.split('id="corrigir"')[0]


def test_aguardando_o_lex_depende_do_lex_conectado(web):
    tela = web  # o relógio de verdade: o contato do Lex é de agora
    conn = banco.conectar(tela.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    sem = "O Lex não está lendo agora: preencha à mão ou confira a conexão do Lex"
    for rota in (f"/prazos/{pid}", "/prazos"):
        t = _texto(tela.get(rota))
        assert sem in t and "Aguardando o Lex" not in t
    assert f"/prazos/{pid}/corrigir" in _texto(tela.get(f"/prazos/{pid}"))  # sempre dá para preencher
    conn = banco.conectar(tela.caminho)
    with conn:
        ponte.registrar_contato_do_leitor(conn)  # o leitor consulta a fila: o contato fica recente
    conn.close()
    for rota in (f"/prazos/{pid}", "/prazos"):
        t = _texto(tela.get(rota))
        assert "Aguardando o Lex" in t and sem not in t
    conn = banco.conectar(tela.caminho)
    with conn:
        ponte.registrar_contato(conn, claude_ok=False)  # conectado, mas sem o plano
    conn.close()
    assert sem in _texto(tela.get(f"/prazos/{pid}"))


def test_f1_so_o_mac_do_squad_nao_abre_o_portao_da_tela_de_prazos(web):
    conn = banco.conectar(web.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
        ponte.registrar_contato(conn, claude_ok=True)
    conn.close()
    sem = "O Lex não está lendo agora: preencha à mão ou confira a conexão do Lex"
    for rota in (f"/prazos/{pid}", "/prazos"):
        assert sem in _texto(web.get(rota))


def _trava_com_conexoes_rapidas(c, monkeypatch):
    original = banco.conectar

    def rapido(*a, **k):
        conn = original(*a, **k)
        conn.execute("PRAGMA busy_timeout = 50")
        return conn
    trava = original(c.caminho)
    monkeypatch.setattr(banco, "conectar", rapido)
    trava.execute("BEGIN IMMEDIATE")
    return trava


@pytest.mark.parametrize("coluna, para", [("protocolado", "revisao"), ("chegou", "acao")],
                         ids=["saindo-de-protocolado", "outra-coluna"])
def test_f8_mover_com_o_banco_ocupado_diz_a_frase_e_nao_derruba(web, monkeypatch, coluna, para):
    _sql(web, "INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
              "VALUES (5, ?, ?, '2026-11-05', 'x', 'x')", A, coluna)
    trava = _trava_com_conexoes_rapidas(web, monkeypatch)
    try:
        r = web.post("/demanda/5/mover", data={"para": para},
                     headers={"Accept": "application/json"})
    finally:
        trava.rollback()
        trava.close()
    assert r.status_code == 409 and r.get_json()["erro"] == painel.MSG_BANCO_OCUPADO
    assert _sql(web, "SELECT coluna FROM demanda WHERE id = 5")[0][0] == coluna


def test_ler_de_novo_avisa_antes_e_some_quando_nao_vale(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    f = _form(t, f"/prazos/{tela.pid}/ler-de-novo")
    assert "confirm(" in f["onsubmit"] and "à mão" in f["onsubmit"]
    conn = banco.conectar(tela.caminho)
    with conn:
        espera = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    assert "/ler-de-novo" not in _texto(tela.get(f"/prazos/{espera}"))  # ainda nem foi lido
    _sql(tela, "UPDATE processo SET descartado_em = '2026-11-01', advogado_atua = 0 "
               "WHERE numero = ?", A)
    assert "/ler-de-novo" not in _texto(tela.get(f"/prazos/{tela.pid}"))


def test_texto_do_modelo_do_diario_e_do_usuario_sai_escapado(tela):
    _sql(tela, "UPDATE prazo SET ato = ?, trecho = ?, motivo = ?, alerta = ?, ressalvas = ?, "
               "calculo = ? WHERE id = ?", "<script>alert(1)</script>", "<img src=x onerror=alert(2)>",
         "<b>motivo</b>", "<u>alerta</u>", json.dumps(["<i>ressalva</i>"]),
         json.dumps({"avisos": ["<s>aviso</s>"], "passos": ["<em>passo</em>"]}), tela.pid)
    _sql(tela, "UPDATE processo SET cliente = '<marquee>CLIENTE</marquee>' WHERE numero = ?", A)
    _sql(tela, "UPDATE publicacao SET link = 'javascript:alert(3)' WHERE id = 1")
    for rota in (f"/prazos/{tela.pid}", "/prazos", "/prazos?vista=semana&ref=2026-11-30",
                 "/prazos?vista=mes&ref=2026-11-30"):
        t = _texto(tela.get(rota))
        for cru in ("<script>alert(1)", "<img src=x", "<b>motivo", "<u>alerta", "<i>ressalva",
                    "<s>aviso", "<em>passo", "<marquee>", "javascript:alert"):
            assert cru not in t, (rota, cru)
    assert "&lt;script&gt;alert(1)" in _texto(tela.get(f"/prazos/{tela.pid}"))


def test_as_telas_novas_nao_usam_html_cru_nem_numero_de_lei():
    nova = ("prazos.html", "prazo.html", "configuracoes_prazos.html")
    # «um dia» fica de fora: é o artigo («Incluir um dia»), não uma quantidade de prazo
    padrao = re.compile(r"\b(\d+|dois|duas|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|"
                        r"quinze|vinte|trinta)\s+(dias?|horas?)\b", re.IGNORECASE)
    for nome in nova:
        fonte = (TEMPLATES / nome).read_text(encoding="utf-8")
        assert "|safe" not in fonte and "nota_md" not in fonte and "|md" not in fonte, nome
        assert "autoescape false" not in fonte, nome
        assert not padrao.search(fonte), (nome, padrao.search(fonte).group(0))


def test_duas_datas_mostram_as_duas_e_a_maior_so_por_escolha(tela):
    pub, _ = _duas_datas(tela)
    p = _p(tela, pub)
    menor, maior = p["fatal"], p["fatal_alternativa"]
    assert menor < maior
    t = _texto(tela.get(f"/prazos/{pub}"))
    assert prazos.dia_br(date.fromisoformat(menor)) in t
    assert prazos.dia_br(date.fromisoformat(maior)) in t
    assert "Vale a que vence primeiro" in t and "Usar a maior" in t and "arriscado" in t
    assert "Chegou intimação" in t  # o alerta fica junto da data
    assert prazos_banco.AVISO_DUAS_DATAS in t
    f = _form(t, f"/prazos/{pub}/fatal")
    assert f["campos"]["maior"] == "1"
    _enviar(tela, f)
    assert _p(tela, pub)["fatal_maior"] == 1
    t = _texto(tela.get(f"/prazos/{pub}"))
    assert "Você escolheu a data maior" in t and "Voltar à que vence primeiro" in t
    assert _form(t, f"/prazos/{pub}/fatal")["campos"]["maior"] == "0"
    assert _p(tela, pub)["estado"] == "a_conferir"  # a escolha não é conferir


def test_aviso_do_pje_pede_o_dia_da_ciencia(tela):
    pub, aviso = _duas_datas(tela)
    t = _texto(tela.get(f"/prazos/{aviso}"))
    f = _form(t, f"/prazos/{aviso}/ciencia")
    assert f["tipos"]["data"] == "date" and "csrf" in f["campos"]
    assert "ciencia" not in _texto(tela.get(f"/prazos/{pub}")).lower().replace("ciência", "")
    assert f"/prazos/{pub}/ciencia" not in _texto(tela.get(f"/prazos/{pub}"))
    # a intimação coberta leva ao prazo da publicação
    assert f"/prazos/{pub}" in t


def test_o_prazo_ja_cumprido_nao_oferece_corrigir(tela):
    _post(tela, f"/prazos/{tela.pid}/cumprido")
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert f"/prazos/{tela.pid}/desfazer-cumprido" in t
    assert f"/prazos/{tela.pid}/corrigir" not in t and f"/prazos/{tela.pid}/conferir" not in t


def test_data_que_nao_se_le_fica_visivel_e_deixa_digitar_a_data(tela):
    ruim = _novo(tela, "30", fatal="2026-11-14", ato="Data torta", conferir=True)
    por_conferir = _novo(tela, "31", fatal="2026-11-14", ato="Outra torta")
    for pid in (ruim, por_conferir):
        _sql(tela, "UPDATE prazo SET fatal = '14/11/2026', trabalho = '14/11/2026' WHERE id = ?", pid)
    t = _texto(tela.get("/prazos"))
    assert "Data torta" in t and "Outra torta" in t and "14/11/2026" in t
    assert "Data ilegível" in t
    for pid in (ruim, por_conferir):
        d = _texto(tela.get(f"/prazos/{pid}"))
        f = _form(d, f"/prazos/{pid}/corrigir")
        assert "fatal_informada" in f["campos"]
        assert f"/prazos/{pid}/conferir" not in d  # ninguém confere o que não consegue ler
    # digitada a data, o prazo volta a poder ser conferido
    d = _texto(tela.get(f"/prazos/{por_conferir}"))
    _enviar(tela, _form(d, f"/prazos/{por_conferir}/corrigir"), fatal_informada="2026-11-16",
            unidade="horas")
    assert f"/prazos/{por_conferir}/conferir" in _texto(tela.get(f"/prazos/{por_conferir}"))
    # a vista do mês também abre
    assert tela.get("/prazos?vista=mes&ref=2026-11-14").status_code == 200


def test_o_alcance_dos_cartoes_roda_ao_abrir_a_aba_e_nunca_derruba_a_pagina(tela, monkeypatch, caplog):
    chamadas = []

    def estraga(conn):
        chamadas.append(1)
        raise RuntimeError("TEXTO SIGILOSO DA PUBLICAÇÃO")
    monkeypatch.setattr(prazos_banco, "criar_dos_cartoes_abertos", estraga)
    with caplog.at_level("DEBUG"):
        assert tela.get("/prazos").status_code == 200
    assert len(chamadas) == 1
    assert "SIGILOSO" not in caplog.text
    tela.get(f"/prazos/{tela.pid}")
    tela.get("/configuracoes/prazos")
    assert len(chamadas) == 1  # as outras telas só leem


def test_o_alcance_dos_cartoes_nao_roda_no_modo_apresentacao(tela, monkeypatch):
    chamadas = []
    monkeypatch.setattr(prazos_banco, "criar_dos_cartoes_abertos",
                        lambda conn: chamadas.append(1) or 0)
    with tela.session_transaction() as s:
        s["apresentacao"] = True
    assert tela.get("/prazos").status_code == 200
    assert chamadas == []


def test_a_aba_cria_o_prazo_do_cartao_aberto_uma_vez(tela):
    _outro_processo(tela)
    _sql(tela, "INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
               "VALUES (8, ?, 'autos', '2026-11-05', 'x', 'x')", B)
    _sql(tela, "INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
               "VALUES (8, ?, 'TJMT', '2026-11-05', 'x')", B)
    _sql(tela, "INSERT INTO demanda_item (demanda_id, tipo, ref, data) VALUES (8, 'publicacao', "
               "'8', '2026-11-05')")
    for _ in range(2):
        _texto(tela.get("/prazos"))
    conn = banco.conectar(tela.caminho)
    n = conn.execute("SELECT COUNT(*) FROM prazo WHERE origem_ref = '8'").fetchone()[0]
    conn.close()
    assert n == 1


def test_a_grade_rola_dentro_do_proprio_quadro_no_celular(tela):
    t = _texto(tela.get("/prazos?vista=mes&ref=2026-11-30"))
    assert 'class="grade-rolagem"' in t
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.grade-rolagem\s*\{[^}]*overflow-x:\s*auto", css)
    assert re.search(r"\.grade-dias\s*\{[^}]*min-width", css)


def test_confirmacao_do_cartao_le_bem_com_o_prazo_atrasado(tela):
    _sql(tela, "INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
               "VALUES (5, ?, 'chegou', '2026-11-05', 'x', 'x')", A)
    _sql(tela, "UPDATE prazo SET demanda_id = 5 WHERE id = ?", tela.pid)
    _novo(tela, "40", fatal="2026-11-02", ato="Velho")
    _sql(tela, "UPDATE prazo SET estado = 'cumprido' WHERE id = ?", tela.pid)
    t = _texto(tela.get("/"))
    assert "1 prazo aberto: 1 atrasado desde 02/11. Continuam na aba Prazos." in t


def test_f5_confirmacao_avisa_quem_so_tem_prazo_sem_data(tela):
    _sql(tela, "INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
               "VALUES (5, ?, 'chegou', '2026-11-05', 'x', 'x')", A)
    _sql(tela, "UPDATE prazo SET estado = 'cumprido' WHERE id = ?", tela.pid)
    conn = banco.conectar(tela.caminho)
    with conn:
        prazos_banco.abrir(conn, A, 5, "publicacao", "2", "2026-11-05")
    conn.close()
    frase = "1 prazo aberto: 1 sem data. Continuam na aba Prazos."
    for rota in ("/", f"/processo/{A}"):
        form = _Forms()
        form.feed(_texto(tela.get(rota)))
        confirmacoes = [f["onsubmit"] for f in form.forms if "arquivar" in f["action"]]
        assert confirmacoes and frase in confirmacoes[0]
        assert confirmacoes[0].count("'") == 2  # nada abre ou fecha a frase do confirm antes da hora


def test_f5_confirmacao_nao_leva_nome_de_ato_nem_de_cliente(tela):
    _sql(tela, "INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
               "VALUES (5, ?, 'chegou', '2026-11-05', 'x', 'x')", A)
    _sql(tela, "UPDATE prazo SET ato = 'D''Ávila <b>' WHERE id = ?", tela.pid)
    form = _Forms()
    form.feed(_texto(tela.get(f"/processo/{A}")))
    confirmacao = [f["onsubmit"] for f in form.forms if "arquivar" in f["action"]][0]
    assert "vila" not in confirmacao and "MARIA" not in confirmacao


def test_configuracoes_mostram_origem_fonte_e_dias_fora_da_conta(tela):
    conn = banco.conectar(tela.caminho)
    with conn:
        dia, _ = prazos_banco.incluir_dia(conn, "2027-02-08", "TJMT", "", "sem_expediente",
                                          "Carnaval", "Portaria Y", origem="ato")
        prazos_banco.desativar_dia(conn, dia)
    conn.close()
    t = _texto(tela.get("/configuracoes/prazos?tribunal=TJMT&ano=2027"))
    linha = t[t.index("08/02/2027"):]
    linha = linha[:linha.index("</tr>")]
    assert "fora da conta" in linha.lower() and "Portaria Y" in linha and "Ato do tribunal" in linha
    assert "Origem" in t
    t = _texto(tela.get("/configuracoes/prazos?tribunal=TJMT&ano=2026"))
    assert "Lei" in t and "Fonte" in t


def test_configuracoes_com_tribunal_ou_ano_estranho_nao_quebram(tela):
    for q in ("tribunal=XXX", "tribunal=%3Cb%3E&ano=abc", "ano=99999999999", "ano=-5",
              "tribunal=&ano=2026"):
        assert tela.get(f"/configuracoes/prazos?{q}").status_code == 200
    assert "<b>" not in _texto(tela.get("/configuracoes/prazos?tribunal=%3Cb%3E"))


# --- rodada 1 de correções da Task 11 ----------------------------------------------------

MARCA = "SEGREDO-XYZ"
FORA = "FORA-XYZ"  # texto do processo e da publicação (o Cockpit antigo os mostra)
SEM_MASCARA = lambda texto, *a, **k: texto  # noqa: E731 - mostra o que a tela escreve, sem a máscara


def _principal(t):
    return t[t.index("<main"):t.index("</main>")]


def test_apresentacao_nao_mostra_texto_livre_em_tela_nenhuma(tela, monkeypatch):
    """C1: ato, motivo, alerta, ressalvas, trecho e avisos nem são escritos na página (a máscara
    por nomes conhecidos não alcança texto livre). Roda sem a máscara para provar isso."""
    monkeypatch.setattr(painel.apresentacao, "mascarar_html", SEM_MASCARA)
    pub, aviso = _duas_datas(tela)
    com_codigo = _novo(tela, "50", fatal="2026-11-18", ato=f"{MARCA} ato com código")
    _sql(tela, "UPDATE prazo SET codigo_ato = 'contestacao' WHERE id = ?", com_codigo)
    calculo = json.dumps({"avisos": [f"{MARCA} aviso"], "passos": ["Disponibilização: qui"],
                          "alternativa": {"passos": ["Vencimento: ter"],
                                          "avisos": [f"{MARCA} aviso da outra"]}})
    for pid in (tela.pid, pub, aviso, com_codigo):
        _sql(tela, "UPDATE prazo SET motivo = ?, alerta = ?, trecho = ?, ressalvas = ?, "
                   "calculo = CASE WHEN id = ? THEN ? ELSE calculo END WHERE id = ?",
             f"{MARCA} motivo", f"{MARCA} alerta", f"{MARCA} trecho",
             json.dumps([f"{MARCA} ressalva"]), tela.pid, calculo, pid)
    _sql(tela, "UPDATE prazo SET ato = ? WHERE id IN (?, ?)", f"{MARCA} ato", tela.pid, pub)
    _sql(tela, "UPDATE processo SET cliente = ?, parte_contraria = ?, orgao_julgador = ?",
         f"{FORA} cliente", f"{FORA} contrária", f"{FORA} vara")
    _sql(tela, "UPDATE publicacao SET orgao = ?, link = ?, texto = ?",
         f"{FORA} órgão", f"https://exemplo.test/{FORA}", f"{FORA} texto")
    with tela.session_transaction() as s:
        s["apresentacao"] = True
    telas = ["/prazos", "/prazos?vista=semana&ref=2026-11-18", "/prazos?vista=mes&ref=2026-11-18",
             "/prazos?vista=semana&ref=2026-11-30", "/configuracoes/prazos"]
    telas += [f"/prazos/{pid}" for pid in (tela.pid, pub, aviso, com_codigo)]
    for rota in telas:
        t = _texto(tela.get(rota))
        assert MARCA not in t and FORA not in t, rota
        assert "FICTÍCI" not in t, rota  # nem o nome do cliente
    # o Cockpit é de antes: o que é do prazo não aparece nele
    assert MARCA not in _texto(tela.get("/"))
    d = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert "Este prazo tem uma observação" in d
    assert prazos_legais.PRAZOS["contestacao"].ato in _texto(tela.get(f"/prazos/{com_codigo}"))
    assert "<h1>Prazo</h1>" in d  # sem código da lista, o ato não aparece
    # fora do modo, a mesma tela mostra tudo
    with tela.session_transaction() as s:
        s["apresentacao"] = False
    assert f"{MARCA} ato" in _texto(tela.get(f"/prazos/{tela.pid}"))


def test_para_apresentacao_deixa_passar_so_o_que_esta_na_lista(tela):
    conn = banco.conectar(tela.caminho)
    d = prazos_tela.detalhe(conn, tela.pid, HOJE)
    conn.close()
    d["campo_novo_com_texto_livre"] = MARCA
    d["cliente"], d["trecho"], d["motivo"] = f"{MARCA}1", f"{MARCA}2", f"{MARCA}3"
    seguro = prazos_tela.para_apresentacao(d)
    assert MARCA not in repr(seguro)
    assert seguro["cliente"] == "" and seguro["trecho"] == "" and seguro["ressalvas"] == []
    assert seguro["motivo"] == "Este prazo tem uma observação"
    assert seguro["ato"] == "Prazo" and seguro["fatal_br"] == d["fatal_br"]


def test_modo_apresentacao_esconde_o_cliente_sem_depender_da_mascara(tela, monkeypatch):
    monkeypatch.setattr(painel.apresentacao, "mascarar_html", SEM_MASCARA)
    with tela.session_transaction() as s:
        s["apresentacao"] = True
    for rota in ("/prazos", f"/prazos/{tela.pid}"):
        assert "MARIA FICTÍCIA" not in _texto(tela.get(rota)), rota
    with tela.session_transaction() as s:
        s["apresentacao"] = False
    assert "MARIA FICTÍCIA" in _texto(tela.get("/prazos"))


# I1 -------------------------------------------------------------------------------------


def test_nao_ha_prazo_aqui_pede_confirmacao_e_leva_a_versao(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    f = _form(t, f"/prazos/{tela.pid}/descartar")
    assert "confirm(" in f["onsubmit"] and f["campos"]["versao"] == _token(tela)


def test_formulario_velho_nao_descarta_prazo_que_mudou(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    f = _form(t, f"/prazos/{tela.pid}/descartar")
    _post(tela, f"/prazos/{tela.pid}/corrigir", data={"dias": "7"})  # a data mudou depois
    r = _enviar(tela, f)
    assert r.status_code == 302 and MUDOU in _erros(tela)
    assert _p(tela)["estado"] == "a_conferir"


# I2 -------------------------------------------------------------------------------------


def test_na_lista_so_o_conferido_tem_cumprido_e_a_mensagem_leva_ao_desfazer(tela):
    conferido = _novo(tela, "60", fatal="2026-11-20", ato="Já conferido", conferir=True)
    t = _texto(tela.get("/prazos"))
    assert f"/prazos/{conferido}/cumprido" in t
    assert f"/prazos/{tela.pid}/cumprido" not in t  # a conferir: pelo prazo, com confirmação
    f = _form(t, f"/prazos/{conferido}/cumprido")
    assert f["campos"]["versao"] == _token(tela, conferido)
    _enviar(tela, f)
    with tela.session_transaction() as s:
        textos = [m for _, m in s["_flashes"]]
    assert any(f'href="/prazos/{conferido}"' in str(m) for m in textos)
    # a mensagem da lista é lida como HTML só no link; o resto sai escapado
    t = _texto(tela.get("/prazos"))
    assert f'href="/prazos/{conferido}"' in t and "Prazo cumprido" in t
    assert _form(_texto(tela.get(f"/prazos/{conferido}")), f"/prazos/{conferido}/desfazer-cumprido")


def test_cumprido_de_prazo_ainda_nao_conferido_pede_confirmacao(tela):
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert "confirm(" in _form(t, f"/prazos/{tela.pid}/cumprido")["onsubmit"]


# I3 -------------------------------------------------------------------------------------


def test_atrasado_so_aparece_em_prazo_aberto(tela):
    pid = _novo(tela, "61", fatal="2026-11-02", ato="Velho", conferir=True)
    assert "Atrasado" in _principal(_texto(tela.get(f"/prazos/{pid}")))
    for estado in ("cumprido", "descartado"):
        _sql(tela, "UPDATE prazo SET estado = ? WHERE id = ?", estado, pid)
        assert "Atrasado" not in _principal(_texto(tela.get(f"/prazos/{pid}"))), estado


# I4 -------------------------------------------------------------------------------------


def test_o_aviso_diz_de_onde_vem_a_data(tela):
    lido = "Sugestão do Lex. Confira antes de confiar."
    digitada = "Data digitada por você. Confira antes de confiar."
    manual = "Preenchido por você, com a conta do sistema. Confira."
    pje = "Intimação do PJe: abra no PJe para saber o ato."
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert manual in t and lido not in t and digitada not in t and "Ato lido" not in t
    _sql(tela, "UPDATE prazo SET trecho = 'prazo de quinze dias', origem_dos_dias = 'texto' "
               "WHERE id = ?", tela.pid)
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert lido in t and manual not in t
    digit = _novo(tela, "62", fatal="2026-11-25", ato="Com data digitada")
    t = _texto(tela.get(f"/prazos/{digit}"))
    assert digitada in t and lido not in t and manual not in t
    _, aviso = _duas_datas(tela)
    _sql(tela, "UPDATE prazo SET estado = 'a_conferir', fatal = '2026-11-24' WHERE id = ?", aviso)
    t = _texto(tela.get(f"/prazos/{aviso}"))
    assert pje in t and lido not in t and manual not in t


def test_ato_lido_so_quando_o_lex_leu(tela):
    _sql(tela, "UPDATE prazo SET origem_dos_dias = 'lei', codigo_ato = 'contestacao' WHERE id = ?",
         tela.pid)
    t = _texto(tela.get(f"/prazos/{tela.pid}"))
    assert "Ato lido" not in t and "Na lista da lei" in t  # escolhido por você da lista
    _sql(tela, "UPDATE prazo SET trecho = 'trecho da publicação' WHERE id = ?", tela.pid)
    assert "Ato lido" in _texto(tela.get(f"/prazos/{tela.pid}"))


# I5 -------------------------------------------------------------------------------------


def test_nao_e_meu_nao_diz_aguardando_o_lex(web):
    conn = banco.conectar(web.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    _sql(web, "UPDATE processo SET descartado_em = '2026-11-02', advogado_atua = 0")
    t = _principal(_texto(web.get(f"/prazos/{pid}")))
    assert "Processo marcado como “Não é meu”" in t and "Aguardando o Lex" not in t


def test_sem_prazo_diz_que_o_lex_nao_viu_prazo(web):
    conn = banco.conectar(web.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05",
                                 estado="sem_prazo")
    conn.close()
    t = _principal(_texto(web.get(f"/prazos/{pid}")))
    assert "O Lex não viu prazo aqui" in t and "Prazo a definir" not in t


def test_conferido_sem_data_pede_a_data(web):
    _sql(web, "UPDATE prazo SET estado = 'conferido', fatal = NULL WHERE id = ?", web.pid)
    t = _principal(_texto(web.get(f"/prazos/{web.pid}")))
    assert "Conferido, sem data: informe a data fatal" in t and 'href="#corrigir"' in t
    assert "fatal_informada" in _form(t, f"/prazos/{web.pid}/corrigir")["campos"]


# I6 -------------------------------------------------------------------------------------


def test_prazo_parado_na_fila_diz_ha_quantos_dias(web):
    conn = banco.conectar(web.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    _sql(web, "UPDATE prazo SET criado_em = '2025-01-02T10:00:00-04:00', tentativas = 3 "
              "WHERE id = ?", pid)
    with web.session_transaction():
        pass
    conn = banco.conectar(web.caminho)
    with conn:
        ponte.registrar_contato_do_leitor(conn)
    conn.close()
    dias = (painel_dados.hoje_cuiaba() - date(2025, 1, 2)).days
    frase = f"Está na fila há {dias} dias. Se o prazo pode estar perto, preencha à mão."
    for rota in (f"/prazos/{pid}", "/prazos"):
        t = _texto(web.get(rota))
        assert frase in t, rota
        assert "quando chegar a vez" not in t and "em instantes" not in t
    # «Ler de novo» recomeça a fila: a idade conta daí (o evento), não da criação do prazo
    conn = banco.conectar(web.caminho)
    with conn:
        eventos.registrar(conn, "prazo_ler_de_novo", A, {"prazo": pid, "por": "advogado"})
    conn.close()
    assert "Está na fila há" not in _texto(web.get(f"/prazos/{pid}"))


def test_prazo_recente_na_fila_nao_ganha_o_aviso_de_atraso(web):
    conn = banco.conectar(web.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    assert "Está na fila há" not in _texto(web.get(f"/prazos/{pid}"))


# I7 -------------------------------------------------------------------------------------


def test_o_ato_nao_trava_a_escolha_da_lista_numa_linha_nao_lida(web):
    conn = banco.conectar(web.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    t = _texto(web.get(f"/prazos/{pid}"))
    assert "required" not in re.search(r'<input[^>]*name="ato"[^>]*>', t).group(0)
    f = _form(t, f"/prazos/{pid}/corrigir")
    assert f["campos"]["ato"] == ""
    r = _enviar(web, f, codigo="contestacao")  # só o código da lista; o ato segue vazio
    assert r.status_code == 302 and not _erros(web)
    p = _p(web, pid)
    assert p["estado"] == "a_conferir" and p["ato"] == prazos_legais.PRAZOS["contestacao"].ato


def test_ato_em_branco_sem_codigo_continua_recusado_na_linha_nao_lida(web):
    conn = banco.conectar(web.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
    conn.close()
    f = _form(_texto(web.get(f"/prazos/{pid}")), f"/prazos/{pid}/corrigir")
    _enviar(web, f, dias="5", unidade="uteis")
    assert _erros(web) and _p(web, pid)["estado"] == "aguardando_lex"


# I8 -------------------------------------------------------------------------------------


def test_depois_da_primeira_vez_abrir_a_aba_nao_escreve_nem_trava(tela, monkeypatch):
    vistos = []
    original = banco.conectar

    def com_rastro(*a, **kw):
        conn = original(*a, **kw)
        conn.set_trace_callback(vistos.append)
        return conn
    monkeypatch.setattr(banco, "conectar", com_rastro)
    assert tela.get("/prazos").status_code == 200  # a primeira vez faz o alcance e marca
    assert _sql(tela, "SELECT valor FROM config WHERE chave = 'prazos_alcance_em'")
    vistos.clear()
    assert tela.get("/prazos").status_code == 200
    assert tela.get("/prazos?vista=mes").status_code == 200
    escritas = [v for v in vistos if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|BEGIN)", v, re.I)]
    assert escritas == []


# M1 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["9999-12-31", "0001-01-01", "2026-13-45", "1999-12-31",
                                 "2100-01-01"])
@pytest.mark.parametrize("vista", ["semana", "mes"])
def test_ref_fora_do_alcance_cai_em_hoje(tela, ref, vista):
    t = _texto(tela.get(f"/prazos?vista={vista}&ref={ref}"))
    assert ("Novembro de 2026" if vista == "mes" else "09/11 a 15/11/2026") in t


def test_id_enorme_e_404(tela):
    assert tela.get("/prazos/9223372036854775808").status_code == 404
    assert tela.get("/prazos/99999999999999999999999").status_code == 404
    r = tela.post("/prazos/9223372036854775808/conferir")
    assert r.status_code == 302 and _mensagem(tela, r).get("erro")


# M2 -------------------------------------------------------------------------------------


def test_retirar_um_dia_pede_confirmacao(tela):
    t = _texto(tela.get("/configuracoes/prazos?tribunal=TJMT&ano=2026"))
    retirar = [f for f in _formularios(t) if f["action"].endswith("/desativar")]
    assert retirar and all("Tirar este dia da conta" in f["onsubmit"] for f in retirar)


def test_a_faixa_que_falha_por_qualquer_motivo_nao_derruba_a_pagina(tela, monkeypatch, caplog):
    def estraga(*a, **k):
        raise RuntimeError("TEXTO SIGILOSO")
    monkeypatch.setattr(prazos_tela, "faixa", estraga)
    with caplog.at_level("DEBUG"):
        t = _texto(tela.get("/carteira"))
    assert "faixa-prazos" not in t and "SIGILOSO" not in caplog.text


def test_aviso_de_recusa_aparece_na_tela_do_prazo_depois_do_redirecionamento(web):
    """O navegador devolve a rolagem de antes; o aviso do alto ficaria fora da vista. A tela
    do prazo leva o aviso à vista, sem mexer em página que não tem aviso."""
    aba_velha = _token(web)
    conn = banco.conectar(web.caminho)
    with conn:
        prazos_banco.corrigir(conn, web.pid, dias=7, unidade="uteis", ato="Réplica")
    conn.close()
    r = web.post(f"/prazos/{web.pid}/conferir", data={"versao": aba_velha})
    assert r.status_code == 302
    t = _texto(web.get(f"/prazos/{web.pid}"))
    assert re.search(r"data-aviso-da-acao>\s*" + re.escape(MUDOU), t)
    assert "scrollIntoView" in t and "scrollRestoration" in t
    sem_aviso = _texto(web.get(f"/prazos/{web.pid}"))
    assert "data-aviso-da-acao>" not in sem_aviso  # o aviso é lido uma vez só


def test_o_aviso_da_acao_so_leva_a_rolagem_na_tela_do_prazo(web):
    web.post(f"/prazos/{web.pid}/conferir", data={"versao": "velha"})
    t = _texto(web.get("/prazos"))
    assert "scrollIntoView" not in t


def test_esperando_o_lex_a_frase_nao_promete_hora(web):
    tela = web  # o relógio de verdade: o contato do Lex é de agora
    conn = banco.conectar(tela.caminho)
    with conn:
        pid = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05")
        ponte.registrar_contato_do_leitor(conn)
    conn.close()
    t = _texto(tela.get(f"/prazos/{pid}"))
    assert ("O Lex ainda não leu esta publicação, e não há hora marcada para isso. "
            "Se preferir, preencha à mão.") in t
    assert "Conectado" not in t and "deu sinal" not in t


def test_calendario_agrupa_o_fim_de_ano_e_cada_dia_segue_com_os_botoes(tela):
    t = _texto(tela.get("/configuracoes/prazos?tribunal=TJMT&ano=2026"))
    assert re.search(r"de 2\d/12/2026 a 31/12/2026", t)
    conn = banco.conectar(tela.caminho)
    dia = conn.execute("SELECT id FROM dia_forense WHERE data = '2026-12-28' AND tribunal = '' AND "
                       "tipo = 'suspensao'").fetchone()[0]
    conn.close()
    # o botão do dia solto continua na tela, dentro do grupo
    assert f"/configuracoes/prazos/dia/{dia}/desativar" in t
    assert f"/configuracoes/prazos/dia/{dia}/corrigir" in t
    assert len(t) < 400_000
