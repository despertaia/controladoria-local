"""Trabalhador da controladoria (linha de comando).

Uso:
    python trabalhador.py rodar            # laço contínuo (serviço systemd)
    python trabalhador.py uma              # executa no máximo uma tarefa
    python trabalhador.py pedir varredura  # põe uma varredura na fila (timer)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import warnings

from dotenv import load_dotenv

warnings.filterwarnings("ignore")

from nucleo import banco, carteira, painel_dados, tarefas_fila  # noqa: E402
from nucleo import trabalhador as nucleo_trabalhador  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Trabalhador da controladoria.")
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("rodar")
    sub.add_parser("uma")
    pedir = sub.add_parser("pedir")
    pedir.add_argument("tipo", choices=["varredura"])
    args = parser.parse_args(argv)

    if args.comando == "rodar":
        nucleo_trabalhador.rodar(banco.conectar, adv_fn=carteira.advogado_do_env,
                                 hoje_fn=painel_dados.hoje_cuiaba)
        return 0
    conn = banco.conectar()
    try:
        if args.comando == "pedir":
            with conn:
                criada = tarefas_fila.pedir(conn, "varredura", por="agendador")
            print("Varredura posta na fila." if criada else
                  "Já há uma varredura na fila ou rodando.")
            return 0
        tipo = nucleo_trabalhador.executar_uma(conn, adv=carteira.advogado_do_env(),
                                               hoje=painel_dados.hoje_cuiaba())
        print(f"Executada: {tipo}" if tipo else "Fila vazia.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
