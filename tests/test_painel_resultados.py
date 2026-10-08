import pytest

import painel
from nucleo import banco, painel_dados

A = "00000010220248110041"


def _sql(caminho, sql, *params):
    conn = banco.conectar(caminho)
    try:
        with conn:
            return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _peca(caminho, item, criada, inicio, fim, squad="contestacao", falhas=0, resultado="pronto"):
    conn = banco.conectar(caminho)
    try:
        with conn:
            conn.execute("INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em) "
                         "VALUES (?, 'revisao', '2026-10-01', ?, ?)", (A, criada, criada))
            did = conn.execute("SELECT max(id) FROM demanda").fetchone()[0]
            conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
                         "resultado, squad, citacoes_total, citacoes_falhas) "
                         "VALUES (?, 1, 'novo', ?, ?, ?, ?, 3, ?)",
                         (did, inicio, fim, resultado, squad, falhas))
    finally:
        conn.close()


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    caminho = str(tmp_path / "d.db")
    conn = banco.conectar(caminho)
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado, cliente) "
                 "VALUES (?, 'TJMT', 1, 0, 'MARIA FICTÍCIA')", (A,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    # "agora" fixo: 7 de outubro de 2026
    monkeypatch.setattr(painel_dados, "agora_cuiaba",
                        lambda: painel_dados.datetime.fromisoformat("2026-10-07T12:00:00-04:00"))
    c = painel.app.test_client()
    c.caminho = caminho
    return c


def _tres_pecas(c):
    _peca(c.caminho, "1", "2026-10-01T08:00:00-04:00", "2026-10-01T08:30:00-04:00",
          "2026-10-01T08:50:00-04:00", squad="contestacao", falhas=0)      # 50 min total
    _peca(c.caminho, "2", "2026-10-03T08:00:00-04:00", "2026-10-03T09:20:00-04:00",
          "2026-10-03T10:00:00-04:00", squad="recurso", falhas=1)           # 2 h
    _peca(c.caminho, "3", "2026-08-20T08:00:00-04:00", "2026-08-20T08:20:00-04:00",
          "2026-08-20T08:30:00-04:00", squad="contestacao", falhas=0)       # 30 min, agosto


def test_vazio(cliente):
    r = cliente.get("/resultados")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Ainda não há peças do Lex neste período." in html
    assert "barra-linha" not in html


def test_numeros_padrao_12_meses(cliente):
    _tres_pecas(cliente)
    html = cliente.get("/resultados").get_data(as_text=True)
    assert "1 h 7 min" in html            # tempo médio total
    assert "23 min" in html               # tempo médio do Lex
    assert "67%" in html                  # citações conferidas de primeira
    assert "Melhores marcas" in html and "20/08/2026" in html
    assert "ago/2026" in html and "out/2026" in html
    assert "Contestação" in html and "Recurso" in html and "contestacao" not in html
    assert 'aria-current="page"' in html and "Resultados" in html
    assert 'class="ativo" aria-current="page">12 meses' in html


def test_periodo_30d_exclui_agosto(cliente):
    _tres_pecas(cliente)
    html = cliente.get("/resultados?periodo=30d").get_data(as_text=True)
    assert "ago/2026" not in html and "20/08/2026" not in html
    assert 'class="ativo" aria-current="page">30 dias' in html


def test_periodo_invalido_cai_no_padrao(cliente):
    _tres_pecas(cliente)
    html = cliente.get("/resultados?periodo=<script>").get_data(as_text=True)
    assert 'class="ativo" aria-current="page">12 meses' in html and "ago/2026" in html


def test_barras_proporcionais_ao_maior(cliente):
    _tres_pecas(cliente)
    html = cliente.get("/resultados?periodo=tudo").get_data(as_text=True)
    # contestacao 2 (100%), recurso 1 (50%)
    assert 'style="width: 100%"' in html and 'style="width: 50%"' in html


def test_nome_do_tipo_e_escapado(cliente):
    _peca(cliente.caminho, "9", "2026-10-01T08:00:00-04:00", "2026-10-01T08:10:00-04:00",
          "2026-10-01T08:20:00-04:00", squad="<img src=x onerror=1>")
    html = cliente.get("/resultados").get_data(as_text=True)
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_exige_login_quando_ha_senha(cliente, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    r = cliente.get("/resultados")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_link_na_navegacao_do_cockpit(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert 'href="/resultados"' in html
