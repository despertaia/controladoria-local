import pytest

from nucleo import autos, banco, eventos, quadro

A = "00000010220248110041"
TRF = "00000130220244013600"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _cartao_em_acao(conn, numero=A):
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado) "
                 "VALUES (?, 'TJMT', 1, 0)", (numero,))
    quadro.adicionar_item(conn, numero, "publicacao", "1", "2026-10-05")
    quadro.mover(conn, 1, "acao", "advogado")
    conn.commit()
    return 1


def _lote_falso(monkeypatch, por_instancia):
    chamadas = []

    def baixar(cliente, numero, subpasta=None, pular_existentes=False, **_):
        chamadas.append((subpasta, pular_existentes))
        yield from por_instancia[subpasta]
    monkeypatch.setattr(autos.lote, "baixar_processo_completo", baixar)
    return chamadas


def _fim(ok, falhas):
    return [{"evento": "processo_inicio", "total": ok + falhas},
            {"evento": "processo_fim", "ok": ok, "falhas": falhas, "pulados": 0,
             "bytes": 1, "pasta": "x"}]


def test_baixa_as_duas_instancias_e_move_para_autos(conn, monkeypatch):
    _cartao_em_acao(conn)
    chamadas = _lote_falso(monkeypatch, {"1grau": _fim(5, 0), "2grau": _fim(2, 1)})
    r = autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())
    assert r == {"estado": "ok", "pecas": 7, "faltaram": 1, "detalhe": "faltaram 1 peça(s)"}
    assert chamadas == [("1grau", True), ("2grau", True)]
    d = conn.execute("SELECT coluna, autos_estado, autos_detalhe FROM demanda").fetchone()
    assert tuple(d) == ("autos", "ok", "faltaram 1 peça(s)")
    assert "autos_baixados" in [e["tipo"] for e in eventos.listar(conn, A)]


def test_ausente_no_2o_grau_e_normal(conn, monkeypatch):
    _cartao_em_acao(conn)
    _lote_falso(monkeypatch, {"1grau": _fim(3, 0),
                              "2grau": [{"evento": "processo_ausente", "numero": A}]})
    assert autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())["estado"] == "ok"


def test_erro_do_pje_marca_falha_e_fica_em_acao(conn, monkeypatch):
    _cartao_em_acao(conn)
    _lote_falso(monkeypatch, {"1grau": [{"evento": "erro_processo", "numero": A,
                                         "msg": "Tempo esgotado no TJMT."}],
                              "2grau": _fim(0, 0)})
    r = autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())
    assert r["estado"] == "falhou" and "Tempo esgotado" in r["detalhe"]
    d = conn.execute("SELECT coluna, autos_estado FROM demanda").fetchone()
    assert tuple(d) == ("acao", "falhou")
    assert "autos_falharam" in [e["tipo"] for e in eventos.listar(conn, A)]


def test_nao_encontrado_em_nenhuma_instancia(conn, monkeypatch):
    _cartao_em_acao(conn)
    ausente = [{"evento": "processo_ausente", "numero": A}]
    _lote_falso(monkeypatch, {"1grau": ausente, "2grau": ausente})
    r = autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())
    assert r["estado"] == "falhou" and "não encontrado" in r["detalhe"]


def test_fora_do_tjmt_segue_para_autos_sem_chamar_o_pje(conn, monkeypatch):
    _cartao_em_acao(conn, TRF)
    _lote_falso(monkeypatch, {})
    r = autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: pytest.fail("chamou o PJe"))
    texto = ("os autos deste tribunal não vêm pelo PJe do TJMT; "
             "consulte no sistema do tribunal")
    assert r == {"estado": "ok", "pecas": 0, "faltaram": 0, "detalhe": texto}
    d = conn.execute("SELECT coluna, autos_estado, autos_detalhe FROM demanda WHERE id = 1").fetchone()
    assert (d["coluna"], d["autos_estado"], d["autos_detalhe"]) == ("autos", "ok", texto)
    ev = conn.execute("SELECT dados FROM evento WHERE tipo = 'autos_baixados'").fetchone()
    import json
    assert json.loads(ev["dados"]) == {"demanda": 1, "pecas": 0, "faltaram": 0,
                                       "fora_do_tjmt": True}


def test_cartao_que_saiu_de_acao_e_ignorado(conn, monkeypatch):
    _cartao_em_acao(conn)
    quadro.mover(conn, 1, "chegou", "advogado")
    r = autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: pytest.fail("chamou o PJe"))
    assert r["estado"] == "ignorado"


def test_inicio_do_download_registra_evento(conn, monkeypatch):
    _cartao_em_acao(conn)
    _lote_falso(monkeypatch, {"1grau": _fim(1, 0), "2grau": _fim(0, 0)})
    autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())
    ev = conn.execute("SELECT numero, dados FROM evento WHERE tipo = 'autos_iniciados'").fetchone()
    assert ev["numero"] == A and ev["dados"] == '{"demanda": 1}'


def _sai_de_acao_no_meio(conn, monkeypatch, eventos_lote):
    def baixar(cliente, numero, subpasta=None, pular_existentes=False, **_):
        with conn:  # o advogado mexeu no cartão enquanto o trabalhador baixava
            conn.execute("UPDATE demanda SET coluna = 'chegou', autos_estado = '' WHERE id = 1")
        yield from eventos_lote
    monkeypatch.setattr(autos.lote, "baixar_processo_completo", baixar)


def test_cartao_que_saiu_de_acao_durante_o_download_nao_e_movido(conn, monkeypatch):
    _cartao_em_acao(conn)
    _sai_de_acao_no_meio(conn, monkeypatch, _fim(2, 0))
    r = autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())
    assert r["estado"] == "ignorado"
    d = conn.execute("SELECT coluna, autos_estado FROM demanda").fetchone()
    assert tuple(d) == ("chegou", "")
    assert "autos_baixados" not in [e["tipo"] for e in eventos.listar(conn, A)]


def test_falha_depois_de_sair_de_acao_nao_marca_falhou(conn, monkeypatch):
    _cartao_em_acao(conn)
    _sai_de_acao_no_meio(conn, monkeypatch, [{"evento": "erro_processo", "numero": A,
                                              "msg": "Tempo esgotado."}])
    r = autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())
    assert r["estado"] == "ignorado"
    d = conn.execute("SELECT coluna, autos_estado FROM demanda").fetchone()
    assert tuple(d) == ("chegou", "")
    assert "autos_falharam" not in [e["tipo"] for e in eventos.listar(conn, A)]


def test_com_a_chave_ligada_o_cartao_baixado_segue_para_o_lex(conn, monkeypatch):
    from nucleo import config
    config.gravar(conn, "lex_automatico", "1")
    _cartao_em_acao(conn)
    _lote_falso(monkeypatch, {"1grau": _fim(2, 0), "2grau": _fim(0, 0)})
    autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: object())
    d = conn.execute("SELECT coluna, lex_estado FROM demanda WHERE id = 1").fetchone()
    assert (d["coluna"], d["lex_estado"]) == ("lex", "na_fila")


def test_com_a_chave_ligada_fora_do_tjmt_tambem_segue_para_o_lex(conn, monkeypatch):
    from nucleo import config
    config.gravar(conn, "lex_automatico", "1")
    _cartao_em_acao(conn, TRF)
    autos.baixar_para_demanda(conn, 1, fabrica=lambda inst: pytest.fail("chamou o PJe"))
    d = conn.execute("SELECT coluna, lex_estado FROM demanda WHERE id = 1").fetchone()
    assert (d["coluna"], d["lex_estado"]) == ("lex", "na_fila")
