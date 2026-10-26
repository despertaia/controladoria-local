"""Download dos autos de um cartão (instâncias do PJe do tribunal do processo),
na mesma pasta de peças que a página do processo já lista
(peticoes/<numero>/<instância>/).
Retomável: o que já está em disco não é baixado de novo."""

from __future__ import annotations

import sqlite3

from captura import lote
from nucleo import carteira, eventos, quadro, tribunal
from nucleo.mni_fabrica import CredencialAusenteError, criar_cliente

_IGNORADO = {"estado": "ignorado", "pecas": 0, "faltaram": 0, "detalhe": ""}


def detalhe_fora() -> str:
    siglas = "/".join(t.sigla for t in tribunal.configurados())
    return (f"os autos deste tribunal não vêm pelo PJe do {siglas}; "
            "consulte no sistema do tribunal")


DETALHE_FALTAM = "faltam os autos: coloque o PDF do processo na pasta do caso"


def _espera_os_autos(conn: sqlite3.Connection, demanda_id: int, numero: str,
                     detalhe: str = DETALHE_FALTAM, **extra) -> dict:
    """O PJe não entrega os autos (outro tribunal ou sem senha): o cartão vai para
    "Autos baixados" esperando o PDF na pasta do caso, sem seguir para o Lex."""
    with conn:
        if not _ainda_em_acao(conn, demanda_id):
            return dict(_IGNORADO)
        quadro.marcar_faltam_autos(conn, demanda_id, detalhe)
        quadro.mover(conn, demanda_id, "autos", "trabalhador")
        eventos.registrar(conn, "autos_baixados", numero,
                          {"demanda": demanda_id, "pecas": 0, "faltaram": 0, **extra})
    return {"estado": "ok", "pecas": 0, "faltaram": 0, "detalhe": detalhe}


def _ainda_em_acao(conn: sqlite3.Connection, demanda_id: int) -> bool:
    """Relê a coluna com o banco travado para escrita (dentro de `with conn:`).
    Se o advogado tirou o cartão de "Precisa de ação" durante o download, zera o
    estado dos autos e não mexe em mais nada."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    d = conn.execute("SELECT coluna FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    if d is not None and d["coluna"] == "acao":
        return True
    if d is not None:
        quadro.marcar_autos(conn, demanda_id, "")
    return False


def baixar_para_demanda(conn: sqlite3.Connection, demanda_id: int, *,
                        fabrica=criar_cliente) -> dict:
    d = conn.execute("SELECT numero, coluna FROM demanda WHERE id = ?",
                     (demanda_id,)).fetchone()
    if d is None or d["coluna"] != "acao":
        return dict(_IGNORADO)
    numero = d["numero"]
    if not carteira.e_do_tribunal(numero):
        # Fora do tribunal do escritório não há o que baixar pelo PJe: não é falha, o
        # cartão espera o PDF dos autos na pasta do caso.
        return _espera_os_autos(conn, demanda_id, numero, fora_do_tjmt=True)
    with conn:
        quadro.marcar_autos(conn, demanda_id, "baixando")
        eventos.registrar(conn, "autos_iniciados", numero, {"demanda": demanda_id})

    pecas = faltaram = 0
    encontrado = False
    trib = carteira.tribunal_de(numero)
    for instancia in carteira.instancias_de_busca(trib):
        try:
            cliente = carteira.criar_para(fabrica, instancia, trib)
        except CredencialAusenteError:  # tribunal configurado, mas sem senha do PJe
            return _espera_os_autos(conn, demanda_id, numero,
                                    quadro.detalhe_sem_senha(trib.sigla), sem_credencial=True)
        except Exception as exc:  # WSDL fora do ar
            return _falhou(conn, demanda_id, numero, f"PJe indisponível: {exc}")
        for ev in lote.baixar_processo_completo(cliente, numero, subpasta=instancia,
                                                pular_existentes=True):
            if ev["evento"] == "erro_processo":
                return _falhou(conn, demanda_id, numero, ev.get("msg") or "erro do PJe")
            if ev["evento"] == "processo_fim":
                encontrado = True
                pecas += ev["ok"] + ev.get("pulados", 0)
                faltaram += ev["falhas"]
            elif ev["evento"] == "processo_inicio":
                encontrado = True
    if not encontrado:
        return _falhou(conn, demanda_id, numero, "Processo não encontrado no PJe.")

    detalhe = f"faltaram {faltaram} peça(s)" if faltaram else ""
    with conn:
        if not _ainda_em_acao(conn, demanda_id):
            return dict(_IGNORADO)
        quadro.marcar_autos(conn, demanda_id, "ok", detalhe)
        quadro.mover(conn, demanda_id, "autos", "trabalhador")
        eventos.registrar(conn, "autos_baixados", numero,
                          {"demanda": demanda_id, "pecas": pecas, "faltaram": faltaram})
        quadro.enfileirar_automatico(conn, demanda_id)
    return {"estado": "ok", "pecas": pecas, "faltaram": faltaram, "detalhe": detalhe}


def _falhou(conn: sqlite3.Connection, demanda_id: int, numero: str, detalhe: str) -> dict:
    detalhe = " ".join(str(detalhe).split())[:200]
    with conn:
        if not _ainda_em_acao(conn, demanda_id):
            return dict(_IGNORADO)
        quadro.marcar_autos(conn, demanda_id, "falhou", detalhe)
        eventos.registrar(conn, "autos_falharam", numero,
                          {"demanda": demanda_id, "erro": detalhe})
    return {"estado": "falhou", "pecas": 0, "faltaram": 0, "detalhe": detalhe}
