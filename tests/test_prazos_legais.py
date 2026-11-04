"""A lista do código é a tabela lida nas fontes, linha a linha. O arquivo de fontes só existe
no repositório privado: na árvore pública o teste de comparação é pulado."""

import pathlib
import re

import pytest

from nucleo import prazos, prazos_legais

LISTA = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "fontes"
         / "2026-11-01-prazos-legais.md")
UNIDADES = {"úteis": prazos.UTEIS, "corridos": prazos.CORRIDOS}


def _linhas_da_tabela():
    linhas = []
    for linha in LISTA.read_text(encoding="utf-8").splitlines():
        if linha.startswith("| ") and not linha.startswith(("| Código", "|---")):
            linhas.append([c.strip() for c in linha.strip().strip("|").split("|")])
    return linhas


@pytest.mark.skipif(not LISTA.exists(), reason="fontes só no repositório privado")
def test_codigo_igual_a_tabela_das_fontes():
    tabela = _linhas_da_tabela()
    assert len(tabela) == len(prazos_legais.PRAZOS) == 33
    for codigo, ato, dias, unidade, artigo, ressalva in tabela:
        p = prazos_legais.PRAZOS[codigo]
        assert (p.codigo, p.ato, p.dias, p.unidade, p.artigo, p.ressalva) == (
            codigo, ato, int(dias), UNIDADES[unidade], artigo, ressalva)


RAIZ = pathlib.Path(__file__).resolve().parent.parent / "nucleo"
# Onde número de lei pode morar: a lista, a tabela do calendário e a conta.
PERMITIDOS = {"prazos_legais.py", "calendario_semente.py", "prazos.py"}
NUMERO_DE_PRAZO = re.compile(
    r"\b(\d+|um|uma|dois|duas|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|"
    r"quatorze|quinze|vinte|trinta)\s+(dias?|horas?)\b", re.IGNORECASE)
# Linhas de outro assunto (retenção, espera, redação de alerta) que a regra não deve pegar.
LINHAS_QUE_NAO_SAO_LEI: set[tuple[str, str]] = {
    # retenção da lixeira do próprio sistema, não prazo processual
    ("acoes_processo.py", '"excluir": ("Foi para a lixeira. Dá para restaurar por 30 dias.",'),
    ("acoes_processo.py", '"{n} processos foram para a lixeira. Dá para restaurar por 30 dias."),'),
    ("carteira.py", '"""«Restaurar» (lixeira): o processo volta para onde estava (Ativa, A conferir,'),
}


def _textos_literais(codigo: str):
    """Os textos entre aspas do módulo, com a linha: comentários e nomes não entram."""
    import ast
    for no in ast.walk(ast.parse(codigo)):
        if isinstance(no, ast.Constant) and isinstance(no.value, str):
            yield no.lineno, no.value


def test_nenhum_numero_legal_fora_da_lista():
    """Nenhum módulo do núcleo escreve dias de lei em texto: quem precisa lê de prazos_legais."""
    achados = []
    for caminho in sorted(RAIZ.glob("*.py")):
        if caminho.name in PERMITIDOS:
            continue
        codigo = caminho.read_text(encoding="utf-8")
        linhas = codigo.splitlines()
        for n, texto in _textos_literais(codigo):
            m = NUMERO_DE_PRAZO.search(texto)
            if m and (caminho.name, linhas[n - 1].strip()) not in LINHAS_QUE_NAO_SAO_LEI:
                achados.append(f"{caminho.name}:{n}: {m.group(0)}")
    assert not achados, achados


def test_a_regra_pega_o_que_deve_pegar():
    for texto in ("15 dias", "5 dias úteis", "quinze dias", "prazo de 10 dias corridos", "48 horas"):
        assert NUMERO_DE_PRAZO.search(texto), texto
    assert not NUMERO_DE_PRAZO.search("dias úteis")


def test_constantes_que_nao_estao_na_tabela():
    assert prazos_legais.SEM_NUMERO not in prazos_legais.PRAZOS
    assert prazos_legais.DIAS_SEM_NUMERO > 0 and prazos_legais.DIAS_CIENCIA_TACITA > 0
    assert "conferir" in prazos_legais.RESSALVA_SEM_NUMERO
