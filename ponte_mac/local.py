"""Ponte do Lex no modo local (mentorado): painel e Lex no mesmo computador.

O lançador (`python -m local.iniciar`) roda a ponte numa thread do próprio processo,
apontando para o painel em `http://127.0.0.1:<porta>`. A chave da ponte é gerada direto
no banco e guardada no cofre do sistema na primeira vez, sem copiar e colar; o acesso ao
plano do Claude é opcional (sem ele, vale o login normal do `claude` do usuário).

Uso pelo lançador:

    parar = threading.Event()
    thread = ponte_mac.local.rodar_em_thread(f"http://127.0.0.1:{porta}", None, parar)
    ...
    ponte_mac.local.parar(parar, thread)   # ao sair
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from ponte_mac import chaves, executor, laco, leitor
from ponte_mac.cliente import Painel

log = logging.getLogger("ponte_mac.local")

ESPERA_S = 60  # casa da Banca ausente, banco ou cofre com problema: tenta de novo
ESTADO_PADRAO = Path("dados") / "ponte-estado.json"


modo_local = chaves.modo_local


def casa_da_banca() -> str:
    """A casa da Banca do mentorado: `CONTROLADORIA_CASA_BANCA`, se definida; senão o
    ajuste `casa_banca` da tela /configurar (`dados/ajustes.json`)."""
    casa = (os.getenv("CONTROLADORIA_CASA_BANCA") or "").strip()
    if casa:
        return casa
    try:
        from nucleo import ajustes_locais
        return str(ajustes_locais.ler().get("casa_banca") or "").strip()
    except Exception as exc:  # ajustes ilegíveis: como se não houvesse casa
        log.warning("não consegui ler a casa da Banca nos ajustes: %s",
                    executor.limpar_detalhe(exc))
        return ""


def preparar_chave_local(conn) -> str:
    """A chave da ponte deste computador: a do cofre, se ela ainda vale para o hash do
    banco; senão gera uma nova pelo `nucleo.ponte` (só o hash fica no banco) e a guarda no
    cofre. A troca no banco só é confirmada depois de o cofre aceitar a chave."""
    from nucleo import ponte as nucleo_ponte

    guardada = chaves.ler(chaves.CHAVE_PONTE)
    if guardada and nucleo_ponte.chave_valida(conn, guardada):
        return guardada
    with conn:  # se o cofre recusar, o banco volta à chave anterior
        chave = nucleo_ponte.gerar_chave(conn)
        chaves.guardar(chaves.CHAVE_PONTE, chave)
    log.info("chave da ponte gerada e guardada no cofre do sistema")
    return chave


_TRAVA_DA_CHAVE = threading.Lock()  # a ponte e a leitura pedem a chave ao mesmo tempo


def _chave_do_banco() -> str | None:
    """Abre o banco do painel (CONTROLADORIA_BANCO) e devolve a chave pronta, ou None."""
    with _TRAVA_DA_CHAVE:
        return _chave_do_banco_sem_trava()


def _chave_do_banco_sem_trava() -> str | None:
    from nucleo import banco

    try:
        conn = banco.conectar()
    except Exception as exc:
        log.error("não consegui abrir o banco para a chave da ponte: %s",
                  executor.limpar_detalhe(exc))
        return None
    try:
        return preparar_chave_local(conn)
    except Exception as exc:  # banco travado, cofre recusou: a ponte tenta de novo depois
        log.error("não consegui preparar a chave da ponte: %s", executor.limpar_detalhe(exc))
        return None
    finally:
        conn.close()


def _token() -> str:
    return chaves.ler(chaves.ACESSO_CLAUDE) or ""


_AVISOU_SEM_CHAVE = False


def _avisar_uma_vez_sem_chave() -> None:
    global _AVISOU_SEM_CHAVE
    if not _AVISOU_SEM_CHAVE:
        _AVISOU_SEM_CHAVE = True
        log.warning("sem a chave da ponte, a leitura de prazos espera (confira o cofre e o banco)")


def rodar_local(painel_url: str, casa, parar_evento: threading.Event,
                estado: Path | None = None, *, rodar=laco.rodar,
                obter_chave=_chave_do_banco) -> None:
    """O laço da ponte até `parar_evento`. `casa` None: a dos ajustes, relida a cada
    tentativa (trocada em /configurar, vale sem reiniciar). Casa da Banca ausente ou
    chave indisponível não derrubam nada: registra no log e tenta de novo a cada minuto."""
    executor.usar_parada(parar_evento)
    estado = Path(estado) if estado else Path.cwd() / ESTADO_PADRAO
    dormir = parar_evento.wait
    while not parar_evento.is_set():
        atual = str(casa or casa_da_banca() or "")
        casa_path = Path(atual).expanduser() if atual else None
        if casa_path is None or not casa_path.is_dir():
            log.warning("casa da Banca não encontrada; a ponte do Lex espera "
                        "(confira a pasta nas Configurações)")
            dormir(ESPERA_S)
            continue
        chave = obter_chave()
        if not chave:
            dormir(ESPERA_S)
            continue
        executor.usar_casa(str(casa_path))  # a higienização tira o caminho dela
        try:
            painel = Painel(painel_url, chave)
        except ValueError as exc:
            log.error("endereço do painel inválido para a ponte: %s", exc)
            return

        def recriar():
            nova = obter_chave()  # 401: a chave do banco mudou; gera e guarda outra
            return Painel(painel_url, nova) if nova else None

        try:
            rodar(painel, casa_path, _token, estado, dormir=dormir, parar=parar_evento.is_set,
                  recriar=recriar, reler_chave=obter_chave, exigir_token=False)
        except Exception as exc:  # nada derruba o lançador; volta depois de um minuto
            log.error("a ponte do Lex parou com erro: %s", executor.limpar_detalhe(exc))
            dormir(ESPERA_S)


def rodar_em_thread(painel_url: str, casa, parar_evento: threading.Event,
                    estado: Path | None = None) -> threading.Thread:
    """Sobe a ponte numa thread (daemon) e a devolve já iniciada, com a leitura de prazo no
    fio dela. A leitura não precisa da casa da Banca: a chave da ponte sai do mesmo caminho da
    ponte, a do cofre ou uma gerada agora, senão quem não tem casa nunca teria chave. O login do
    Claude é outra coisa: a leitura só o gasta depois de «Conectar o Lex», que guarda o acesso que
    `_token` lê. O laço do squad segue com o login normal (`exigir_token=False`, em `rodar_local`)."""
    atual: dict = {}

    def painel_das_leituras():
        """O painel das leituras, sem depender da casa da Banca: a chave sai do mesmo caminho
        da ponte (cofre, ou gerada agora). Reaproveita o painel até o 401."""
        painel = atual.get("painel")
        if painel is not None and not painel.chave_recusada:
            return painel
        chave = _chave_do_banco()
        if not chave:
            _avisar_uma_vez_sem_chave()
            return None
        atual["painel"] = Painel(painel_url, chave)
        return atual["painel"]

    leitura = leitor.em_thread(painel_das_leituras, _token,
                               Path.cwd() / "dados" / "leitura-vazia",
                               parar=parar_evento.is_set,
                               dormir=parar_evento.wait, exigir_token=True)
    thread = threading.Thread(target=rodar_local, args=(painel_url, casa, parar_evento, estado),
                              name="ponte-lex", daemon=True)
    thread.leitura = leitura  # `parar` espera por ele também
    thread.start()
    return thread


def parar(parar_evento: threading.Event, thread: threading.Thread | None = None,
          espera_s: float = 30) -> None:
    """Pede a parada, encerra o Lex em andamento (com a árvore de processos dele) e espera
    a thread terminar. O cartão em andamento não vira falha: volta para a fila quando a
    reserva vencer, e a execução é retomada pelo run guardado."""
    parar_evento.set()
    executor.encerrar_em_andamento()
    leitor.encerrar_em_andamento()
    if thread is not None:
        thread.join(timeout=espera_s)
        leitura = getattr(thread, "leitura", None)
        if leitura is not None:  # a reserva que ficar segurada vence sozinha
            leitura.join(timeout=min(espera_s, 10))
