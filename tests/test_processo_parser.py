from types import SimpleNamespace as NS

from captura.processo_parser import processo_para_dict
from tests.fixtures_mni import OAB_DANIEL, movimento, processo_do_cliente

NUMERO = "00000010220248110041"


def _com_movimentos():
    movs = [
        movimento("20260716120000", 246, "Arquivado Definitivamente"),
        movimento("20260917090000", "581", "Juntada de Certidão"),
        NS(dataHora="20260101080000", movimentoNacional=None,
           movimentoLocal=NS(descricao="Movimento local"), complemento=None),
    ]
    return processo_do_cliente(movs)


def test_andamentos_trazem_codigo_nacional_em_ordem_decrescente():
    d = processo_para_dict(NUMERO, _com_movimentos())
    assert [a["codigo"] for a in d["andamentos"]] == [581, 246, None]


def test_data_de_ajuizamento():
    assert processo_para_dict(NUMERO, _com_movimentos())["data_ajuizamento"] == "20230105"


def test_advogados_trazem_a_inscricao():
    d = processo_para_dict(NUMERO, _com_movimentos())
    passivo = [p for p in d["partes"] if p["polo"] == "Polo Passivo"][0]
    assert passivo["integrantes"][0]["advogados"][0] == {
        "nome": "MARIA EXEMPLO DA SILVA", "oab": OAB_DANIEL}
