import os
import time
from datetime import date, datetime, timedelta

import pytest

from nucleo import banco, eventos, quadro, tarefas_fila, trabalhador
from nucleo.carteira import Advogado

ADV = Advogado("MARIA EXEMPLO DA SILVA", "54321", "MT")
HOJE = date(2026, 10, 6)
A = "00000010220248110041"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def test_fila_vazia(conn):
    assert trabalhador.executar_uma(conn, adv=ADV, hoje=HOJE) is None


def test_executa_varredura_e_conclui(conn, monkeypatch):
    chamadas = []
    monkeypatch.setattr(trabalhador.varredura, "executar",
                        lambda c, adv, **kw: chamadas.append(kw["hoje"]) or {"erros": []})
    with conn:
        tarefas_fila.pedir(conn, "varredura", por="agendador")
    assert trabalhador.executar_uma(conn, adv=ADV, hoje=HOJE) == "varredura"
    assert chamadas == [HOJE]
    assert conn.execute("SELECT estado FROM tarefa").fetchone()[0] == "ok"


def test_varredura_que_quebra_vira_evento_e_tarefa_falha(conn, monkeypatch):
    def quebra(c, adv, **kw):
        raise KeyError("campo novo do DJEN")
    monkeypatch.setattr(trabalhador.varredura, "executar", quebra)
    with conn:
        tarefas_fila.pedir(conn, "varredura")
    trabalhador.executar_uma(conn, adv=ADV, hoje=HOJE)
    assert conn.execute("SELECT estado, erro FROM tarefa").fetchone()[0] == "falhou"
    falhou = [e for e in eventos.listar(conn) if e["tipo"] == "varredura_falhou"]
    assert falhou and "KeyError" in falhou[0]["dados"]["erro"]


def test_executa_autos(conn, monkeypatch):
    feitos = []
    monkeypatch.setattr(trabalhador.autos, "baixar_para_demanda",
                        lambda c, demanda_id, **kw: feitos.append(demanda_id) or {"estado": "ok"})
    with conn:
        tarefas_fila.pedir(conn, "autos", "7")
    assert trabalhador.executar_uma(conn, adv=ADV, hoje=HOJE) == "autos"
    assert feitos == [7]


def test_autos_que_quebra_marca_o_cartao(conn, monkeypatch):
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado) "
                 "VALUES (?, 'TJMT', 1, 0)", (A,))
    quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    quadro.mover(conn, 1, "acao", "advogado")
    conn.commit()

    def quebra(c, demanda_id, **kw):
        raise OSError("disco cheio")
    monkeypatch.setattr(trabalhador.autos, "baixar_para_demanda", quebra)
    trabalhador.executar_uma(conn, adv=ADV, hoje=HOJE)
    d = conn.execute("SELECT autos_estado, autos_detalhe FROM demanda").fetchone()
    assert d[0] == "falhou" and "disco cheio" in d[1]


def test_preparar_reabre_o_que_foi_interrompido(conn):
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado) "
                 "VALUES (?, 'TJMT', 1, 0)", (A,))
    quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    quadro.mover(conn, 1, "acao", "advogado")
    tarefas_fila.pegar_proxima(conn)
    quadro.marcar_autos(conn, 1, "baixando")
    conn.commit()
    assert trabalhador.preparar(conn) == 1
    assert conn.execute("SELECT autos_estado FROM demanda").fetchone()[0] == "na_fila"
    assert conn.execute("SELECT estado FROM tarefa").fetchone()[0] == "na_fila"


def test_batida(tmp_path):
    caminho = str(tmp_path / "batida")
    assert trabalhador.estado_da_batida(caminho) == "desconhecido"
    trabalhador.bater(caminho)
    assert trabalhador.estado_da_batida(caminho) == "ativo"
    velho = time.time() - 600
    os.utime(caminho, (velho, velho))
    assert trabalhador.estado_da_batida(caminho) == "parado"


def test_rodar_para_quando_pedido(tmp_path, monkeypatch):
    caminho = str(tmp_path / "t.db")
    monkeypatch.setattr(trabalhador, "BATIDA", str(tmp_path / "batida"))
    voltas = []
    trabalhador.rodar(lambda: banco.conectar(caminho), adv_fn=lambda: ADV,
                      hoje_fn=lambda: HOJE, pausa=0,
                      parar=lambda: voltas.append(1) or len(voltas) > 2)
    assert os.path.exists(tmp_path / "batida")


def test_batedor_atualiza_a_batida_enquanto_a_tarefa_roda(tmp_path, monkeypatch):
    import threading
    caminho = str(tmp_path / "batida")
    monkeypatch.setattr(trabalhador, "BATIDA", caminho)
    c = banco.conectar(str(tmp_path / "t.db"))
    with c:
        tarefas_fila.pedir(c, "varredura")
    c.close()
    velho = time.time() - 600
    trabalhador.bater(caminho)
    os.utime(caminho, (velho, velho))
    vista = threading.Event()

    def longa(conn, adv, **kw):  # tarefa "longa": só termina depois de ver a batida renovada
        for _ in range(200):
            if os.path.getmtime(caminho) > velho + 300:
                vista.set()
                break
            time.sleep(0.005)
        return {"erros": []}
    monkeypatch.setattr(trabalhador.varredura, "executar", longa)
    monkeypatch.setattr(trabalhador, "INTERVALO_BATIDA_SEGUNDOS", 0.01)
    voltas = []
    # parar() só autoriza a 1ª volta; o bater() do laço roda antes, então desligamos
    # esse para provar que quem renovou foi o fio de fundo.
    monkeypatch.setattr(trabalhador, "bater", lambda caminho=None: (
        open(caminho or trabalhador.BATIDA, "w").close()
        if threading.current_thread().name == "batida" else None))
    trabalhador.rodar(lambda: banco.conectar(str(tmp_path / "t.db")), adv_fn=lambda: ADV,
                      hoje_fn=lambda: HOJE, pausa=0,
                      parar=lambda: voltas.append(1) or len(voltas) > 1)
    assert vista.is_set()


def test_batedor_para_ao_fim(tmp_path, monkeypatch):
    monkeypatch.setattr(trabalhador, "BATIDA", str(tmp_path / "batida"))
    parado, fio = trabalhador.iniciar_batedor(0.01)
    time.sleep(0.05)
    parado.set()
    fio.join(timeout=1)
    assert not fio.is_alive() and os.path.exists(tmp_path / "batida")


def test_falha_desfaz_escritas_parciais_da_tarefa(conn, monkeypatch):
    def quebra(c, adv, **kw):
        c.execute("INSERT INTO evento (quando, tipo, dados) VALUES ('x', 'meio', '{}')")
        raise RuntimeError("caiu no meio")
    monkeypatch.setattr(trabalhador.varredura, "executar", quebra)
    with conn:
        tarefas_fila.pedir(conn, "varredura")
    trabalhador.executar_uma(conn, adv=ADV, hoje=HOJE)
    tipos = [e["tipo"] for e in eventos.listar(conn)]
    assert "meio" not in tipos and "varredura_falhou" in tipos


def test_referencia_malformada_nao_derruba_e_conclui_com_erro(conn):
    with conn:
        tarefas_fila.pedir(conn, "autos", "abc")
    assert trabalhador.executar_uma(conn, adv=ADV, hoje=HOJE) == "autos"
    estado, erro = conn.execute("SELECT estado, erro FROM tarefa").fetchone()
    assert estado == "falhou" and "abc" in erro


def test_rodar_vence_reservas_e_libera_pausados_a_cada_volta(tmp_path, monkeypatch):
    caminho = str(tmp_path / "t.db")
    monkeypatch.setattr(trabalhador, "BATIDA", str(tmp_path / "batida"))
    c = banco.conectar(caminho)
    c.execute("INSERT INTO processo (numero, tribunal, advogado_atua) VALUES (?, 'TJMT', 1)", (A,))
    c.execute("INSERT INTO processo (numero, tribunal, advogado_atua) VALUES (?, 'TJMT', 1)",
              ("00000020220248110041",))
    c.execute("INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em, "
              "lex_estado, lex_reservado_ate) VALUES (1, ?, 'lex', '2026-10-01', 'x', 'x', "
              "'trabalhando', '2020-01-01T00:00:00-04:00')", (A,))
    c.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em) "
              "VALUES (1, 1, 'novo', '2020-01-01T00:00:00-04:00')")
    c.execute("INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em, "
              "lex_estado, lex_tentar_depois) VALUES (2, ?, 'lex', '2026-10-01', 'x', 'x', "
              "'pausado_limite', '2020-01-01T00:00:00-04:00')", ("00000020220248110041",))
    c.commit(); c.close()
    voltas = []
    trabalhador.rodar(lambda: banco.conectar(caminho), adv_fn=lambda: ADV,
                      hoje_fn=lambda: HOJE, pausa=0,
                      parar=lambda: voltas.append(1) or len(voltas) > 1)
    c = banco.conectar(caminho)
    estados = [r[0] for r in c.execute("SELECT lex_estado FROM demanda ORDER BY id")]
    assert estados == ["na_fila", "na_fila"]
    assert c.execute("SELECT resultado FROM execucao_lex").fetchone()[0] == "reserva_vencida"
    c.close()


def test_rodar_sobrevive_a_banco_travado_na_checagem_de_reservas(tmp_path, monkeypatch):
    import sqlite3
    caminho = str(tmp_path / "t.db")
    monkeypatch.setattr(trabalhador, "BATIDA", str(tmp_path / "batida"))

    def travado(c, agora):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(trabalhador.ponte, "vencer_reservas", travado)
    voltas = []
    trabalhador.rodar(lambda: banco.conectar(caminho), adv_fn=lambda: ADV,
                      hoje_fn=lambda: HOJE, pausa=0,
                      parar=lambda: voltas.append(1) or len(voltas) > 2)
    assert len(voltas) == 3  # o laço seguiu girando


def test_rodar_sem_advogado_configurado_espera_e_nao_morre(tmp_path, monkeypatch):
    """Instalação local nova: o advogado ainda não foi configurado. O laço não cai;
    quando a configuração chega, a tarefa que esperava na fila roda."""
    caminho = str(tmp_path / "t.db")
    monkeypatch.setattr(trabalhador, "BATIDA", str(tmp_path / "batida"))
    c = banco.conectar(caminho)
    with c:
        tarefas_fila.pedir(c, "varredura", por="agendador")
    c.close()
    monkeypatch.setattr(trabalhador.varredura, "executar", lambda c, adv, **kw: {"erros": []})
    tentativas = []

    def adv_fn():
        tentativas.append(1)
        if len(tentativas) < 3:
            raise ValueError("Preencha CARTEIRA_ADVOGADO_NOME")
        return ADV

    voltas = []
    trabalhador.rodar(lambda: banco.conectar(caminho), adv_fn=adv_fn, hoje_fn=lambda: HOJE,
                      pausa=0, parar=lambda: voltas.append(1) or len(voltas) > 4)
    c = banco.conectar(caminho)
    assert c.execute("SELECT estado FROM tarefa").fetchone()[0] == "ok"
    c.close()
