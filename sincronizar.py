"""
Sincroniza processos: consulta o PJe do tribunal do escritório e grava o cache JSON.

Este é o código reutilizado por:
- carga inicial / atualização manual em lote (linha de comando);
- botão "buscar novos andamentos" do painel (importa sincronizar_um);
- futuro cron diário às 4h.

Uso:
    python sincronizar.py                 # sincroniza todos os da planilha
    python sincronizar.py <numero>        # sincroniza apenas um processo
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings

from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from captura import cache  # noqa: E402
from captura.mni_client import MNIClient, MNIError  # noqa: E402
from captura.planilha import encontrar_planilha, ler_processos  # noqa: E402
from captura.processo_parser import processo_para_dict  # noqa: E402
from nucleo.mni_fabrica import criar_cliente as criar_cliente_mni  # noqa: E402


def criar_cliente() -> MNIClient:
    load_dotenv()
    return criar_cliente_mni("1grau", intervalo=1.0)


def sincronizar_um(cliente: MNIClient, numero: str) -> dict:
    """Consulta um processo e grava o cache. Retorna os dados gravados."""
    resp = cliente.consultar_processo(numero, incluir_movimentos=True)
    dados = processo_para_dict(numero, resp.processo)
    cache.gravar(dados)
    return dados


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincroniza o cache de processos com o PJe do tribunal.")
    parser.add_argument("numero", nargs="?", help="Número de um processo (opcional).")
    parser.add_argument("--planilha", help="Caminho da planilha .xlsx.")
    args = parser.parse_args()

    try:
        cliente = criar_cliente()
    except (RuntimeError, MNIError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    if args.numero:
        numeros = [args.numero]
    else:
        try:
            caminho = encontrar_planilha(args.planilha)
        except FileNotFoundError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 1
        numeros = [p["numero"] for p in ler_processos(caminho)]
        print(f"Planilha: {caminho} ({len(numeros)} processo(s))\n")

    ok = falhas = 0
    for i, numero in enumerate(numeros, start=1):
        try:
            dados = sincronizar_um(cliente, numero)
            print(f"  [{i}/{len(numeros)}] {dados['numero_formatado']} — "
                  f"{len(dados['andamentos'])} andamentos, {len(dados['documentos'])} documentos")
            ok += 1
        except (MNIError, ValueError) as exc:
            falhas += 1
            print(f"  [{i}/{len(numeros)}] {numero} — FALHOU: {exc}")

    print(f"\nConcluído: {ok} sincronizado(s), {falhas} falha(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
