import pytest

from nucleo.advogado import e_o_advogado, normalizar_inscricao


@pytest.mark.parametrize("inscricao,esperado", [
    ("MT0054321A", ("MT", "54321")),
    ("54321/MT", ("MT", "54321")),
    ("MT54321", ("MT", "54321")),
    ("13408/B", ("", "13408")),
    ("OAB/MT 54321", ("MT", "54321")),
    ("OAB MT 54321", ("MT", "54321")),
    ("54.321/MT", ("MT", "54321")),
    ("MT-54321-A", ("MT", "54321")),
    ("", None),
    (None, None),
])
def test_normalizar_inscricao(inscricao, esperado):
    assert normalizar_inscricao(inscricao) == esperado


def test_e_o_advogado_compara_numero_e_uf():
    assert e_o_advogado("MT0054321A", "54321", "MT")
    assert not e_o_advogado("MT0010999A", "54321", "MT")
    assert not e_o_advogado("SP0054321A", "54321", "MT")
    assert e_o_advogado("54321", "54321", "MT")  # sem UF na inscrição: vale o número
