"""
Lê uma planilha de processos e gera, para cada um, um relatório com os
andamentos e a lista de documentos — SEM baixar o conteúdo das peças.

Uso:
    python listar.py                        # usa o .xlsx em "Meus processos/"
    python listar.py caminho/planilha.xlsx  # usa uma planilha específica

Saída: um arquivo markdown por processo em relatorios/ (ignorado pelo Git).
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import warnings

import openpyxl
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from captura import credenciais  # noqa: E402
from nucleo.mni_fabrica import criar_cliente as criar_cliente_mni  # noqa: E402
from captura.mni_client import (  # noqa: E402
    CredencialInvalidaError,
    MNIError,
    ProcessoNaoEncontradoError,
    ServicoIndisponivelError,
    TimeoutTJMTError,
)

PASTA_RELATORIOS = "relatorios"
PASTA_PLANILHAS = "Meus processos"


# ---------------------------------------------------------------------------
# Leitura da planilha.
# ---------------------------------------------------------------------------

def encontrar_planilha(caminho_arg: str | None) -> str:
    if caminho_arg:
        return caminho_arg
    candidatos = glob.glob(os.path.join(PASTA_PLANILHAS, "*.xlsx"))
    if not candidatos:
        raise FileNotFoundError(
            f"Nenhuma planilha .xlsx encontrada em '{PASTA_PLANILHAS}/'. "
            f"Informe o caminho: python listar.py caminho/arquivo.xlsx"
        )
    return candidatos[0]


def ler_numeros(caminho: str) -> list[str]:
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    linhas = list(ws.iter_rows(values_only=True))
    if not linhas:
        return []

    cabecalho = linhas[0]
    coluna = None
    for i, titulo in enumerate(cabecalho):
        if titulo and "processo" in str(titulo).lower():
            coluna = i
            break

    numeros = []
    for linha in linhas[1:]:
        # Se achamos a coluna, usamos ela; senão, varremos a linha inteira.
        celulas = [linha[coluna]] if (coluna is not None and coluna < len(linha)) else linha
        for celula in celulas:
            if not celula:
                continue
            digitos = "".join(c for c in str(celula) if c.isdigit())
            if len(digitos) == 20:
                numeros.append(str(celula).strip())
                break
    return numeros


# ---------------------------------------------------------------------------
# Formatação.
# ---------------------------------------------------------------------------

def _data(valor) -> str:
    s = "".join(c for c in str(valor or "") if c.isdigit())
    if len(s) >= 8:
        base = f"{s[6:8]}/{s[4:6]}/{s[0:4]}"
        return f"{base} {s[8:10]}:{s[10:12]}" if len(s) >= 12 else base
    return "—"


def _numero_cnj(numero) -> str:
    n = "".join(c for c in str(numero) if c.isdigit())
    if len(n) != 20:
        return str(numero)
    return f"{n[0:7]}-{n[7:9]}.{n[9:13]}.{n[13:14]}.{n[14:16]}.{n[16:20]}"


def _texto_movimento(m) -> str:
    partes: list[str] = []
    mn = getattr(m, "movimentoNacional", None)
    if mn is not None:
        for x in (getattr(mn, "complemento", None) or []):
            if str(x).strip():
                partes.append(str(x).strip())
        if not partes:
            cod = getattr(mn, "codigoNacional", None)
            if cod is not None:
                partes.append(f"[movimento nacional {cod}]")
    ml = getattr(m, "movimentoLocal", None)
    if ml is not None:
        desc = getattr(ml, "descricao", None)
        if desc:
            partes.append(str(desc))
    for x in (getattr(m, "complemento", None) or []):
        if str(x).strip():
            partes.append(str(x).strip())
    return " — ".join(partes) if partes else "(sem descrição)"


def _polo(sigla: str) -> str:
    return {
        "AT": "Polo Ativo", "PA": "Polo Passivo", "TC": "Terceiro",
        "TJ": "Terceiro Juízo", "VI": "Vítima", "FL": "Fiscal da Lei",
    }.get(str(sigla).upper(), f"Polo {sigla}")


def montar_relatorio(numero: str, processo) -> str:
    linhas = [f"# Processo {_numero_cnj(numero)}", ""]

    cab = getattr(processo, "dadosBasicos", None)
    if cab is not None:
        orgao = getattr(cab, "orgaoJulgador", None)
        nome_orgao = getattr(orgao, "nomeOrgao", "—") if orgao is not None else "—"
        linhas += [
            f"- **Classe (código):** {getattr(cab, 'classeProcessual', '—')}",
            f"- **Valor da causa:** {getattr(cab, 'valorCausa', '—')}",
            f"- **Órgão julgador:** {nome_orgao}",
            "",
            "## Partes",
            "",
        ]
        polos = getattr(cab, "polo", None) or []
        if polos:
            for polo in polos:
                linhas.append(f"**{_polo(getattr(polo, 'polo', ''))}:**")
                for parte in (getattr(polo, "parte", None) or []):
                    pessoa = getattr(parte, "pessoa", None)
                    nome = getattr(pessoa, "nome", "—") if pessoa is not None else "—"
                    linhas.append(f"- {nome}")
                linhas.append("")
        else:
            linhas += ["(não retornadas)", ""]

    # Andamentos (mais recentes primeiro).
    movs = list(getattr(processo, "movimento", None) or [])
    movs.sort(key=lambda m: str(getattr(m, "dataHora", "")), reverse=True)
    linhas += [f"## Andamentos ({len(movs)})", ""]
    if movs:
        for m in movs:
            linhas.append(f"- {_data(getattr(m, 'dataHora', ''))} — {_texto_movimento(m)}")
    else:
        linhas.append("(nenhum andamento retornado)")
    linhas.append("")

    # Documentos (só metadados; nada é baixado).
    docs = list(getattr(processo, "documento", None) or [])
    linhas += [f"## Documentos ({len(docs)})", ""]
    if docs:
        for d in docs:
            descricao = getattr(d, "descricao", "(sem descrição)")
            mimetype = getattr(d, "mimetype", "—")
            linhas.append(f"- {_data(getattr(d, 'dataHora', ''))} — {descricao} ({mimetype})")
    else:
        linhas.append("(nenhum documento retornado)")
    linhas.append("")

    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Principal.
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gera relatórios de andamentos e documentos a partir de uma planilha."
    )
    parser.add_argument("planilha", nargs="?", help="Caminho do .xlsx (opcional).")
    args = parser.parse_args()

    load_dotenv()
    if credenciais.credencial_do_env() is None:
        print("ERRO: preencha o CPF e a senha do PJe no .env (PJE_CPF e PJE_SENHA; "
              "TJMT_CPF e TJMT_SENHA também valem).", file=sys.stderr)
        return 1

    try:
        caminho = encontrar_planilha(args.planilha)
    except FileNotFoundError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    numeros = ler_numeros(caminho)
    if not numeros:
        print(f"Nenhum número de processo válido encontrado em {caminho}.", file=sys.stderr)
        return 1

    print(f"Planilha: {caminho}")
    print(f"{len(numeros)} processo(s) encontrado(s).\n")

    cliente = criar_cliente_mni("1grau", intervalo=1.0)
    os.makedirs(PASTA_RELATORIOS, exist_ok=True)

    ok = 0
    falhas = 0
    for i, numero in enumerate(numeros, start=1):
        rotulo = _numero_cnj(numero)
        try:
            resp = cliente.consultar_processo(numero, incluir_movimentos=True)
            conteudo = montar_relatorio(numero, resp.processo)
            n_mov = len(getattr(resp.processo, "movimento", None) or [])
            n_doc = len(getattr(resp.processo, "documento", None) or [])
        except (CredencialInvalidaError, ProcessoNaoEncontradoError,
                TimeoutTJMTError, ServicoIndisponivelError, MNIError) as exc:
            falhas += 1
            print(f"  [{i}/{len(numeros)}] {rotulo} — FALHOU: {exc}")
            # Grava um relatório mínimo registrando a falha.
            digitos = "".join(c for c in numero if c.isdigit())
            with open(os.path.join(PASTA_RELATORIOS, f"{digitos}.md"), "w", encoding="utf-8") as f:
                f.write(f"# Processo {rotulo}\n\nFALHA NA CONSULTA: {exc}\n")
            continue

        digitos = "".join(c for c in numero if c.isdigit())
        caminho_saida = os.path.join(PASTA_RELATORIOS, f"{digitos}.md")
        with open(caminho_saida, "w", encoding="utf-8") as f:
            f.write(conteudo)
        ok += 1
        print(f"  [{i}/{len(numeros)}] {rotulo} — {n_mov} andamentos, {n_doc} documentos -> {os.path.basename(caminho_saida)}")

    print()
    print("-" * 60)
    print(f"Concluído: {ok} relatório(s) gerado(s), {falhas} falha(s).")
    print(f"Pasta: {os.path.abspath(PASTA_RELATORIOS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
