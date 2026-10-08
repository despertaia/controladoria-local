"""
Baixa e salva as peças de um processo do tribunal do escritório (TJMT, TJMG…).

Uso:
    python baixar.py <numero>                 # baixa TODAS as peças
    python baixar.py <numero> -q 10           # as 10 mais recentes
    python baixar.py <numero> --intervalo 5 20  # da 5ª à 20ª (na lista)

As peças são salvas em peticoes/<numero>/ (pasta ignorada pelo Git).
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings

from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from captura import credenciais  # noqa: E402
from nucleo.mni_fabrica import criar_cliente as criar_cliente_mni  # noqa: E402
from captura.downloader import pasta_do_processo, salvar_documento  # noqa: E402
from captura.mni_client import (  # noqa: E402
    CredencialInvalidaError,
    MNIError,
    ProcessoNaoEncontradoError,
    ServicoIndisponivelError,
    TimeoutTJMTError,
)


def _formatar_tamanho(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _lotes(lista, tamanho):
    for i in range(0, len(lista), tamanho):
        yield lista[i:i + tamanho]


def _selecionar(docs, args):
    """Aplica a opção de seleção sobre a lista (ordenada do mais recente p/ o mais antigo)."""
    if args.intervalo:
        inicio, fim = args.intervalo
        if inicio < 1 or fim < inicio:
            raise ValueError("Intervalo inválido. Use, por exemplo: --intervalo 1 10.")
        return docs[inicio - 1:fim]
    if args.quantidade is not None:
        if args.quantidade < 1:
            raise ValueError("A quantidade precisa ser pelo menos 1.")
        return docs[:args.quantidade]
    return docs  # padrão: todas


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baixa as peças de um processo do PJe e salva em disco."
    )
    parser.add_argument("numero_processo", help="Número do processo (com ou sem máscara CNJ).")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument(
        "-q", "--quantidade", type=int, metavar="N",
        help="Baixa apenas as N peças mais recentes.",
    )
    grupo.add_argument(
        "--intervalo", type=int, nargs=2, metavar=("INICIO", "FIM"),
        help="Baixa da posição INICIO até FIM (1 = mais recente).",
    )
    parser.add_argument(
        "--lote", type=int, default=5, metavar="N",
        help="Quantas peças pedir por requisição ao tribunal (padrão: 5).",
    )
    args = parser.parse_args()

    load_dotenv()
    if credenciais.credencial_do_env() is None:
        print("ERRO: preencha o CPF e a senha do PJe no .env (PJE_CPF e PJE_SENHA; "
              "TJMT_CPF e TJMT_SENHA também valem).", file=sys.stderr)
        return 1

    try:
        cliente = criar_cliente_mni("1grau", intervalo=1.0)

        print(f"Listando documentos do processo {args.numero_processo}...")
        resp = cliente.consultar_processo(args.numero_processo)
        docs = getattr(resp.processo, "documento", None) or []
        if not docs:
            print("Nenhum documento encontrado neste processo.")
            return 0

        selecionados = _selecionar(docs, args)
        total = len(selecionados)
        print(f"{len(docs)} peça(s) no processo; vou baixar {total}.\n")

        meta_por_id = {str(getattr(d, "idDocumento", "")): d for d in selecionados}
        pasta = pasta_do_processo(args.numero_processo)

        indice = 0
        ok = 0
        falhas = 0
        bytes_totais = 0

        for lote in _lotes(selecionados, args.lote):
            ids = [str(getattr(d, "idDocumento", "")) for d in lote]
            baixados = cliente.baixar_documentos(args.numero_processo, ids)
            baixados_por_id = {str(getattr(d, "idDocumento", "")): d for d in baixados}

            for doc_meta in lote:
                indice += 1
                id_d = str(getattr(doc_meta, "idDocumento", ""))
                descricao = getattr(doc_meta, "descricao", "documento")
                doc_baixado = baixados_por_id.get(id_d)

                if doc_baixado is None:
                    falhas += 1
                    print(f"  [{indice}/{total}] {descricao} — FALHOU (não retornou)")
                    continue

                r = salvar_documento(pasta, indice, doc_baixado, metadados=doc_meta)
                if r.erro:
                    falhas += 1
                    print(f"  [{indice}/{total}] {descricao} — FALHOU ({r.erro})")
                else:
                    ok += 1
                    bytes_totais += r.tamanho_bytes
                    nome = os.path.basename(r.caminho)
                    print(f"  [{indice}/{total}] {nome}  ({_formatar_tamanho(r.tamanho_bytes)})")

    except CredencialInvalidaError as exc:
        print(f"\nERRO DE CREDENCIAL: {exc}", file=sys.stderr)
        return 2
    except ProcessoNaoEncontradoError as exc:
        print(f"\nPROCESSO NÃO ENCONTRADO: {exc}", file=sys.stderr)
        return 3
    except TimeoutTJMTError as exc:
        print(f"\nTIMEOUT: {exc}", file=sys.stderr)
        return 4
    except ServicoIndisponivelError as exc:
        print(f"\nSERVIÇO INDISPONÍVEL: {exc}", file=sys.stderr)
        return 5
    except ValueError as exc:
        print(f"\nENTRADA INVÁLIDA: {exc}", file=sys.stderr)
        return 6
    except MNIError as exc:
        print(f"\nERRO: {exc}", file=sys.stderr)
        return 7

    print()
    print("-" * 60)
    print(f"Concluído: {ok} salva(s), {falhas} falha(s), "
          f"{_formatar_tamanho(bytes_totais)} no total.")
    print(f"Pasta: {os.path.abspath(pasta)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
