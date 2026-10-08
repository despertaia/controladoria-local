from datetime import date
from types import SimpleNamespace as NS

import pytest

from captura.mni_client import CredencialInvalidaError, MNIError, ProcessoNaoEncontradoError
from nucleo import banco, carteira, eventos, quadro, varredura
from nucleo.djen import DJENError
from tests.fixtures_djen import item_djen
from tests.fixtures_mni import processo_do_cliente

ADV = carteira.Advogado("MARIA EXEMPLO DA SILVA", "54321", "MT")
HOJE = date(2026, 10, 6)
A = "00000010220248110041"
B = "00000020220248110041"


class PJeFalso:
    def __init__(self, respostas=None, avisos=(), avisos_erro=None):
        self.respostas = respostas or {}
        self.avisos = list(avisos)
        self.avisos_erro = avisos_erro
        self.consultas = []

    def consultar_avisos_pendentes(self):
        if self.avisos_erro:
            raise self.avisos_erro
        return self.avisos

    def consultar_processo(self, numero, incluir_movimentos=False):
        self.consultas.append(numero)
        r = self.respostas.get(numero)
        if isinstance(r, BaseException):
            raise r
        if r is None:
            raise ProcessoNaoEncontradoError("Processo não encontrado.")
        return NS(processo=r)

    def __getattr__(self, nome):  # trava: nenhuma operação que registra ciência
        raise AssertionError(f"operação proibida na varredura: {nome}")


def _aviso(id_, numero):
    return NS(idAviso=id_, tipoComunicacao="INT", dataDisponibilizacao="20261005100000",
              processo=NS(numero=numero, orgaoJulgador=NS(nomeOrgao="1ª VARA")),
              destinatario=None)


def _djen(*itens):
    def obter(params):
        return {"status": "success", "items": list(itens) if "nomeAdvogado" in params else []}
    return obter


_pub = {"tribunal": "TJMT", "tipo": "Intimação", "orgao": "1ª VARA", "classe": "",
        "texto": "Intime-se.", "link": "", "oab_confirmada": False}


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _rodar(conn, pje1, pje2=None, obter=None):
    pje2 = pje2 or PJeFalso()
    return varredura.executar(conn, ADV, hoje=HOJE,
                              fabrica=lambda inst: pje1 if inst == "1grau" else pje2,
                              obter_djen=obter or _djen(), pausa_djen=0)


def test_varredura_completa_gera_cartoes_e_registra(conn):
    pje1 = PJeFalso({A: processo_do_cliente()}, avisos=[_aviso("9", B)])
    r = _rodar(conn, pje1, obter=_djen(item_djen(1, A, data="2026-10-05"),
                                      item_djen(2, A, data="2026-01-10")))
    assert r["publicacoes_novas"] == 2 and r["avisos_novos"] == 1
    assert r["djen_ok"] and r["pje_ok"] and r["erros"] == []
    assert r["cartoes_novos"] == 2  # A (pub recente; a antiga fica fora) e B (intimação)
    itens = conn.execute("SELECT ref FROM demanda_item ORDER BY ref").fetchall()
    assert [i[0] for i in itens] == ["1", "1grau:9"]
    assert conn.execute("SELECT fontes FROM processo WHERE numero = ?", (B,)).fetchone()[0] == '["aviso"]'
    tipos = [e["tipo"] for e in eventos.listar(conn) if e["tipo"].startswith("varredura")]
    assert tipos == ["varredura_iniciada", "varredura_concluida"]


def test_segunda_varredura_nao_duplica(conn):
    pje1 = PJeFalso({A: processo_do_cliente()}, avisos=[_aviso("9", B)])
    obter = _djen(item_djen(1, A, data="2026-10-05"))
    _rodar(conn, pje1, obter=obter)
    r = _rodar(conn, pje1, obter=obter)
    assert r["publicacoes_novas"] == 0 and r["avisos_novos"] == 0 and r["cartoes_novos"] == 0
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 2


def test_falha_do_djen_nao_derruba_o_resto(conn):
    def obter(params):
        raise DJENError("DJEN respondeu 503")
    pje1 = PJeFalso({A: processo_do_cliente()}, avisos=[_aviso("9", A)])
    carteira.registrar_candidato(conn, A, "TJMT", "djen")
    r = _rodar(conn, pje1, obter=obter)
    assert not r["djen_ok"] and r["pje_ok"]
    assert r["erros"] == ["DJEN: DJEN respondeu 503"]
    assert r["cartoes_novos"] == 1


def test_senha_recusada_nas_intimacoes_pula_o_pje(conn):
    recusa = CredencialInvalidaError("loginFailed")
    pje1 = PJeFalso({A: processo_do_cliente()}, avisos_erro=recusa)
    carteira.registrar_candidato(conn, A, "TJMT", "djen")
    r = _rodar(conn, pje1, pje2=PJeFalso(avisos_erro=recusa))
    assert not r["pje_ok"] and pje1.consultas == []
    assert "PJe (1º grau): senha recusada" in r["erros"]


def test_pje_lento_nas_intimacoes_nao_encerra_as_pendentes(conn):
    _rodar(conn, PJeFalso(avisos=[_aviso("9", A)]))
    _rodar(conn, PJeFalso(avisos_erro=MNIError("tempo esgotado")))
    assert conn.execute("SELECT pendente FROM aviso").fetchone()[0] == 1


def test_sincroniza_ativos_e_novos_do_tjmt_mas_nao_arquivados(conn):
    for n in (A, B):
        carteira.registrar_candidato(conn, n, "TJMT", "djen")
    conn.execute("UPDATE processo SET arquivado = 1, advogado_atua = 1 WHERE numero = ?", (B,))
    pje1 = PJeFalso({A: processo_do_cliente()})
    _rodar(conn, pje1)
    assert pje1.consultas == [A]


def test_desde_para_djen(conn):
    assert varredura.desde_para_djen(conn, HOJE) == date(2026, 9, 21)
    with conn:
        eventos.registrar(conn, "varredura_concluida", None, {"djen_ok": False},
                          quando="2026-10-05T12:00:00-04:00")
    assert varredura.desde_para_djen(conn, HOJE) == date(2026, 9, 21)
    with conn:
        eventos.registrar(conn, "varredura_concluida", None, {"djen_ok": True},
                          quando="2026-10-04T06:00:00-04:00")
    assert varredura.desde_para_djen(conn, HOJE) == date(2026, 10, 2)


def test_varredura_interrompida_antes_dos_cartoes_nao_perde_cartoes(conn, monkeypatch):
    pje1 = PJeFalso({A: processo_do_cliente()}, avisos=[_aviso("9", B)])
    obter = _djen(item_djen(1, A, data="2026-10-05"))
    _rodar(conn, PJeFalso())  # quadro já ligado: a primeira ligada não salva o caso
    original = carteira.recalcular_situacao

    def quebra(_conn):
        raise RuntimeError("reiniciado no meio")
    monkeypatch.setattr(carteira, "recalcular_situacao", quebra)
    with pytest.raises(RuntimeError):
        _rodar(conn, pje1, obter=obter)
    assert conn.execute("SELECT COUNT(*) FROM publicacao").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM aviso").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 0

    monkeypatch.setattr(carteira, "recalcular_situacao", original)
    r = _rodar(conn, pje1, obter=obter)
    assert r["publicacoes_novas"] == 0 and r["avisos_novos"] == 0
    assert r["cartoes_novos"] == 2
    itens = conn.execute("SELECT tipo, ref FROM demanda_item ORDER BY ref").fetchall()
    assert [tuple(i) for i in itens] == [("publicacao", "1"), ("aviso", "1grau:9")]


def test_publicacao_gravada_por_fora_vira_cartao_na_varredura_seguinte(conn):
    _rodar(conn, PJeFalso())  # liga o quadro (sem nada)
    carteira.registrar_candidato(conn, A, "TJMT", "djen")
    with conn:  # como faz carteira.py atualizar: grava publicações sem cartão
        carteira.gravar_publicacoes_novas(conn, [
            {**_pub, "id": 7, "numero": A, "data_disponibilizacao": "2026-10-03"},
            {**_pub, "id": 8, "numero": A, "data_disponibilizacao": "2026-08-01"}])
    r = _rodar(conn, PJeFalso({A: processo_do_cliente()}))
    assert r["cartoes_novos"] == 1
    refs = [x[0] for x in conn.execute("SELECT ref FROM demanda_item")]
    assert refs == ["7"]  # a antiga (fora da janela) fica sem cartão


def test_intimacao_ilegivel_vira_erro_e_nao_encerra_pendentes(conn):
    _rodar(conn, PJeFalso(avisos=[_aviso("9", A)]))
    ilegivel = NS(idAviso="", tipoComunicacao="INT", dataDisponibilizacao="", processo=None)
    r = _rodar(conn, PJeFalso(avisos=[ilegivel, _aviso("10", B)]))
    assert "PJe (1º grau): 1 intimação(ões) ilegível(is)" in r["erros"]
    assert r["avisos_novos"] == 1  # a legível entra normalmente
    pend = dict(conn.execute("SELECT id, pendente FROM aviso").fetchall())
    assert pend == {"9": 1, "10": 1}  # a "9" sumiu da lista, mas a lista veio incompleta


def test_lista_sem_ilegivel_continua_encerrando(conn):
    _rodar(conn, PJeFalso(avisos=[_aviso("9", A)]))
    r = _rodar(conn, PJeFalso(avisos=[]))
    assert r["erros"] == []
    assert conn.execute("SELECT pendente FROM aviso").fetchone()[0] == 0


C = "00000030220244013600"  # TRF1: só o DJEN acompanha


def _djen_por_numero(por_numero, pelo_advogado=(), falha=()):
    """DJEN falso: itens pelo nome/OAB do advogado e por número de processo; um
    número em `falha` faz o DJEN responder com erro. Guarda as chamadas."""
    chamadas = []

    def obter(params):
        chamadas.append(params)
        if "numeroProcesso" in params:
            if params["numeroProcesso"] in falha:
                raise DJENError("DJEN respondeu 503")
            return {"status": "success", "items": list(por_numero.get(params["numeroProcesso"], []))}
        return {"status": "success", "items": list(pelo_advogado) if "nomeAdvogado" in params else []}
    obter.chamadas = chamadas
    return obter


def _confirmado(conn, numero, tribunal="TJMT", quando=None):
    with conn:
        carteira.registrar_candidato(conn, numero, tribunal, "manual")
        conn.execute("UPDATE processo SET confirmado_em = ? WHERE numero = ?",
                     (quando or eventos.agora(), numero))


def test_confirmados_sao_consultados_no_djen_pelo_numero_e_viram_cartao(conn):
    _rodar(conn, PJeFalso())  # quadro já ligado
    _confirmado(conn, C, "TRF1")
    obter = _djen_por_numero({C: [item_djen(5, C, "TRF1", data="2026-10-05")]},
                             pelo_advogado=[item_djen(1, A, data="2026-10-05")])
    r = _rodar(conn, PJeFalso(), obter=obter)
    assert r["publicacoes_novas"] == 2 and r["djen_ok"] and r["erros"] == []
    por_numero = [c for c in obter.chamadas if "numeroProcesso" in c]
    assert [c["numeroProcesso"] for c in por_numero] == [C]
    # recém-confirmado: a janela inteira do quadro, não só desde a última varredura
    assert por_numero[0]["dataDisponibilizacaoInicio"] == "2026-09-21"
    refs = [x[0] for x in conn.execute("SELECT ref FROM demanda_item WHERE tipo = 'publicacao'")]
    assert "5" in refs  # publicação de processo confirmado (sem a OAB) gera cartão
    assert conn.execute("SELECT numero FROM publicacao WHERE id = 5").fetchone()[0] == C


def test_djen_por_numero_nao_conta_de_novo_o_que_veio_pelo_advogado(conn):
    _confirmado(conn, A)
    pub = item_djen(1, A, data="2026-10-05")
    r = _rodar(conn, PJeFalso({A: processo_do_cliente()}),
               obter=_djen_por_numero({A: [pub]}, pelo_advogado=[pub]))
    assert r["publicacoes_novas"] == 1
    assert conn.execute("SELECT COUNT(*) FROM publicacao").fetchone()[0] == 1


def test_djen_por_numero_ignora_nao_confirmados_arquivados_e_descartados(conn):
    for n in (A, B):
        carteira.registrar_candidato(conn, n, "TJMT", "djen")
    _confirmado(conn, C, "TRF1")
    conn.execute("UPDATE processo SET advogado_atua = 1 WHERE numero = ?", (A,))
    with conn:
        conn.execute("UPDATE processo SET confirmado_em = '2026-10-01', arquivado = 1 "
                     "WHERE numero = ?", (B,))
    D = "00000040220244013600"
    _confirmado(conn, D, "TRF1")
    with conn:
        carteira.descartar(conn, D)
    obter = _djen_por_numero({})
    _rodar(conn, PJeFalso(), obter=obter)
    assert [c["numeroProcesso"] for c in obter.chamadas if "numeroProcesso" in c] == [C]


def test_erro_do_djen_num_processo_nao_derruba_a_varredura(conn):
    _rodar(conn, PJeFalso())
    D = "00000040220244013600"
    _confirmado(conn, C, "TRF1")
    _confirmado(conn, D, "TRF1", quando="2026-10-05T08:00:00-04:00")
    obter = _djen_por_numero({D: [item_djen(9, D, "TRF1", data="2026-10-05")]}, falha={C})
    r = _rodar(conn, PJeFalso(), obter=obter)
    assert r["djen_ok"] and r["publicacoes_novas"] == 1 and r["cartoes_novos"] == 1
    assert r["erros"] == ["DJEN (por número): 1 processo(s) sem resposta (DJEN respondeu 503)"]
    assert [x[0] for x in conn.execute("SELECT ref FROM demanda_item")] == ["9"]


def test_djen_por_numero_tem_limite_por_varredura(conn, monkeypatch):
    monkeypatch.setattr(varredura, "LIMITE_POR_NUMERO", 2)
    for i, n in enumerate(("00000040220244013600", "00000050220244013600", C)):
        _confirmado(conn, n, "TRF1", quando=f"2026-10-0{i + 1}T08:00:00-04:00")
    obter = _djen_por_numero({})
    r = _rodar(conn, PJeFalso(), obter=obter)
    consultados = [c["numeroProcesso"] for c in obter.chamadas if "numeroProcesso" in c]
    assert consultados == [C, "00000050220244013600"]  # os confirmados mais recentes
    assert "DJEN (por número): 1 processo(s) além do limite de 2 não consultados" in r["erros"]


def test_confirmado_do_tjmt_entra_na_sincronizacao_do_pje(conn):
    _confirmado(conn, A)
    with conn:
        conn.execute("UPDATE processo SET advogado_atua = 0 WHERE numero = ?", (A,))
    assert varredura._numeros_para_sincronizar(conn) == [A]


def test_sem_senha_do_pje_so_o_djen_e_sem_erro(conn):
    """Instalação local sem senha do PJe: a busca no DJEN segue e não aparece erro."""
    from nucleo.mni_fabrica import CredencialAusenteError

    def fabrica(_inst):
        raise CredencialAusenteError("Credencial do PJe ausente")
    r = varredura.executar(conn, ADV, hoje=HOJE, fabrica=fabrica,
                           obter_djen=_djen(item_djen(1, A, data="2026-10-05")), pausa_djen=0)
    assert r["djen_ok"] and not r["pje_ok"] and r["erros"] == []
    assert r["pje_sem_senha"] == ["TJMT"]
    assert r["publicacoes_novas"] == 1 and r["cartoes_novos"] == 1
