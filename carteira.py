"""Carteira do advogado (linha de comando).

Uso:
    python carteira.py atualizar [--desde AAAA-MM-DD]   # DJEN + PJe → banco
    python carteira.py exportar [--saida Carteira.xlsx]  # banco → planilha
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import date

from dotenv import load_dotenv

warnings.filterwarnings("ignore")

from captura.mni_client import MNIError  # noqa: E402
from nucleo import banco  # noqa: E402
from nucleo import carteira as nucleo_carteira  # noqa: E402
from nucleo.djen import DJENError  # noqa: E402
from nucleo.exportar_carteira import gerar_xlsx  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    # Caminhos relativos (banco, cache/, grupos/, "Meus processos/") valem a
    # partir da pasta do projeto, de onde quer que o comando seja chamado.
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv()
    parser = argparse.ArgumentParser(description="Carteira de processos do advogado.")
    sub = parser.add_subparsers(dest="comando", required=True)
    atualizar = sub.add_parser("atualizar", help="Busca no DJEN e no PJe e grava no banco.")
    atualizar.add_argument("--desde", default="2020-01-01",
                           help="Data inicial das publicações (AAAA-MM-DD).")
    exportar = sub.add_parser("exportar", help="Gera a planilha Carteira.xlsx.")
    exportar.add_argument("--saida", default="Carteira.xlsx")
    args = parser.parse_args(argv)

    conn = banco.conectar()
    try:
        if args.comando == "atualizar":
            try:
                adv = nucleo_carteira.advogado_do_env()
                resultado = nucleo_carteira.atualizar(conn, adv,
                                                      desde=date.fromisoformat(args.desde))
            except (ValueError, MNIError, DJENError) as exc:
                print(f"ERRO: {exc}", file=sys.stderr)
                return 1
            print("Resumo: " + ", ".join(f"{k}={v}" for k, v in resultado.items()))
            return 0
        conteudo = gerar_xlsx(nucleo_carteira.listar(conn, "ativa"),
                              nucleo_carteira.listar(conn, "a_conferir"))
        with open(args.saida, "wb") as f:
            f.write(conteudo)
        print(f"Planilha gravada: {args.saida}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
