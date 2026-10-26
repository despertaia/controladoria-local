"""
Motor de captura processual — PJe do tribunal do escritório (TJMT, TJMG…) / MNI 2.2.2.

Uso:
    python main.py <numero_do_processo>

Exemplo:
    python main.py 1234567-89.2023.8.11.0001

Lê as credenciais do arquivo .env, consulta o processo no webservice do tribunal
e imprime no terminal os dados básicos, as partes e a lista de documentos.
NÃO baixa o conteúdo das peças nesta etapa — apenas lista os metadados.
"""

from __future__ import annotations

import argparse
import sys
import warnings

from dotenv import load_dotenv

# Silencia o aviso inofensivo de LibreSSL no Python do macOS.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from captura import credenciais  # noqa: E402
from nucleo import tribunal  # noqa: E402
from nucleo.mni_fabrica import criar_cliente as criar_cliente_mni  # noqa: E402
from captura.mni_client import (  # noqa: E402
    CredencialInvalidaError,
    MNIError,
    ProcessoNaoEncontradoError,
    ServicoIndisponivelError,
    TimeoutTJMTError,
)


# ---------------------------------------------------------------------------
# Apresentação dos dados (formatação amigável no terminal).
# ---------------------------------------------------------------------------

LARGURA = 70


def _linha(caractere: str = "-") -> str:
    return caractere * LARGURA


def _valor(campo, *nomes, padrao: str = "—"):
    """Tenta ler o primeiro atributo existente dentre os nomes informados."""
    for nome in nomes:
        valor = getattr(campo, nome, None)
        if valor not in (None, ""):
            return valor
    return padrao


def _formatar_numero_cnj(numero: str) -> str:
    """Reaplica a máscara CNJ a 20 dígitos: NNNNNNN-DD.AAAA.J.TR.OOOO."""
    n = "".join(c for c in str(numero) if c.isdigit())
    if len(n) != 20:
        return str(numero)
    return f"{n[0:7]}-{n[7:9]}.{n[9:13]}.{n[13:14]}.{n[14:16]}.{n[16:20]}"


def imprimir_processo(processo) -> None:
    """Recebe a estrutura <processo> do MNI e imprime de forma organizada."""
    if processo is None:
        print("O tribunal não retornou dados do processo.")
        return

    cabecalho = getattr(processo, "dadosBasicos", None)

    print(_linha("="))
    print("DADOS DO PROCESSO")
    print(_linha("="))

    if cabecalho is not None:
        numero = _valor(cabecalho, "numero")
        classe = _valor(cabecalho, "classeProcessual")
        valor_causa = _valor(cabecalho, "valorCausa")
        orgao = getattr(cabecalho, "orgaoJulgador", None)
        nome_orgao = _valor(orgao, "nomeOrgao") if orgao is not None else "—"

        print(f"  Número .......: {_formatar_numero_cnj(numero)}")
        print(f"  Classe .......: {classe}")
        print(f"  Valor da causa: {_formatar_valor_causa(valor_causa)}")
        print(f"  Órgão julgador: {nome_orgao}")
    else:
        print("  (cabeçalho não retornado)")

    _imprimir_partes(cabecalho)
    _imprimir_documentos(processo)


def _formatar_valor_causa(valor) -> str:
    if valor in (None, "", "—"):
        return "—"
    try:
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(valor)


def _imprimir_partes(cabecalho) -> None:
    print()
    print(_linha())
    print("PARTES")
    print(_linha())

    if cabecalho is None:
        print("  (não retornadas)")
        return

    polos = getattr(cabecalho, "polo", None) or []
    if not polos:
        print("  (nenhuma parte retornada)")
        return

    for polo in polos:
        tipo_polo = _traduzir_polo(getattr(polo, "polo", ""))
        print(f"\n  {tipo_polo}:")
        partes = getattr(polo, "parte", None) or []
        if not partes:
            print("    (vazio)")
            continue
        for parte in partes:
            pessoa = getattr(parte, "pessoa", None)
            nome = _valor(pessoa, "nome") if pessoa is not None else "—"
            tipo_pessoa = _valor(pessoa, "tipoPessoa", padrao="") if pessoa is not None else ""
            sufixo = f" ({tipo_pessoa})" if tipo_pessoa else ""
            print(f"    - {nome}{sufixo}")


def _traduzir_polo(sigla: str) -> str:
    mapa = {
        "AT": "Polo Ativo",
        "PA": "Polo Passivo",
        "TC": "Terceiro",
        "TJ": "Terceiro Juízo",
        "VI": "Vítima",
        "FL": "Fiscal da Lei",
    }
    return mapa.get(str(sigla).upper(), f"Polo {sigla}")


def _imprimir_documentos(processo) -> None:
    print()
    print(_linha())
    print("DOCUMENTOS")
    print(_linha())

    documentos = getattr(processo, "documento", None) or []
    if not documentos:
        print("  (nenhum documento retornado)")
        return

    print(f"  Total: {len(documentos)} documento(s)\n")
    for i, doc in enumerate(documentos, start=1):
        descricao = _valor(doc, "descricao", "outroParametro", padrao="(sem descrição)")
        tipo = _valor(doc, "tipoDocumento", "tipoDocumentoLocal", padrao="—")
        data = _formatar_data(_valor(doc, "dataHora", padrao=""))
        print(f"  {i:>3}. {descricao}")
        print(f"       tipo: {tipo}  |  data: {data}")


def _formatar_data(valor: str) -> str:
    """Converte AAAAMMDDhhmmss (formato do MNI) para DD/MM/AAAA hh:mm."""
    s = "".join(c for c in str(valor) if c.isdigit())
    if len(s) >= 8:
        ano, mes, dia = s[0:4], s[4:6], s[6:8]
        hora_min = ""
        if len(s) >= 12:
            hora_min = f" {s[8:10]}:{s[10:12]}"
        return f"{dia}/{mes}/{ano}{hora_min}"
    return valor or "—"


# ---------------------------------------------------------------------------
# Programa principal.
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consulta um processo no MNI do PJe e lista seus dados."
    )
    parser.add_argument(
        "numero_processo",
        help="Número do processo (com ou sem máscara CNJ).",
    )
    args = parser.parse_args()

    load_dotenv()
    if credenciais.credencial_do_env() is None:
        print("ERRO: preencha o CPF e a senha do PJe no .env (PJE_CPF e PJE_SENHA; "
              "TJMT_CPF e TJMT_SENHA também valem).", file=sys.stderr)
        return 1

    print(f"Consultando processo {args.numero_processo} no {tribunal.atual().sigla}...\n")

    try:
        cliente = criar_cliente_mni("1grau", intervalo=1.0)
        resposta = cliente.consultar_processo(args.numero_processo)
    except CredencialInvalidaError as exc:
        print(f"ERRO DE CREDENCIAL: {exc}", file=sys.stderr)
        return 2
    except ProcessoNaoEncontradoError as exc:
        print(f"PROCESSO NÃO ENCONTRADO: {exc}", file=sys.stderr)
        return 3
    except TimeoutTJMTError as exc:
        print(f"TIMEOUT: {exc}", file=sys.stderr)
        return 4
    except ServicoIndisponivelError as exc:
        print(f"SERVIÇO INDISPONÍVEL: {exc}", file=sys.stderr)
        return 5
    except ValueError as exc:
        print(f"ENTRADA INVÁLIDA: {exc}", file=sys.stderr)
        return 6
    except MNIError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 7

    imprimir_processo(resposta.processo)

    if resposta.mensagem:
        print()
        print(_linha())
        print(f"Mensagem do tribunal: {resposta.mensagem}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
