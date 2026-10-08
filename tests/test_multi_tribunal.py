"""Dois tribunais na mesma instalação (TJMT e TJMG, o mesmo CPF e senhas diferentes):
cada processo vai ao PJe do tribunal do número, com a senha e as instâncias dele."""

import os

import pytest

import painel
import painel_local
from captura import credenciais
from captura.mni_client import CredencialInvalidaError
from nucleo import ajustes_locais, autos, banco, carteira, cofre, mni_fabrica, quadro, tribunal, varredura
from tests.cofre_falso import cofre_falso  # noqa: F401
from tests.fixtures_mni import processo_do_cliente
from tests.test_varredura import ADV, HOJE, PJeFalso, _aviso, _djen

MT = "00000010220248110041"
MG = "50012348020248130024"
TRF = "00000130220244013600"


@pytest.fixture
def dois(monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAIS", "TJMT,TJMG")
    monkeypatch.setenv("PJE_CPF", "12345678901")
    monkeypatch.setenv("PJE_SENHA_TJMT", "senha-mt")
    monkeypatch.setenv("PJE_SENHA_2GRAU_TJMT", "senha-mt-2")
    monkeypatch.setenv("PJE_SENHA_TJMG", "senha-mg")


# --- nucleo.tribunal -----------------------------------------------------------

def test_configurados_principal_e_do_numero(dois):
    assert [t.sigla for t in tribunal.configurados()] == ["TJMT", "TJMG"]
    assert tribunal.atual().sigla == "TJMT"
    assert tribunal.do_numero(MT).sigla == "TJMT"
    assert tribunal.do_numero("5001234-80.2024.8.13.0024").sigla == "TJMG"
    assert tribunal.do_numero(TRF) is None and tribunal.do_numero("123") is None
    assert tribunal.e_do_tribunal("8", "11") and tribunal.e_do_tribunal("8", "13")
    assert carteira.e_do_tribunal(MT) and carteira.e_do_tribunal(MG)
    assert not carteira.e_do_tribunal(TRF)
    assert carteira.instancias_de_busca(MT) == ("1grau", "2grau")
    assert carteira.instancias_de_busca(MG) == ("1grau",)


def test_senhas_por_tribunal(dois):
    assert tribunal.senha() == tribunal.senha("TJMT") == "senha-mt"
    assert tribunal.senha("tjmg") == "senha-mg"
    assert tribunal.senha_2grau("TJMG") == ""
    assert credenciais.credencial_do_env("TJMG") == {"cpf": "12345678901", "senha": "senha-mg"}
    assert credenciais.credencial_do_env() == {"cpf": "12345678901", "senha": "senha-mt",
                                               "senha_2grau": "senha-mt-2"}


def test_senha_sem_sigla_do_principal_cai_na_antiga(monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAIS", "TJMG,TJMT")
    monkeypatch.setenv("PJE_SENHA", "antiga")
    assert tribunal.atual().sigla == "TJMG"
    assert tribunal.senha("TJMG") == "antiga"
    assert tribunal.senha("TJMT") == ""  # a antiga é só do principal


def test_producao_so_tjmt_continua_igual(monkeypatch):
    monkeypatch.setenv("TJMT_CPF", "111")
    monkeypatch.setenv("TJMT_SENHA", "prod")
    monkeypatch.setenv("TJMT_SENHA_2GRAU", "prod-2")
    assert [t.sigla for t in tribunal.configurados()] == ["TJMT"]
    assert tribunal.senha() == tribunal.senha("TJMT") == "prod"
    assert credenciais.credencial_do_env() == {"cpf": "111", "senha": "prod",
                                               "senha_2grau": "prod-2"}
    assert not carteira.e_do_tribunal(MG) and carteira.a_conferir_fora() == "A conferir (fora do TJMT)"


# --- fábrica -------------------------------------------------------------------

class _Registro:
    def __init__(self, **kw):
        self.kw = kw


def test_fabrica_usa_wsdl_senha_e_endereco_de_cada_tribunal(dois, monkeypatch):
    monkeypatch.setattr(mni_fabrica, "MNIClient", _Registro)
    mt1 = mni_fabrica.criar_cliente("1grau")
    mt2 = mni_fabrica.criar_cliente("2grau", tribunal="TJMT")
    mg = mni_fabrica.criar_cliente("1grau", tribunal=tribunal.TRIBUNAIS["TJMG"])
    assert mt1.kw["senha"] == "senha-mt" and mt2.kw["senha"] == "senha-mt-2"
    assert "endereco" not in mt1.kw and "tjmt" in mt1.kw["wsdl_url"]
    assert mg.kw["senha"] == "senha-mg" and "tjmg" in mg.kw["wsdl_url"]
    assert mg.kw["endereco"] == mg.kw["wsdl_url"].split("?")[0]
    with pytest.raises(Exception):
        mni_fabrica.criar_cliente("2grau", tribunal="TJMG")  # TJMG não tem 2º grau


# --- varredura -----------------------------------------------------------------

@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _fabrica(por_tribunal, chamadas):
    def fabrica(inst, tribunal=None):
        sigla = tribunal.sigla if tribunal is not None else "TJMT"
        chamadas.append((sigla, inst))
        return por_tribunal[(sigla, inst)]
    return fabrica


def test_varredura_consulta_cada_processo_no_pje_do_seu_tribunal(dois, conn):
    mt1 = PJeFalso({MT: processo_do_cliente()})
    mt2 = PJeFalso()
    mg = PJeFalso({MG: processo_do_cliente()}, avisos=[_aviso("9", MG)])
    chamadas = []
    r = varredura.executar(
        conn, ADV, hoje=HOJE,
        fabrica=_fabrica({("TJMT", "1grau"): mt1, ("TJMT", "2grau"): mt2,
                          ("TJMG", "1grau"): mg}, chamadas),
        obter_djen=_djen(), pausa_djen=0)
    assert chamadas == [("TJMT", "1grau"), ("TJMT", "2grau"), ("TJMG", "1grau")]
    assert r["erros"] == [] and r["pje_ok"] and r["avisos_novos"] == 1
    assert mt1.consultas == [] and mg.consultas == [MG]  # MT não estava na carteira
    # Aviso do TJMG guardado com a sigla na chave (não se mistura com o 1º grau do TJMT).
    assert conn.execute("SELECT instancia FROM aviso").fetchone()[0] == "TJMG-1grau"
    linha = conn.execute("SELECT instancia, advogado_atua FROM processo WHERE numero = ?",
                         (MG,)).fetchone()
    assert tuple(linha) == ("1grau", 1)


def test_senha_recusada_num_tribunal_nao_impede_o_outro(dois, conn):
    conn.execute("INSERT INTO processo (numero, tribunal) VALUES (?, 'TJMT')", (MT,))
    conn.execute("INSERT INTO processo (numero, tribunal) VALUES (?, 'TJMG')", (MG,))
    conn.commit()
    mt1 = PJeFalso({MT: processo_do_cliente()})
    mg = PJeFalso(avisos_erro=CredencialInvalidaError("recusada"))
    r = varredura.executar(
        conn, ADV, hoje=HOJE,
        fabrica=_fabrica({("TJMT", "1grau"): mt1, ("TJMT", "2grau"): PJeFalso(),
                          ("TJMG", "1grau"): mg}, []),
        obter_djen=_djen(), pausa_djen=0)
    assert r["erros"] == ["PJe TJMG (1º grau): senha recusada"]
    assert r["pje_ok"] and r["sincronizados"] == 1
    assert mt1.consultas == [MT] and mg.consultas == []


def test_confirmado_do_tjmg_vai_ao_djen_pelo_numero_e_ao_pje_do_tjmg(dois, conn):
    """"É meu" (main) com vários tribunais (local): o confirmado do TJMG, sem a OAB,
    é consultado no DJEN pelo número e sincronizado no PJe do TJMG; o do TRF1, só no DJEN."""
    from tests.fixtures_djen import item_djen
    from tests.test_varredura import _djen_por_numero
    with conn:
        for numero, sigla in ((MG, "TJMG"), (TRF, "TRF1")):
            carteira.registrar_candidato(conn, numero, sigla, "manual")
            carteira.confirmar(conn, numero)
        conn.execute("UPDATE processo SET advogado_atua = 0 WHERE numero = ?", (MG,))
    mt1, mg = PJeFalso(), PJeFalso({MG: processo_do_cliente()})
    obter = _djen_por_numero({MG: [item_djen(7, MG, "TJMG", data="2026-10-05")]})
    r = varredura.executar(
        conn, ADV, hoje=HOJE,
        fabrica=_fabrica({("TJMT", "1grau"): mt1, ("TJMT", "2grau"): PJeFalso(),
                          ("TJMG", "1grau"): mg}, []),
        obter_djen=obter, pausa_djen=0)
    assert r["erros"] == [] and r["publicacoes_novas"] == 1
    assert sorted(c["numeroProcesso"] for c in obter.chamadas if "numeroProcesso" in c) \
        == sorted([MG, TRF])
    assert mt1.consultas == [] and mg.consultas == [MG]
    ativa = [x[0] for x in conn.execute(
        f"SELECT numero FROM processo WHERE {carteira.FILTRO_ATIVA} ORDER BY numero")]
    assert ativa == sorted([MG, TRF])
    linha = conn.execute("SELECT * FROM processo WHERE numero = ?", (TRF,)).fetchone()
    assert carteira.situacao(linha) == carteira.ATUA_PELO_DJEN  # confirmado, fora do PJe


def test_nao_confirmado_fora_dos_tribunais_mostra_as_siglas_de_todos(dois, conn):
    with conn:
        carteira.registrar_candidato(conn, TRF, "TRF1", "djen")
    linha = conn.execute("SELECT * FROM processo WHERE numero = ?", (TRF,)).fetchone()
    assert carteira.situacao(linha) == "A conferir (fora do TJMT/TJMG)"


def test_autos_do_tjmg_so_no_1o_grau_do_tjmg(dois, conn, monkeypatch):
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado) "
                 "VALUES (?, 'TJMG', 1, 0)", (MG,))
    quadro.adicionar_item(conn, MG, "publicacao", "1", "2026-10-05")
    quadro.mover(conn, 1, "acao", "advogado")
    conn.commit()
    pedidos = []

    def baixar(cliente, numero, subpasta=None, **_):
        pedidos.append((cliente, subpasta))
        yield {"evento": "processo_fim", "ok": 3, "falhas": 0, "pulados": 0}
    monkeypatch.setattr(autos.lote, "baixar_processo_completo", baixar)
    chamadas = []
    r = autos.baixar_para_demanda(
        conn, 1, fabrica=_fabrica({("TJMG", "1grau"): "cliente-mg"}, chamadas))
    assert r["estado"] == "ok" and r["pecas"] == 3
    assert chamadas == [("TJMG", "1grau")] and pedidos == [("cliente-mg", "1grau")]


# --- painel --------------------------------------------------------------------

def test_painel_cria_o_cliente_do_tribunal_do_processo(dois, monkeypatch):
    criados = []

    def criar(instancia, cred, *, tribunal=None, **_):
        criados.append((tribunal.sigla, instancia, cred["senha"]))
        return object()
    monkeypatch.setattr(painel, "criar_cliente", criar)
    monkeypatch.setattr(painel, "_clientes", {})
    with painel.app.test_request_context():
        painel._cliente_do_processo(MT)
        painel._cliente_do_processo(MT, "2grau")
        painel._cliente_do_processo(MG)
        painel._cliente_do_processo(MG)  # do cache
    assert criados == [("TJMT", "1grau", "senha-mt"), ("TJMT", "2grau", "senha-mt-2"),
                       ("TJMG", "1grau", "senha-mg")]
    assert {chave[0] for chave in painel._clientes} == {"TJMT", "TJMG"}


# --- ajustes -------------------------------------------------------------------

@pytest.fixture
def ajustes(tmp_path, monkeypatch, cofre_falso):  # noqa: F811
    caminho = tmp_path / "dados" / "ajustes.json"
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(caminho))
    for var in ("CARTEIRA_ADVOGADO_NOME", "CARTEIRA_OAB_NUMERO", "CARTEIRA_OAB_UF",
                "SECRET_KEY"):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    return caminho


def test_ajustes_antigos_so_tribunal_e_pje_senha_continuam_valendo(ajustes):
    ajustes.parent.mkdir(parents=True)
    ajustes.write_text('{"tribunal": "TJMT", "advogado_nome": "Maria", "oab_numero": "1",'
                       ' "oab_uf": "MT"}', encoding="utf-8")
    cofre.gravar("pje_cpf", "12345678901")
    cofre.gravar("pje_senha", "antiga")
    cofre.gravar("pje_senha_2grau", "antiga-2")
    assert ajustes_locais.tribunais_guardados() == ["TJMT"]
    assert ajustes_locais.configurado() is True
    ajustes_locais.carregar_no_ambiente()
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT"
    assert os.environ["PJE_SENHA"] == os.environ["PJE_SENHA_TJMT"] == "antiga"
    assert os.environ["PJE_SENHA_2GRAU_TJMT"] == "antiga-2"
    assert credenciais.credencial_do_env("TJMT")["senha_2grau"] == "antiga-2"


def test_salvar_dois_tribunais_e_desmarcar_apaga_as_senhas(ajustes):
    ajustes_locais.salvar(tribunais=["TJMT", "TJMG"], advogado_nome="Maria", oab_numero="1",
                          oab_uf="MT", pje_cpf="12345678901",
                          senhas={"TJMT": ("mt", "mt-2"), "TJMG": ("mg", "")})
    assert ajustes_locais.ler()["tribunais"] == ["TJMT", "TJMG"]
    assert ajustes_locais.ler()["tribunal"] == "TJMT"
    assert cofre.ler("pje_senha_TJMT") == "mt" and cofre.ler("pje_senha_TJMG") == "mg"
    assert cofre.ler("pje_senha") == "mt"  # espelho do principal
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"
    assert os.environ["PJE_SENHA_TJMG"] == "mg" and os.environ["PJE_SENHA"] == "mt"
    assert tribunal.senha("TJMG") == "mg" and tribunal.senha_2grau("TJMT") == "mt-2"

    ajustes_locais.salvar(tribunais=["TJMG"], advogado_nome="Maria", oab_numero="1",
                          oab_uf="MT", pje_cpf="12345678901", senhas={"TJMG": (None, None)})
    assert cofre.ler("pje_senha_TJMT") is None and cofre.ler("pje_senha_2grau_TJMT") is None
    assert cofre.ler("pje_senha_TJMG") == "mg" and cofre.ler("pje_senha") == "mg"
    assert "PJE_SENHA_TJMT" not in os.environ and os.environ["CONTROLADORIA_TRIBUNAL"] == "TJMG"


# --- tela /configurar ----------------------------------------------------------

@pytest.fixture
def tela(ajustes, tmp_path, monkeypatch):
    caminho = str(tmp_path / "c.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_configurado", False)
    cli = painel.app.test_client()
    cli.sondas = []
    cli.recusar = set()

    def sonda_falsa(cred, instancias, *, forcar_endereco=None, **_k):
        senha = cred["senha"]
        cli.sondas.append((senha, cred.get("senha_2grau"), [i[0] for i in instancias],
                           forcar_endereco))
        estado = ("recusada", "recusou") if senha in cli.recusar else ("ok", "ok")
        return {i[0]: estado for i in instancias}
    monkeypatch.setattr(painel_local, "sondar_login", sonda_falsa)
    return cli


def _form(**extra):
    dados = {"tribunais": ["TJMT", "TJMG"], "cpf": "12345678901",
             "senha_TJMT": "mt", "senha_2grau_TJMT": "mt-2", "senha_TJMG": "mg",
             "advogado_nome": "Maria", "oab_numero": "1", "oab_uf": "MT"}
    dados.update(extra)
    return dados


def test_tela_mostra_uma_caixa_por_tribunal(tela):
    html = tela.get("/configurar").get_data(as_text=True)
    assert "Tenho processos no TJMT" in html and "Tenho processos no TJMG" in html
    assert 'name="senha_TJMG"' in html and 'name="senha_2grau_TJMT"' in html
    assert 'name="senha_2grau_TJMG"' not in html  # TJMG não tem 2º grau


def test_tela_marca_os_dois_e_sonda_cada_um_com_a_sua_senha(tela):
    r = tela.post("/configurar", data=_form())
    assert r.status_code == 302
    assert tela.sondas == [("mt", "mt-2", ["1grau", "2grau"], False),
                           ("mg", None, ["1grau"], True)]
    assert ajustes_locais.tribunais_guardados() == ["TJMT", "TJMG"]
    assert cofre.ler("pje_senha_TJMG") == "mg" and cofre.ler("pje_senha_2grau_TJMT") == "mt-2"
    html = tela.get("/configurar").get_data(as_text=True)
    assert html.count("guardada ✓") == 3 and 'value="mg"' not in html


def test_tela_senha_recusada_num_deles_nao_grava(tela):
    tela.recusar = {"mg"}
    r = tela.post("/configurar", data=_form())
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "O PJe do TJMG recusou o login" in html
    assert "TJMT recusou" not in html and "Nada foi gravado" in html
    assert ajustes_locais.ler() == {} and cofre.ler("pje_senha_TJMT") is None


def test_tela_tribunal_novo_exige_senha_e_vazio_mantem_a_guardada(tela):
    tela.post("/configurar", data=_form(tribunais=["TJMT"], senha_TJMG=""))
    r = tela.post("/configurar", data=_form(senha_TJMT="", senha_2grau_TJMT="", senha_TJMG=""))
    assert "Informe a senha do PJe do TJMG" in r.get_data(as_text=True)
    r = tela.post("/configurar", data=_form(senha_TJMT="", senha_2grau_TJMT=""))
    assert r.status_code == 302
    assert tela.sondas[-2][:2] == ("mt", "mt-2")  # a guardada foi testada de novo
    assert cofre.ler("pje_senha_TJMG") == "mg"


def test_tela_desmarcar_apaga_as_senhas_do_tribunal(tela):
    tela.post("/configurar", data=_form())
    r = tela.post("/configurar", data=_form(tribunais=["TJMG"], senha_TJMG=""))
    assert r.status_code == 302
    assert cofre.ler("pje_senha_TJMT") is None and cofre.ler("pje_senha_2grau_TJMT") is None
    assert ajustes_locais.tribunais_guardados() == ["TJMG"]
    assert os.environ["CONTROLADORIA_TRIBUNAL"] == "TJMG"


def test_tela_sem_tribunal_marcado_guarda_sem_pje(tela):
    r = tela.post("/configurar", data=_form(tribunais=[]))
    assert r.status_code == 302 and tela.sondas == []
    assert ajustes_locais.tribunais_guardados() == [] and cofre.ler("pje_cpf") is None
