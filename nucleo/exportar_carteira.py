"""Planilha Carteira.xlsx a partir do banco: aba "Carteira" (processos em que
o advogado atua, sem arquivados) e aba "A conferir" (atuação não confirmada:
fora do tribunal do escritório, OAB não consta no processo ou sem acesso pelo PJe)."""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from captura.processo_parser import formatar_numero_cnj
from nucleo.carteira import data_de_referencia, situacao

COLUNAS = [
    "Processo", "Tribunal", "Instância", "Cliente", "Polo do cliente", "Parte contrária",
    "Órgão julgador", "Classe", "Valor da causa", "Último andamento em", "Último andamento",
    "Dias sem movimento", "Última publicação em", "Fontes", "Situação",
]
_LARGURAS = [27, 9, 10, 32, 14, 32, 30, 8, 16, 12, 45, 10, 12, 24, 26]
_INSTANCIAS = {"1grau": "1º grau", "2grau": "2º grau"}


def _data_br(aaaammdd: str | None) -> str:
    s = (aaaammdd or "")[:8]
    return f"{s[6:8]}/{s[4:6]}/{s[0:4]}" if len(s) == 8 else ""


def dias_sem_movimento(aaaammdd: str | None, hoje: date) -> int | None:
    s = (aaaammdd or "")[:8]
    if len(s) != 8:
        return None
    return (hoje - date(int(s[0:4]), int(s[4:6]), int(s[6:8]))).days


def _linha(p, hoje: date) -> list:
    referencia = data_de_referencia(p)
    return [
        formatar_numero_cnj(p["numero"]), p["tribunal"],
        _INSTANCIAS.get(p["instancia"] or "", ""), p["cliente"] or "",
        p["polo_cliente"] or "", p["parte_contraria"] or "", p["orgao_julgador"] or "",
        p["classe"] or "", p["valor_causa"] or "", _data_br(p["ultimo_andamento_data"]),
        p["ultimo_andamento_texto"] or "", dias_sem_movimento(referencia, hoje),
        _data_br(p["ultima_publicacao_data"]), ", ".join(json.loads(p["fontes"])),
        situacao(p),
    ]


def gerar_xlsx(ativa: list, a_conferir: list, hoje: date | None = None) -> bytes:
    hoje = hoje or date.today()
    wb = Workbook()
    principal = wb.active
    principal.title = "Carteira"
    for aba, linhas in ((principal, ativa), (wb.create_sheet("A conferir"), a_conferir)):
        aba.append(COLUNAS)
        for celula in aba[1]:
            celula.font = Font(bold=True)
        for p in linhas:
            aba.append(_linha(p, hoje))
        aba.freeze_panes = "A2"
        for i, largura in enumerate(_LARGURAS):
            aba.column_dimensions[chr(ord("A") + i)].width = largura
    saida = BytesIO()
    wb.save(saida)
    return saida.getvalue()
