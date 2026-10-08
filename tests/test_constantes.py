"""Rótulos de situação e o filtro da carteira ativa vivem num lugar só."""

import pathlib

RAIZ = pathlib.Path(__file__).resolve().parent.parent
ROTULOS = ('"Ativo"', '"Sem acesso pelo PJe"', "Atua (pelo DJEN", '"Arquivado"',
           "A conferir (")


def _fontes():
    yield RAIZ / "painel.py"
    for caminho in sorted((RAIZ / "nucleo").glob("*.py")):
        if caminho.name != "carteira.py":
            yield caminho


def test_rotulos_de_situacao_so_em_carteira():
    for caminho in _fontes():
        texto = caminho.read_text(encoding="utf-8")
        for rotulo in ROTULOS:
            assert rotulo not in texto, f"{rotulo} escrito à mão em {caminho.name}"


def test_filtro_da_carteira_ativa_so_em_carteira():
    for caminho in _fontes():
        assert "advogado_atua = 1 AND arquivado = 0" not in caminho.read_text(encoding="utf-8")
