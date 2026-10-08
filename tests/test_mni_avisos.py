from types import SimpleNamespace

import pytest

from captura.mni_client import (
    CredencialInvalidaError,
    MNIClient,
    OperacaoBloqueadaError,
    _RateLimiter,
)


def _cliente_com(**operacoes):
    """MNIClient sem baixar WSDL: o serviço SOAP é substituído por funções."""
    cli = MNIClient.__new__(MNIClient)
    cli._cpf, cli._senha = "12345678901", "s"
    cli._rate_limiter = _RateLimiter(0)
    cli._client = SimpleNamespace(service=SimpleNamespace(**operacoes))
    return cli


def test_avisos_pendentes_devolve_a_lista():
    recebido = {}

    def consultarAvisosPendentes(**kw):
        recebido.update(kw)
        return SimpleNamespace(sucesso=True, mensagem="ok", aviso=["a1", "a2"])

    cli = _cliente_com(consultarAvisosPendentes=consultarAvisosPendentes)
    assert cli.consultar_avisos_pendentes() == ["a1", "a2"]
    assert recebido == {"idConsultante": "12345678901", "senhaConsultante": "s"}


def test_avisos_pendentes_vazio_devolve_lista_vazia():
    cli = _cliente_com(consultarAvisosPendentes=lambda **kw: SimpleNamespace(
        sucesso=True, mensagem="Consultados 0 de 0", aviso=None))
    assert cli.consultar_avisos_pendentes() == []


def test_avisos_pendentes_com_login_recusado():
    cli = _cliente_com(consultarAvisosPendentes=lambda **kw: SimpleNamespace(
        sucesso=False, aviso=None,
        mensagem="Erro ao realizar login via MNI. exception invoking: loginFailed"))
    with pytest.raises(CredencialInvalidaError):
        cli.consultar_avisos_pendentes()


def test_consultar_processo_continua_igual():
    recebido = {}

    def consultarProcesso(**kw):
        recebido.update(kw)
        return SimpleNamespace(sucesso=True, mensagem="", processo="P")

    cli = _cliente_com(consultarProcesso=consultarProcesso)
    resposta = cli.consultar_processo("1008888-29.2023.8.11.0041")
    assert resposta.processo == "P"
    assert recebido["numeroProcesso"] == "10088882920238110041"
    assert recebido["idConsultante"] == "12345678901"


def _stub_que_registra():
    chamadas = []

    def stub(**kw):
        chamadas.append(kw)
        return SimpleNamespace(sucesso=True, mensagem="")

    return stub, chamadas


def test_consultar_teor_comunicacao_e_bloqueado_sem_ordem_humana():
    stub, chamadas = _stub_que_registra()
    cli = _cliente_com(consultarTeorComunicacao=stub)
    with pytest.raises(OperacaoBloqueadaError):
        cli._chamar("consultarTeorComunicacao")
    assert chamadas == []


def test_confirmar_recebimento_e_bloqueado_sem_ordem_humana():
    stub, chamadas = _stub_que_registra()
    cli = _cliente_com(confirmarRecebimento=stub)
    with pytest.raises(OperacaoBloqueadaError):
        cli._chamar("confirmarRecebimento")
    assert chamadas == []


def test_operacao_que_registra_ciencia_passa_com_ordem_humana():
    stub, chamadas = _stub_que_registra()
    cli = _cliente_com(consultarTeorComunicacao=stub)
    cli._chamar("consultarTeorComunicacao", ordem_humana=True, x=1)
    assert chamadas == [{"x": 1}]
