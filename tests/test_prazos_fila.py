"""De onde nascem os prazos: publicações novas que viram cartão e os cartões já abertos."""

from datetime import date, datetime

import pytest

from nucleo import banco, carteira, config, prazos_banco, quadro

HOJE = date(2026, 11, 6)
AGORA = datetime.fromisoformat("2026-11-06T10:00:00-04:00")
A = "00000010220248110041"
B = "00000020220248110041"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _processo(conn, numero=A, **campos):
    colunas = {"numero": numero, "tribunal": "TJMT", "advogado_atua": 1,
               "orgao_julgador": "1ª Vara Cível de Cuiabá", **campos}
    conn.execute(f"INSERT INTO processo ({', '.join(colunas)}) "
                 f"VALUES ({', '.join('?' for _ in colunas)})", tuple(colunas.values()))


def _publicacao(conn, id_=1, numero=A, data="2026-11-05", texto="Cumpra-se o despacho."):
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                 "VALUES (?, ?, 'TJMT', ?, ?)", (id_, numero, data, texto))


def _prazos(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM prazo ORDER BY id")]


def test_publicacao_que_vira_cartao_abre_prazo_aguardando_o_lex(conn):
    _processo(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    p = _prazos(conn)[0]
    assert p["estado"] == "aguardando_lex" and p["origem_tipo"] == "publicacao"
    assert p["origem_ref"] == "1" and p["disponibilizacao"] == "2026-11-05"
    d = conn.execute("SELECT id FROM demanda").fetchone()[0]
    assert p["demanda_id"] == d


def test_toda_escrita_de_publicacao_chega_a_fila_pela_varredura(conn):
    _processo(conn)
    carteira.gravar_publicacoes_novas(conn, [{
        "id": 9, "numero": A, "tribunal": "TJMT", "data_disponibilizacao": "2026-11-05",
        "tipo": "", "orgao": "", "classe": "", "texto": "x", "link": "", "oab_confirmada": True}])
    assert _prazos(conn) == []  # ainda sem cartão
    quadro.gerar_pendentes(conn, HOJE)
    assert [p["origem_ref"] for p in _prazos(conn)] == ["9"]


def test_rodar_de_novo_nao_duplica(conn):
    _processo(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    quadro.gerar_pendentes(conn, HOJE)
    assert len(_prazos(conn)) == 1


@pytest.mark.parametrize("campos", [{"arquivado_por_voce_em": "x"}, {"excluido_em": "x"},
                                    {"descartado_em": "x", "advogado_atua": 0}])
def test_processo_que_nao_gera_cartao_nao_gera_prazo(conn, campos):
    _processo(conn, **campos)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    assert _prazos(conn) == []


def test_publicacao_velha_fora_da_janela_nao_gera_prazo(conn):
    _processo(conn)
    _publicacao(conn, data="2026-06-01")
    quadro.gerar_pendentes(conn, HOJE)
    assert _prazos(conn) == []


def _cartao(conn, numero, coluna, item_ref, tipo="publicacao"):
    d = conn.execute("INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em) "
                     "VALUES (?, ?, '2026-10-01', 'x', 'x')", (numero, coluna)).lastrowid
    conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) VALUES (?, ?, ?, '2026-10-01')",
                 (d, tipo, item_ref))
    return d


@pytest.mark.parametrize("coluna, abre", [("chegou", True), ("acao", True), ("autos", True),
                                          ("lex", True), ("revisao", True), ("acompanhar", True),
                                          ("resolvida", False), ("protocolado", False)])
def test_alcance_so_os_cartoes_abertos(conn, coluna, abre):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, coluna, "1")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == int(abre)
    assert len(_prazos(conn)) == int(abre)


def test_a_varredura_dos_cartoes_abertos_e_idempotente_e_roda_toda_vez(conn):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, "autos", "1")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1
    assert config.ler(conn, prazos_banco.CHAVE_ALCANCE)
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0
    _publicacao(conn, id_=2, data="2026-10-02")
    _cartao(conn, A, "autos", "2")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1
    assert len(_prazos(conn)) == 2


def test_a_fila_so_faz_o_alcance_na_primeira_vez(conn):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, "autos", "1")
    prazos_banco.proxima_leitura(conn, AGORA)
    assert config.ler(conn, prazos_banco.CHAVE_ALCANCE)
    _publicacao(conn, id_=2, data="2026-10-02")
    _cartao(conn, A, "autos", "2")
    prazos_banco.proxima_leitura(conn, AGORA)
    assert [p["origem_ref"] for p in _prazos(conn)] == ["1"]  # o resto é da varredura


def test_a_primeira_consulta_da_fila_traz_os_cartoes_abertos(conn):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, "autos", "1")
    pacote = prazos_banco.proxima_leitura(conn, AGORA)
    assert pacote and "Cumpra-se" in pacote["pedido"]


def _em_revisao(conn):
    _processo(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    d = conn.execute("SELECT id FROM demanda").fetchone()[0]
    conn.execute("UPDATE demanda SET coluna = 'revisao' WHERE id = ?", (d,))
    return d, _prazos(conn)[0]["id"]


def test_protocolado_marca_cumprido_e_desfazer_volta(conn):
    d, pid = _em_revisao(conn)
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, pid)
    r = quadro.mover(conn, d, "protocolado", "advogado")
    assert r["prazos_cumpridos"] == 1 and r["desfazer"] == "revisao"
    assert prazos_banco.obter(conn, pid)["estado"] == "cumprido"
    quadro.mover(conn, d, "revisao", "advogado")
    assert prazos_banco.obter(conn, pid)["estado"] == "conferido"


def test_arquivar_nao_tira_o_prazo(conn):
    _processo(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _prazos(conn)[0]["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    carteira.arquivar(conn, A)
    quadro.tirar_do_quadro(conn, A, motivo=quadro.MOTIVO_ARQUIVADO)
    assert prazos_banco.obter(conn, pid)["estado"] == "a_conferir"


# --- falha na fila de prazos nunca derruba o cartão nem a varredura ------------------------

def _quebrar(monkeypatch, nome):
    def estoura(*args, **kwargs):
        raise RuntimeError("falha de teste")
    monkeypatch.setattr(prazos_banco, nome, estoura)


def test_falha_ao_abrir_prazo_nao_impede_o_cartao(conn, monkeypatch):
    _processo(conn)
    _publicacao(conn)
    _quebrar(monkeypatch, "abrir")
    assert quadro.gerar_pendentes(conn, HOJE) == 1
    assert conn.execute("SELECT COUNT(*) FROM demanda_item").fetchone()[0] == 1
    assert _prazos(conn) == []


def test_falha_no_alcance_nao_quebra_e_tenta_de_novo(conn, monkeypatch):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, "autos", "1")
    _quebrar(monkeypatch, "abrir")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0
    monkeypatch.undo()
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1


def test_falha_na_fila_nao_desfaz_o_resto_da_transacao(conn, monkeypatch):
    _processo(conn)
    _publicacao(conn)
    _quebrar(monkeypatch, "abrir")
    with conn:
        quadro.gerar_pendentes(conn, HOJE)
        assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 1


# --- a varredura recorrente --------------------------------------------------------------

def test_abrir_falha_uma_vez_e_a_proxima_varredura_cria_o_prazo(conn, monkeypatch):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, "autos", "1")
    real = prazos_banco.abrir
    _quebrar(monkeypatch, "abrir")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0
    assert _prazos(conn) == []
    monkeypatch.setattr(prazos_banco, "abrir", real)
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1


def test_criar_para_item_que_falhou_e_recuperado_pela_varredura(conn, monkeypatch):
    _processo(conn)
    _publicacao(conn)
    real = prazos_banco.abrir
    _quebrar(monkeypatch, "abrir")
    quadro.gerar_pendentes(conn, HOJE)
    assert _prazos(conn) == []
    monkeypatch.setattr(prazos_banco, "abrir", real)
    prazos_banco.criar_dos_cartoes_abertos(conn)
    assert [p["estado"] for p in _prazos(conn)] == ["aguardando_lex"]


def test_cartao_arquivado_antes_da_atualizacao_e_desarquivado_ganha_o_prazo(conn):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    d = _cartao(conn, A, "resolvida", "1")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0
    conn.execute("UPDATE demanda SET coluna = 'chegou' WHERE id = ?", (d,))
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1


@pytest.mark.parametrize("estado", ["descartado", "sem_prazo", "a_definir", "cumprido"])
def test_prazo_ja_existente_em_qualquer_estado_nao_e_recriado(conn, estado):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    d = _cartao(conn, A, "autos", "1")
    pid = prazos_banco.abrir(conn, A, d, "publicacao", "1", "2026-10-01")
    conn.execute("UPDATE prazo SET estado = ? WHERE id = ?", (estado, pid))
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0
    assert [p["estado"] for p in _prazos(conn)] == [estado]


def test_aviso_ganha_prazo_a_definir_pelo_cartao_e_pela_varredura(conn):
    # Task 8: a intimação do PJe ganha um prazo «a definir» ao entrar no cartão, e a
    # varredura recupera a que ficou sem ele, esteja ou não na lista de pendentes.
    _processo(conn)
    d = _cartao(conn, A, "autos", "x:1", tipo="aviso")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1
    assert quadro.adicionar_item(conn, A, "aviso", "9:9", "2026-11-05") is not None
    assert sorted((p["origem_ref"], p["estado"]) for p in _prazos(conn)) == [
        ("9:9", "a_definir"), ("x:1", "a_definir")]
    assert prazos_banco.criar_para_item(conn, A, d, "aviso", "9:9", "2026-11-05") is None


# --- processo descartado ("não é meu") -----------------------------------------------------

DESCARTADO = {"descartado_em": "2026-10-01", "advogado_atua": 0}


def test_processo_descartado_nao_ganha_prazo_nem_da_varredura_nem_do_cartao(conn):
    _processo(conn, **DESCARTADO)
    _publicacao(conn, data="2026-10-01")
    d = _cartao(conn, A, "autos", "1")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0
    assert prazos_banco.criar_para_item(conn, A, d, "publicacao", "1", "2026-10-01") is None
    assert _prazos(conn) == []


def test_processo_descartado_sem_a_marca_de_atuacao_tambem_nao(conn):
    _processo(conn, descartado_em="2026-10-01", advogado_atua=None)
    _publicacao(conn, data="2026-10-01")
    d = _cartao(conn, A, "autos", "1")
    assert prazos_banco.criar_para_item(conn, A, d, "publicacao", "1", "2026-10-01") is None


@pytest.mark.parametrize("campos", [{"arquivado_por_voce_em": "x"}, {"excluido_em": "x"},
                                    {"arquivado": 1}])
def test_processo_arquivado_ou_na_lixeira_mantem_e_ganha_prazo(conn, campos):
    _processo(conn, **campos)
    _publicacao(conn, data="2026-10-01")
    d = _cartao(conn, A, "revisao", "1")
    assert prazos_banco.criar_para_item(conn, A, d, "publicacao", "1", "2026-10-01") is not None


def test_fila_pula_processo_descartado_sem_mudar_o_estado_e_le_se_voltar(conn):
    _processo(conn, **DESCARTADO)
    _publicacao(conn)
    d = _cartao(conn, A, "autos", "1")
    pid = prazos_banco.abrir(conn, A, d, "publicacao", "1", "2026-11-05")
    config.gravar(conn, prazos_banco.CHAVE_ALCANCE, "x")
    assert prazos_banco.proxima_leitura(conn, AGORA) is None
    assert prazos_banco.obter(conn, pid)["estado"] == "aguardando_lex"
    conn.execute("UPDATE processo SET advogado_atua = 1 WHERE numero = ?", (A,))
    assert prazos_banco.proxima_leitura(conn, AGORA)["id"] == pid


def test_fila_le_prazo_de_processo_arquivado(conn):
    _processo(conn, arquivado_por_voce_em="x")
    _publicacao(conn)
    d = _cartao(conn, A, "revisao", "1")
    pid = prazos_banco.abrir(conn, A, d, "publicacao", "1", "2026-11-05")
    config.gravar(conn, prazos_banco.CHAVE_ALCANCE, "x")
    assert prazos_banco.proxima_leitura(conn, AGORA)["id"] == pid


# --- ordem da fila --------------------------------------------------------------------------

def test_publicacao_recente_vai_antes_do_acumulado_e_o_acumulado_escoa(conn):
    _processo(conn)
    config.gravar(conn, prazos_banco.CHAVE_ALCANCE, "x")
    d = _cartao(conn, A, "acompanhar", "1")
    for i, data in enumerate(["2026-01-10", "2026-01-11", "2026-01-12", "2026-01-13"], start=1):
        _publicacao(conn, id_=i, data=data)
        prazos_banco.abrir(conn, A, d, "publicacao", str(i), data)
    _publicacao(conn, id_=10, data="2026-11-04")
    prazos_banco.abrir(conn, A, d, "publicacao", "10", "2026-11-04")
    _publicacao(conn, id_=11, data="2026-10-20")
    prazos_banco.abrir(conn, A, d, "publicacao", "11", "2026-10-20")
    ordem = []
    for _ in range(6):
        pacote = prazos_banco.proxima_leitura(conn, AGORA)
        ordem.append(prazos_banco.obter(conn, pacote["id"])["origem_ref"])
        conn.execute("UPDATE prazo SET estado = 'a_definir' WHERE id = ?", (pacote["id"],))
    assert ordem == ["11", "10", "1", "2", "3", "4"]


def test_empate_de_data_desempata_pelo_id_do_prazo(conn):
    _processo(conn)
    config.gravar(conn, prazos_banco.CHAVE_ALCANCE, "x")
    d = _cartao(conn, A, "autos", "1")
    for i in (5, 3):
        _publicacao(conn, id_=i, data="2026-11-04")
        prazos_banco.abrir(conn, A, d, "publicacao", str(i), "2026-11-04")
    pacote = prazos_banco.proxima_leitura(conn, AGORA)
    assert prazos_banco.obter(conn, pacote["id"])["origem_ref"] == "5"  # a primeira aberta


# --- Protocolado só cumpre o que o advogado já viu com data ---------------------------------

def _dois_prazos_no_cartao(conn):
    """Um com data (a conferir) e um ainda sem leitura, no mesmo cartão em revisão."""
    _processo(conn)
    _publicacao(conn, id_=1)
    _publicacao(conn, id_=2)
    d = _cartao(conn, A, "revisao", "1")
    conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) "
                 "VALUES (?, 'publicacao', '2', '2026-11-05')", (d,))
    a = prazos_banco.abrir(conn, A, d, "publicacao", "1", "2026-11-05")
    b = prazos_banco.abrir(conn, A, d, "publicacao", "2", "2026-11-05")
    prazos_banco.corrigir(conn, a, ato="Réplica", dias=5, unidade="uteis")
    return d, a, b


@pytest.mark.parametrize("estado", ["aguardando_lex", "a_definir"])
def test_protocolado_nao_cumpre_o_que_ninguem_viu_com_data(conn, estado):
    d, a, b = _dois_prazos_no_cartao(conn)
    conn.execute("UPDATE prazo SET estado = ? WHERE id = ?", (estado, b))
    r = quadro.mover(conn, d, "protocolado", "advogado")
    assert r["prazos_cumpridos"] == 1 and r["prazos_nao_lidos"] == 1
    assert prazos_banco.obter(conn, a)["estado"] == "cumprido"
    assert prazos_banco.obter(conn, b)["estado"] == estado
    quadro.mover(conn, d, "revisao", "advogado")
    assert prazos_banco.obter(conn, a)["estado"] == "a_conferir"
    assert prazos_banco.obter(conn, b)["estado"] == estado


def test_protocolado_cumpre_a_conferir_e_conferido(conn):
    d, a, b = _dois_prazos_no_cartao(conn)
    prazos_banco.corrigir(conn, b, ato="Réplica", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, b)
    r = quadro.mover(conn, d, "protocolado", "advogado")
    assert r["prazos_cumpridos"] == 2 and r["prazos_nao_lidos"] == 0
    quadro.mover(conn, d, "revisao", "advogado")
    assert prazos_banco.obter(conn, a)["estado"] == "a_conferir"
    assert prazos_banco.obter(conn, b)["estado"] == "conferido"


def test_cumprido_a_mao_continua_cumprido_ao_desfazer_o_protocolado(conn):
    d, a, b = _dois_prazos_no_cartao(conn)
    prazos_banco.cumprir(conn, a)
    quadro.mover(conn, d, "protocolado", "advogado")
    quadro.mover(conn, d, "revisao", "advogado")
    assert prazos_banco.obter(conn, a)["estado"] == "cumprido"


# --- falha, isolamento e transação -----------------------------------------------------------

def test_isolado_desfaz_a_escrita_parcial_com_transacao_aberta(conn):
    config.gravar(conn, "de_fora", "1")  # abre a transação do chamador
    with pytest.raises(RuntimeError):
        with prazos_banco.isolado(conn):
            config.gravar(conn, "parcial", "1")
            raise RuntimeError("depois da escrita")
    assert not config.ler(conn, "parcial") and config.ler(conn, "de_fora") == "1"
    assert conn.in_transaction


def test_isolado_sem_transacao_aberta_desfaz_a_escrita_parcial_e_nao_deixa_aberta(conn):
    assert not conn.in_transaction
    with pytest.raises(RuntimeError):
        with prazos_banco.isolado(conn):
            config.gravar(conn, "parcial", "1")
            raise RuntimeError("depois da escrita")
    assert not config.ler(conn, "parcial") and not conn.in_transaction


def test_isolado_com_sucesso_sem_transacao_aberta_fecha_a_que_abriu(conn):
    with prazos_banco.isolado(conn):
        config.gravar(conn, "feito", "1")
    assert not conn.in_transaction and config.ler(conn, "feito") == "1"


def test_isolado_dentro_de_transacao_nao_comita_e_some_com_o_rollback_de_fora(conn):
    config.gravar(conn, "de_fora", "1")
    with prazos_banco.isolado(conn):
        config.gravar(conn, "dentro", "1")
    conn.rollback()
    assert not config.ler(conn, "dentro") and not config.ler(conn, "de_fora")


def test_rollback_de_fora_no_meio_sobe_como_transacao_perdida(conn):
    config.gravar(conn, "de_fora", "1")
    with pytest.raises(prazos_banco.TransacaoPerdida):
        with prazos_banco.isolado(conn):
            conn.rollback()  # a transação do chamador some
            raise RuntimeError("falha")


def test_sem_quebrar_nao_engole_transacao_perdida(conn):
    config.gravar(conn, "de_fora", "1")

    def trabalho():
        conn.rollback()
        raise RuntimeError("falha")
    with pytest.raises(prazos_banco.TransacaoPerdida):
        prazos_banco.sem_quebrar(conn, "teste", trabalho)


def _escrever_e_estourar_na_segunda(monkeypatch):
    real = prazos_banco.abrir
    chamadas = []

    def abrir(*args, **kwargs):
        chamadas.append(args[4])
        r = real(*args, **kwargs)
        if len(chamadas) == 2:
            raise RuntimeError("falha de teste")
        return r
    monkeypatch.setattr(prazos_banco, "abrir", abrir)


def test_falha_depois_da_escrita_parcial_na_varredura_desfaz_so_essa_linha(conn, monkeypatch):
    _processo(conn)
    for i in (1, 2, 3):
        _publicacao(conn, id_=i, data="2026-10-01")
    d = _cartao(conn, A, "autos", "1")
    for i in (2, 3):
        conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) "
                     "VALUES (?, 'publicacao', ?, '2026-10-02')", (d, str(i)))
    _escrever_e_estourar_na_segunda(monkeypatch)
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 2
    assert sorted(p["origem_ref"] for p in _prazos(conn)) == ["1", "3"]
    assert not [e for e in conn.execute("SELECT dados FROM evento WHERE tipo = 'prazo_aberto'")
                if '"2"' in e["dados"]]


def test_falha_depois_da_escrita_parcial_em_criar_para_item_desfaz(conn, monkeypatch):
    _processo(conn)
    d = _cartao(conn, A, "autos", "1")
    real = prazos_banco.abrir

    def abrir(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("falha de teste")
    monkeypatch.setattr(prazos_banco, "abrir", abrir)
    assert prazos_banco.criar_para_item(conn, A, d, "publicacao", "55", "2026-11-05") is None
    assert _prazos(conn) == []


def test_cumprir_que_falha_no_meio_desfaz_tudo_e_o_cartao_anda_com_aviso(conn, monkeypatch):
    d, a, b = _dois_prazos_no_cartao(conn)
    prazos_banco.corrigir(conn, b, ato="Réplica", dias=5, unidade="uteis")
    real = prazos_banco.cumprir
    chamadas = []

    def cumprir(*args, **kwargs):
        chamadas.append(args[1])
        real(*args, **kwargs)
        if len(chamadas) == 2:
            raise RuntimeError("falha de teste")
    monkeypatch.setattr(prazos_banco, "cumprir", cumprir)
    r = quadro.mover(conn, d, "protocolado", "advogado")
    assert r["prazos_falha"] is True and r["prazos_cumpridos"] == 0
    assert conn.execute("SELECT coluna FROM demanda WHERE id = ?", (d,)).fetchone()[0] == "protocolado"
    assert prazos_banco.obter(conn, a)["estado"] == "a_conferir"
    assert prazos_banco.obter(conn, b)["estado"] == "a_conferir"


def test_mover_nao_diz_que_moveu_se_a_transacao_de_fora_se_perdeu(conn, monkeypatch):
    d, a, b = _dois_prazos_no_cartao(conn)

    def cumprir(*args, **kwargs):
        conn.rollback()
        raise RuntimeError("falha de teste")
    monkeypatch.setattr(prazos_banco, "cumprir_do_cartao", cumprir)
    with pytest.raises(prazos_banco.TransacaoPerdida):
        quadro.mover(conn, d, "protocolado", "advogado")


def test_desfazer_que_falha_nao_move_o_cartao_nem_deixa_prazo_cumprido(conn, monkeypatch):
    d, a, b = _dois_prazos_no_cartao(conn)
    quadro.mover(conn, d, "protocolado", "advogado")
    _quebrar(monkeypatch, "desfazer_do_cartao")
    with pytest.raises(quadro.PrazosNaoVoltaram):
        quadro.mover(conn, d, "revisao", "advogado")
    assert conn.execute("SELECT coluna FROM demanda WHERE id = ?", (d,)).fetchone()[0] == "protocolado"
    assert prazos_banco.obter(conn, a)["estado"] == "cumprido"


# --- rodada 2 -------------------------------------------------------------------------------

def test_borda_de_30_e_31_dias_da_janela_da_fila(conn):
    # AGORA é 06/11: 07/10 está a 30 dias (recente); 06/10, a 31 (acumulado).
    _processo(conn)
    config.gravar(conn, prazos_banco.CHAVE_ALCANCE, "x")
    d = _cartao(conn, A, "autos", "1")
    for i, data in ((1, "2026-10-06"), (2, "2026-10-07")):
        _publicacao(conn, id_=i, data=data)
        prazos_banco.abrir(conn, A, d, "publicacao", str(i), data)
    pacote = prazos_banco.proxima_leitura(conn, AGORA)
    assert prazos_banco.obter(conn, pacote["id"])["origem_ref"] == "2"


def test_marca_da_primeira_rodada_so_se_nenhuma_linha_falhou(conn, monkeypatch):
    # A regra do código: a marca `prazos_alcance_em` só é gravada quando a varredura
    # termina sem nenhuma linha falhar; a fila então tenta a primeira rodada de novo.
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, "autos", "1")
    real = prazos_banco.abrir
    _quebrar(monkeypatch, "abrir")
    prazos_banco.criar_dos_cartoes_abertos(conn)
    assert not config.ler(conn, prazos_banco.CHAVE_ALCANCE)
    monkeypatch.setattr(prazos_banco, "abrir", real)
    prazos_banco.criar_dos_cartoes_abertos(conn)
    assert config.ler(conn, prazos_banco.CHAVE_ALCANCE)


def test_varredura_nao_engole_transacao_perdida(conn, monkeypatch):
    _processo(conn)
    _publicacao(conn, data="2026-10-01")
    _cartao(conn, A, "autos", "1")

    def abrir(*_a, **_k):
        conn.rollback()
        raise RuntimeError("falha de teste")
    monkeypatch.setattr(prazos_banco, "abrir", abrir)
    config.gravar(conn, "de_fora", "1")
    with pytest.raises(prazos_banco.TransacaoPerdida):
        prazos_banco.criar_dos_cartoes_abertos(conn)


def test_release_que_falha_no_sucesso_sobe_como_transacao_perdida(conn):
    config.gravar(conn, "de_fora", "1")
    with pytest.raises(prazos_banco.TransacaoPerdida):
        with prazos_banco.isolado(conn):
            conn.rollback()  # sem erro no corpo: quem falha é o RELEASE


def test_isolado_pega_a_trava_de_escrita_ao_abrir(tmp_path):
    import sqlite3
    caminho = str(tmp_path / "t.db")
    a = banco.conectar(caminho)
    b = sqlite3.connect(caminho, timeout=0)
    with prazos_banco.isolado(a):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            b.execute("INSERT INTO config (chave, valor) VALUES ('b', '1')")
    b.execute("INSERT INTO config (chave, valor) VALUES ('b', '1')")
    b.commit()
    a.close()
    b.close()


def test_mover_de_protocolado_aguenta_gravacao_de_outra_conexao_no_meio(tmp_path, monkeypatch):
    import sqlite3
    import threading
    caminho = str(tmp_path / "m.db")
    a = banco.conectar(caminho)
    _processo(a)
    _publicacao(a)
    quadro.gerar_pendentes(a, HOJE)
    d = a.execute("SELECT id FROM demanda").fetchone()[0]
    a.execute("UPDATE demanda SET coluna = 'protocolado' WHERE id = ?", (d,))
    a.commit()
    real = quadro._demanda
    feitos = []

    def escritor():
        b = sqlite3.connect(caminho, timeout=20)
        b.execute("INSERT INTO config (chave, valor) VALUES ('outro', '1')")
        b.commit()
        feitos.append(True)
        b.close()

    def demanda_com_escritor(conn, demanda_id):
        lida = real(conn, demanda_id)  # a leitura que prende o instantâneo
        t = threading.Thread(target=escritor)
        t.start()
        threading.Event().wait(0.3)  # o outro escritor tenta comitar bem aqui
        demanda_com_escritor.t = t
        return lida
    monkeypatch.setattr(quadro, "_demanda", demanda_com_escritor)
    r = quadro.mover(a, d, "revisao", "advogado")  # não pode dar «database is locked»
    assert r["para"] == "revisao"
    demanda_com_escritor.t.join(10)
    assert feitos == [True]
    a.close()
