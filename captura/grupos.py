"""
Grupos de processos: coleções nomeadas de números de processo, persistidas em
grupos/<slug>.json. Servem para o usuário juntar uma lista (ex.: a carteira de
um cliente), baixar/analisar tudo e revisitar o histórico depois.

Um grupo só *referencia* números — o cache e as peças continuam compartilhados
por número (sem duplicação). O progresso do download em segundo plano fica num
arquivo separado grupos/<slug>.status.json (ver captura/tarefas.py).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime

PASTA_GRUPOS = "grupos"


def _slugificar(nome: str) -> str:
    t = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "grupo"


def _caminho(slug: str) -> str:
    return os.path.join(PASTA_GRUPOS, f"{slug}.json")


def _slug_unico(base: str) -> str:
    slug, i = base, 2
    while os.path.exists(_caminho(slug)):
        slug, i = f"{base}-{i}", i + 1
    return slug


def criar(nome: str, descricao: str, numeros: list[str]) -> dict:
    os.makedirs(PASTA_GRUPOS, exist_ok=True)
    slug = _slug_unico(_slugificar(nome))
    grupo = {
        "slug": slug,
        "nome": (nome or "").strip() or slug,
        "descricao": (descricao or "").strip(),
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "numeros": list(dict.fromkeys(numeros)),  # dedup preservando ordem
    }
    with open(_caminho(slug), "w", encoding="utf-8") as f:
        json.dump(grupo, f, ensure_ascii=False, indent=2)
    return grupo


def ler(slug: str) -> dict | None:
    caminho = _caminho(slug)
    if not os.path.exists(caminho):
        return None
    with open(caminho, "r", encoding="utf-8") as f:
        return json.load(f)


def listar() -> list[dict]:
    if not os.path.isdir(PASTA_GRUPOS):
        return []
    grupos = []
    for arq in os.listdir(PASTA_GRUPOS):
        if arq.endswith(".json") and not arq.endswith(".status.json"):
            g = ler(arq[:-5])
            if g:
                grupos.append(g)
    grupos.sort(key=lambda g: g.get("criado_em", ""), reverse=True)
    return grupos


def excluir(slug: str) -> None:
    for caminho in (_caminho(slug), os.path.join(PASTA_GRUPOS, f"{slug}.status.json")):
        if os.path.exists(caminho):
            os.remove(caminho)
