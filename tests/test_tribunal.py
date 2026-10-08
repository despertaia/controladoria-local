"""Tribunal do escritório: TJMT (padrão, produção) ou TJMG, escolhido por
CONTROLADORIA_TRIBUNAL e lido a cada uso (o lançador local ajusta o ambiente
depois de alguns imports)."""

import os

import pytest
import requests

from captura import credenciais
from captura.instancias import INSTANCIAS, instancias
from captura.mni_client import MNIClient, ServicoIndisponivelError
from nucleo import mni_fabrica, tribunal

WSDL_TJMG = os.path.join(os.path.dirname(__file__), "fixtures", "tjmg_intercomunicacao.wsdl")


@pytest.fixture
def tjmg(monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "TJMG")


def test_padrao_e_o_tjmt():
    assert tribunal.atual().sigla == "TJMT"
    assert [i[0] for i in instancias()] == ["1grau", "2grau"]


def test_sigla_desconhecida_cai_no_tjmt(monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "TJXX")
    assert tribunal.atual().sigla == "TJMT"


def test_instancias_do_tjmg_mesmo_com_import_anterior(tjmg):
    # INSTANCIAS foi importado no topo do módulo, antes do ambiente mudar.
    assert [i[0] for i in INSTANCIAS] == ["1grau"]
    assert len(INSTANCIAS) == 1 and INSTANCIAS[0][2] == "TJMG_WSDL_URL"
    assert not tribunal.atual().tem_instancia("2grau")


def test_credenciais_pje_com_fallback_tjmt(monkeypatch):
    for v in ("TJMT_CPF", "TJMT_SENHA", "TJMT_SENHA_2GRAU"):
        monkeypatch.delenv(v, raising=False)
    assert credenciais.credencial_do_env() is None
    monkeypatch.setenv("TJMT_CPF", "111")
    monkeypatch.setenv("TJMT_SENHA", "velha")
    assert credenciais.credencial_do_env() == {"cpf": "111", "senha": "velha"}
    monkeypatch.setenv("PJE_CPF", "222")
    monkeypatch.setenv("PJE_SENHA", "nova")
    monkeypatch.setenv("PJE_SENHA_2GRAU", "nova2")
    assert credenciais.credencial_do_env() == {"cpf": "222", "senha": "nova",
                                               "senha_2grau": "nova2"}


class _Registro:
    def __init__(self, **kw):
        self.kw = kw


def test_fabrica_no_tjmg_usa_a_consulta_publica_e_forca_o_endereco(tjmg, monkeypatch):
    monkeypatch.setattr(mni_fabrica, "MNIClient", _Registro)
    monkeypatch.delenv("TJMG_WSDL_URL", raising=False)
    monkeypatch.setenv("PJE_TIMEOUT", "30")
    monkeypatch.setenv("PJE_INTERVALO_SEGUNDOS", "2")
    c = mni_fabrica.criar_cliente("1grau", {"cpf": "1", "senha": "s"})
    assert c.kw["wsdl_url"] == ("https://pje-consulta-publica.tjmg.jus.br/pje/"
                                "intercomunicacao?wsdl")
    assert c.kw["endereco"] == "https://pje-consulta-publica.tjmg.jus.br/pje/intercomunicacao"
    assert c.kw["timeout"] == 30 and c.kw["intervalo_minimo_segundos"] == 2.0
    with pytest.raises(mni_fabrica.MNIError, match="TJMG"):
        mni_fabrica.criar_cliente("2grau", {"cpf": "1", "senha": "s"})


def test_fabrica_no_tjmt_nao_forca_endereco(monkeypatch):
    monkeypatch.setattr(mni_fabrica, "MNIClient", _Registro)
    monkeypatch.setenv("TJMT_TIMEOUT", "50")
    c = mni_fabrica.criar_cliente("1grau", {"cpf": "1", "senha": "s"})
    assert "endereco" not in c.kw and c.kw["timeout"] == 50


def test_sem_credencial_fala_em_configuracao(monkeypatch):
    monkeypatch.setenv("TJMT_SENHA", "")
    with pytest.raises(mni_fabrica.CredencialAusenteError, match="Configuração"):
        mni_fabrica.criar_cliente("1grau")


def _endereco_chamado(monkeypatch, cliente) -> str:
    chamados = []

    def post(self, url, *a, **k):
        chamados.append(url)
        raise requests.exceptions.ConnectionError("sem rede no teste")

    monkeypatch.setattr(requests.Session, "post", post)
    with pytest.raises(ServicoIndisponivelError) as erro:
        cliente.consultar_avisos_pendentes()
    assert tribunal.atual().sigla in str(erro.value)  # mensagem com a sigla do tribunal
    return chamados[0]


def test_wsdl_do_tjmg_declara_o_host_bloqueado(tjmg, monkeypatch):
    cli = MNIClient(WSDL_TJMG, "1", "s", intervalo_minimo_segundos=0)
    assert _endereco_chamado(monkeypatch, cli) == "https://pje.tjmg.jus.br/pje/intercomunicacao"


def test_endereco_forcado_e_o_usado_nas_chamadas(tjmg, monkeypatch):
    destino = "https://pje-consulta-publica.tjmg.jus.br/pje/intercomunicacao"
    cli = MNIClient(WSDL_TJMG, "1", "s", intervalo_minimo_segundos=0, endereco=destino)
    assert _endereco_chamado(monkeypatch, cli) == destino


def test_fuso_do_tribunal(tjmg):
    from datetime import datetime
    assert datetime(2026, 10, 7, 12, tzinfo=tribunal.fuso()).utcoffset().total_seconds() == -3 * 3600


TJMG_N = "50012348020248130024"
TJMT_N = "00000013920248110041"


def test_so_o_tribunal_do_escritorio_vai_ao_pje(tjmg):
    from captura.processo_parser import validar_cnj
    from nucleo import carteira
    assert carteira.e_do_tribunal(TJMG_N) and carteira.e_tjmt(TJMG_N)  # apelido antigo
    assert not carteira.e_do_tribunal(TJMT_N)
    v = validar_cnj(TJMT_N)
    assert not v["ok"] and v["motivo"].startswith("não é do TJMG")
    assert validar_cnj(TJMG_N)["ok"] and validar_cnj(TJMG_N)["tjmt"]
    assert carteira.instancias_de_busca() == ("1grau",)


def test_no_tjmt_continua_como_antes():
    from nucleo import carteira
    assert carteira.e_tjmt(TJMT_N) and not carteira.e_tjmt(TJMG_N)
    assert carteira.instancias_de_busca() == ("1grau", "2grau")


def test_sigla_pelo_numero_conhece_o_tjmg():
    from nucleo import carteira
    assert carteira.tribunal_do_numero(TJMG_N) == "TJMG"
    assert carteira.tribunal_do_numero(TJMT_N) == "TJMT"


def test_situacao_a_conferir_fora_do_tribunal(tjmg):
    from nucleo import carteira
    linha = {"erro_sincronizacao": None, "advogado_atua": None, "instancia": None}
    assert carteira.situacao({**linha, "tribunal": "TJMG"}) == carteira.A_CONFERIR_NAO_SINCRONIZADO
    assert carteira.situacao({**linha, "tribunal": "TJMT"}) == "A conferir (fora do TJMG)"


def test_relogio_no_fuso_do_tribunal(tjmg):
    from nucleo import eventos, painel_dados
    assert eventos.agora().endswith("-03:00")
    assert painel_dados.agora_cuiaba().utcoffset().total_seconds() == -3 * 3600
    assert eventos.FUSO.key == "America/Sao_Paulo" and painel_dados.FUSO.key == "America/Sao_Paulo"


def test_fuso_sem_tzdata_usa_deslocamento_fixo(tjmg, monkeypatch):
    import zoneinfo

    def sem_base(_nome):
        raise zoneinfo.ZoneInfoNotFoundError("sem tzdata")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", sem_base)
    assert tribunal.fuso().utcoffset(None).total_seconds() == -3 * 3600


def test_autos_fora_do_tribunal_falam_a_sigla(tjmg):
    from nucleo import autos
    assert "PJe do TJMG" in autos.detalhe_fora()
