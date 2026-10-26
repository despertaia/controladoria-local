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
import tempfile
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


def _gravar(grupo: dict) -> None:
    """Escrita atômica (arquivo temporário na mesma pasta e `os.replace`): um grupo
    nunca fica pela metade se o computador desligar no meio."""
    os.makedirs(PASTA_GRUPOS, exist_ok=True)
    destino = _caminho(grupo["slug"])
    fd, tmp = tempfile.mkstemp(prefix=".grupo-", suffix=".tmp", dir=PASTA_GRUPOS)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(grupo, f, ensure_ascii=False, indent=2)
        os.replace(tmp, destino)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def criar(nome: str, descricao: str, numeros: list[str]) -> dict:
    slug = _slug_unico(_slugificar(nome))
    grupo = {
        "slug": slug,
        "nome": (nome or "").strip() or slug,
        "descricao": (descricao or "").strip(),
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "numeros": list(dict.fromkeys(numeros)),  # dedup preservando ordem
    }
    _gravar(grupo)
    return grupo


def incluir(slug: str, numeros: list[str]) -> dict | None:
    """Acrescenta números (já conferidos por quem chama) ao fim do grupo, sem duplicar.
    Devolve {"entraram": [...], "ja_estavam": [...]}, ou None se o grupo não existe."""
    grupo = ler(slug)
    if grupo is None:
        return None
    atuais = list(grupo.get("numeros", []))
    antes = set(atuais)  # repetido na mesma lista não é "já estava no grupo"
    entraram, ja_estavam = [], []
    for n in numeros:
        if n in antes:
            if n not in ja_estavam:
                ja_estavam.append(n)
        elif n not in entraram:
            atuais.append(n)
            entraram.append(n)
    if entraram:
        grupo["numeros"] = atuais
        _gravar(grupo)
    return {"entraram": entraram, "ja_estavam": ja_estavam}


def retirar(slug: str, numero: str) -> bool:
    """Tira o número do grupo (o cache e as peças baixadas ficam). False se o grupo ou
    o número não estão lá."""
    grupo = ler(slug)
    if grupo is None or numero not in grupo.get("numeros", []):
        return False
    grupo["numeros"] = [n for n in grupo["numeros"] if n != numero]
    _gravar(grupo)
    return True


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
