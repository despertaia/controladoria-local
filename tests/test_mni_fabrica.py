import pytest

from captura.mni_client import MNIError
from nucleo import mni_fabrica

CRED = {"cpf": "12345678901", "senha": "s1", "senha_2grau": "s2"}


class _Registro:
    def __init__(self, **kw):
        self.kw = kw


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch):
    monkeypatch.setattr(mni_fabrica, "MNIClient", _Registro)
    for variavel in ("TJMT_WSDL_URL", "TJMT_WSDL_URL_2GRAU", "TJMT_TIMEOUT",
                     "TJMT_INTERVALO_SEGUNDOS"):
        monkeypatch.delenv(variavel, raising=False)


def test_senha_e_wsdl_de_cada_instancia(monkeypatch):
    monkeypatch.setenv("TJMT_TIMEOUT", "45")
    c1 = mni_fabrica.criar_cliente("1grau", CRED)
    c2 = mni_fabrica.criar_cliente("2grau", CRED)
    assert c1.kw["senha"] == "s1" and c2.kw["senha"] == "s2"
    assert c1.kw["wsdl_url"] == "https://pje.tjmt.jus.br/pje/intercomunicacao?wsdl"
    assert c2.kw["wsdl_url"] == "https://pje2.tjmt.jus.br/pje2/intercomunicacao?wsdl"
    assert c1.kw["timeout"] == 45 and c1.kw["cpf"] == "12345678901"


def test_intervalo_do_env_e_sobrescrita(monkeypatch):
    assert mni_fabrica.criar_cliente("1grau", CRED).kw["intervalo_minimo_segundos"] == 3.0
    monkeypatch.setenv("TJMT_INTERVALO_SEGUNDOS", "5")
    assert mni_fabrica.criar_cliente("1grau", CRED).kw["intervalo_minimo_segundos"] == 5.0
    assert mni_fabrica.criar_cliente("1grau", CRED, intervalo=0).kw["intervalo_minimo_segundos"] == 0


def test_sem_credencial(monkeypatch):
    monkeypatch.setenv("TJMT_SENHA", "")
    with pytest.raises(mni_fabrica.CredencialAusenteError):
        mni_fabrica.criar_cliente("1grau")


def test_instancia_desconhecida():
    with pytest.raises(MNIError):
        mni_fabrica.criar_cliente("3grau", CRED)


# --- sondar_login: a sonda da tela de configuração (sem rede) ---------------------

from captura.mni_client import (  # noqa: E402
    CredencialInvalidaError, ServicoIndisponivelError, TimeoutTJMTError,
)
from nucleo import mni_fabrica as _fabrica  # noqa: E402
from nucleo import tribunal as _tribunal  # noqa: E402


def _cliente_falso(efeitos: dict, chamadas: list):
    class Falso:
        def __init__(self, *, wsdl_url, cpf, senha, timeout, intervalo_minimo_segundos,
                     endereco=None):
            chamadas.append({"wsdl": wsdl_url, "cpf": cpf, "senha": senha, "timeout": timeout,
                             "endereco": endereco})
            self.efeito = efeitos.get(wsdl_url)
            if isinstance(self.efeito, type) and issubclass(self.efeito, BaseException):
                raise self.efeito("no construtor")

        def consultar_avisos_pendentes(self):
            if isinstance(self.efeito, Exception):
                raise self.efeito
            return []

        def consultar_processo(self, *a, **k):
            raise AssertionError("a sonda usa avisos pendentes")
    return Falso


def test_sondar_login_classifica_cada_instancia(monkeypatch):
    monkeypatch.delenv("TJMT_WSDL_URL", raising=False)
    monkeypatch.delenv("TJMT_WSDL_URL_2GRAU", raising=False)
    tjmt = _tribunal.TRIBUNAIS["TJMT"]
    w1, w2 = tjmt.instancias[0][3], tjmt.instancias[1][3]
    chamadas = []
    falso = _cliente_falso({w2: CredencialInvalidaError("Falha no login. senha=xyz")}, chamadas)
    r = _fabrica.sondar_login({"cpf": "123", "senha": "s1", "senha_2grau": "s2"},
                              tjmt.instancias, cliente_mni=falso)
    assert r["1grau"][0] == "ok"
    assert r["2grau"] == ("recusada", "O PJe recusou o CPF ou a senha.")
    senhas = {c["wsdl"]: c["senha"] for c in chamadas}
    assert senhas == {w1: "s1", w2: "s2"}
    assert {c["timeout"] for c in chamadas} == {_fabrica.TIMEOUT_SONDA}


def test_sondar_login_indisponivel_no_wsdl_e_no_tempo(monkeypatch):
    tjmg = _tribunal.TRIBUNAIS["TJMG"]
    monkeypatch.setenv("TJMG_WSDL_URL", "https://exemplo.invalid/wsdl")
    chamadas = []
    falso = _cliente_falso({"https://exemplo.invalid/wsdl": ValueError}, chamadas)
    r = _fabrica.sondar_login({"cpf": "1", "senha": "s"}, tjmg.instancias,
                              cliente_mni=falso, timeout=5)
    assert r == {"1grau": ("indisponivel", "no construtor")}
    assert chamadas[0]["timeout"] == 5
    falso = _cliente_falso({"https://exemplo.invalid/wsdl": TimeoutTJMTError("demorou. x")}, [])
    assert _fabrica.sondar_login({"cpf": "1", "senha": "s"}, tjmg.instancias,
                                 cliente_mni=falso)["1grau"] == ("indisponivel", "demorou")
    falso = _cliente_falso({"https://exemplo.invalid/wsdl": ServicoIndisponivelError("fora")}, [])
    assert _fabrica.sondar_login({"cpf": "1", "senha": "s"}, tjmg.instancias,
                                 cliente_mni=falso)["1grau"][0] == "indisponivel"


def test_sondar_login_aceita_chave_da_instancia_atual():
    chamadas = []
    r = _fabrica.sondar_login({"cpf": "1", "senha": "s"}, ["1grau"],
                              cliente_mni=_cliente_falso({}, chamadas))
    assert r == {"1grau": ("ok", "Login aceito pelo PJe.")}
    assert chamadas[0]["wsdl"] == _fabrica.wsdl_da_instancia("1grau")
    assert _fabrica.sondar_login({"cpf": "1", "senha": "s"}, []) == {}


def test_sondar_login_tjmg_forca_o_endereco_do_wsdl(monkeypatch):
    monkeypatch.delenv("TJMG_WSDL_URL", raising=False)
    tjmg = _tribunal.TRIBUNAIS["TJMG"]
    chamadas = []
    falso = _cliente_falso({}, chamadas)
    _fabrica.sondar_login({"cpf": "1", "senha": "s"}, tjmg.instancias,
                          cliente_mni=falso, forcar_endereco=tjmg.forcar_endereco)
    assert chamadas[0]["endereco"] == tjmg.instancias[0][3].split("?")[0]
