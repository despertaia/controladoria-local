from datetime import date, datetime

import pytest

from nucleo import banco, config, eventos, quadro, tarefas_fila

HOJE = date(2026, 10, 6)
A = "00000010220248110041"
B = "00000020220248110041"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _processo(conn, numero, *, arquivado=0, atua=1, descartado=None):
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado, descartado_em) "
                 "VALUES (?, 'TJMT', ?, ?, ?)", (numero, atua, arquivado, descartado))


def _cartoes(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT numero, coluna, referencia_em FROM demanda ORDER BY id")]


def test_elegibilidade(conn):
    _processo(conn, A)
    _processo(conn, B, arquivado=1)
    assert quadro.processo_elegivel(conn, A)
    assert not quadro.processo_elegivel(conn, B)
    assert not quadro.processo_elegivel(conn, "99999999999999999999")  # fora da carteira
    conn.execute("UPDATE processo SET advogado_atua = 0, descartado_em = 'x' WHERE numero = ?", (A,))
    assert not quadro.processo_elegivel(conn, A)
    conn.execute("UPDATE processo SET advogado_atua = 1 WHERE numero = ?", (A,))
    assert quadro.processo_elegivel(conn, A)  # descartado que passou a atuar volta a valer


def test_janela_de_15_dias(conn):
    _processo(conn, A)
    assert quadro.publicacao_gera_cartao(conn, A, "2026-09-21", HOJE)
    assert not quadro.publicacao_gera_cartao(conn, A, "2026-09-20", HOJE)
    assert not quadro.publicacao_gera_cartao(conn, A, "lixo", HOJE)


def test_novidades_do_mesmo_processo_juntam_no_cartao_aberto(conn):
    _processo(conn, A)
    assert quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03") == (1, True)
    assert quadro.adicionar_item(conn, A, "aviso", "1grau:77", "2026-10-01") == (1, False)
    assert quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03") is None
    assert _cartoes(conn) == [(A, "chegou", "2026-10-01")]  # referência = a mais antiga
    tipos = [e["tipo"] for e in eventos.listar(conn, A)]
    assert tipos == ["demanda_criada", "demanda_item_juntado"]


def test_cartao_fora_de_chegou_nao_recebe_item(conn):
    _processo(conn, A)
    quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03")
    quadro.mover(conn, 1, "acompanhar", "advogado")
    assert quadro.adicionar_item(conn, A, "publicacao", "11", "2026-10-05") == (2, True)
    assert [c[1] for c in _cartoes(conn)] == ["acompanhar", "chegou"]


def test_marcar_acao_pede_autos_e_desfazer_cancela(conn):
    _processo(conn, A)
    quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03")
    r = quadro.mover(conn, 1, "acao", "advogado")
    assert r == {"de": "chegou", "para": "acao", "desfazer": "chegou"}
    assert conn.execute("SELECT autos_estado FROM demanda").fetchone()[0] == "na_fila"
    assert tarefas_fila.ativa(conn, "autos", "1")
    quadro.mover(conn, 1, "chegou", "advogado")
    assert not tarefas_fila.ativa(conn, "autos", "1")
    assert tuple(conn.execute("SELECT coluna, autos_estado FROM demanda").fetchone()) == ("chegou", "")


def test_nao_desfaz_com_autos_baixando(conn):
    _processo(conn, A)
    quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03")
    quadro.mover(conn, 1, "acao", "advogado")
    quadro.marcar_autos(conn, 1, "baixando")
    with pytest.raises(quadro.MovimentoInvalido, match="começaram a baixar"):
        quadro.mover(conn, 1, "chegou", "advogado")


def test_movimentos_fora_da_tabela_sao_recusados(conn):
    _processo(conn, A)
    quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03")
    with pytest.raises(quadro.MovimentoInvalido) as erro:
        quadro.mover(conn, 1, "autos", "advogado")      # pular coluna
    assert str(erro.value) == "Não dá para mover de “Chegou do PJe” para “Autos baixados”."
    quadro.mover(conn, 1, "acao", "advogado")
    with pytest.raises(quadro.MovimentoInvalido):
        quadro.mover(conn, 1, "autos", "advogado")      # só o trabalhador
    quadro.mover(conn, 1, "autos", "trabalhador")
    with pytest.raises(quadro.MovimentoInvalido, match="não encontrado"):
        quadro.mover(conn, 99, "acao", "advogado")


def test_voltar_a_triagem_recusado_se_ja_ha_cartao_novo(conn):
    _processo(conn, A)
    quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03")
    quadro.mover(conn, 1, "acompanhar", "advogado")
    quadro.adicionar_item(conn, A, "publicacao", "11", "2026-10-05")
    with pytest.raises(quadro.MovimentoInvalido, match="esperando triagem"):
        quadro.mover(conn, 1, "chegou", "advogado")


def test_resolver_e_desfazer(conn):
    _processo(conn, A)
    quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03")
    quadro.mover(conn, 1, "acao", "advogado")
    quadro.mover(conn, 1, "autos", "trabalhador")
    assert quadro.mover(conn, 1, "resolvida", "advogado")["desfazer"] == "autos"
    assert quadro.mover(conn, 1, "autos", "advogado")["desfazer"] is None
    movidas = [e["dados"] for e in eventos.listar(conn, A) if e["tipo"] == "demanda_movida"]
    assert [(m["de"], m["para"], m["por"]) for m in movidas] == [
        ("chegou", "acao", "advogado"), ("acao", "autos", "trabalhador"),
        ("autos", "resolvida", "advogado"), ("resolvida", "autos", "advogado")]


def test_tentar_autos_de_novo(conn):
    _processo(conn, A)
    quadro.adicionar_item(conn, A, "publicacao", "10", "2026-10-03")
    with pytest.raises(quadro.MovimentoInvalido):
        quadro.tentar_autos_de_novo(conn, 1)
    quadro.mover(conn, 1, "acao", "advogado")
    tarefas_fila.concluir(conn, tarefas_fila.pegar_proxima(conn)["id"], erro="x")
    quadro.marcar_autos(conn, 1, "falhou", "tempo esgotado")
    quadro.tentar_autos_de_novo(conn, 1)
    assert conn.execute("SELECT autos_estado FROM demanda").fetchone()[0] == "na_fila"
    assert tarefas_fila.ativa(conn, "autos", "1")
    ev = conn.execute("SELECT numero, dados FROM evento WHERE tipo = 'autos_pedidos_de_novo'"
                      ).fetchone()
    assert ev["numero"] == A and ev["dados"] == '{"demanda": 1}'


def test_ligar_cria_cartoes_dos_ultimos_15_dias_uma_vez_so(conn):
    _processo(conn, A)
    _processo(conn, B, arquivado=1)
    pub = ("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao) "
           "VALUES (?, ?, 'TJMT', ?)")
    conn.execute(pub, (1, A, "2026-09-01"))   # antiga: fica fora
    conn.execute(pub, (2, A, "2026-10-01"))
    conn.execute(pub, (3, A, "2026-10-04"))   # mesmo processo: junta
    conn.execute(pub, (4, B, "2026-10-04"))   # arquivado: fora
    conn.execute("INSERT INTO aviso (instancia, id, numero, data_disponibilizacao, visto_em) "
                 "VALUES ('1grau', '55', ?, '', 'x')", (B,))  # intimação vale sempre
    assert quadro.ligar(conn, HOJE) == 2
    assert quadro.ligar(conn, HOJE) == 0
    assert _cartoes(conn) == [(A, "chegou", "2026-10-01"), (B, "chegou", "2026-10-06")]
    assert quadro.contar_na_triagem(conn) == 2
    assert quadro.ja_ligado(conn)


def test_gerar_pendentes_le_do_banco_e_e_idempotente(conn):
    _processo(conn, A)
    _processo(conn, B, arquivado=1)
    pub = ("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao) "
           "VALUES (?, ?, 'TJMT', ?)")
    conn.execute(pub, (1, A, "2026-09-01"))   # fora da janela
    conn.execute(pub, (2, A, "2026-10-02"))
    conn.execute(pub, (4, B, "2026-10-04"))   # arquivado: fora
    conn.execute("INSERT INTO aviso (instancia, id, numero, data_disponibilizacao, visto_em) "
                 "VALUES ('2grau', '7', ?, '2026-10-03', 'x')", (B,))
    conn.execute("INSERT INTO aviso (instancia, id, numero, data_disponibilizacao, visto_em, "
                 "pendente) VALUES ('1grau', '8', ?, '2026-10-03', 'x', 0)", (A,))  # encerrado
    assert quadro.gerar_pendentes(conn, HOJE) == 2
    assert quadro.gerar_pendentes(conn, HOJE) == 0
    refs = sorted(r[0] for r in conn.execute("SELECT ref FROM demanda_item"))
    assert refs == ["2", "2grau:7"]
    assert not quadro.ja_ligado(conn)  # só ligar() registra a primeira ligada


_LEX_N = [0]


def _cartao_em(conn, coluna):
    _LEX_N[0] += 1
    numero = f"{_LEX_N[0]:07d}0220248110041"
    _processo(conn, numero)
    return conn.execute("INSERT INTO demanda (numero, coluna, referencia_em, criada_em, "
                        "atualizada_em) VALUES (?, ?, '2026-10-05', 'x', 'x')",
                        (numero, coluna)).lastrowid


def _campos(conn, d, *nomes):
    return tuple(conn.execute(f"SELECT {', '.join(nomes)} FROM demanda WHERE id = ?",
                              (d,)).fetchone())


def _coluna(conn, d):
    return _campos(conn, d, "coluna")[0]


def test_mandar_pro_lex_com_orientacao_e_desfazer(conn):
    d = _cartao_em(conn, "autos")
    r = quadro.mover(conn, d, "lex", "advogado", orientacao="contestar só a preliminar")
    assert r == {"de": "autos", "para": "lex", "desfazer": "autos"}
    assert _campos(conn, d, "lex_estado", "lex_orientacao") == ("na_fila", "contestar só a preliminar")
    quadro.mover(conn, d, "autos", "advogado")
    assert _campos(conn, d, "coluna", "lex_estado") == ("autos", "")


def test_tirar_do_lex_recusado_com_o_mac_trabalhando(conn):
    d = _cartao_em(conn, "lex")
    conn.execute("UPDATE demanda SET lex_estado='trabalhando', "
                 "lex_reservado_ate='2999-01-01T00:00:00-04:00' WHERE id=?", (d,))
    with pytest.raises(quadro.MovimentoInvalido, match="já está trabalhando"):
        quadro.mover(conn, d, "autos", "advogado")


def test_revisao_protocolado_e_devolver_com_ajuste(conn):
    d = _cartao_em(conn, "revisao")
    with pytest.raises(quadro.MovimentoInvalido, match="o que o Lex deve ajustar"):
        quadro.mover(conn, d, "lex", "advogado")
    quadro.mover(conn, d, "lex", "advogado", ajuste="trocar o valor da causa")
    assert _campos(conn, d, "lex_estado", "lex_ajuste") == ("na_fila", "trocar o valor da causa")
    conn.execute("UPDATE demanda SET coluna='revisao', lex_estado='pronto' WHERE id=?", (d,))
    assert quadro.mover(conn, d, "protocolado", "advogado")["desfazer"] == "revisao"
    assert _campos(conn, d, "protocolado_em")[0]
    quadro.mover(conn, d, "revisao", "advogado")
    assert _campos(conn, d, "protocolado_em") == (None,)


def test_so_o_mac_entrega_e_so_o_sistema_ou_advogado_enfileira(conn):
    d = _cartao_em(conn, "lex")
    with pytest.raises(quadro.MovimentoInvalido):
        quadro.mover(conn, d, "revisao", "advogado")
    quadro.mover(conn, d, "revisao", "mac")
    e = _cartao_em(conn, "autos")
    with pytest.raises(quadro.MovimentoInvalido):
        quadro.mover(conn, e, "lex", "mac")


def test_mac_devolve_sem_tipo(conn):
    d = _cartao_em(conn, "lex"); conn.execute("UPDATE demanda SET lex_estado='trabalhando' WHERE id=?", (d,))
    quadro.mover(conn, d, "autos", "mac")
    assert _campos(conn, d, "coluna", "lex_estado") == ("autos", "sem_tipo")


def test_enfileirar_automatico(conn):
    d = _cartao_em(conn, "autos")
    assert quadro.enfileirar_automatico(conn, d) is False
    config.definir_lex_automatico(conn, True)
    e = _cartao_em(conn, "autos")
    assert quadro.enfileirar_automatico(conn, e) is True and _coluna(conn, e) == "lex"
    assert quadro.enfileirar_automatico(conn, e) is False  # já saiu de "autos"


def test_tentar_lex_de_novo(conn):
    d = _cartao_em(conn, "lex"); conn.execute("UPDATE demanda SET lex_estado='falhou', lex_detalhe='x' WHERE id=?", (d,))
    quadro.tentar_lex_de_novo(conn, d)
    assert _campos(conn, d, "lex_estado", "lex_detalhe") == ("na_fila", "")
    with pytest.raises(quadro.MovimentoInvalido):
        quadro.tentar_lex_de_novo(conn, d)


def test_mover_aceita_orientacao_e_ajuste_none(conn):
    d = _cartao_em(conn, "autos")
    quadro.mover(conn, d, "lex", "advogado", orientacao=None, ajuste=None)
    assert _campos(conn, d, "lex_orientacao") == ("",)
    e = _cartao_em(conn, "autos")
    quadro.mover(conn, e, "lex", "advogado", orientacao="  só a preliminar  ")
    assert _campos(conn, e, "lex_orientacao") == ("só a preliminar",)
    f = _cartao_em(conn, "revisao")
    with pytest.raises(quadro.MovimentoInvalido):
        quadro.mover(conn, f, "lex", "advogado", ajuste=None)


def test_mandar_pro_lex_de_novo_zera_o_ajuste_antigo(conn):
    d = _cartao_em(conn, "autos")
    conn.execute("UPDATE demanda SET lex_ajuste = 'ajuste velho' WHERE id = ?", (d,))
    quadro.mover(conn, d, "lex", "advogado")
    assert _campos(conn, d, "lex_ajuste") == ("",)


def test_tirar_do_lex_com_reserva_vencida_vence_e_permite(conn):
    d = _cartao_em(conn, "lex")
    conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em) "
                 "VALUES (?, 1, 'novo', '2026-10-05T10:00:00-04:00')", (d,))
    conn.execute("UPDATE demanda SET lex_estado = 'trabalhando', "
                 "lex_reservado_ate = '2020-01-01T00:00:00-04:00' WHERE id = ?", (d,))
    quadro.mover(conn, d, "autos", "advogado", agora=datetime(2026, 10, 7, 12, 0, tzinfo=eventos.FUSO))
    assert _campos(conn, d, "coluna", "lex_estado") == ("autos", "")
    r = conn.execute("SELECT resultado, terminada_em FROM execucao_lex").fetchone()
    assert r["resultado"] == "reserva_vencida" and r["terminada_em"]
    assert [e for e in eventos.listar(conn) if e["tipo"] == "lex_reserva_vencida"]


def test_tirar_do_lex_com_reserva_valida_continua_recusado(conn):
    d = _cartao_em(conn, "lex")
    conn.execute("UPDATE demanda SET lex_estado = 'reservado', "
                 "lex_reservado_ate = '2999-01-01T00:00:00-04:00' WHERE id = ?", (d,))
    with pytest.raises(quadro.MovimentoInvalido, match="já está trabalhando"):
        quadro.mover(conn, d, "autos", "advogado")
    assert _campos(conn, d, "coluna") == ("lex",)


def test_tentar_lex_de_novo_em_pausado_limite(conn):
    d = _cartao_em(conn, "lex")
    conn.execute("UPDATE demanda SET lex_estado = 'pausado_limite', lex_detalhe = 'limite', "
                 "lex_tentar_depois = '2999-01-01T00:00:00-04:00' WHERE id = ?", (d,))
    quadro.tentar_lex_de_novo(conn, d)
    assert _campos(conn, d, "lex_estado", "lex_detalhe", "lex_tentar_depois") == ("na_fila", "", None)


def test_tirar_do_lex_com_reserva_sem_data_conta_como_vencida(conn):
    d = _cartao_em(conn, "lex")
    conn.execute("UPDATE demanda SET lex_estado = 'reservado', lex_reservado_ate = NULL "
                 "WHERE id = ?", (d,))
    quadro.mover(conn, d, "autos", "advogado",
                 agora=datetime(2026, 10, 7, 12, 0, tzinfo=eventos.FUSO))
    assert _campos(conn, d, "coluna") == ("autos",)


def test_tirar_do_lex_respeita_o_relogio_injetado(conn):
    d = _cartao_em(conn, "lex")
    conn.execute("UPDATE demanda SET lex_estado = 'trabalhando', "
                 "lex_reservado_ate = '2026-10-07T12:00:00-04:00' WHERE id = ?", (d,))
    with pytest.raises(quadro.MovimentoInvalido, match="já está trabalhando"):
        quadro.mover(conn, d, "autos", "advogado",
                     agora=datetime(2026, 10, 7, 11, 59, tzinfo=eventos.FUSO))
    quadro.mover(conn, d, "autos", "advogado",
                 agora=datetime(2026, 10, 7, 12, 1, tzinfo=eventos.FUSO))
    assert _campos(conn, d, "coluna") == ("autos",)


# --- onda final -------------------------------------------------------------------

def test_tirar_do_lex_recusado_se_o_mac_reservou_no_meio(conn, monkeypatch):
    d = _cartao_em(conn, "lex")
    conn.execute("UPDATE demanda SET lex_estado = 'na_fila' WHERE id = ?", (d,))
    lido = conn.execute("SELECT * FROM demanda WHERE id = ?", (d,)).fetchone()
    # Depois da leitura, o Mac reserva o cartão (corrida com /ponte/proximo).
    conn.execute("UPDATE demanda SET lex_estado = 'reservado', "
                 "lex_reservado_ate = '2999-01-01T00:00:00-04:00' WHERE id = ?", (d,))
    monkeypatch.setattr(quadro, "_demanda", lambda c, i: lido)
    with pytest.raises(quadro.MovimentoInvalido, match="mudou enquanto isso"):
        quadro.mover(conn, d, "autos", "advogado")
    assert _campos(conn, d, "coluna", "lex_estado") == ("lex", "reservado")
    assert not [e for e in eventos.listar(conn) if e["tipo"] == "demanda_movida"]


def test_voltar_a_triagem_limpa_tudo_do_lex(conn):
    d = _cartao_em(conn, "autos")
    conn.execute("UPDATE demanda SET lex_estado = 'sem_tipo', lex_detalhe = 'sem segurança', "
                 "lex_orientacao = 'o', lex_ajuste = 'a', lex_squad = 's', "
                 "lex_squad_nome = 'S', lex_etapa = 'e', "
                 "lex_reservado_ate = 'x', lex_tentar_depois = 'y' WHERE id = ?", (d,))
    quadro.mover(conn, d, "chegou", "advogado")
    assert _campos(conn, d, "coluna", "lex_estado", "lex_detalhe", "lex_orientacao", "lex_ajuste",
                   "lex_squad", "lex_squad_nome", "lex_etapa", "lex_reservado_ate",
                   "lex_tentar_depois") == ("chegou", "", "", "", "", "", "", "", None, None)


def _com_peca_pronta_em_ajuste(conn, estado="na_fila"):
    d = _cartao_em(conn, "revisao")
    conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
                 "resultado, squad) VALUES (?, 1, 'novo', 'x', 'y', 'pronto', 's')", (d,))
    conn.execute("UPDATE demanda SET lex_estado = 'pronto' WHERE id = ?", (d,))
    quadro.mover(conn, d, "lex", "advogado", ajuste="trocar o pedido")
    conn.execute("UPDATE demanda SET lex_estado = ? WHERE id = ?", (estado, d))
    return d


@pytest.mark.parametrize("estado", ["na_fila", "falhou", "pausado_limite"])
def test_desistir_do_ajuste_volta_a_peca_anterior(conn, estado):
    d = _com_peca_pronta_em_ajuste(conn, estado)
    r = quadro.mover(conn, d, "revisao", "advogado")
    assert r["para"] == "revisao"
    assert _campos(conn, d, "coluna", "lex_estado", "lex_ajuste") == ("revisao", "pronto", "")
    mov = [e for e in eventos.listar(conn) if e["tipo"] == "demanda_movida"][-1]
    assert mov["dados"] == {"demanda": d, "de": "lex", "para": "revisao", "por": "advogado"}


@pytest.mark.parametrize("estado", ["reservado", "trabalhando"])
def test_desistir_do_ajuste_recusado_com_o_lex_trabalhando(conn, estado):
    d = _com_peca_pronta_em_ajuste(conn, estado)
    with pytest.raises(quadro.MovimentoInvalido, match="ajuste para desistir"):
        quadro.mover(conn, d, "revisao", "advogado")
    assert _campos(conn, d, "coluna", "lex_estado") == ("lex", estado)


def test_desistir_do_ajuste_exige_peca_pronta_anterior(conn):
    d = _cartao_em(conn, "lex")
    conn.execute("UPDATE demanda SET lex_estado = 'falhou', lex_ajuste = 'a' WHERE id = ?", (d,))
    with pytest.raises(quadro.MovimentoInvalido):
        quadro.mover(conn, d, "revisao", "advogado")


def test_desistir_do_ajuste_recusado_se_o_mac_reservou_no_meio(conn, monkeypatch):
    d = _com_peca_pronta_em_ajuste(conn, "na_fila")
    lido = conn.execute("SELECT * FROM demanda WHERE id = ?", (d,)).fetchone()
    conn.execute("UPDATE demanda SET lex_estado = 'reservado' WHERE id = ?", (d,))
    monkeypatch.setattr(quadro, "_demanda", lambda c, i: lido)
    with pytest.raises(quadro.MovimentoInvalido, match="mudou enquanto isso"):
        quadro.mover(conn, d, "revisao", "advogado")
    assert _campos(conn, d, "coluna", "lex_estado") == ("lex", "reservado")


# --- "Não é meu": o cartão sai do quadro e volta ao desfazer ------------------------

def _processo_sem_atuacao(conn, numero):
    conn.execute("INSERT INTO processo (numero, tribunal) VALUES (?, 'TRF1')", (numero,))


def test_tirar_do_quadro_leva_para_resolvido_e_devolver_traz_de_volta(conn):
    from nucleo import carteira
    numero = "00000030220244013600"
    _processo_sem_atuacao(conn, numero)
    demanda_id, _ = quadro.adicionar_item(conn, numero, "publicacao", "77", "2026-10-05")
    carteira.descartar(conn, numero)
    assert quadro.tirar_do_quadro(conn, numero) == [demanda_id]
    assert conn.execute("SELECT coluna FROM demanda WHERE id = ?", (demanda_id,)).fetchone()[0] == (
        "resolvida")
    assert quadro.tirar_do_quadro(conn, numero) == []  # já está em Resolvido
    carteira.restaurar(conn, numero)
    assert quadro.devolver_ao_quadro(conn, numero) == [demanda_id]
    assert conn.execute("SELECT coluna FROM demanda WHERE id = ?", (demanda_id,)).fetchone()[0] == (
        "chegou")
    assert quadro.devolver_ao_quadro(conn, numero) == []


def test_devolver_nao_mexe_em_cartao_resolvido_por_outro_motivo(conn):
    numero = "00000030220244013600"
    _processo_sem_atuacao(conn, numero)
    demanda_id, _ = quadro.adicionar_item(conn, numero, "publicacao", "78", "2026-10-05")
    conn.execute("UPDATE demanda SET coluna = 'autos' WHERE id = ?", (demanda_id,))
    quadro.mover(conn, demanda_id, "resolvida", "advogado")  # "Resolvido" comum
    assert quadro.devolver_ao_quadro(conn, numero) == []
    # Pelo "Não é meu" a partir de Autos baixados, volta para Autos baixados.
    quadro.mover(conn, demanda_id, "autos", "advogado")
    assert quadro.tirar_do_quadro(conn, numero) == [demanda_id]
    assert quadro.devolver_ao_quadro(conn, numero) == [demanda_id]
    assert conn.execute("SELECT coluna FROM demanda WHERE id = ?", (demanda_id,)).fetchone()[0] == (
        "autos")


def test_tirar_do_quadro_nao_mexe_em_trabalho_do_lex(conn):
    numero = "00000030220244013600"
    _processo_sem_atuacao(conn, numero)
    demanda_id, _ = quadro.adicionar_item(conn, numero, "publicacao", "79", "2026-10-05")
    conn.execute("UPDATE demanda SET coluna = 'revisao' WHERE id = ?", (demanda_id,))
    assert quadro.tirar_do_quadro(conn, numero) == []
