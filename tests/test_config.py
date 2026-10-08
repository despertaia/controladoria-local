import pytest

from nucleo import banco, config

_N = [0]


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _cartao_em(conn, coluna):
    _N[0] += 1
    numero = f"{_N[0]:07d}0220248110041"
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado) "
                 "VALUES (?, 'TJMT', 1, 0)", (numero,))
    cur = conn.execute("INSERT INTO demanda (numero, coluna, referencia_em, criada_em, "
                       "atualizada_em) VALUES (?, ?, '2026-10-05', 'x', 'x')", (numero, coluna))
    return cur.lastrowid


def _coluna(conn, d):
    return conn.execute("SELECT coluna FROM demanda WHERE id = ?", (d,)).fetchone()[0]


def _lex(conn, d):
    return conn.execute("SELECT lex_estado FROM demanda WHERE id = ?", (d,)).fetchone()[0]


def test_config_ler_gravar_e_chave(conn):
    assert config.ler(conn, "x", "padrao") == "padrao"
    config.gravar(conn, "x", "1"); config.gravar(conn, "x", "2")
    assert config.ler(conn, "x") == "2"
    assert config.lex_automatico(conn) is False


def test_ligar_chave_enfileira_os_que_estao_em_autos(conn):
    a = _cartao_em(conn, "autos"); b = _cartao_em(conn, "acao")
    assert config.definir_lex_automatico(conn, True) == 1
    assert config.lex_automatico(conn) is True
    assert _coluna(conn, a) == "lex" and _lex(conn, a) == "na_fila"
    assert _coluna(conn, b) == "acao"
    assert config.definir_lex_automatico(conn, False) == 0
    assert config.lex_automatico(conn) is False
    tipos = [r[0] for r in conn.execute("SELECT tipo FROM evento WHERE tipo LIKE "
                                        "'lex_automatico%' ORDER BY id")]
    assert tipos == ["lex_automatico_ligado", "lex_automatico_desligado"]


def test_ligar_chave_nao_enfileira_cartao_que_espera_orientacao(conn):
    a = _cartao_em(conn, "autos"); b = _cartao_em(conn, "autos")
    conn.execute("UPDATE demanda SET lex_estado = 'sem_tipo' WHERE id = ?", (b,))
    assert config.definir_lex_automatico(conn, True) == 1
    assert _coluna(conn, a) == "lex" and _coluna(conn, b) == "autos"
    assert _lex(conn, b) == "sem_tipo"
