import pytest

from nucleo import banco, tarefas_fila


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def test_pedir_nao_duplica_enquanto_ativa(conn):
    primeira = tarefas_fila.pedir(conn, "varredura", por="agendador")
    assert primeira is not None
    assert tarefas_fila.pedir(conn, "varredura") is None
    assert tarefas_fila.ativa(conn, "varredura")
    assert tarefas_fila.pedir(conn, "autos", "7") is not None
    assert tarefas_fila.pedir(conn, "autos", "8") is not None  # ref diferente


def test_tipo_desconhecido(conn):
    with pytest.raises(ValueError):
        tarefas_fila.pedir(conn, "protocolo")


def test_pegar_proxima_em_ordem_e_concluir(conn):
    a = tarefas_fila.pedir(conn, "autos", "1")
    b = tarefas_fila.pedir(conn, "varredura")
    t = tarefas_fila.pegar_proxima(conn)
    assert t["id"] == a and t["estado"] == "rodando" and t["iniciada_em"]
    assert tarefas_fila.ativa(conn, "autos", "1")  # rodando ainda conta
    tarefas_fila.concluir(conn, a)
    assert tarefas_fila.pegar_proxima(conn)["id"] == b
    tarefas_fila.concluir(conn, b, erro="DJEN fora do ar")
    estados = dict(conn.execute("SELECT id, estado FROM tarefa").fetchall())
    assert estados == {a: "ok", b: "falhou"}
    assert tarefas_fila.pegar_proxima(conn) is None
    assert tarefas_fila.pedir(conn, "varredura") is not None  # terminada libera


def test_cancelar_so_na_fila(conn):
    tarefas_fila.pedir(conn, "autos", "1")
    assert tarefas_fila.cancelar(conn, "autos", "1") is True
    assert not tarefas_fila.ativa(conn, "autos", "1")
    tarefas_fila.pedir(conn, "autos", "2")
    tarefas_fila.pegar_proxima(conn)
    assert tarefas_fila.cancelar(conn, "autos", "2") is False


def test_reabrir_interrompidas(conn):
    tarefas_fila.pedir(conn, "varredura")
    tarefas_fila.pegar_proxima(conn)
    assert tarefas_fila.reabrir_interrompidas(conn) == 1
    t = tarefas_fila.pegar_proxima(conn)
    assert t is not None and t["tipo"] == "varredura"
