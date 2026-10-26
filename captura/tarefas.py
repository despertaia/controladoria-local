"""
Execução de tarefas de grupo em SEGUNDO PLANO.

Um único worker (thread daemon) consome uma fila e roda uma tarefa por vez
(1 CPU + rate limit do tribunal tornam paralelismo inútil). O progresso é gravado
em grupos/<slug>.status.json, lido pela página do grupo via polling — assim o
usuário pode fechar a aba e voltar depois.

Tipos de tarefa:
  - "baixar": baixa as peças (instâncias do tribunal) + dossiê + atualiza o cache.
  - "sincronizar": só atualiza os andamentos no cache (sem baixar peças).

O painel injeta a fábrica de clientes e as instâncias via configurar(), para
evitar import circular.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime

from captura import grupos
from captura.credenciais import credencial_do_env, senha_da_instancia
from captura.lote import baixar_processo_completo
from captura.processo_parser import formatar_numero_cnj
from nucleo import tribunal
from sincronizar import sincronizar_um

_fila: queue.Queue = queue.Queue()
_lock = threading.Lock()
_worker_iniciado = False

_fabrica_cliente = None          # callable(instancia) -> MNIClient
_instancias = []                 # [(chave, rotulo, ...), ...] (INSTANCIAS: segue o tribunal)


def configurar(fabrica_cliente, instancias) -> None:
    """Chamado pelo painel no import: injeta a fábrica de clientes
    fabrica_cliente(instancia, cpf, senha) e as instâncias. Guarda a sequência
    como veio (não uma cópia): captura.instancias.INSTANCIAS acompanha o tribunal
    atual, que o lançador local pode ajustar depois do import."""
    global _fabrica_cliente, _instancias
    _fabrica_cliente = fabrica_cliente
    _instancias = instancias
    _garantir_worker()


def _do_tribunal(numero: str, cred: dict) -> tuple:
    """(instâncias, credencial, extras da fábrica) do tribunal do processo. O
    principal segue como antes (instâncias injetadas, credencial do job); outro
    tribunal configurado usa as instâncias e a senha guardada DELE."""
    trib = tribunal.do_numero(numero)
    if trib is None or trib == tribunal.atual():
        return _instancias, cred, {}
    return trib.instancias, credencial_do_env(trib.sigla) or cred, {"tribunal": trib}


# --- status (lido pela página via polling) ---------------------------------

def _status_path(slug: str) -> str:
    return os.path.join(grupos.PASTA_GRUPOS, f"{slug}.status.json")


def ler_status(slug: str) -> dict | None:
    caminho = _status_path(slug)
    if not os.path.exists(caminho):
        return None
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _escrever_status(slug: str, status: dict) -> None:
    status["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(grupos.PASTA_GRUPOS, exist_ok=True)
        with open(_status_path(slug), "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False)
    except OSError:
        pass


# --- fila e worker ----------------------------------------------------------

def enfileirar(slug: str, tipo: str, cred: dict) -> None:
    """Enfileira uma tarefa. `cred` = {'cpf','senha'[, 'senha_2grau']} fica SÓ em memória (na
    fila/no job) — nunca é gravada no arquivo de status."""
    _garantir_worker()
    st = ler_status(slug) or {}
    st.update({"tipo": tipo, "estado": "na_fila",
               "linhas": (st.get("linhas") or [])[-1:]})
    _escrever_status(slug, st)
    _fila.put((slug, tipo, cred))


def _idade_segundos(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return 1e9


def esta_ocupado(slug: str) -> bool:
    """True só se houver tarefa viva. Um status 'rodando' sem batimento há mais
    de 120s é considerado travado (ex.: serviço reiniciou) e liberado para retomar."""
    st = ler_status(slug)
    if not st or st.get("estado") not in ("na_fila", "rodando"):
        return False
    return _idade_segundos(st.get("atualizado_em")) < 120


def _garantir_worker() -> None:
    global _worker_iniciado
    with _lock:
        if not _worker_iniciado:
            threading.Thread(target=_loop, daemon=True).start()
            _worker_iniciado = True


def _loop() -> None:
    while True:
        slug, tipo, cred = _fila.get()
        try:
            _executar(slug, tipo, cred)
        except Exception as exc:  # noqa: BLE001 — worker nunca deve morrer
            st = ler_status(slug) or {}
            st.update({"estado": "erro", "erro": str(exc)})
            _escrever_status(slug, st)
        finally:
            _fila.task_done()


def _executar(slug: str, tipo: str, cred: dict) -> None:
    cpf = cred["cpf"]
    grupo = grupos.ler(slug)
    if not grupo:
        return
    numeros = grupo.get("numeros", [])
    total = len(numeros)
    linhas: list[str] = []
    st = {
        "tipo": tipo, "estado": "rodando", "total": total, "indice": 0,
        "ok": 0, "falhas": 0, "processo_atual": "", "linhas": linhas,
        "iniciado_em": datetime.now().isoformat(timespec="seconds"),
    }

    def log(msg: str) -> None:
        linhas.append(msg)
        st["linhas"] = linhas[-100:]
        _escrever_status(slug, st)

    _escrever_status(slug, st)

    for i, numero in enumerate(numeros, start=1):
        st["indice"] = i
        st["processo_atual"] = formatar_numero_cnj(numero)

        instancias_num, cred_num, extras = _do_tribunal(numero, cred)
        cpf = cred_num["cpf"]

        if tipo == "sincronizar":
            try:
                d = sincronizar_um(
                    _fabrica_cliente("1grau", cpf, senha_da_instancia(cred_num, "1grau"),
                                     **extras), numero)
                st["ok"] += 1
                log(f"[{i}/{total}] {formatar_numero_cnj(numero)} — "
                    f"{len(d['andamentos'])} andamentos")
            except Exception as exc:  # noqa: BLE001
                st["falhas"] += 1
                log(f"[{i}/{total}] {formatar_numero_cnj(numero)} — FALHOU: {exc}")
            continue

        # tipo == "baixar"
        log(f"[{i}/{total}] {formatar_numero_cnj(numero)}")
        proc_ok = proc_falhas = proc_pulados = 0
        ausente_em = []
        for chave, rotulo, *_ in instancias_num:
            cli = _fabrica_cliente(chave, cpf, senha_da_instancia(cred_num, chave), **extras)
            for ev in baixar_processo_completo(cli, numero, subpasta=chave,
                                               pular_existentes=True):
                evento = ev["evento"]
                if evento == "peca":
                    # batimento: mantém atualizado_em fresco em downloads longos
                    st["peca"] = f"{chave} {ev.get('indice','?')}/{ev.get('total','?')}"
                    _escrever_status(slug, st)
                elif evento == "processo_fim":
                    proc_ok += ev["ok"]
                    proc_falhas += ev["falhas"]
                    proc_pulados += ev.get("pulados", 0)
                elif evento == "processo_ausente":
                    ausente_em.append(rotulo)
                elif evento == "erro_processo":
                    proc_falhas += 1
                    log(f"   {rotulo}: erro — {ev['msg']}")
        st["ok"] += proc_ok
        st["falhas"] += proc_falhas
        resumo = f"   ✓ {proc_ok} peça(s)"
        if proc_pulados:
            resumo += f", {proc_pulados} já existia(m)"
        if proc_falhas:
            resumo += f", {proc_falhas} falha(s)"
        if ausente_em:
            resumo += f" · ausente em: {', '.join(ausente_em)}"
        log(resumo)

    st["estado"] = "concluido"
    _escrever_status(slug, st)
