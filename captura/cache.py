"""
Cache em disco dos dados de cada processo (um JSON por processo).

O painel lê daqui (rápido, offline). A sincronização escreve aqui.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

PASTA_CACHE = "cache"


def _caminho(numero_digitos: str) -> str:
    return os.path.join(PASTA_CACHE, f"{numero_digitos}.json")


def gravar(dados: dict) -> str:
    os.makedirs(PASTA_CACHE, exist_ok=True)
    dados = dict(dados)
    dados["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    # Preserva o resumo de IA já existente ao re-sincronizar os dados do processo.
    if "resumo_ia" not in dados:
        anterior = ler(dados["numero"])
        if anterior and anterior.get("resumo_ia"):
            dados["resumo_ia"] = anterior["resumo_ia"]
            dados["resumo_em"] = anterior.get("resumo_em")
    caminho = _caminho(dados["numero"])
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    return caminho


def gravar_resumo(numero_digitos: str, texto: str) -> None:
    """Grava o resumo de IA no cache do processo, sem mexer no resto."""
    dados = ler(numero_digitos) or {"numero": numero_digitos}
    dados["resumo_ia"] = texto
    dados["resumo_em"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(PASTA_CACHE, exist_ok=True)
    with open(_caminho(numero_digitos), "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def ler(numero_digitos: str) -> dict | None:
    caminho = _caminho(numero_digitos)
    if not os.path.exists(caminho):
        return None
    with open(caminho, "r", encoding="utf-8") as f:
        return json.load(f)


def existe(numero_digitos: str) -> bool:
    return os.path.exists(_caminho(numero_digitos))
