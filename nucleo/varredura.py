"""Varredura agendada (Fase 2A): intimações pendentes → DJEN (pelo advogado e, para
os processos que ele confirmou, pelo número) → PJe dos processos ativos de cada tribunal
do escritório (cada um no PJe dele) → situação → cartões do quadro.

Falha de uma fonte não derruba as outras: tudo vai para o evento
`varredura_concluida`. Nunca abre teor de comunicação nem registra ciência."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, timedelta

from captura.mni_client import CredencialInvalidaError, MNIError
from nucleo import avisos, carteira, eventos, quadro, tribunal
from nucleo.djen import DJENError, buscar_por_advogado, buscar_por_processo
from nucleo.mni_fabrica import CredencialAusenteError, criar_cliente

FOLGA_DIAS = 2
LIMITE_POR_NUMERO = 200  # processos confirmados consultados pelo número, por varredura
_ROTULO = {"1grau": "1º grau", "2grau": "2º grau"}


def _frase(exc: Exception, limite: int = 160) -> str:
    texto = " ".join(str(exc).split())
    frase = re.split(r"(?<=[.!?])\s", texto, maxsplit=1)[0]
    return (frase[:limite].rstrip() + "…" if len(frase) > limite else frase) or type(exc).__name__


def desde_para_djen(conn: sqlite3.Connection, hoje: date) -> date:
    """Desde a última varredura em que o DJEN respondeu (com folga); na primeira,
    a janela do quadro."""
    quando = conn.execute(
        "SELECT MAX(quando) FROM evento WHERE tipo = 'varredura_concluida' "
        "AND json_extract(dados, '$.djen_ok') = 1").fetchone()[0]
    if quando:
        return date.fromisoformat(quando[:10]) - timedelta(days=FOLGA_DIAS)
    return hoje - timedelta(days=quadro.JANELA_DIAS)


def _numeros_para_sincronizar(conn: sqlite3.Connection) -> list[str]:
    """Ativos dos tribunais do escritório (a OAB atua ou o advogado confirmou:
    FILTRO_ATIVA) e os ainda não conferidos (recém-descobertos), sem arquivados nem
    descartados."""
    linhas = conn.execute(
        f"SELECT numero FROM processo WHERE ({carteira.FILTRO_ATIVA}) "
        "OR (advogado_atua IS NULL AND arquivado = 0 AND descartado_em IS NULL) "
        "ORDER BY numero").fetchall()
    return [r["numero"] for r in linhas if carteira.e_do_tribunal(r["numero"])]


def _confirmados_para_o_djen(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Processos que o advogado confirmou ("É meu", "Adicionar e acompanhar"), sem
    arquivados nem descartados: o DJEN é consultado pelo número deles, porque a OAB
    dele pode não constar nas publicações. Os confirmados mais recentes primeiro."""
    return conn.execute(
        "SELECT numero, confirmado_em FROM processo WHERE confirmado_em IS NOT NULL "
        "AND arquivado = 0 AND descartado_em IS NULL "
        "ORDER BY confirmado_em DESC, numero").fetchall()


def _djen_por_numero(conn: sqlite3.Connection, adv: carteira.Advogado, desde: date,
                     hoje: date, r: dict, obter, pausa: float, progresso) -> list[dict]:
    """Publicações novas dos processos confirmados, consultados um a um pelo número.
    Recém-confirmado (depois do início da janela) ganha a janela do quadro inteira.
    Erro do DJEN num processo não derruba os outros: vai resumido para `erros`."""
    confirmados = _confirmados_para_o_djen(conn)
    if len(confirmados) > LIMITE_POR_NUMERO:
        r["erros"].append(f"DJEN (por número): {len(confirmados) - LIMITE_POR_NUMERO} "
                          f"processo(s) além do limite de {LIMITE_POR_NUMERO} não consultados")
        confirmados = confirmados[:LIMITE_POR_NUMERO]
    novas: list[dict] = []
    falhas: list[str] = []
    for i, linha in enumerate(confirmados):
        if i:
            time.sleep(pausa)
        inicio = desde
        if str(linha["confirmado_em"])[:10] >= desde.isoformat():
            inicio = min(desde, hoje - timedelta(days=quadro.JANELA_DIAS))
        try:
            pubs = buscar_por_processo(linha["numero"], inicio, hoje, numero_oab=adv.numero_oab,
                                       uf_oab=adv.uf_oab, obter=obter, pausa=pausa)
        except (DJENError, ValueError) as exc:
            falhas.append(_frase(exc))
            continue
        with conn:
            novas += carteira.gravar_publicacoes_novas(conn, pubs)
    if falhas:
        r["erros"].append(f"DJEN (por número): {len(falhas)} processo(s) sem resposta "
                          f"({falhas[0]})")
    if confirmados:
        progresso(f"DJEN por número: {len(confirmados)} processo(s) confirmado(s), "
                  f"{len(novas)} publicação(ões) nova(s)")
    return novas


def executar(conn: sqlite3.Connection, adv: carteira.Advogado, *, hoje: date,
             fabrica=criar_cliente, obter_djen=None, pausa_djen: float = 0.4,
             progresso=lambda _mensagem: None) -> dict:
    with conn:
        eventos.registrar(conn, "varredura_iniciada")
    r = {"publicacoes_novas": 0, "avisos_novos": 0, "sincronizados": 0, "falhas_pje": 0,
         "cartoes_novos": 0, "djen_ok": False, "pje_ok": False, "erros": []}

    # Um conjunto de clientes por tribunal configurado: senha recusada ou PJe fora do
    # ar num tribunal não impede o outro.
    varios = len(tribunal.configurados()) > 1
    por_tribunal: dict[str, dict] = {}
    prontos: set[str] = set()  # tribunais com todas as instâncias e senha aceita
    novos_avisos: list[dict] = []
    for trib in tribunal.configurados():
        def rotulo(inst: str, trib=trib) -> str:
            return f"PJe {trib.sigla} ({_ROTULO[inst]})" if varios else f"PJe ({_ROTULO[inst]})"

        clientes = por_tribunal[trib.sigla] = {}
        sem_senha = False
        for inst in carteira.instancias_de_busca(trib):
            try:
                clientes[inst] = carteira.criar_para(fabrica, inst, trib)
            except CredencialAusenteError:  # sem senha do PJe: só o DJEN, sem erro
                sem_senha = True
            except Exception as exc:  # WSDL fora do ar: segue sem o PJe
                r["erros"].append(f"{rotulo(inst)}: {_frase(exc)}")
        if sem_senha and not clientes:
            r.setdefault("pje_sem_senha", []).append(trib.sigla)

        senha_recusada = False
        for inst, cliente in clientes.items():
            try:
                lidos = cliente.consultar_avisos_pendentes()
            except CredencialInvalidaError:
                senha_recusada = True
                r["erros"].append(f"{rotulo(inst)}: senha recusada")
                continue
            except MNIError as exc:
                r["erros"].append(f"{rotulo(inst)}: {_frase(exc)}")
                continue
            lidos = list(lidos or [])
            # Os avisos do principal ficam com a chave da instância ("1grau"); os dos
            # outros tribunais, com a sigla na frente ("TJMG-1grau"), para os ids de um
            # PJe não se confundirem com os do outro.
            chave = inst if trib == tribunal.atual() else f"{trib.sigla}-{inst}"
            convertidos = [a for a in (avisos.ler_aviso(o, chave) for o in lidos) if a]
            ilegiveis = len(lidos) - len(convertidos)
            if ilegiveis:
                r["erros"].append(f"{rotulo(inst)}: {ilegiveis} intimação(ões) ilegível(is)")
            with conn:
                # Lista com ilegível não é completa: não encerra as pendentes que "sumiram".
                novos = avisos.sincronizar(conn, chave, convertidos, encerrar=not ilegiveis)
                for a in novos:
                    carteira.registrar_candidato(conn, a["numero"],
                                                 carteira.tribunal_do_numero(a["numero"]), "aviso")
            novos_avisos += novos
        if len(clientes) == len(carteira.instancias_de_busca(trib)) and not senha_recusada:
            prontos.add(trib.sigla)
    r["avisos_novos"] = len(novos_avisos)
    progresso(f"Intimações pendentes novas: {len(novos_avisos)}")

    novas_pubs: list[dict] = []
    desde = desde_para_djen(conn, hoje)
    try:
        pubs = buscar_por_advogado(adv.nome, adv.numero_oab, adv.uf_oab, desde, hoje,
                                   obter=obter_djen, pausa=pausa_djen)
    except DJENError as exc:
        r["erros"].append(f"DJEN: {_frase(exc)}")
    else:
        with conn:
            novas_pubs = carteira.gravar_publicacoes_novas(conn, pubs)
            for p in pubs:
                carteira.registrar_candidato(conn, p["numero"], p["tribunal"], "djen")
        r["djen_ok"] = True
    progresso(f"DJEN: {len(novas_pubs)} publicação(ões) nova(s)")
    # Depois da busca pelo advogado: o que ela já gravou não conta de novo (id único).
    novas_pubs += _djen_por_numero(conn, adv, desde, hoje, r, obter_djen, pausa_djen, progresso)
    r["publicacoes_novas"] = len(novas_pubs)

    numeros = _numeros_para_sincronizar(conn)
    for trib in tribunal.configurados():
        if trib.sigla not in prontos:
            continue
        try:
            ok, falhas = carteira.sincronizar_lista(
                conn, [n for n in numeros if tribunal.do_numero(n) == trib],
                por_tribunal[trib.sigla], adv, progresso)
            r["sincronizados"] += ok
            r["falhas_pje"] += falhas
            r["pje_ok"] = True
        except CredencialInvalidaError:
            r["erros"].append(f"PJe{' ' + trib.sigla if varios else ''}: "
                              "senha recusada em vários processos seguidos")
    with conn:
        carteira.recalcular_situacao(conn)

    with conn:
        # Lê do banco: o que uma varredura interrompida deixou sem cartão entra agora.
        r["cartoes_novos"] += quadro.ligar(conn, hoje) + quadro.gerar_pendentes(conn, hoje)
        eventos.registrar(conn, "varredura_concluida", None, r)
    return r
