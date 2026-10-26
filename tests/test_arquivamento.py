import pytest

from nucleo.arquivamento import data_arquivamento, esta_arquivado


def _a(data, codigo):
    return {"data_ordenavel": data, "codigo": codigo, "texto": "x"}


def test_juntada_depois_do_arquivamento_nao_reabre():
    andamentos = [_a("20260917090000", 581), _a("20260716120000", 246), _a("20260608100000", 85)]
    assert data_arquivamento(andamentos) == "20260716"


def test_desarquivamento_reabre():
    assert data_arquivamento([_a("20260801100000", 893), _a("20260716120000", 246)]) is None


def test_arquivado_de_novo_depois_de_desarquivar():
    andamentos = [_a("20260901100000", 246), _a("20260801100000", 893), _a("20260716120000", 246)]
    assert data_arquivamento(andamentos) == "20260901"


def test_baixa_definitiva_conta_como_arquivamento():
    assert data_arquivamento([_a("20250310100000", 22)]) == "20250310"


def test_sem_codigo_de_arquivamento():
    assert data_arquivamento([_a("20260917090000", 581), _a("20260101000000", None)]) is None


@pytest.mark.parametrize("data_arq,ultima_pub,esperado", [
    (None, None, False),
    ("20260716", None, True),
    ("20260716", "20260901", False),   # 47 dias depois: reabre
    ("20260716", "20260816", False),   # 31 dias: reabre
    ("20260716", "20260815", True),    # 30 dias exatos: ainda não reabre
    ("20260716", "20260814", True),    # 29 dias
    ("20260716", "20260801", True),    # 16 dias: custas/arquivamento, não reabre
    ("20260716", "20260716", True),
    ("20260716", "20260101", True),
    ("20260716", "2026-09-01", False),
    ("20260716", "2026-08-01", True),
])
def test_esta_arquivado(data_arq, ultima_pub, esperado):
    assert esta_arquivado(data_arq, ultima_pub) is esperado
