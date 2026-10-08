"""Trabalhador: executa a fila (varredura e download de autos), uma tarefa por
vez, num processo separado do site (serviço systemd próprio). Ao iniciar,
devolve à fila o que um reinício interrompeu."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime

from nucleo import autos, eventos, ponte, quadro, tarefas_fila, varredura
from nucleo.mni_fabrica import criar_cliente

BATIDA = "dados/trabalhador.batida"
BATIDA_MAXIMA_SEGUNDOS = 120
INTERVALO_BATIDA_SEGUNDOS = 30
log = logging.getLogger("trabalhador")


def bater(caminho: str | None = None) -> None:
    caminho = caminho or BATIDA
    os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(eventos.agora())


def estado_da_batida(caminho: str | None = None, agora: float | None = None) -> str:
    caminho = caminho or BATIDA
    try:
        idade = (agora or time.time()) - os.path.getmtime(caminho)
    except OSError:
        return "desconhecido"
    return "ativo" if idade < BATIDA_MAXIMA_SEGUNDOS else "parado"


def _bater_ate_parar(parado: threading.Event, intervalo: float) -> None:
    """Bate o coração a cada `intervalo` até `parado` ser acionado, para que uma
    tarefa longa não faça o /saude acusar o trabalhador como parado."""
    while not parado.wait(intervalo):
        try:
            bater()
        except OSError:
            log.exception("não consegui gravar a batida")


def iniciar_batedor(intervalo: float | None = None) -> tuple[threading.Event, threading.Thread]:
    parado = threading.Event()
    fio = threading.Thread(
        target=_bater_ate_parar,
        args=(parado, INTERVALO_BATIDA_SEGUNDOS if intervalo is None else intervalo),
        name="batida", daemon=True)
    fio.start()
    return parado, fio


def preparar(conn: sqlite3.Connection) -> int:
    with conn:
        reabertas = tarefas_fila.reabrir_interrompidas(conn)
        conn.execute("UPDATE demanda SET autos_estado = 'na_fila' "
                     "WHERE autos_estado = 'baixando'")
    return reabertas


def executar_uma(conn: sqlite3.Connection, *, adv, hoje: date, fabrica=criar_cliente,
                 obter_djen=None) -> str | None:
    with conn:
        tarefa = tarefas_fila.pegar_proxima(conn)
    if tarefa is None:
        return None
    erro = ""
    demanda_id = None
    if tarefa["tipo"] != "varredura":
        try:
            demanda_id = int(tarefa["ref"])
        except (TypeError, ValueError):
            erro = f"referência inválida: {tarefa['ref']!r}"[:300]
    try:
        if erro:
            log.error("tarefa %s falhou: %s", tarefa["id"], erro)
        elif tarefa["tipo"] == "varredura":
            r = varredura.executar(conn, adv, hoje=hoje, fabrica=fabrica,
                                   obter_djen=obter_djen, progresso=log.info)
            log.info("varredura concluída: %s", r)
        else:
            r = autos.baixar_para_demanda(conn, demanda_id, fabrica=fabrica)
            log.info("autos do cartão %s: %s", tarefa["ref"], r.get("estado"))
    except Exception as exc:  # noqa: BLE001 — nada derruba o trabalhador; tudo vira registro
        erro = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("tarefa %s falhou", tarefa["id"])
        conn.rollback()  # o que a tarefa quebrada deixou pela metade não vai para o banco
        with conn:
            if tarefa["tipo"] == "varredura":
                eventos.registrar(conn, "varredura_falhou", None, {"erro": erro})
            else:
                quadro.marcar_autos(conn, demanda_id, "falhou", erro)
    with conn:
        tarefas_fila.concluir(conn, tarefa["id"], erro)
    return tarefa["tipo"]


def rodar(conectar, *, adv_fn, hoje_fn, pausa: float = 10.0, parar=lambda: False) -> None:
    conn = conectar()
    parado, fio = iniciar_batedor()
    sem_advogado = False
    try:
        preparar(conn)
        while not parar():
            bater()
            # O Mac do Lex pode sumir no meio de um trabalho: devolve à fila a reserva
            # vencida e solta o que esperava o limite do plano.
            try:
                agora = datetime.now(eventos.FUSO)
                with conn:
                    ponte.vencer_reservas(conn, agora)
                    ponte.liberar_pausados(conn, agora)
            except sqlite3.Error:  # banco travado por outro processo: tenta na próxima volta
                log.exception("não consegui vencer reservas do Lex nesta volta")
            try:
                adv = adv_fn()
            except ValueError as exc:  # sem advogado configurado (instalação nova): espera
                if not sem_advogado:
                    log.warning("trabalhador aguardando a configuração: %s", exc)
                sem_advogado = True
                time.sleep(pausa)
                continue
            sem_advogado = False
            if executar_uma(conn, adv=adv, hoje=hoje_fn()) is None:
                time.sleep(pausa)
    finally:
        parado.set()
        fio.join(timeout=2)
        conn.close()
