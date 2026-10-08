"""Versão da Controladoria local, as novidades de cada versão e o aviso de versão nova.

- `atual()`: a versão instalada, lida do arquivo VERSAO da raiz do app (com cache).
- `novidades()`: as seções do NOVIDADES.md ({versao, data, itens}), a mais recente primeiro.
- `versao_publicada()`: a VERSAO do repositório público (GET com timeout curto, cache de
  24 h). Qualquer erro devolve None: sem rede, a Controladoria segue igual.
- `aviso_de_versao()`: só no modo local e só quando a publicada é maior que a instalada.
  Nunca vai à rede: lê o cache que `verificar_em_segundo_plano()` preenche numa thread
  (disparada pelo lançador e pelo primeiro request do painel).
"""

from __future__ import annotations

import os
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
URL_PUBLICADA = ("https://raw.githubusercontent.com/despertaia/controladoria-local/"
                 "main/VERSAO")
TIMEOUT = 5  # segundos
VALIDADE_CACHE = 24 * 3600  # segundos; resposta boa
VALIDADE_ERRO = 3600  # sem resposta: tenta de novo em 1 h, não a cada página

_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# "## 1.2.0 — 07/10/2026" (travessão, meia-risca ou hífen entre a versão e a data)
_TITULO = re.compile(r"^##\s+v?(\d+\.\d+\.\d+)\s*(?:[—–-]\s*(.*?))?\s*$")
_ITEM = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def valida(versao) -> bool:
    return isinstance(versao, str) and _SEMVER.fullmatch(versao.strip()) is not None


def _tupla(versao: str) -> tuple[int, int, int]:
    m = _SEMVER.fullmatch(str(versao).strip())
    if m is None:
        raise ValueError(f"Versão inválida: {versao!r} (esperado X.Y.Z)")
    return int(m[1]), int(m[2]), int(m[3])


def comparar(a: str, b: str) -> int:
    """-1 se a < b, 0 se iguais, 1 se a > b (X.Y.Z, número a número)."""
    ta, tb = _tupla(a), _tupla(b)
    return (ta > tb) - (ta < tb)


@lru_cache(maxsize=None)
def _ler_versao(caminho: str) -> str:
    try:
        texto = Path(caminho).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return texto if valida(texto) else ""


def atual(raiz: Path | str = RAIZ) -> str:
    """Versão instalada ("1.2.0"); "" se o arquivo VERSAO faltar ou for inválido."""
    return _ler_versao(str(Path(raiz) / "VERSAO"))


def ler_novidades(texto: str) -> list[dict]:
    """Seções "## X.Y.Z — data" do NOVIDADES.md, na ordem do arquivo, com os itens
    ("- texto") de cada uma. Linhas fora de uma seção e títulos sem versão ficam de fora."""
    secoes: list[dict] = []
    for linha in texto.splitlines():
        titulo = _TITULO.match(linha)
        if titulo:
            secoes.append({"versao": titulo[1], "data": titulo[2] or "", "itens": []})
            continue
        if linha.startswith("#"):
            continue
        item = _ITEM.match(linha)
        if item and secoes:
            secoes[-1]["itens"].append(item[1])
        elif linha.strip() and secoes and secoes[-1]["itens"] and linha.startswith((" ", "\t")):
            secoes[-1]["itens"][-1] += " " + linha.strip()  # item quebrado em duas linhas
    return secoes


@lru_cache(maxsize=None)
def _novidades_de(caminho: str) -> tuple:
    try:
        texto = Path(caminho).read_text(encoding="utf-8")
    except OSError:
        return ()
    return tuple(ler_novidades(texto))


def novidades(raiz: Path | str = RAIZ) -> list[dict]:
    """[{versao, data, itens}], a mais recente primeiro (a ordem do NOVIDADES.md)."""
    return [dict(s, itens=list(s["itens"])) for s in _novidades_de(str(Path(raiz) / "NOVIDADES.md"))]


# --- versão publicada -------------------------------------------------------------------

_trava = threading.Lock()
_cache: dict = {"valor": None, "quando": None, "validade": 0}
_verificando = False


def _obter_padrao(url: str, timeout: float) -> str:
    import requests
    resposta = requests.get(url, timeout=timeout)
    resposta.raise_for_status()
    return resposta.text


def limpar_cache() -> None:
    global _verificando
    with _trava:
        _cache.update(valor=None, quando=None, validade=0)
        _verificando = False


def _cache_valido(agora: float) -> bool:
    return _cache["quando"] is not None and agora - _cache["quando"] < _cache["validade"]


def versao_publicada(obter=None, agora: float | None = None) -> str | None:
    """A VERSAO do repositório público ("1.3.0") ou None (sem rede, erro, formato
    inválido). Cache em memória: 24 h com resposta boa, 1 h sem ela. `obter(url,
    timeout)` devolve o texto (padrão: GET pelo requests)."""
    agora = time.monotonic() if agora is None else agora
    with _trava:
        if _cache_valido(agora):
            return _cache["valor"]
    try:
        texto = (obter or _obter_padrao)(URL_PUBLICADA, TIMEOUT)
        valor = str(texto).strip() if valida(str(texto)) else None
    except Exception:  # noqa: BLE001 — sem rede, 404, timeout: nada disso derruba o painel
        valor = None
    with _trava:
        _cache.update(valor=valor, quando=agora,
                      validade=VALIDADE_CACHE if valor else VALIDADE_ERRO)
    return valor


def modo_local() -> bool:
    return os.getenv("CONTROLADORIA_LOCAL", "").strip() == "1"


def verificar_em_segundo_plano(obter=None) -> threading.Thread | None:
    """Atualiza o cache da versão publicada numa thread (só no modo local, só com o
    cache vencido e uma de cada vez). Barato: pode ser chamada a cada request."""
    global _verificando
    if not modo_local():
        return None
    with _trava:
        if _verificando or _cache_valido(time.monotonic()):
            return None
        _verificando = True

    def alvo():
        global _verificando
        try:
            versao_publicada(obter)
        finally:
            with _trava:
                _verificando = False

    fio = threading.Thread(target=alvo, name="versao-publicada", daemon=True)
    fio.start()
    return fio


def aviso_de_versao() -> dict | None:
    """{"nova", "atual"} quando há versão maior publicada; None fora do modo local, sem
    resposta do GitHub ou já na mais recente. Só lê o cache (nunca vai à rede)."""
    if not modo_local():
        return None
    with _trava:
        publicada = _cache["valor"] if _cache["quando"] is not None else None
    instalada = atual()
    if not publicada or not instalada or comparar(publicada, instalada) <= 0:
        return None
    return {"nova": publicada, "atual": instalada}
