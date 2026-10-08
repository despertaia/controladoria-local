"""Cliente da API pública do DJEN (Diário de Justiça Eletrônico Nacional).

Busca as publicações em nome do advogado. O TJMT publica sem vincular a OAB,
por isso a busca é feita pelo nome E pela OAB/UF, mês a mês, e os resultados
são unidos sem repetição. Os processos que o advogado confirmou na carteira
também são consultados pelo número. A API é pública: nenhuma credencial é enviada.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import requests

from nucleo.advogado import e_o_advogado

URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"
ITENS_POR_PAGINA = 100
MAX_PAGINAS = 100


class DJENError(Exception):
    """O DJEN não respondeu ou respondeu com erro."""


TENTATIVAS = 6
ESPERA_MAXIMA = 120


def _espera(tentativa: int, resposta) -> float:
    """Segundos até a próxima tentativa: o Retry-After numérico do servidor
    (limitado a ESPERA_MAXIMA) ou 3, 6, 9… segundos."""
    if resposta is not None:
        valor = (resposta.headers.get("Retry-After") or "").strip()
        if valor.isdigit():
            return min(int(valor), ESPERA_MAXIMA)
    return 3 * (tentativa + 1)


def _obter_padrao(params: dict) -> dict:
    ultimo_erro = None
    for tentativa in range(TENTATIVAS):
        resposta_http = None
        try:
            resposta = requests.get(URL, params=params, timeout=60,
                                    headers={"Accept": "application/json"})
            resposta.raise_for_status()
            return resposta.json()
        except requests.HTTPError as exc:
            resposta_http = exc.response
            if resposta_http is not None and 400 <= resposta_http.status_code < 500:
                if resposta_http.status_code not in (408, 429):
                    raise DJENError(f"DJEN respondeu com erro {resposta_http.status_code}: {exc}")
            ultimo_erro = exc
        except (requests.RequestException, ValueError) as exc:
            ultimo_erro = exc
        if tentativa < TENTATIVAS - 1:
            time.sleep(_espera(tentativa, resposta_http))
    raise DJENError(f"DJEN indisponível após {TENTATIVAS} tentativas: {ultimo_erro}")


TENTATIVAS_INTERATIVAS = 2
TEMPO_INTERATIVO = 12


def obter_interativo(params: dict) -> dict:
    """Busca para quem está esperando na tela: no máximo 2 tentativas de 12 s,
    1 s de pausa, sem obedecer Retry-After. Qualquer falha vira DJENError."""
    ultimo_erro = None
    for tentativa in range(TENTATIVAS_INTERATIVAS):
        try:
            resposta = requests.get(URL, params=params, timeout=TEMPO_INTERATIVO,
                                    headers={"Accept": "application/json"})
            resposta.raise_for_status()
            return resposta.json()
        except (requests.RequestException, ValueError) as exc:
            ultimo_erro = exc
        if tentativa < TENTATIVAS_INTERATIVAS - 1:
            time.sleep(1)
    raise DJENError(f"DJEN não respondeu: {ultimo_erro}")


def janelas_mensais(inicio: date, fim: date) -> list[tuple[date, date]]:
    janelas = []
    atual = inicio
    while atual <= fim:
        proximo_mes = (atual.replace(day=28) + timedelta(days=4)).replace(day=1)
        janelas.append((atual, min(proximo_mes - timedelta(days=1), fim)))
        atual = proximo_mes
    return janelas


def buscar(params: dict, *, obter=None, pausa: float = 0.4) -> list[dict]:
    obter = obter or _obter_padrao
    itens: list[dict] = []
    pagina = 1
    total: int | None = None
    while True:
        if pagina > MAX_PAGINAS:
            raise DJENError(f"DJEN: mais de {MAX_PAGINAS} páginas para {params} — busca interrompida.")
        resposta = obter({**params, "itensPorPagina": ITENS_POR_PAGINA, "pagina": pagina})
        if resposta.get("status") not in (None, "success"):
            raise DJENError(f"DJEN respondeu com erro: {resposta.get('message')}")
        if pagina == 1 and isinstance(resposta.get("count"), int):
            total = resposta.get("count")
        lote = resposta.get("items") or []
        itens.extend(lote)
        if len(lote) < ITENS_POR_PAGINA:
            if isinstance(total, int) and len(itens) < total:
                raise DJENError(f"DJEN devolveu {len(itens)} de {total} publicações (paginação incompleta).")
            return itens
        pagina += 1
        time.sleep(pausa)


_POLOS_PUBLICACAO = {"A": "ativo", "ATIVO": "ativo", "P": "passivo", "PASSIVO": "passivo"}


def partes_da_publicacao(item: dict) -> list[dict]:
    """Destinatários da publicação como [{nome, polo}], polo "ativo", "passivo" ou ""
    (o DJEN manda "A"/"P", às vezes por extenso). Sem nome vazio nem repetido."""
    partes: list[dict] = []
    vistos: set[tuple[str, str]] = set()
    for d in item.get("destinatarios") or []:
        if not isinstance(d, dict):
            continue
        nome = " ".join(str(d.get("nome") or "").split())
        polo = _POLOS_PUBLICACAO.get(str(d.get("polo") or "").strip().upper(), "")
        if nome and (nome.casefold(), polo) not in vistos:
            vistos.add((nome.casefold(), polo))
            partes.append({"nome": nome, "polo": polo})
    return partes


def normalizar(item: dict, numero_oab: str, uf_oab: str) -> dict:
    advogados = [d.get("advogado") or {} for d in (item.get("destinatarioadvogados") or [])]
    return {
        "id": int(item["id"]),
        "numero": "".join(c for c in str(item.get("numero_processo") or "") if c.isdigit()),
        "tribunal": str(item.get("siglaTribunal") or ""),
        "data_disponibilizacao": str(item.get("data_disponibilizacao") or "")[:10],
        "tipo": str(item.get("tipoComunicacao") or ""),
        "orgao": str(item.get("nomeOrgao") or ""),
        "classe": str(item.get("nomeClasse") or ""),
        "texto": str(item.get("texto") or ""),
        "link": str(item.get("link") or ""),
        "partes": partes_da_publicacao(item),
        "oab_confirmada": any(
            e_o_advogado(f"{(a.get('uf_oab') or '')}{(a.get('numero_oab') or '')}", numero_oab, uf_oab)
            for a in advogados),
    }


def buscar_por_advogado(nome: str, numero_oab: str, uf_oab: str, inicio: date, fim: date,
                        *, obter=None, pausa: float = 0.4) -> list[dict]:
    por_id: dict[int, dict] = {}
    for inicio_janela, fim_janela in janelas_mensais(inicio, fim):
        datas = {"dataDisponibilizacaoInicio": inicio_janela.isoformat(),
                 "dataDisponibilizacaoFim": fim_janela.isoformat()}
        for filtro in ({"nomeAdvogado": nome}, {"numeroOab": numero_oab, "ufOab": uf_oab}):
            for item in buscar({**filtro, **datas}, obter=obter, pausa=pausa):
                publicacao = normalizar(item, numero_oab, uf_oab)
                if publicacao["numero"]:
                    if publicacao["id"] in por_id:
                        por_id[publicacao["id"]]["oab_confirmada"] = (
                            por_id[publicacao["id"]]["oab_confirmada"] or publicacao["oab_confirmada"]
                        )
                    else:
                        por_id[publicacao["id"]] = publicacao
            time.sleep(pausa)
    return sorted(por_id.values(), key=lambda p: (p["data_disponibilizacao"], p["id"]))


def buscar_por_processo(numero: str, inicio: date, fim: date, *, numero_oab: str = "",
                        uf_oab: str = "", obter=None, pausa: float = 0.4) -> list[dict]:
    """Publicações de um processo pelo número (filtro `numeroProcesso`), normalizadas
    como as da busca por advogado: `oab_confirmada` se a OAB informada consta nelas.
    Serve aos processos que o advogado confirmou e em que a OAB dele não aparece."""
    digitos = "".join(c for c in str(numero) if c.isdigit())
    if len(digitos) != 20:
        raise ValueError(f"número CNJ inválido: {numero!r}")
    params = {"numeroProcesso": digitos,
              "dataDisponibilizacaoInicio": inicio.isoformat(),
              "dataDisponibilizacaoFim": fim.isoformat()}
    por_id: dict[int, dict] = {}
    for item in buscar(params, obter=obter, pausa=pausa):
        publicacao = normalizar(item, numero_oab, uf_oab)
        if publicacao["numero"] == digitos:  # nada de outro processo, se o filtro falhar
            por_id.setdefault(publicacao["id"], publicacao)
    return sorted(por_id.values(), key=lambda p: (p["data_disponibilizacao"], p["id"]))


LIMITE_BUSCA_NOME = 500


def _so_http(link) -> str:
    link = str(link or "").strip()
    return link if link.lower().startswith(("http://", "https://")) else ""


def _formatar_cnj(numero: str) -> str:
    if len(numero) != 20:
        return numero
    return f"{numero[0:7]}-{numero[7:9]}.{numero[9:13]}.{numero[13]}.{numero[14:16]}.{numero[16:20]}"


_POLOS = {"A": "Polo Ativo", "P": "Polo Passivo"}


def _publicacao_com_partes(item) -> dict | None:
    """Publicação do DJEN reduzida ao que a busca por nome mostra, com as partes
    (destinatários) e o polo de cada uma. None se o item não tem id ou número."""
    if not isinstance(item, dict):
        return None
    try:
        id_ = int(item.get("id"))
    except (TypeError, ValueError):
        return None
    numero = "".join(c for c in str(item.get("numero_processo") or "") if c.isdigit())
    if not numero:
        return None
    partes = [{"nome": str(d.get("nome") or "").strip(),
               "polo": _POLOS.get(str(d.get("polo") or "").upper(), "")}
              for d in (item.get("destinatarios") or [])
              if isinstance(d, dict) and str(d.get("nome") or "").strip()]
    return {
        "id": id_,
        "numero": numero,
        "numero_formatado": str(item.get("numeroprocessocommascara") or "") or _formatar_cnj(numero),
        "tribunal": str(item.get("siglaTribunal") or ""),
        "orgao": str(item.get("nomeOrgao") or ""),
        "classe": str(item.get("nomeClasse") or ""),
        "data": str(item.get("data_disponibilizacao") or "")[:10],
        "link": _so_http(item.get("link")),
        "partes": partes,
    }


def buscar_por_parte(nome: str, tribunal: str, inicio: date, fim: date, *,
                     limite: int = LIMITE_BUSCA_NOME, obter=None, pausa: float = 0.4) -> dict:
    """Publicações em que `nome` aparece como parte, da mais recente para a mais antiga.

    Para ao juntar `limite` publicações (truncado=True) ou numa página incompleta.
    Não confere o `count` do DJEN: ele trava em 10000 e aqui paramos antes de propósito.
    """
    obter = obter or _obter_padrao
    params = {"nomeParte": nome,
              "dataDisponibilizacaoInicio": inicio.isoformat(),
              "dataDisponibilizacaoFim": fim.isoformat()}
    if tribunal:
        params["siglaTribunal"] = tribunal
    itens: list[dict] = []
    pagina = 1
    truncado = False
    while True:
        resposta = obter({**params, "itensPorPagina": ITENS_POR_PAGINA, "pagina": pagina})
        if not isinstance(resposta, dict):
            raise DJENError("DJEN respondeu num formato inesperado.")
        if resposta.get("status") not in (None, "success"):
            raise DJENError(f"DJEN respondeu com erro: {resposta.get('message')}")
        lote = resposta.get("items") or []
        itens.extend(lote)
        if len(itens) >= limite:
            truncado = True
            itens = itens[:limite]
            break
        if len(lote) < ITENS_POR_PAGINA:
            break
        pagina += 1
        time.sleep(pausa)
    publicacoes: dict[int, dict] = {}
    for item in itens:
        publicacao = _publicacao_com_partes(item)
        if publicacao is not None:
            publicacoes.setdefault(publicacao["id"], publicacao)  # a mesma publicação pode repetir entre páginas
    return {"publicacoes": list(publicacoes.values()), "truncado": truncado}


def agrupar_por_processo(publicacoes: list[dict]) -> list[dict]:
    """Um item por processo: dados da publicação mais recente, partes únicas,
    data da última publicação e quantas foram. Mais recentes primeiro."""
    por_numero: dict[str, list[dict]] = {}
    for p in publicacoes:
        por_numero.setdefault(p["numero"], []).append(p)
    grupos = []
    for numero, pubs in por_numero.items():
        pubs = sorted(pubs, key=lambda p: (p["data"], p["id"]), reverse=True)
        recente = pubs[0]
        partes: list[dict] = []
        vistos: set[str] = set()
        for p in pubs:
            for parte in p["partes"]:
                if parte["nome"] not in vistos:
                    vistos.add(parte["nome"])
                    partes.append(dict(parte))
        grupos.append({
            "numero": numero, "numero_formatado": recente["numero_formatado"],
            "tribunal": recente["tribunal"], "orgao": recente["orgao"], "classe": recente["classe"],
            "partes": partes, "ultima_publicacao": recente["data"],
            "n_publicacoes": len(pubs), "link": recente["link"],
        })
    grupos.sort(key=lambda g: g["numero"])
    grupos.sort(key=lambda g: g["ultima_publicacao"], reverse=True)
    return grupos
