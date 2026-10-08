"""
Leitura da planilha mestra de processos (.xlsx).

A planilha é a "lista de números" que alimenta o motor. Cada linha vira um
dicionário com os campos disponíveis (número, partes, vara, etc.).
"""

from __future__ import annotations

import glob
import os

import openpyxl

PASTA_PLANILHAS = "Meus processos"

# Mapeia rótulos de coluna (em minúsculas, sem acento exigido) para chaves internas.
_ALIASES = {
    "tipo": "tipo",
    "n do processo": "numero",
    "no do processo": "numero",
    "numero do processo": "numero",
    "processo": "numero",
    "partes": "partes",
    "vara": "vara",
    "distribuido em": "distribuido_em",
    "ultimo movimento": "ultimo_movimento",
    "data ultimo movimento": "data_ultimo_movimento",
}


def _normalizar_titulo(titulo: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(titulo)).encode("ascii", "ignore").decode("ascii")
    return t.strip().lower().replace("º", "").replace(".", "")


def encontrar_planilha(caminho_arg: str | None = None) -> str:
    if caminho_arg:
        return caminho_arg
    candidatos = sorted(glob.glob(os.path.join(PASTA_PLANILHAS, "*.xlsx")))
    if not candidatos:
        raise FileNotFoundError(
            f"Nenhuma planilha .xlsx encontrada em '{PASTA_PLANILHAS}/'."
        )
    return candidatos[0]


def ler_processos(caminho: str) -> list[dict]:
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    linhas = list(ws.iter_rows(values_only=True))
    if not linhas:
        return []

    cabecalho = linhas[0]
    # Descobre qual índice de coluna corresponde a cada chave interna.
    indice_para_chave: dict[int, str] = {}
    for i, titulo in enumerate(cabecalho):
        if titulo is None:
            continue
        chave = _ALIASES.get(_normalizar_titulo(titulo))
        if chave:
            indice_para_chave[i] = chave

    processos = []
    for linha in linhas[1:]:
        registro: dict[str, str] = {}
        for i, valor in enumerate(linha):
            chave = indice_para_chave.get(i)
            if chave and valor not in (None, ""):
                registro[chave] = str(valor).strip()

        numero = registro.get("numero", "")
        digitos = "".join(c for c in numero if c.isdigit())
        if len(digitos) != 20:
            continue  # linha sem número de processo válido
        registro["numero"] = numero
        registro["numero_digitos"] = digitos
        processos.append(registro)

    return processos
