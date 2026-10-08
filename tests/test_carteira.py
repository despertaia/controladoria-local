import json
from datetime import date
from types import SimpleNamespace

import pytest

from captura.mni_client import (CredencialInvalidaError, MNIError, ProcessoNaoEncontradoError,
                                ServicoIndisponivelError, TimeoutTJMTError)
from captura.processo_parser import processo_para_dict
from nucleo import banco, carteira, eventos
from nucleo.djen import normalizar
from tests.fixtures_djen import item_djen
from tests.fixtures_mni import advogado, movimento, parte, polo, processo, processo_do_cliente

ADV = carteira.Advogado("MARIA EXEMPLO DA SILVA", "54321", "MT")
TJMT_A = "00000010220248110041"
TRF1_B = "00000030220244013600"
TJMT_C = "00000040220248110041"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


class ClienteFalso:
    def __init__(self, respostas, avisos_erro=None):
        self.respostas = respostas
        self.avisos_erro = avisos_erro

    def consultar_avisos_pendentes(self):
        if self.avisos_erro is not None:
            raise self.avisos_erro
        return []

    def consultar_processo(self, numero, incluir_movimentos=False):
        resposta = self.respostas.get(numero)
        if isinstance(resposta, BaseException):
            raise resposta
        if resposta is None:
            raise ProcessoNaoEncontradoError("Processo não encontrado.")
        return SimpleNamespace(processo=resposta)


def processo_de_terceiro():
    """Processo em que Daniel NÃO advoga (só um homônimo de sobrenome)."""
    return processo([
        polo("AT", parte("JOÃO AUTOR", advogado("OUTRO SAGIN", "MT0010999A"))),
        polo("PA", parte("EMPRESA RÉ LTDA", advogado("FULANA DE TAL", "MT0099999A"))),
    ])


def _pub(id_, numero, tribunal="TJMT", data="2026-07-10", oab=None):
    return normalizar(item_djen(id_, numero, tribunal, data, oab), "54321", "MT")


def test_analisar_identifica_cliente_e_parte_contraria():
    a = carteira.analisar_processo(processo_para_dict(TJMT_A, processo_do_cliente()), ADV)
    assert a["advogado_atua"] == 1
    assert a["polo_cliente"] == "Polo Passivo"
    assert a["cliente"] == "MARIA CLIENTE"
    assert a["parte_contraria"] == "BANCO ALFA S.A."


def test_analisar_nao_confunde_homonimo():
    a = carteira.analisar_processo(processo_para_dict(TJMT_C, processo_de_terceiro()), ADV)
    assert a["advogado_atua"] == 0
    assert a["cliente"] == "" and a["parte_contraria"] == "" and a["polo_cliente"] == ""


def test_analisar_arquivamento_e_ultimo_andamento():
    movs = [movimento("20260716120000", 246, "Arquivado Definitivamente"),
            movimento("20260917090000", 581, "Juntada de Certidão")]
    a = carteira.analisar_processo(processo_para_dict(TJMT_A, processo_do_cliente(movs)), ADV)
    assert a["data_arquivamento"] == "20260716"
    assert a["ultimo_andamento_data"] == "20260917"
    assert a["ultimo_andamento_texto"] == "Juntada de Certidão"


def test_registrar_candidato_soma_fontes_e_registra_descoberta(conn):
    assert carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen") is True
    assert carteira.registrar_candidato(conn, TJMT_A, "TJMT", "sistema:cache") is False
    assert carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen") is False
    linha = conn.execute("SELECT fontes FROM processo WHERE numero = ?", (TJMT_A,)).fetchone()
    assert json.loads(linha["fontes"]) == ["djen", "sistema:cache"]
    assert [e["tipo"] for e in eventos.listar(conn, TJMT_A)] == ["processo_descoberto"]


def test_gravar_publicacoes_e_idempotente(conn):
    pubs = [_pub(1, TJMT_A), _pub(2, TRF1_B, "TRF1", oab=("54321", "MT"))]
    assert carteira.gravar_publicacoes(conn, pubs) == 2
    assert carteira.gravar_publicacoes(conn, pubs) == 0
    tipos = [e["tipo"] for e in eventos.listar(conn)]
    assert tipos.count("publicacao_detectada") == 2


def test_sincronizar_cai_para_o_segundo_grau(conn):
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    clientes = {
        "1grau": ClienteFalso({}),
        "2grau": ClienteFalso({TJMT_A: processo_do_cliente(
            [movimento("20260801100000", 85, "Juntada de Petição")])}),
    }
    assert carteira.sincronizar_processo(conn, TJMT_A, clientes, ADV) is True
    p = conn.execute("SELECT * FROM processo WHERE numero = ?", (TJMT_A,)).fetchone()
    assert p["instancia"] == "2grau" and p["cliente"] == "MARIA CLIENTE"
    assert p["advogado_atua"] == 1 and p["erro_sincronizacao"] is None
    assert conn.execute("SELECT COUNT(*) FROM andamento WHERE numero = ?",
                        (TJMT_A,)).fetchone()[0] == 1
    assert eventos.listar(conn, TJMT_A)[-1]["dados"] == {"instancia": "2grau", "andamentos": 1}


def test_sincronizar_falha_nas_duas_instancias_grava_o_erro(conn):
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    clientes = {"1grau": ClienteFalso({}),
                "2grau": ClienteFalso({TJMT_A: MNIError("Processo em segredo de justiça")})}
    assert carteira.sincronizar_processo(conn, TJMT_A, clientes, ADV) is False
    p = conn.execute("SELECT erro_sincronizacao FROM processo WHERE numero = ?",
                     (TJMT_A,)).fetchone()
    assert p["erro_sincronizacao"] == "Processo em segredo de justiça"
    assert eventos.listar(conn, TJMT_A)[-1]["tipo"] == "falha_sincronizacao"


class ClienteProibido:
    """2º grau que não pode ser consultado (falha o teste se for)."""

    def consultar_processo(self, numero, incluir_movimentos=False):
        raise AssertionError("2º grau não deveria ser consultado")


@pytest.mark.parametrize("erro", [TimeoutTJMTError("lento"),
                                  ServicoIndisponivelError("fora do ar")])
def test_erro_real_do_pje_nao_cai_para_o_segundo_grau(conn, erro):
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    bons = {"1grau": ClienteFalso({TJMT_A: processo_do_cliente()}), "2grau": ClienteFalso({})}
    assert carteira.sincronizar_processo(conn, TJMT_A, bons, ADV) is True
    clientes = {"1grau": ClienteFalso({TJMT_A: erro}), "2grau": ClienteProibido()}
    assert carteira.sincronizar_processo(conn, TJMT_A, clientes, ADV) is False
    p = conn.execute("SELECT cliente, instancia, erro_sincronizacao FROM processo "
                     "WHERE numero = ?", (TJMT_A,)).fetchone()
    assert str(erro) in p["erro_sincronizacao"]
    assert p["cliente"] == "MARIA CLIENTE" and p["instancia"] == "1grau"
    assert eventos.listar(conn, TJMT_A)[-1]["tipo"] == "falha_sincronizacao"


def test_sincronizar_com_senha_recusada_interrompe(conn):
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    clientes = {"1grau": ClienteFalso({TJMT_A: CredencialInvalidaError("Falha no login")}),
                "2grau": ClienteFalso({})}
    with pytest.raises(CredencialInvalidaError):
        carteira.sincronizar_processo(conn, TJMT_A, clientes, ADV)


def test_publicacao_depois_do_arquivamento_reabre(conn):
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    conn.execute("UPDATE processo SET advogado_atua = 1, data_arquivamento = '20260716' "
                 "WHERE numero = ?", (TJMT_A,))
    carteira.recalcular_situacao(conn)
    assert conn.execute("SELECT arquivado FROM processo").fetchone()[0] == 1
    carteira.gravar_publicacoes(conn, [_pub(8, TJMT_A, data="2026-08-01")])
    carteira.recalcular_situacao(conn)
    assert conn.execute("SELECT arquivado FROM processo").fetchone()[0] == 1  # 16 dias: não reabre
    carteira.gravar_publicacoes(conn, [_pub(9, TJMT_A, data="2026-09-01")])
    carteira.recalcular_situacao(conn)
    p = conn.execute("SELECT arquivado, ultima_publicacao_data FROM processo").fetchone()
    assert p["arquivado"] == 0 and p["ultima_publicacao_data"] == "20260901"


def test_fora_do_tjmt_atua_pela_oab_no_djen(conn):
    carteira.registrar_candidato(conn, TRF1_B, "TRF1", "djen")
    carteira.gravar_publicacoes(conn, [_pub(2, TRF1_B, "TRF1", oab=("54321", "MT"))])
    carteira.recalcular_situacao(conn)
    assert conn.execute("SELECT advogado_atua FROM processo WHERE numero = ?",
                        (TRF1_B,)).fetchone()[0] == 1


def test_candidatos_do_sistema(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / f"{TJMT_C}.json").write_text("{}")
    (tmp_path / "grupos").mkdir()
    (tmp_path / "grupos" / "g.json").write_text(json.dumps({"numeros": [TJMT_A, "123"]}))
    (tmp_path / "grupos" / "g.status.json").write_text(
        json.dumps({"numeros": ["99999999999999999999"]}))
    assert carteira.candidatos_do_sistema(str(tmp_path)) == {
        TJMT_C: "sistema:cache", TJMT_A: "sistema:grupo"}


def test_atualizar_ponta_a_ponta(conn, tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / f"{TJMT_C}.json").write_text("{}")

    def obter_djen(params):
        if "nomeAdvogado" in params:
            return {"status": "success",
                    "items": [item_djen(1, TJMT_A), item_djen(2, TRF1_B, "TRF1")]}
        return {"status": "success",
                "items": [item_djen(2, TRF1_B, "TRF1", oab=("54321", "MT"))]}

    respostas = {TJMT_A: processo_do_cliente(), TJMT_C: processo_de_terceiro()}

    def fabrica(instancia):
        return ClienteFalso(respostas if instancia == "1grau" else {})

    r = carteira.atualizar(conn, ADV, desde=date(2026, 7, 1), ate=date(2026, 7, 31),
                           obter_djen=obter_djen, fabrica=fabrica, raiz=str(tmp_path),
                           pausa_djen=0, progresso=lambda mensagem: None)
    assert r == {"processos": 3, "carteira_ativa": 2, "arquivados": 0,
                 "a_conferir": 1, "falhas_pje": 0, "publicacoes": 2}
    assert [p["numero"] for p in carteira.listar(conn, "a_conferir")] == [TJMT_C]


def test_analisar_advogado_nos_dois_polos():
    p = processo([
        polo("AT", parte("AUTOR UM", advogado("MARIA EXEMPLO DA SILVA", "MT0054321A"))),
        polo("PA", parte("RÉU DOIS", advogado("MARIA EXEMPLO DA SILVA", "MT0054321A")),
             parte("TERCEIRO SEM ADVOGADO")),
    ])
    a = carteira.analisar_processo(processo_para_dict(TJMT_A, p), ADV)
    assert a["advogado_atua"] == 1
    assert a["polo_cliente"] == "Polo Ativo; Polo Passivo"
    assert a["cliente"] == "AUTOR UM; RÉU DOIS"
    assert a["parte_contraria"] == "TERCEIRO SEM ADVOGADO"


def test_interrupcao_nao_esconde_processo_reativado(conn, tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / f"{TJMT_A}.json").write_text("{}")
    (tmp_path / "cache" / f"{TJMT_C}.json").write_text("{}")
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    conn.execute("UPDATE processo SET advogado_atua = 1, data_arquivamento = '20260716', "
                 "arquivado = 1 WHERE numero = ?", (TJMT_A,))
    conn.commit()
    respostas = {
        TJMT_A: processo_do_cliente([movimento("20260716120000", 246, "Arquivado Definitivamente"),
                                     movimento("20260801090000", 893, "Desarquivamento")]),
        TJMT_C: KeyboardInterrupt(),
    }
    with pytest.raises(KeyboardInterrupt):
        carteira.atualizar(conn, ADV, desde=date(2026, 7, 1), ate=date(2026, 7, 31),
                           obter_djen=lambda params: {"status": "success", "items": []},
                           fabrica=lambda instancia: ClienteFalso(
                               respostas if instancia == "1grau" else {}),
                           raiz=str(tmp_path), pausa_djen=0, progresso=lambda m: None)
    assert [p["numero"] for p in carteira.listar(conn, "ativa")] == [TJMT_A]


def test_resposta_malformada_nao_derruba_a_varredura(conn, tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / f"{TJMT_A}.json").write_text("{}")
    (tmp_path / "cache" / f"{TJMT_C}.json").write_text("{}")
    respostas = {TJMT_A: RuntimeError("xml inválido"), TJMT_C: processo_do_cliente()}
    r = carteira.atualizar(conn, ADV, desde=date(2026, 7, 1), ate=date(2026, 7, 31),
                           obter_djen=lambda params: {"status": "success", "items": []},
                           fabrica=lambda instancia: ClienteFalso(
                               respostas if instancia == "1grau" else {}),
                           raiz=str(tmp_path), pausa_djen=0, progresso=lambda m: None)
    assert r["falhas_pje"] == 1
    a = conn.execute("SELECT erro_sincronizacao FROM processo WHERE numero = ?",
                     (TJMT_A,)).fetchone()
    assert "RuntimeError: xml inválido" in a["erro_sincronizacao"]
    c = conn.execute("SELECT cliente FROM processo WHERE numero = ?", (TJMT_C,)).fetchone()
    assert c["cliente"] == "MARIA CLIENTE"


def test_resposta_sem_partes_nao_apaga_dados(conn):
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    bons = {"1grau": ClienteFalso({TJMT_A: processo_do_cliente()}), "2grau": ClienteFalso({})}
    assert carteira.sincronizar_processo(conn, TJMT_A, bons, ADV) is True

    class ClienteVazio:
        def consultar_processo(self, numero, incluir_movimentos=False):
            return SimpleNamespace(processo=None)

    vazios = {"1grau": ClienteVazio(), "2grau": ClienteFalso({})}
    assert carteira.sincronizar_processo(conn, TJMT_A, vazios, ADV) is False
    p = conn.execute("SELECT cliente, erro_sincronizacao FROM processo WHERE numero = ?",
                     (TJMT_A,)).fetchone()
    assert p["cliente"] == "MARIA CLIENTE"
    assert "1grau" in p["erro_sincronizacao"] and "2grau" in p["erro_sincronizacao"]


def test_tribunal_do_numero():
    assert carteira.tribunal_do_numero(TJMT_A) == "TJMT"
    assert carteira.tribunal_do_numero(TRF1_B) == "TRF1"
    assert carteira.tribunal_do_numero("00000030220249990000") == "?"


def test_listar_situacao_desconhecida(conn):
    with pytest.raises(ValueError):
        carteira.listar(conn, "qualquer")


def test_planilha_trava_e_corrompida_ignoradas(tmp_path):
    pasta = tmp_path / "Meus processos"
    pasta.mkdir()
    (pasta / "~$a.xlsx").write_bytes(b"lixo")
    (pasta / "corrompida.xlsx").write_bytes(b"isto nao e um xlsx")
    assert carteira.candidatos_do_sistema(str(tmp_path)) == {}


TJMT_D = "00000050220248110041"
TJMT_E = "00000060220248110041"
NAO_AUTORIZADO = "Usuário não autorizado a acessar o processo"


def _cache(tmp_path, *numeros):
    (tmp_path / "cache").mkdir(exist_ok=True)
    for n in numeros:
        (tmp_path / "cache" / f"{n}.json").write_text("{}")


def _atualizar(conn, tmp_path, respostas, *, avisos_erro_2grau=None, obter_djen=None):
    def fabrica(instancia):
        if instancia == "1grau":
            return ClienteFalso(respostas)
        return ClienteFalso({}, avisos_erro=avisos_erro_2grau)

    return carteira.atualizar(
        conn, ADV, desde=date(2026, 7, 1), ate=date(2026, 7, 31),
        obter_djen=obter_djen or (lambda params: {"status": "success", "items": []}),
        fabrica=fabrica, raiz=str(tmp_path), pausa_djen=0, progresso=lambda m: None)


def test_sonda_de_credencial_falha_antes_do_djen(conn, tmp_path):
    chamadas = []

    def obter_djen(params):
        chamadas.append(params)
        return {"status": "success", "items": []}

    with pytest.raises(CredencialInvalidaError):
        _atualizar(conn, tmp_path, {}, obter_djen=obter_djen,
                   avisos_erro_2grau=CredencialInvalidaError("Falha no login"))
    assert chamadas == []


def test_senha_recusada_num_processo_so_registra_a_falha(conn, tmp_path):
    _cache(tmp_path, TJMT_A, TJMT_C)
    respostas = {TJMT_A: CredencialInvalidaError(NAO_AUTORIZADO),
                 TJMT_C: processo_do_cliente()}
    r = _atualizar(conn, tmp_path, respostas)
    assert r["falhas_pje"] == 1
    a = conn.execute("SELECT erro_sincronizacao FROM processo WHERE numero = ?",
                     (TJMT_A,)).fetchone()
    assert NAO_AUTORIZADO in a["erro_sincronizacao"]
    assert conn.execute("SELECT cliente FROM processo WHERE numero = ?",
                        (TJMT_C,)).fetchone()["cliente"] == "MARIA CLIENTE"


def test_tres_senhas_recusadas_seguidas_interrompem(conn, tmp_path):
    _cache(tmp_path, TJMT_A, TJMT_C, TJMT_D, TJMT_E)
    respostas = {n: CredencialInvalidaError(NAO_AUTORIZADO)
                 for n in (TJMT_A, TJMT_C, TJMT_D, TJMT_E)}
    with pytest.raises(CredencialInvalidaError):
        _atualizar(conn, tmp_path, respostas)
    falhas = conn.execute("SELECT COUNT(*) FROM processo "
                          "WHERE erro_sincronizacao IS NOT NULL").fetchone()[0]
    assert falhas == 3  # a 3ª é registrada e interrompe; a 4ª nem é tentada


def test_sucesso_zera_a_contagem_de_senhas_recusadas(conn, tmp_path):
    _cache(tmp_path, TJMT_A, TJMT_C, TJMT_D, TJMT_E)
    ruim = CredencialInvalidaError(NAO_AUTORIZADO)
    respostas = {TJMT_A: ruim, TJMT_C: ruim, TJMT_D: processo_do_cliente(), TJMT_E: ruim}
    r = _atualizar(conn, tmp_path, respostas)
    assert r["falhas_pje"] == 3


def test_senhas_recusadas_seguidas_ainda_recalculam_a_situacao(conn, tmp_path):
    _cache(tmp_path, TJMT_A, TJMT_C, TJMT_D)
    carteira.registrar_candidato(conn, TJMT_A, "TJMT", "djen")
    carteira.gravar_publicacoes(conn, [_pub(1, TJMT_A, data="2026-07-10")])
    respostas = {n: CredencialInvalidaError(NAO_AUTORIZADO) for n in (TJMT_A, TJMT_C, TJMT_D)}
    with pytest.raises(CredencialInvalidaError):
        _atualizar(conn, tmp_path, respostas)
    assert conn.execute("SELECT ultima_publicacao_data FROM processo WHERE numero = ?",
                        (TJMT_A,)).fetchone()[0] == "20260710"


def _linha(**campos):
    base = {"erro_sincronizacao": None, "advogado_atua": None, "instancia": None,
            "tribunal": "TJMT"}
    return {**base, **campos}


@pytest.mark.parametrize("campos, rotulo", [
    ({"erro_sincronizacao": "segredo", "advogado_atua": 1, "instancia": "1grau"},
     "Sem acesso pelo PJe"),
    ({"advogado_atua": 1, "instancia": None, "tribunal": "TRF1"},
     "Atua (pelo DJEN; situação não conferida)"),
    ({"advogado_atua": 1, "instancia": "1grau"}, "Ativo"),
    ({"advogado_atua": None, "tribunal": "TJMT"}, "A conferir (não sincronizado)"),
    ({"advogado_atua": None, "tribunal": "TRF1"}, "A conferir (fora do TJMT)"),
    ({"advogado_atua": 0, "instancia": "1grau"}, "A conferir (OAB não consta)"),
])
def test_situacao_rotulos(campos, rotulo):
    assert carteira.situacao(_linha(**campos)) == rotulo


def test_listar_ordena_pela_data_mais_recente_entre_andamento_e_publicacao(conn):
    for numero, andamento, publicacao in ((TJMT_A, "20260101", "20260901"),
                                           (TJMT_C, "20260601", None),
                                           (TJMT_D, None, "20260301")):
        carteira.registrar_candidato(conn, numero, "TJMT", "djen")
        conn.execute("UPDATE processo SET advogado_atua = 1, ultimo_andamento_data = ?, "
                     "ultima_publicacao_data = ? WHERE numero = ?",
                     (andamento, publicacao, numero))
    assert [p["numero"] for p in carteira.listar(conn, "ativa")] == [TJMT_A, TJMT_C, TJMT_D]


def test_descartar_tira_da_aba_a_conferir_e_restaurar_devolve(conn):
    carteira.registrar_candidato(conn, TJMT_C, "TJMT", "busca")
    assert [p["numero"] for p in carteira.listar(conn, "a_conferir")] == [TJMT_C]
    assert carteira.descartar(conn, TJMT_C) is True
    assert carteira.descartar(conn, TJMT_C) is False  # já estava descartado
    assert carteira.listar(conn, "a_conferir") == []
    assert [p["numero"] for p in carteira.listar(conn, "descartados")] == [TJMT_C]
    assert carteira.resumo(conn)["a_conferir"] == 0
    assert carteira.restaurar(conn, TJMT_C) is True
    assert carteira.restaurar(conn, TJMT_C) is False
    assert [p["numero"] for p in carteira.listar(conn, "a_conferir")] == [TJMT_C]
    tipos = [e["tipo"] for e in eventos.listar(conn, TJMT_C)]
    assert tipos[-2:] == ["processo_descartado", "processo_restaurado"]


def test_descartado_que_passa_a_atuar_aparece_na_ativa(conn):
    carteira.registrar_candidato(conn, TJMT_C, "TJMT", "busca")
    carteira.descartar(conn, TJMT_C)
    conn.execute("UPDATE processo SET advogado_atua = 1 WHERE numero = ?", (TJMT_C,))
    assert [p["numero"] for p in carteira.listar(conn, "ativa")] == [TJMT_C]
    assert carteira.listar(conn, "descartados") == []


def test_situacao_usa_as_constantes():
    linha = {"erro_sincronizacao": None, "advogado_atua": 1, "instancia": "1grau",
             "tribunal": "TJMT"}
    assert carteira.situacao(linha) == carteira.ATIVO


def test_gravar_publicacoes_novas_devolve_so_as_ineditas(conn):
    p1, p2 = _pub(1, TJMT_A), _pub(2, TJMT_A)
    assert [p["id"] for p in carteira.gravar_publicacoes_novas(conn, [p1])] == [1]
    assert [p["id"] for p in carteira.gravar_publicacoes_novas(conn, [p1, p2])] == [2]
    assert carteira.gravar_publicacoes(conn, [p1, p2]) == 0


def test_sincronizar_lista_conta_e_interrompe_apos_3_recusas(conn):
    for n in (TJMT_A, TJMT_C):
        carteira.registrar_candidato(conn, n, "TJMT", "djen")
    clientes = {"1grau": ClienteFalso({TJMT_A: processo_do_cliente(),
                                       TJMT_C: MNIError("PJe lento")}),
                "2grau": ClienteFalso({})}
    assert carteira.sincronizar_lista(conn, [TJMT_A, TJMT_C], clientes, ADV,
                                      progresso=lambda m: None) == (1, 1)
    recusa = CredencialInvalidaError("não autorizado")
    numeros = [f"{i:07d}0220248110041" for i in range(1, 4)]
    for n in numeros:
        carteira.registrar_candidato(conn, n, "TJMT", "djen")
    clientes = {"1grau": ClienteFalso({n: recusa for n in numeros}), "2grau": ClienteFalso({})}
    with pytest.raises(CredencialInvalidaError):
        carteira.sincronizar_lista(conn, numeros, clientes, ADV, progresso=lambda m: None)


# --- "Este processo é seu?" (confirmação do advogado) e partes da publicação ---------

def _abas(conn):
    return {aba: [p["numero"] for p in carteira.listar(conn, aba)]
            for aba in ("ativa", "a_conferir", "descartados")}


def test_confirmar_poe_na_ativa_e_tira_de_a_conferir(conn):
    carteira.registrar_candidato(conn, TRF1_B, "TRF1", "djen")
    assert _abas(conn) == {"ativa": [], "a_conferir": [TRF1_B], "descartados": []}
    assert carteira.confirmar(conn, TRF1_B) is True
    assert carteira.confirmar(conn, TRF1_B) is False  # já confirmado
    assert _abas(conn) == {"ativa": [TRF1_B], "a_conferir": [], "descartados": []}
    assert carteira.resumo(conn)["carteira_ativa"] == 1
    linha = conn.execute("SELECT * FROM processo WHERE numero = ?", (TRF1_B,)).fetchone()
    assert linha["confirmado_em"] and carteira.situacao(linha) == carteira.ATUA_PELO_DJEN
    assert eventos.listar(conn, TRF1_B)[-1]["tipo"] == "processo_confirmado"


def test_confirmar_desfaz_descarte_e_descartar_desfaz_confirmacao(conn):
    carteira.registrar_candidato(conn, TRF1_B, "TRF1", "djen")
    carteira.descartar(conn, TRF1_B)
    assert carteira.confirmar(conn, TRF1_B) is True
    assert _abas(conn)["ativa"] == [TRF1_B]
    linha = conn.execute("SELECT * FROM processo WHERE numero = ?", (TRF1_B,)).fetchone()
    assert linha["descartado_em"] is None
    assert carteira.descartar(conn, TRF1_B) is True  # a decisão mais recente vale
    linha = conn.execute("SELECT * FROM processo WHERE numero = ?", (TRF1_B,)).fetchone()
    assert linha["confirmado_em"] is None and linha["descartado_em"]
    assert _abas(conn) == {"ativa": [], "a_conferir": [], "descartados": [TRF1_B]}


def test_confirmado_arquivado_sai_da_ativa(conn):
    carteira.registrar_candidato(conn, TRF1_B, "TRF1", "djen")
    carteira.confirmar(conn, TRF1_B)
    conn.execute("UPDATE processo SET arquivado = 1 WHERE numero = ?", (TRF1_B,))
    assert _abas(conn) == {"ativa": [], "a_conferir": [], "descartados": []}


@pytest.mark.parametrize("campos, rotulo", [
    ({"advogado_atua": 1, "instancia": "1grau"}, "Ativo"),
    ({"advogado_atua": 0, "instancia": "1grau"}, "Atua (pelo DJEN; situação não conferida)"),
    ({"advogado_atua": None, "instancia": None, "tribunal": "TRF1"},
     "Atua (pelo DJEN; situação não conferida)"),
    ({"advogado_atua": 1, "instancia": None, "tribunal": "TRF1"},
     "Atua (pelo DJEN; situação não conferida)"),
    ({"erro_sincronizacao": "segredo", "advogado_atua": 0}, "Sem acesso pelo PJe"),
])
def test_situacao_do_confirmado(campos, rotulo):
    assert carteira.situacao(_linha(confirmado_em="2026-10-07T10:00:00-04:00", **campos)) == rotulo


def test_sincronizacao_nao_desfaz_a_confirmacao(conn):
    carteira.registrar_candidato(conn, TJMT_C, "TJMT", "busca")
    carteira.confirmar(conn, TJMT_C)
    clientes = {"1grau": ClienteFalso({TJMT_C: processo_de_terceiro()}), "2grau": ClienteFalso({})}
    assert carteira.sincronizar_processo(conn, TJMT_C, clientes, ADV) is True
    linha = conn.execute("SELECT * FROM processo WHERE numero = ?", (TJMT_C,)).fetchone()
    assert linha["advogado_atua"] == 0 and linha["confirmado_em"]
    assert _abas(conn)["ativa"] == [TJMT_C]
    assert carteira.situacao(linha) == carteira.ATUA_PELO_DJEN


def test_gravar_publicacoes_guarda_as_partes_e_completa_as_antigas(conn):
    p = _pub(1, TRF1_B, "TRF1")
    p["partes"] = [{"nome": "JOANA FICTÍCIA", "polo": "ativo"}]
    carteira.gravar_publicacoes_novas(conn, [p])
    assert json.loads(conn.execute("SELECT partes_json FROM publicacao WHERE id = 1"
                                   ).fetchone()[0]) == p["partes"]
    # Publicação gravada antes da migração 6 (sem partes): a varredura seguinte completa.
    antiga = _pub(2, TRF1_B, "TRF1")
    antiga["partes"] = []
    carteira.gravar_publicacoes_novas(conn, [antiga])
    conn.execute("UPDATE publicacao SET texto = 'original' WHERE id = 2")
    antiga["partes"] = [{"nome": "UNIÃO FEDERAL", "polo": "passivo"}]
    antiga["texto"] = "outro"
    assert carteira.gravar_publicacoes_novas(conn, [antiga]) == []
    linha = conn.execute("SELECT texto, partes_json FROM publicacao WHERE id = 2").fetchone()
    assert linha["texto"] == "original"  # o resto não é regravado
    assert json.loads(linha["partes_json"]) == antiga["partes"]
    # Partes já gravadas não são trocadas.
    antiga["partes"] = [{"nome": "OUTRO", "polo": ""}]
    carteira.gravar_publicacoes_novas(conn, [antiga])
    assert "UNIÃO FEDERAL" in conn.execute("SELECT partes_json FROM publicacao WHERE id = 2"
                                           ).fetchone()[0]
