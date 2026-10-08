"""Ponte do Lex no modo local: chave gerada no banco e guardada no cofre, laço numa
thread do lançador e parada."""

import threading

import pytest

from nucleo import banco, cofre, config, ponte
from ponte_mac import chaves, executor, laco
from ponte_mac import local as ponte_local
from tests.cofre_falso import cofre_falso  # noqa: F401


@pytest.fixture
def local(cofre_falso, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "c.db"))
    monkeypatch.setattr(executor, "_parada", threading.Event())
    monkeypatch.setattr(executor, "_casa_local", "")
    return cofre_falso


def _conn():
    return banco.conectar()


def test_preparar_chave_local_gera_guarda_e_reusa(local):
    conn = _conn()
    try:
        chave = ponte_local.preparar_chave_local(conn)
        assert ponte.chave_valida(conn, chave)
        assert local.itens == {(cofre.SERVICO, "ponte_chave"): chave}
        hash_antes = config.ler(conn, ponte.CHAVE_HASH)
        assert ponte_local.preparar_chave_local(conn) == chave  # idempotente
        assert config.ler(conn, ponte.CHAVE_HASH) == hash_antes
        n = conn.execute("SELECT COUNT(*) FROM evento WHERE tipo = 'ponte_chave_gerada'"
                         ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_preparar_chave_local_troca_se_o_banco_tem_outra(local):
    conn = _conn()
    try:
        antiga = ponte_local.preparar_chave_local(conn)
        with conn:
            ponte.gerar_chave(conn)  # alguém gerou outra pela tela
        nova = ponte_local.preparar_chave_local(conn)
        assert nova != antiga and ponte.chave_valida(conn, nova)
        assert chaves.ler(chaves.CHAVE_PONTE) == nova
    finally:
        conn.close()


def test_preparar_chave_local_cofre_recusou_nao_troca_o_banco(local, monkeypatch):
    conn = _conn()
    try:
        antiga = ponte_local.preparar_chave_local(conn)
        with conn:
            ponte.gerar_chave(conn)
        hash_do_banco = config.ler(conn, ponte.CHAVE_HASH)
        monkeypatch.setattr(local, "set_password", lambda *a: None)  # descarta
        with pytest.raises(RuntimeError):
            ponte_local.preparar_chave_local(conn)
        assert config.ler(conn, ponte.CHAVE_HASH) == hash_do_banco  # transação desfeita
        assert chaves.ler(chaves.CHAVE_PONTE) == antiga
    finally:
        conn.close()


def test_chave_do_banco_nao_derruba_com_erro(local, monkeypatch):
    def quebra(conn):
        raise RuntimeError("cofre fora")
    monkeypatch.setattr(ponte_local, "preparar_chave_local", quebra)
    assert ponte_local._chave_do_banco() is None


def test_casa_da_banca_vem_do_ambiente_ou_dos_ajustes(local, monkeypatch, tmp_path):
    ajustes = tmp_path / "ajustes.json"
    ajustes.write_text('{"casa_banca": "C:\\\\Users\\\\Fulano\\\\Banca"}', encoding="utf-8")
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(ajustes))
    monkeypatch.delenv("CONTROLADORIA_CASA_BANCA", raising=False)
    assert ponte_local.casa_da_banca() == "C:\\Users\\Fulano\\Banca"
    monkeypatch.setenv("CONTROLADORIA_CASA_BANCA", "/outra")
    assert ponte_local.casa_da_banca() == "/outra"


def test_rodar_local_sem_casa_espera_e_para(local, monkeypatch):
    monkeypatch.setattr(ponte_local, "casa_da_banca", lambda: "")
    esperas = []

    class Evento(threading.Event):
        def wait(self, s=None):
            esperas.append(s)
            self.set()
            return True

    parar = Evento()
    ponte_local.rodar_local("http://127.0.0.1:5056", None, parar,
                            rodar=lambda *a, **k: pytest.fail("sem casa não roda"),
                            obter_chave=lambda: pytest.fail("sem casa não pede chave"))
    assert esperas == [ponte_local.ESPERA_S]


def test_rodar_local_monta_o_laco_do_modo_local(local, tmp_path):
    parar = threading.Event()
    vistos = {}

    def rodar(painel, casa, token_fn, estado, **kw):
        vistos.update(painel=painel, casa=casa, token=token_fn(), estado=estado, **kw)
        parar.set()

    ponte_local.rodar_local("http://127.0.0.1:5056", str(tmp_path), parar,
                            estado=tmp_path / "e.json", rodar=rodar,
                            obter_chave=lambda: "C" * 43)
    assert vistos["painel"].url == "http://127.0.0.1:5056"
    assert vistos["casa"] == tmp_path and vistos["estado"] == tmp_path / "e.json"
    assert vistos["token"] == ""  # sem acesso guardado: login normal do claude
    assert vistos["exigir_token"] is False
    assert vistos["parar"]() is True and vistos["dormir"] == parar.wait
    assert vistos["recriar"]().url == "http://127.0.0.1:5056"
    assert vistos["reler_chave"]() == "C" * 43
    assert executor._parada is parar
    assert executor._casa_local == str(tmp_path)


def test_rodar_local_usa_o_acesso_guardado(local, tmp_path):
    chaves.guardar(chaves.ACESSO_CLAUDE, "sk-ant-oat01-abc")
    parar = threading.Event()
    tokens = []

    def rodar(painel, casa, token_fn, estado, **kw):
        tokens.append(token_fn())
        parar.set()

    ponte_local.rodar_local("http://127.0.0.1:5056", str(tmp_path), parar, rodar=rodar,
                            obter_chave=lambda: "C" * 43, estado=tmp_path / "e.json")
    assert tokens == ["sk-ant-oat01-abc"]


def test_rodar_local_erro_no_laco_nao_derruba(local, tmp_path):
    class Evento(threading.Event):
        def wait(self, s=None):
            return False

    parar = Evento()
    voltas = []

    def rodar(*a, **k):
        voltas.append(1)
        if len(voltas) == 2:
            parar.set()
            return
        raise RuntimeError("quebrou")

    ponte_local.rodar_local("http://127.0.0.1:5056", str(tmp_path), parar, rodar=rodar,
                            obter_chave=lambda: "C" * 43, estado=tmp_path / "e.json")
    assert len(voltas) == 2


def test_rodar_em_thread_de_ponta_a_ponta_e_parar(local, tmp_path, monkeypatch):
    """Com o laço de verdade (painel falso): a thread sobe, consulta e para."""
    consultas = []

    class PainelFalso:
        chave_recusada = False

        def __init__(self, url, chave):
            self.url = url

        def proximo(self):
            consultas.append(1)
            return None

        def contato(self, ok):
            pass

    monkeypatch.setattr(ponte_local, "Painel", PainelFalso)
    monkeypatch.setattr(executor, "limpar_sobras", lambda casa: 0)
    parar = threading.Event()
    estado = tmp_path / "e.json"
    original = ponte_local.rodar_local

    def rodar_local(url, casa, evento, est):
        original(url, casa, evento, est, obter_chave=lambda: "C" * 43,
                 rodar=lambda *a, **k: laco.rodar(*a, validar=lambda t, c: True, **k))

    monkeypatch.setattr(ponte_local, "rodar_local", rodar_local)
    thread = ponte_local.rodar_em_thread("http://127.0.0.1:5056", str(tmp_path), parar,
                                         estado)
    for _ in range(200):
        if consultas:
            break
        threading.Event().wait(0.01)
    assert thread.is_alive() and thread.daemon
    ponte_local.parar(parar, thread, espera_s=5)
    assert not thread.is_alive() and consultas


def test_parar_encerra_o_lex_em_andamento(monkeypatch):
    encerrados = []
    monkeypatch.setattr(executor, "encerrar_em_andamento", lambda: encerrados.append(1) or 1)
    evento = threading.Event()
    ponte_local.parar(evento)
    assert evento.is_set() and encerrados == [1]
