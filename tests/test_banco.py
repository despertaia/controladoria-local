import pytest
import sqlite3

from nucleo import banco


def _demanda(conn, numero, coluna):
    conn.execute("INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em) "
                 "VALUES (?, ?, '2026-10-01', 'x', 'x')", (numero, coluna))


def test_conectar_cria_tabelas_e_versao():
    conn = banco.conectar(":memory:")
    tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"processo", "publicacao", "andamento", "evento"} <= tabelas
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES)


def test_migrar_de_novo_nao_faz_nada():
    conn = banco.conectar(":memory:")
    conn.execute("INSERT INTO processo (numero, tribunal) VALUES ('1', 'TJMT')")
    assert banco.migrar(conn) == len(banco.MIGRACOES)
    assert conn.execute("SELECT COUNT(*) FROM processo").fetchone()[0] == 1


def test_conectar_cria_a_pasta_do_arquivo(tmp_path, monkeypatch):
    caminho = tmp_path / "sub" / "controladoria.db"
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(caminho))
    banco.conectar().close()
    assert caminho.exists()


def test_migrar_recusa_banco_mais_novo_que_o_codigo():
    conn = banco.conectar(":memory:")
    conn.execute(f"PRAGMA user_version = {len(banco.MIGRACOES) + 1}")
    with pytest.raises(RuntimeError, match="mais novo que este código"):
        banco.migrar(conn)


def test_migracao_2_cria_quadro_fila_e_avisos():
    conn = banco.conectar(":memory:")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES)
    tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"demanda", "demanda_item", "aviso", "tarefa"} <= tabelas
    assert "descartado_em" in {r[1] for r in conn.execute("PRAGMA table_info(processo)")}


def test_um_so_cartao_em_chegou_por_processo():
    conn = banco.conectar(":memory:")
    _demanda(conn, "1", "chegou")
    _demanda(conn, "1", "acao")
    _demanda(conn, "2", "chegou")
    with pytest.raises(sqlite3.IntegrityError):
        _demanda(conn, "1", "chegou")


def test_coluna_e_estado_invalidos_sao_recusados():
    conn = banco.conectar(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        _demanda(conn, "1", "lixo")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tarefa (tipo, ref, estado, pedida_em, pedida_por) "
                     "VALUES ('varredura', '', 'talvez', 'x', 'x')")


def test_item_entra_em_um_cartao_so():
    conn = banco.conectar(":memory:")
    _demanda(conn, "1", "chegou")
    conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) VALUES (1, 'publicacao', '9', '2026-10-01')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) VALUES (1, 'publicacao', '9', '2026-10-01')")


def test_banco_da_versao_1_migra_sem_perder_dados(tmp_path):
    caminho = str(tmp_path / "v1.db")
    antigo = sqlite3.connect(caminho)
    antigo.executescript(f"BEGIN;\n{banco.MIGRACOES[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    antigo.execute("INSERT INTO processo (numero, tribunal) VALUES ('1', 'TJMT')")
    antigo.commit()
    antigo.close()
    conn = banco.conectar(caminho)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES)
    linha = conn.execute("SELECT numero, descartado_em FROM processo").fetchone()
    assert tuple(linha) == ("1", None)


def test_migracao_3_indexa_evento_por_tipo():
    conn = banco.conectar(":memory:")
    assert len(banco.MIGRACOES) >= 3
    indices = {r[1] for r in conn.execute("PRAGMA index_list(evento)")}
    assert "evento_tipo" in indices


def test_migracao_4_colunas_do_lex_e_tabelas_novas():
    conn = banco.conectar(":memory:")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES) >= 4
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(demanda)")}
    assert {"lex_estado", "lex_orientacao", "lex_ajuste", "lex_squad", "lex_etapa",
            "lex_detalhe", "lex_reservado_ate", "lex_tentar_depois", "protocolado_em"} <= colunas
    tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"execucao_lex", "config"} <= tabelas
    for coluna in ("lex", "revisao", "protocolado"):
        _demanda(conn, "9" + coluna, coluna)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE demanda SET lex_estado = 'lixo' WHERE id = 1")


def test_migracao_4_preserva_cartoes_e_indice_parcial(tmp_path):
    caminho = str(tmp_path / "v3.db")
    antigo = sqlite3.connect(caminho)
    for i, sql in enumerate(banco.MIGRACOES[:3], start=1):
        antigo.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {i};\nCOMMIT;")
    antigo.execute("INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em, "
                   "autos_estado) VALUES ('1', 'autos', '2026-10-01', 'x', 'x', 'ok')")
    antigo.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) VALUES (1, 'publicacao', '5', '2026-10-01')")
    antigo.commit(); antigo.close()
    conn = banco.conectar(caminho)
    linha = conn.execute("SELECT id, coluna, autos_estado, lex_estado FROM demanda").fetchone()
    assert tuple(linha) == (1, "autos", "ok", "")
    assert conn.execute("SELECT demanda_id FROM demanda_item").fetchone()[0] == 1
    _demanda(conn, "2", "chegou")
    with pytest.raises(sqlite3.IntegrityError):
        _demanda(conn, "2", "chegou")


def test_migracao_4_checks_de_execucao_lex():
    conn = banco.conectar(":memory:")
    _demanda(conn, "1", "lex")
    conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em) "
                 "VALUES (1, 1, 'novo', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em) "
                     "VALUES (1, 2, 'outro', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE execucao_lex SET resultado = 'lixo' WHERE n = 1")
    for resultado in ("pronto", "falhou", "limite", "sem_tipo", "tempo", "reserva_vencida", ""):
        conn.execute("UPDATE execucao_lex SET resultado = ? WHERE n = 1", (resultado,))
    with pytest.raises(sqlite3.IntegrityError):  # uma execução n por cartão
        conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em) "
                     "VALUES (1, 1, 'ajuste', 'x')")


def test_migracao_5_nome_do_squad(tmp_path):
    caminho = str(tmp_path / "v4.db")
    antigo = sqlite3.connect(caminho)
    for i, sql in enumerate(banco.MIGRACOES[:4], start=1):
        antigo.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {i};\nCOMMIT;")
    antigo.execute("INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em, "
                   "lex_squad) VALUES ('1', 'revisao', '2026-10-01', 'x', 'x', 'replica')")
    antigo.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, squad) "
                   "VALUES (1, 1, 'novo', 'x', 'replica')")
    antigo.commit(); antigo.close()
    conn = banco.conectar(caminho)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES) >= 5
    assert tuple(conn.execute("SELECT lex_squad, lex_squad_nome FROM demanda").fetchone()) == (
        "replica", "")
    assert tuple(conn.execute("SELECT squad, squad_nome FROM execucao_lex").fetchone()) == (
        "replica", "")


def test_migracao_6_confirmacao_e_partes_da_publicacao(tmp_path):
    caminho = str(tmp_path / "v5.db")
    antigo = sqlite3.connect(caminho)
    for i, sql in enumerate(banco.MIGRACOES[:5], start=1):
        antigo.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {i};\nCOMMIT;")
    antigo.execute("INSERT INTO processo (numero, tribunal, descartado_em) "
                   "VALUES ('1', 'TRF1', '2026-10-01')")
    antigo.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                   "VALUES (9, '1', 'TRF1', '2026-10-01', 'Intime-se.')")
    antigo.commit(); antigo.close()
    conn = banco.conectar(caminho)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES) >= 6
    assert tuple(conn.execute("SELECT descartado_em, confirmado_em FROM processo").fetchone()) == (
        "2026-10-01", None)
    assert tuple(conn.execute("SELECT texto, partes_json FROM publicacao").fetchone()) == (
        "Intime-se.", "[]")
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao) "
                 "VALUES (10, '1', 'TRF1', '2026-10-02')")
    assert conn.execute("SELECT partes_json FROM publicacao WHERE id = 10").fetchone()[0] == "[]"


def test_threads_abrindo_banco_novo_ao_mesmo_tempo_nao_brigam(tmp_path):
    """Instalação local: painel, trabalhador e ponte abrem o banco novo juntos."""
    import threading

    caminho = str(tmp_path / "novo.db")
    erros, largada = [], threading.Barrier(6)

    def abrir():
        largada.wait()
        try:
            banco.conectar(caminho).close()
        except Exception as exc:  # noqa: BLE001
            erros.append(exc)
    fios = [threading.Thread(target=abrir) for _ in range(6)]
    for f in fios:
        f.start()
    for f in fios:
        f.join()
    assert erros == []
    conn = banco.conectar(caminho)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES)
    conn.close()
