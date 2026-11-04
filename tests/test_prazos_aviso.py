"""Aviso do PJe: sem texto, nunca abre o teor; com publicação no mesmo cartão, vale a data que
vence primeiro (spec, seção 3.5)."""

import json
import pathlib
import re
from datetime import date

import pytest

from nucleo import banco, prazos_banco, quadro

HOJE = date(2026, 11, 6)
LONGE = date(2026, 12, 1)
A = "00000010220248110041"
RAIZ = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    c.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua) "
              "VALUES (?, 'TJMT', '1ª Vara Cível de Cuiabá', 1)", (A,))
    yield c
    c.close()


def _aviso(conn, id_="9", envio="2026-11-04", pendente=1):
    conn.execute("INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, data_disponibilizacao, "
                 "visto_em, pendente) VALUES ('1grau', ?, ?, 'INT', ?, '2026-11-04T10:00:00-04:00', ?)",
                 (id_, A, envio, pendente))


def _publicacao(conn, id_=1, data="2026-11-05"):
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                 "VALUES (?, ?, 'TJMT', ?, 'x')", (id_, A, data))


def _por_origem(conn, tipo):
    return dict(conn.execute("SELECT * FROM prazo WHERE origem_tipo = ? ORDER BY id LIMIT 1",
                             (tipo,)).fetchone())


def test_aviso_vira_a_definir_com_a_data_da_ciencia_sozinha(conn):
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    p = _por_origem(conn, "aviso")
    assert p["estado"] == "a_definir" and p["origem_ref"] == "1grau:9"
    assert "abra no PJe" in p["motivo"] and "14/11/2026" in p["motivo"]
    assert p["fatal"] is None


def test_aviso_sozinho_nao_vai_para_a_fila_do_lex(conn):
    from datetime import datetime
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    assert prazos_banco.proxima_leitura(
        conn, datetime.fromisoformat("2026-11-06T10:00:00-04:00")) is None


def test_teor_do_pje_nunca_e_chamado():
    for pasta in ("nucleo", "ponte_mac"):
        for arquivo in (RAIZ / pasta).glob("*.py"):
            fonte = arquivo.read_text(encoding="utf-8")
            assert not re.search(r"\.(consultarTeorComunicacao|confirmarRecebimento)\s*\(", fonte), arquivo
    for nome in ("prazos.py", "prazos_banco.py", "leitura.py"):
        fonte = (RAIZ / "nucleo" / nome).read_text(encoding="utf-8")
        assert "consultarTeorComunicacao(" not in fonte and "confirmarRecebimento" not in fonte


def _cartao_com_os_dois(conn, envio="2026-10-20"):
    """Aviso enviado em 20/10 (ciência sozinha em 30/10) e publicação de 05/11."""
    _aviso(conn, envio=envio)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 1
    pub = _por_origem(conn, "publicacao")
    prazos_banco.corrigir(conn, pub["id"], ato="Réplica", dias=5, unidade="uteis")
    return prazos_banco.obter(conn, pub["id"])


def test_vale_a_que_vence_primeiro_com_a_outra_ao_lado(conn):
    p = _cartao_com_os_dois(conn)
    # PJe: ciência sozinha em 30/10; 03, 04, 05, 06, 09/11 (02/11 é feriado)
    assert p["fatal"] == "2026-11-09"
    # Diário: publicação 06/11, início 09/11, cinco dias úteis
    assert p["fatal_alternativa"] == "2026-11-13"
    calculo = json.loads(p["calculo"])
    assert any("pacificada" in a for a in calculo["avisos"])
    assert any("mesmo ato" in a for a in calculo["avisos"])
    assert calculo["alternativa"]["fatal"] in ("2026-11-09", "2026-11-13")
    assert p["trabalho"] == "2026-11-09"


def test_o_prazo_do_aviso_fica_coberto_pelo_da_publicacao(conn):
    p = _cartao_com_os_dois(conn)
    assert _por_origem(conn, "aviso")["pareado_com"] == p["id"]
    prazos_banco.descartar(conn, p["id"])
    assert _por_origem(conn, "aviso")["pareado_com"] is None


def test_advogado_pode_escolher_a_maior_e_a_escolha_sobrevive_ao_recalculo(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.escolher_fatal(conn, p["id"], True)
    p = prazos_banco.obter(conn, p["id"])
    assert p["fatal"] == "2026-11-13" and p["fatal_alternativa"] == "2026-11-09"
    assert p["fatal_maior"] == 1 and p["estado"] == "a_conferir"
    prazos_banco.incluir_dia(conn, "2026-12-11", "TJMT", "", "sem_expediente", "X", "Y")
    assert prazos_banco.obter(conn, p["id"])["fatal"] == "2026-11-13"
    prazos_banco.escolher_fatal(conn, p["id"], False)
    assert prazos_banco.obter(conn, p["id"])["fatal"] == "2026-11-09"


def test_escolher_sem_alternativa_e_recusado(conn):
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.escolher_fatal(conn, pid, True)


def test_datas_iguais_nao_mostram_alternativa(conn):
    # Ciência sozinha em 06/11 (envio 27/10) dá o mesmo início do Diário.
    p = _cartao_com_os_dois(conn, envio="2026-10-27")
    assert p["fatal"] == "2026-11-13" and p["fatal_alternativa"] is None


def test_aviso_que_chega_depois_recalcula_e_avisa(conn):
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, pid)
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-09" and p["estado"] == "a_conferir"
    assert "intimação no PJe" in p["alerta"]


def test_ciencia_informada_usa_a_leitura_mais_curta(conn):
    p = _cartao_com_os_dois(conn)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.informar_ciencia(conn, aviso["id"], "2026-10-27", hoje=HOJE)
    p = prazos_banco.obter(conn, p["id"])
    # dia zero 27/10; 28, 29/10, 03, 04 e 05/11 (30/10 e 02/11 não têm expediente)
    assert p["fatal"] == "2026-11-05" and p["fatal_alternativa"] == "2026-11-13"
    aviso = prazos_banco.obter(conn, aviso["id"])
    assert aviso["ciencia"] == "2026-10-27" and "27/10/2026" in aviso["motivo"]


def test_preencher_o_aviso_a_mao_conta_da_ciencia(conn):
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis",
                          ciencia="2026-11-10", hoje=LONGE)
    p = prazos_banco.obter(conn, aviso["id"])
    assert p["estado"] == "a_conferir" and p["fatal"] == "2026-11-17"


def test_ciencia_registrada_no_pje_avisa_para_informar_a_data(conn):
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("UPDATE aviso SET pendente = 0")
    assert prazos_banco.atualizar_avisos(conn) == 1
    assert "já foi registrada" in _por_origem(conn, "aviso")["motivo"]
    assert prazos_banco.atualizar_avisos(conn) == 0


# --- além do roteiro: bordas que a regra «a que vence primeiro» exige --------------------

def test_aviso_preenchido_sem_ciencia_conta_da_ciencia_sozinha_nao_do_envio(conn):
    _aviso(conn, envio="2026-11-04")  # ciência sozinha em 14/11 (sábado)
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis")
    p = prazos_banco.obter(conn, aviso["id"])
    # dia zero 14/11 (a sozinha não anda); 16, 17, 18, 19 e 23/11 (20/11 é feriado)
    assert p["fatal"] == "2026-11-23" and p["estado"] == "a_conferir"
    assert "Ciência sozinha" in json.loads(p["calculo"])["passos"][0]


def test_informar_ciencia_no_aviso_ja_preenchido_refaz_a_conta_e_pede_conferencia(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis",
                          ciencia="2026-11-10", hoje=LONGE)
    prazos_banco.conferir(conn, aviso["id"])
    prazos_banco.informar_ciencia(conn, aviso["id"], "2026-11-05", hoje=HOJE)
    p = prazos_banco.obter(conn, aviso["id"])
    # dia zero 05/11; 06, 09, 10, 11, 12/11
    assert p["fatal"] == "2026-11-12" and p["estado"] == "a_conferir" and p["alerta"]


def test_informar_ciencia_so_vale_para_aviso_aberto(conn):
    _cartao_com_os_dois(conn)
    pub = _por_origem(conn, "publicacao")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.informar_ciencia(conn, pub["id"], "2026-11-04", hoje=HOJE)
    aviso = _por_origem(conn, "aviso")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.informar_ciencia(conn, aviso["id"], "04/11/2026", hoje=HOJE)
    prazos_banco.descartar(conn, aviso["id"])
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.informar_ciencia(conn, aviso["id"], "2026-11-04", hoje=HOJE)


def test_descartar_o_aviso_devolve_a_data_unica_e_a_escolha_da_maior_nao_fica(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.escolher_fatal(conn, p["id"], True)
    prazos_banco.conferir(conn, p["id"])
    prazos_banco.descartar(conn, _por_origem(conn, "aviso")["id"])
    p = prazos_banco.obter(conn, p["id"])
    # sem a outra leitura, volta a conta do Diário e a escolha da maior não sobra para o próximo
    assert p["fatal"] == "2026-11-13" and p["fatal_alternativa"] is None
    assert p["fatal_maior"] == 0
    # a data é a mesma, mas a escolha que o advogado conferiu deixou de valer: pede o clique
    assert p["estado"] == "a_conferir" and "escolha da data maior foi desfeita" in p["alerta"]
    _aviso(conn, id_="10", envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    prazos_banco.parear_cartao(conn, p["demanda_id"])
    p = prazos_banco.obter(conn, p["id"])
    assert p["fatal"] == "2026-11-09" and p["estado"] == "a_conferir"


def test_ciencia_mais_cedo_do_cartao_vale_entre_dois_avisos(conn):
    _aviso(conn, id_="9", envio="2026-10-20")
    _aviso(conn, id_="10", envio="2026-10-27")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pub = _por_origem(conn, "publicacao")
    prazos_banco.corrigir(conn, pub["id"], ato="Réplica", dias=5, unidade="uteis")
    assert prazos_banco.obter(conn, pub["id"])["fatal"] == "2026-11-09"


def test_protocolado_nao_cumpre_o_aviso_a_definir(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.conferir(conn, p["id"])
    d = p["demanda_id"]
    assert prazos_banco.cumprir_do_cartao(conn, d) == 1
    assert _por_origem(conn, "aviso")["estado"] == "a_definir"
    assert prazos_banco.contar_sem_data_do_cartao(conn, d) == 1


def test_processo_descartado_nao_ganha_prazo_de_aviso(conn):
    conn.execute("UPDATE processo SET advogado_atua = 0, descartado_em = '2026-10-01T00:00:00-04:00'")
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    assert conn.execute("SELECT COUNT(*) FROM prazo").fetchone()[0] == 0


def test_a_varredura_dos_cartoes_abertos_alcanca_o_aviso_sem_prazo(conn):
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("DELETE FROM prazo")  # a abertura falhou na hora
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1
    assert _por_origem(conn, "aviso")["estado"] == "a_definir"
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0


def test_a_varredura_alcanca_o_aviso_que_ja_saiu_da_lista_pedindo_o_dia(conn):
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("DELETE FROM prazo")
    conn.execute("UPDATE aviso SET pendente = 0")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 1
    assert "já foi registrada" in _por_origem(conn, "aviso")["motivo"]
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0


def test_a_varredura_continua_sem_prazo_para_processo_descartado(conn):
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("DELETE FROM prazo")
    conn.execute("UPDATE processo SET advogado_atua = 0, descartado_em = '2026-10-01T00:00:00-04:00'")
    assert prazos_banco.criar_dos_cartoes_abertos(conn) == 0


# --- rodada de correção 1: a data errada para mais tarde é o dano ------------------------

def _separar(conn, para="revisao"):
    """O cartão de agora sai da triagem: o próximo item do processo abre outro cartão."""
    conn.execute("UPDATE demanda SET coluna = ? WHERE coluna = 'chegou'", (para,))


def _ev(conn, tipo):
    return conn.execute("SELECT COUNT(*) FROM evento WHERE tipo = ?", (tipo,)).fetchone()[0]


# C1: a ciência informada nunca empurra a data para depois da que acontece sozinha

def test_c1_ciencia_informada_depois_da_sozinha_nao_atrasa_a_data(conn):
    p = _cartao_com_os_dois(conn)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.informar_ciencia(conn, aviso["id"], "2026-11-05", hoje=HOJE)
    p = prazos_banco.obter(conn, p["id"])
    assert p["fatal"] == "2026-11-09" and p["fatal_alternativa"] == "2026-11-13"
    alt = json.loads(p["calculo"])["alternativa"]
    assert any("Ciência informada em 05/11/2026, depois do limite de ciência automática: "
               "a conta parte de 30/10/2026." in a for a in alt["avisos"])
    assert prazos_banco.obter(conn, aviso["id"])["ciencia"] == "2026-11-05"  # o valor fica guardado


def test_c1_aviso_sozinho_com_ciencia_depois_do_limite_conta_do_limite(conn):
    _aviso(conn, envio="2026-11-04")  # sozinha em 14/11
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis",
                          ciencia="2026-11-20", hoje=LONGE)
    p = prazos_banco.obter(conn, aviso["id"])
    # sozinha: dia zero 14/11; 16, 17, 18, 19 e 23/11 (20/11 é feriado). Informada daria 30/11.
    assert p["fatal"] == "2026-11-23"
    avisos = json.loads(p["calculo"])["avisos"]
    assert any("Ciência informada em 20/11/2026, depois do limite de ciência automática: "
               "a conta parte de 14/11/2026." in a for a in avisos)
    assert p["ciencia"] == "2026-11-20"


def test_c1_vale_a_menor_data_fatal_e_nao_a_menor_data_bruta(conn):
    _aviso(conn, envio="2026-10-29")  # sozinha no domingo 08/11
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis",
                          ciencia="2026-11-07", hoje=LONGE)  # sábado: a ciência andaria para segunda
    p = prazos_banco.obter(conn, aviso["id"])
    # informada: zero 09/11, fatal 16/11. Sozinha: zero 08/11 (não anda), fatal 13/11.
    assert p["fatal"] == "2026-11-13"
    assert any("a conta parte de 08/11/2026" in a for a in json.loads(p["calculo"])["avisos"])


def test_c1_ciencia_informada_antes_da_sozinha_vale_quando_da_data_mais_cedo(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis",
                          ciencia="2026-11-05", hoje=LONGE)
    p = prazos_banco.obter(conn, aviso["id"])
    assert p["fatal"] == "2026-11-12"
    assert not any("a conta parte de" in a for a in json.loads(p["calculo"])["avisos"])


# C2: a comparação é do processo, não só do cartão

def test_c2_aviso_em_outro_cartao_dentro_da_janela_vale(conn):
    _aviso(conn, envio="2026-10-22")  # sozinha em 01/11
    quadro.gerar_pendentes(conn, date(2026, 10, 23))
    _separar(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 2
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-09" and p["fatal_alternativa"] == "2026-11-13"
    assert _por_origem(conn, "aviso")["pareado_com"] == pid


def test_c2_publicacao_conferida_acompanha_o_aviso_que_chega_em_outro_cartao(conn):
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, pid)
    _separar(conn)
    _aviso(conn, envio="2026-10-22")
    quadro.gerar_pendentes(conn, HOJE)
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 2
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-09" and p["estado"] == "a_conferir"
    assert "intimação no PJe" in p["alerta"] and p["fatal_alternativa"] == "2026-11-13"


def test_c2_aviso_fora_da_janela_em_outro_cartao_nao_mexe(conn):
    _aviso(conn, envio="2026-09-01")
    quadro.gerar_pendentes(conn, date(2026, 10, 21))
    _separar(conn)
    _publicacao(conn, data="2026-10-20")
    quadro.gerar_pendentes(conn, date(2026, 10, 21))
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-10-28" and p["fatal_alternativa"] is None
    assert _por_origem(conn, "aviso")["pareado_com"] is None


def test_c2_no_mesmo_cartao_nao_ha_janela(conn):
    _aviso(conn, envio="2026-10-01")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 1
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    assert prazos_banco.obter(conn, pid)["fatal"] < "2026-11-01"


def test_c2_cartao_resolvido_conta_e_aviso_descartado_nao(conn):
    _aviso(conn, envio="2026-10-22")
    quadro.gerar_pendentes(conn, date(2026, 10, 23))
    _separar(conn, "resolvida")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-09"
    prazos_banco.descartar(conn, _por_origem(conn, "aviso")["id"])
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-13" and p["fatal_alternativa"] is None
    assert p["estado"] == "a_conferir" and "A conta do cartão mudou" in p["alerta"]


# C3: ciência registrada no PJe, dia desconhecido: a conta parte do envio

def test_c3_ciencia_registrada_conta_do_envio_e_avisa(conn):
    _aviso(conn, envio="2026-10-20")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, pid)
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-09"
    conn.execute("UPDATE aviso SET pendente = 0")
    assert prazos_banco.atualizar_avisos(conn) == 1
    p = prazos_banco.obter(conn, pid)
    # envio 20/10 como ciência mais cedo possível: 21, 22, 23, 26, 27/10
    assert p["fatal"] == "2026-10-27" and p["fatal_alternativa"] == "2026-11-13"
    assert p["estado"] == "a_conferir"
    assert ("A ciência já foi registrada no PJe e o dia não é conhecido: a conta parte do envio "
            "até você informar a data.") in p["alerta"]
    assert _por_origem(conn, "aviso")["pareado_com"] is None  # a linha que pede o dia continua à vista
    antes = _ev(conn, "prazo_recalculado")
    assert prazos_banco.atualizar_avisos(conn) == 0
    assert _ev(conn, "prazo_recalculado") == antes
    assert prazos_banco.obter(conn, pid)["estado"] == "a_conferir"


def test_c3_ao_informar_o_dia_a_conta_volta_a_regra_da_ciencia(conn):
    _aviso(conn, envio="2026-10-20")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    conn.execute("UPDATE aviso SET pendente = 0")
    prazos_banco.atualizar_avisos(conn)
    prazos_banco.informar_ciencia(conn, _por_origem(conn, "aviso")["id"], "2026-10-22", hoje=HOJE)
    # zero 22/10; 23, 26, 27, 28, 29/10
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-10-29"
    assert _por_origem(conn, "aviso")["pareado_com"] == pid


def test_c3_publicacao_que_chega_depois_tambem_conta_do_envio(conn):
    _aviso(conn, envio="2026-10-20", pendente=0)
    quadro.gerar_pendentes(conn, HOJE)  # só o aviso (já fora da lista) não vira cartão
    conn.execute("UPDATE aviso SET pendente = 1")
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("UPDATE aviso SET pendente = 0")
    prazos_banco.atualizar_avisos(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-10-27"


# I1: quem cobre o aviso é recalculado do zero

def test_i1_pareado_com_volta_a_nulo_quando_a_publicacao_deixa_de_cobrir(conn):
    p = _cartao_com_os_dois(conn)
    aviso = _por_origem(conn, "aviso")
    assert aviso["pareado_com"] == p["id"]
    prazos_banco.ler_de_novo(conn, p["id"])
    assert _por_origem(conn, "aviso")["pareado_com"] is None
    prazos_banco.corrigir(conn, p["id"], ato="Réplica", dias=5, unidade="uteis")
    assert _por_origem(conn, "aviso")["pareado_com"] == p["id"]
    prazos_banco.conferir(conn, p["id"])
    prazos_banco.cumprir_do_cartao(conn, p["demanda_id"])
    assert _por_origem(conn, "aviso")["pareado_com"] is None  # o aviso aparece de novo
    prazos_banco.desfazer_do_cartao(conn, p["demanda_id"])
    assert _por_origem(conn, "aviso")["pareado_com"] == p["id"]


def test_i1_sem_prazo_na_leitura_nao_cobre(conn):
    _aviso(conn)
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("UPDATE prazo SET pareado_com = 999 WHERE origem_tipo = 'aviso'")
    _publicacao(conn)
    conn.execute("UPDATE publicacao SET texto = 'Pauta de julgamento publicada para ciência geral.'")
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    assert prazos_banco.aplicar_leitura(conn, pid, '{"sem_prazo": true}') == "sem_prazo"
    assert _por_origem(conn, "aviso")["pareado_com"] is None


# I2: data fatal informada não cobre

def test_i2_publicacao_com_fatal_informada_nao_cobre_o_aviso(conn):
    _aviso(conn, envio="2026-10-20")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", fatal_informada="2026-11-25")
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-25" and p["fatal_alternativa"] is None
    assert _por_origem(conn, "aviso")["pareado_com"] is None


# I3: a escolha da data maior vale para as duas datas em que foi clicada

def _com_a_maior(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.escolher_fatal(conn, p["id"], True)
    prazos_banco.conferir(conn, p["id"])
    return prazos_banco.obter(conn, p["id"])


def test_i3_segundo_aviso_desfaz_a_escolha_e_avisa(conn):
    p = _com_a_maior(conn)
    assert p["fatal"] == "2026-11-13" and p["estado"] == "conferido"
    _aviso(conn, id_="10", envio="2026-10-13")  # sozinha em 23/10
    quadro.gerar_pendentes(conn, HOJE)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal_maior"] == 0 and q["estado"] == "a_conferir"
    assert q["fatal"] == "2026-11-03" and q["fatal_alternativa"] == "2026-11-13"
    assert "escolha da data maior foi desfeita" in q["alerta"]


def test_i3_ciencia_informada_depois_do_limite_mantem_a_escolha(conn):
    p = _com_a_maior(conn)
    prazos_banco.informar_ciencia(conn, _por_origem(conn, "aviso")["id"], "2026-11-05", hoje=HOJE)
    q = prazos_banco.obter(conn, p["id"])
    # as duas datas continuam 09/11 e 13/11: o clique segue valendo
    assert q["fatal"] == "2026-11-13" and q["fatal_maior"] == 1 and q["estado"] == "conferido"


def test_i3_ciencia_mais_cedo_desfaz_a_escolha(conn):
    p = _com_a_maior(conn)
    prazos_banco.informar_ciencia(conn, _por_origem(conn, "aviso")["id"], "2026-10-27", hoje=HOJE)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal_maior"] == 0 and q["fatal"] == "2026-11-05" and q["estado"] == "a_conferir"


def test_i3_corrigir_os_dias_desfaz_a_escolha(conn):
    p = _com_a_maior(conn)
    prazos_banco.corrigir(conn, p["id"], dias=15, unidade="uteis")
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal_maior"] == 0 and q["fatal"] < q["fatal_alternativa"]
    assert "escolha da data maior foi desfeita" in q["alerta"]


def test_i3_fatal_informada_tambem_desfaz_a_escolha(conn):
    p = _com_a_maior(conn)
    prazos_banco.corrigir(conn, p["id"], fatal_informada="2026-11-25")
    assert prazos_banco.obter(conn, p["id"])["fatal_maior"] == 0
    prazos_banco.corrigir(conn, p["id"], fatal_informada="")
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-11-09"  # a escolha antiga não volta sozinha


def test_i3_recalculo_com_as_mesmas_datas_mantem_o_clique(conn):
    p = _com_a_maior(conn)
    prazos_banco.incluir_dia(conn, "2026-12-11", "TJMT", "", "sem_expediente", "X", "Y")
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal_maior"] == 1 and q["fatal"] == "2026-11-13" and q["estado"] == "conferido"


# I4 está nos testes da varredura acima. I5: datas que não existem

def test_i5_ciencia_antes_do_envio_ou_no_futuro_e_recusada(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    for ruim in ("2026-11-03", "2026-11-08", "9999-12-31"):
        with pytest.raises(prazos_banco.PrazoInvalido):
            prazos_banco.informar_ciencia(conn, aviso["id"], ruim, hoje=HOJE)
    assert prazos_banco.obter(conn, aviso["id"])["ciencia"] is None
    prazos_banco.informar_ciencia(conn, aviso["id"], "2026-11-06", hoje=HOJE)  # hoje vale
    prazos_banco.informar_ciencia(conn, aviso["id"], "2026-11-07", hoje=HOJE)  # amanhã também


def test_i5_ciencia_futura_nao_deixa_data_velha_nem_alerta_falso(conn):
    p = _cartao_com_os_dois(conn)
    aviso = _por_origem(conn, "aviso")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.informar_ciencia(conn, aviso["id"], "9999-12-31", hoje=HOJE)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-11-09" and "calendário mudou" not in q["alerta"]


def test_i5_a_data_da_ciencia_tem_padrao_de_hoje_em_cuiaba(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.informar_ciencia(conn, _por_origem(conn, "aviso")["id"], "2999-01-01")


def test_i5_conta_que_nao_sai_avisa_com_texto_que_vale_para_qualquer_causa():
    assert "calendário" not in prazos_banco.ALERTA_SEM_CONTA


# I6: o aviso preenchido à mão que sai da lista

def test_i6_aviso_preenchido_que_sai_da_lista_tem_a_conta_refeita_do_envio_e_pede_o_dia(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, aviso["id"])
    assert prazos_banco.obter(conn, aviso["id"])["fatal"] == "2026-11-23"  # pela sozinha
    conn.execute("UPDATE aviso SET pendente = 0")
    assert prazos_banco.atualizar_avisos(conn) == 1
    p = prazos_banco.obter(conn, aviso["id"])
    # sem dia conhecido: envio 04/11 como ciência mais cedo; 05, 06, 09, 10 e 11/11
    assert p["estado"] == "a_conferir" and p["fatal"] == "2026-11-11"
    assert "o dia não é conhecido" in p["alerta"]
    prazos_banco.conferir(conn, aviso["id"])
    assert prazos_banco.atualizar_avisos(conn) == 0  # depois do clique não volta a pedir
    assert prazos_banco.obter(conn, aviso["id"])["estado"] == "conferido"


def test_i6_aviso_com_data_fatal_informada_fica_com_a_data_e_pede_o_dia(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", fatal_informada="2026-11-25")
    prazos_banco.conferir(conn, aviso["id"])
    conn.execute("UPDATE aviso SET pendente = 0")
    assert prazos_banco.atualizar_avisos(conn) == 1
    p = prazos_banco.obter(conn, aviso["id"])
    assert p["fatal"] == "2026-11-25" and p["estado"] == "a_conferir"
    assert "confirme o dia" in p["alerta"]


# --- mutações que sobreviviam ------------------------------------------------------------

def test_escolher_a_maior_numa_data_conferida_volta_a_conferir(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.conferir(conn, p["id"])
    prazos_banco.escolher_fatal(conn, p["id"], True)
    q = prazos_banco.obter(conn, p["id"])
    assert q["estado"] == "a_conferir" and q["conferido_em"] is None


def test_atualizar_avisos_so_mexe_no_que_saiu_da_lista_sem_ciencia_e_aberto(conn):
    _aviso(conn, id_="1")                       # ainda pendente
    _aviso(conn, id_="2")                       # saiu, com ciência informada
    _aviso(conn, id_="3")                       # saiu, mas o prazo foi descartado
    _aviso(conn, id_="4")                       # saiu, a ser tratado
    quadro.gerar_pendentes(conn, HOJE)
    por_ref = {r["origem_ref"]: r["id"] for r in conn.execute("SELECT * FROM prazo")}
    prazos_banco.informar_ciencia(conn, por_ref["1grau:2"], "2026-11-05", hoje=HOJE)
    prazos_banco.descartar(conn, por_ref["1grau:3"])
    conn.execute("UPDATE aviso SET pendente = 0 WHERE id IN ('2', '3', '4')")
    assert prazos_banco.atualizar_avisos(conn) == 1
    marcado = {i for i, r in ((i, prazos_banco.obter(conn, i)) for i in por_ref.values())
               if "já foi registrada" in r["motivo"]}
    assert marcado == {por_ref["1grau:4"]}


def test_aviso_nunca_ganha_data_alternativa(conn):
    _cartao_com_os_dois(conn)
    _aviso(conn, id_="10", envio="2026-10-13")  # outra intimação, com ciência mais cedo
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis")
    a = prazos_banco.obter(conn, aviso["id"])
    assert a["fatal_alternativa"] is None and "alternativa" not in json.loads(a["calculo"])


def test_prazo_cumprido_nao_e_refeito_quando_chega_aviso(conn):
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, pid)
    prazos_banco.cumprir(conn, pid)
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    q = prazos_banco.obter(conn, pid)
    assert q["estado"] == "cumprido" and q["fatal"] == "2026-11-13" and q["alerta"] == ""


def test_a_leitura_que_chega_depois_do_aviso_faz_a_conta_das_duas_datas(conn):
    _aviso(conn, envio="2026-10-20")
    conn.execute("UPDATE processo SET orgao_julgador = '1ª Vara Cível de Cuiabá'")
    conn.execute("UPDATE publicacao SET texto = ''")
    _publicacao(conn)
    conn.execute("UPDATE publicacao SET texto = ?", (
        "Fica a parte autora intimada para, no prazo de 5 (cinco) dias, apresentar réplica.",))
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    leitura = {"prazos": [{
        "ato": "Réplica", "dias": 5, "unidade": "uteis", "origem_dos_dias": "texto",
        "rito": "civel", "trecho": "no prazo de 5 (cinco) dias, apresentar réplica",
        "fixado_pelo_juiz": False, "ressalvas": [], "natureza": "processual",
        "inicio_no_diario": True, "processo_suspenso": False}]}
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps(leitura)) == "a_conferir"
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-09" and p["fatal_alternativa"] == "2026-11-13"
    assert _por_origem(conn, "aviso")["pareado_com"] == pid


def test_conta_impossivel_de_outro_prazo_nao_trava_a_chegada_do_aviso(conn):
    _publicacao(conn)
    _publicacao(conn, id_=2)
    quadro.gerar_pendentes(conn, HOJE)
    ids = [r["id"] for r in conn.execute("SELECT id FROM prazo ORDER BY id")]
    for pid in ids:
        prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    conn.execute("UPDATE prazo SET dias = 400 WHERE id = ?", (ids[1],))
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)  # não pode levantar
    ruim = prazos_banco.obter(conn, ids[1])
    assert ruim["estado"] == "a_conferir" and ruim["alerta"] == prazos_banco.ALERTA_SEM_CONTA
    assert prazos_banco.obter(conn, ids[0])["fatal"] == "2026-11-09"


def test_textos_de_alerta_de_cada_causa(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.conferir(conn, p["id"])
    aviso = _por_origem(conn, "aviso")
    prazos_banco.informar_ciencia(conn, aviso["id"], "2026-10-27", hoje=HOJE)
    assert prazos_banco.obter(conn, p["id"])["alerta"].startswith(
        "A ciência no PJe foi informada: a data fatal era 09/11/2026 e agora é 05/11/2026")
    prazos_banco.conferir(conn, p["id"])
    prazos_banco.descartar(conn, aviso["id"])
    assert prazos_banco.obter(conn, p["id"])["alerta"].startswith(
        "A conta do cartão mudou: a data fatal era 05/11/2026 e agora é 13/11/2026")


# --- a grade: a data é sempre a menor das contas independentes -------------------------

def _conta_independente(conn, envio, publicacao, informada, sem_data, dias=5):
    from nucleo import prazos, prazos_legais
    cal = prazos_banco.calendario_do_processo(conn, A)
    tacita = prazos.ciencia_tacita(date.fromisoformat(envio), prazos_legais.DIAS_CIENCIA_TACITA)
    fatais = [prazos.contar(date.fromisoformat(publicacao), dias, "uteis", cal).fatal,
              prazos.contar_da_ciencia(tacita, dias, "uteis", cal, tacita=True).fatal]
    if informada:
        fatais.append(prazos.contar_da_ciencia(date.fromisoformat(informada), dias, "uteis",
                                               cal).fatal)
    if sem_data:
        fatais.append(prazos.contar_da_ciencia(date.fromisoformat(envio), dias, "uteis", cal,
                                               tacita=True).fatal)
    return min(fatais).isoformat()


@pytest.mark.parametrize("envio", ["2026-10-20", "2026-10-29", "2026-12-01", "2026-12-12"])
@pytest.mark.parametrize("publicacao", ["2026-11-05", "2026-12-10", "2026-12-18"])
@pytest.mark.parametrize("ciencia", [None, "+2", "+8", "+12", "sem"])
def test_grade_a_data_e_a_menor_das_contas_independentes(envio, publicacao, ciencia):
    from datetime import timedelta
    c = banco.conectar(":memory:")
    try:
        c.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua) "
                  "VALUES (?, 'TJMT', '1ª Vara Cível de Cuiabá', 1)", (A,))
        _aviso(c, envio=envio)
        _publicacao(c, data=publicacao)
        quadro.gerar_pendentes(c, date.fromisoformat(publicacao) + timedelta(days=1))
        pid = _por_origem(c, "publicacao")["id"]
        prazos_banco.corrigir(c, pid, ato="Réplica", dias=5, unidade="uteis")
        informada = None
        if ciencia == "sem":
            c.execute("UPDATE aviso SET pendente = 0")
            prazos_banco.atualizar_avisos(c)
        elif ciencia:
            informada = (date.fromisoformat(envio) + timedelta(days=int(ciencia))).isoformat()
            prazos_banco.informar_ciencia(c, _por_origem(c, "aviso")["id"], informada,
                                          hoje=date(2027, 6, 1))
        p = prazos_banco.obter(c, pid)
        assert p["fatal"] == _conta_independente(c, envio, publicacao, informada, ciencia == "sem")
        assert p["fatal_maior"] == 0
        if p["fatal_alternativa"]:
            assert p["fatal_alternativa"] > p["fatal"]
    finally:
        c.close()


# --- rodada de correção 2 ----------------------------------------------------------------

def _aviso_preenchido(conn, envio, **campos):
    _aviso(conn, envio=envio)
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis", **campos)
    return aviso["id"]


# 1. o teto da ciência sozinha vale para toda forma de começar a conta

def test_r2_inicio_informado_no_aviso_nao_passa_da_ciencia_sozinha(conn):
    aid = _aviso_preenchido(conn, "2026-10-20", inicio="2026-11-20")
    p = prazos_banco.obter(conn, aid)
    # sozinha 30/10: 03, 04, 05, 06 e 09/11. O início informado daria 27/11.
    assert p["fatal"] == "2026-11-09"
    assert any("Início informado em 20/11/2026, depois do limite de ciência automática: "
               "a conta parte de 30/10/2026." in a for a in json.loads(p["calculo"])["avisos"])


def test_r2_ciencia_informada_com_inicio_guardado_tambem_tem_teto(conn):
    aid = _aviso_preenchido(conn, "2026-10-20", inicio="2026-11-20")
    prazos_banco.corrigir(conn, aid, ciencia="2026-11-05", hoje=LONGE)
    p = prazos_banco.obter(conn, aid)
    assert p["fatal"] == "2026-11-09"
    assert any("Ciência informada em 05/11/2026, depois do limite" in a
               for a in json.loads(p["calculo"])["avisos"])


def test_r2_data_fatal_informada_fica_fora_da_conta(conn):
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    prazos_banco.corrigir(conn, aid, ato="Manifestação", fatal_informada="2026-11-25")
    assert prazos_banco.obter(conn, aid)["fatal"] == "2026-11-25"


# 2. aviso preenchido depois de sair da lista: a conta parte do envio na hora

def test_r2_aviso_preenchido_depois_de_sair_da_lista_conta_do_envio_na_hora(conn):
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("UPDATE aviso SET pendente = 0")  # saiu da lista antes de qualquer varredura
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis")
    p = prazos_banco.obter(conn, aviso["id"])
    assert p["fatal"] == "2026-10-27"  # 21, 22, 23, 26 e 27/10
    assert any("o dia não é conhecido" in a for a in json.loads(p["calculo"])["avisos"])


def test_r2_ciencia_informada_mantem_a_data_do_aviso_que_saiu_da_lista(conn):
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("UPDATE aviso SET pendente = 0")
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis",
                          ciencia="2026-10-22", hoje=LONGE)
    assert prazos_banco.obter(conn, aviso["id"])["fatal"] == "2026-10-29"


# 3. ciência ilegível em outra intimação

def test_r2_ciencia_ilegivel_cai_na_automatica_nao_cobre_e_avisa_uma_vez(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.conferir(conn, p["id"])
    conn.execute("UPDATE prazo SET ciencia = 'lixo' WHERE origem_tipo = 'aviso'")
    prazos_banco.parear_processo(conn, A)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-11-09" and q["fatal_alternativa"] == "2026-11-13"  # a automática
    assert q["estado"] == "a_conferir" and "não consegue ler" in q["alerta"]
    assert _por_origem(conn, "aviso")["pareado_com"] is None
    prazos_banco.conferir(conn, p["id"])
    antes = _ev(conn, "prazo_ciencia_ilegivel")
    prazos_banco.parear_processo(conn, A)
    assert _ev(conn, "prazo_ciencia_ilegivel") == antes
    assert prazos_banco.obter(conn, p["id"])["estado"] == "conferido"


# 4. custo: o pareamento é do processo inteiro e roda dentro do movimento do cartão

def _processo_grande(conn, n):
    for i in range(1, n + 1):
        dia = date(2026, 10, 14 + (i % 14)).isoformat()
        _publicacao(conn, id_=i, data=dia)
        _aviso(conn, id_=str(i), envio=dia)
    for j in range(1500):  # o resto da base: outros processos com avisos e prazos
        numero = f"{j + 100:020d}"
        conn.execute("INSERT INTO processo (numero, tribunal) VALUES (?, 'TJMT')", (numero,))
        conn.execute("INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, "
                     "data_disponibilizacao, visto_em, pendente) VALUES ('1grau', ?, ?, 'INT', "
                     "'2026-10-05', 'x', 1)", (f"o{j}", numero))
    quadro.gerar_pendentes(conn, date(2026, 10, 28))


def test_r2_pareamento_de_processo_grande_e_rapido_e_com_poucas_consultas(conn):
    import time
    _processo_grande(conn, 40)
    conn.execute("UPDATE processo SET advogado_atua = 1")
    for r in conn.execute("SELECT id FROM prazo WHERE origem_tipo = 'publicacao' AND numero = ?",
                          (A,)).fetchall():
        conn.execute("UPDATE prazo SET estado = 'a_conferir', dias = 5, unidade = 'uteis', "
                     "ato = 'X', fatal = '2099-01-01', calculo = '{}' WHERE id = ?", (r["id"],))
    consultas = []
    conn.set_trace_callback(consultas.append)
    t = time.time()
    prazos_banco.parear_processo(conn, A)
    gasto = time.time() - t
    conn.set_trace_callback(None)
    assert gasto < 3 and len(consultas) < 40 * 25  # folga larga: o que se evita é o quadrático
    cartao = conn.execute("SELECT DISTINCT demanda_id FROM prazo WHERE numero = ?", (A,)).fetchone()[0]
    t = time.time()
    assert prazos_banco.cumprir_do_cartao(conn, cartao) == 40
    assert prazos_banco.desfazer_do_cartao(conn, cartao) == 40
    assert time.time() - t < 3


def test_r2_cumprir_do_cartao_pareia_uma_vez_so(conn, monkeypatch):
    _cartao_com_os_dois(conn)
    chamadas = []
    original = prazos_banco.parear_processo
    monkeypatch.setattr(prazos_banco, "parear_processo",
                        lambda *a, **k: (chamadas.append(1), original(*a, **k)))
    pub = _por_origem(conn, "publicacao")
    prazos_banco.corrigir(conn, pub["id"], ato="Outro", dias=3, unidade="uteis")
    chamadas.clear()
    prazos_banco.cumprir_do_cartao(conn, pub["demanda_id"])
    assert len(chamadas) == 1


# 5. aviso cumprido que sai da lista

def test_r2_aviso_cumprido_que_sai_da_lista_refaz_a_publicacao(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.conferir(conn, p["id"])
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis")
    prazos_banco.cumprir(conn, aviso["id"])
    conn.execute("UPDATE aviso SET pendente = 0")
    prazos_banco.atualizar_avisos(conn)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-10-27" and q["estado"] == "a_conferir"
    assert prazos_banco.obter(conn, aviso["id"])["estado"] == "cumprido"


# 6 e 8. aviso sem data de envio

@pytest.mark.parametrize("envio", ["", "lixo"])
def test_r2_aviso_sem_data_de_envio_nao_inventa_data(conn, envio):
    _aviso(conn, envio=envio)
    quadro.gerar_pendentes(conn, HOJE)
    p = _por_origem(conn, "aviso")
    assert p["estado"] == "a_definir" and p["disponibilizacao"] is None
    assert p["motivo"] == "O PJe não informou a data de envio: confira e informe a ciência."
    assert "sozinha" not in p["motivo"]
    prazos_banco.informar_ciencia(conn, p["id"], "2020-01-01", hoje=HOJE)  # sem limite de baixo
    assert prazos_banco.obter(conn, p["id"])["ciencia"] == "2020-01-01"


def test_r2_aviso_sem_envio_preenchido_conta_da_ciencia_informada(conn):
    _aviso(conn, envio="")
    quadro.gerar_pendentes(conn, HOJE)
    p = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, p["id"], ato="Manifestação", dias=5, unidade="uteis",
                          ciencia="2026-11-04", hoje=LONGE)
    assert prazos_banco.obter(conn, p["id"])["fatal"] == "2026-11-11"
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, p["id"], ciencia="")  # sem envio nem ciência não há de onde


@pytest.mark.parametrize("valor", [None, "", "lixo"])
def test_r2_prazo_de_aviso_com_envio_nulo_ou_ilegivel_nunca_da_erro_cru(conn, valor):
    p = _cartao_com_os_dois(conn)
    aviso = _por_origem(conn, "aviso")
    conn.execute("UPDATE prazo SET disponibilizacao = ? WHERE id = ?", (valor, aviso["id"]))
    prazos_banco.parear_processo(conn, A)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-11-13" and q["fatal_alternativa"] is None  # nada a comparar
    assert _por_origem(conn, "aviso")["pareado_com"] is None
    prazos_banco.atualizar_avisos(conn)


# 7. a lista vai e volta

def test_r2_aviso_que_sai_volta_e_sai_de_novo_refaz_a_conta(conn):
    _aviso(conn, envio="2026-10-20")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    conn.execute("UPDATE aviso SET pendente = 0")
    prazos_banco.atualizar_avisos(conn)
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-10-27"
    conn.execute("UPDATE aviso SET pendente = 1")
    prazos_banco.parear_processo(conn, A)  # qualquer ação sobre o processo
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-09"
    prazos_banco.conferir(conn, pid)
    conn.execute("UPDATE aviso SET pendente = 0")
    assert prazos_banco.atualizar_avisos(conn) == 0  # a linha já tem a mensagem
    q = prazos_banco.obter(conn, pid)
    assert q["fatal"] == "2026-10-27" and q["estado"] == "a_conferir"


# 9. texto do alerta

def test_r2_data_igual_diz_so_que_a_escolha_foi_desfeita(conn):
    p = _com_a_maior(conn)
    prazos_banco.descartar(conn, _por_origem(conn, "aviso")["id"])
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-11-13"
    assert q["alerta"] == "A sua escolha da data maior foi desfeita: vale a que vence primeiro."
    assert "agora é 13/11/2026" not in q["alerta"]


def test_r2_ciencia_registrada_sobre_a_maior_clicada_mostra_as_duas_coisas(conn):
    p = _com_a_maior(conn)
    conn.execute("UPDATE aviso SET pendente = 0")
    prazos_banco.atualizar_avisos(conn)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-10-27" and q["fatal_maior"] == 0
    assert "o dia não é conhecido" in q["alerta"] and "escolha da data maior" in q["alerta"]


# 10 e 12. bordas

def test_r2_ciencia_de_amanha_vale_e_a_de_depois_de_amanha_nao(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    prazos_banco.informar_ciencia(conn, aid, "2026-11-07", hoje=HOJE)
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.informar_ciencia(conn, aid, "2026-11-08", hoje=HOJE)


def test_r2_corrigir_recusa_ciencia_antes_do_envio_no_aviso(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, aid, ato="X", dias=5, unidade="uteis", ciencia="2026-11-03", hoje=LONGE)
    prazos_banco.corrigir(conn, aid, ato="X", dias=5, unidade="uteis", ciencia="2026-11-04", hoje=LONGE)


def test_r2_motivo_do_aviso_a_definir_diz_o_teto_quando_a_informada_passa_dele(conn):
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    prazos_banco.informar_ciencia(conn, aid, "2026-11-05", hoje=HOJE)
    m = prazos_banco.obter(conn, aid)["motivo"]
    assert ("Ciência informada em 05/11/2026, depois do limite de ciência automática: a conta "
            "parte de 30/10/2026.") in m


@pytest.mark.parametrize("envio,entra", [("2026-10-21", True), ("2026-10-20", False),
                                         ("2026-11-20", True), ("2026-11-21", False)])
def test_r2_janela_de_outro_cartao_quinze_dias_para_cada_lado(conn, envio, entra):
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    _separar(conn)
    _aviso(conn, envio=envio)
    quadro.gerar_pendentes(conn, HOJE)
    assert conn.execute("SELECT COUNT(*) FROM demanda").fetchone()[0] == 2
    q = prazos_banco.obter(conn, pid)
    assert (q["fatal_alternativa"] is not None) is entra


def test_r2_linhas_antigas_sem_pareamento_sao_pareadas_pelo_processo(conn):
    p = _cartao_com_os_dois(conn)
    conn.execute("UPDATE prazo SET pareado_com = NULL, calculo = '{}'")  # como o código antigo
    conn.execute("UPDATE prazo SET fatal = '2026-11-13', fatal_alternativa = NULL "
                 "WHERE origem_tipo = 'publicacao'")
    prazos_banco.parear_processo(conn, A)
    q = prazos_banco.obter(conn, p["id"])
    assert q["fatal"] == "2026-11-09" and q["fatal_alternativa"] == "2026-11-13"
    assert _por_origem(conn, "aviso")["pareado_com"] == p["id"]


def test_r2_banco_de_demonstracao_pareia_sem_erro(monkeypatch, tmp_path):
    from scripts import banco_demo
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "dados" / "chave.key"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    c = banco.conectar(":memory:")
    banco_demo.gerar(c, date(2026, 10, 6))
    for r in c.execute("SELECT DISTINCT numero FROM prazo").fetchall():
        prazos_banco.parear_processo(c, r["numero"])
        prazos_banco.parear_processo(c, r["numero"])
    avisos = c.execute("SELECT COUNT(*) FROM prazo WHERE origem_tipo = 'aviso'").fetchone()[0]
    assert avisos >= 1
    for r in c.execute("SELECT pareado_com FROM prazo WHERE pareado_com IS NOT NULL"):
        assert prazos_banco.obter(c, r["pareado_com"])["origem_tipo"] == "publicacao"
    c.close()


# 11. linha com dado ruim

def test_r2_linha_ruim_nao_vira_a_cada_pareamento(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.conferir(conn, p["id"])
    conn.execute("UPDATE prazo SET dias = 400 WHERE id = ?", (p["id"],))
    prazos_banco.parear_processo(conn, A)
    assert prazos_banco.obter(conn, p["id"])["alerta"] == prazos_banco.ALERTA_SEM_CONTA
    antes = _ev(conn, "prazo_recalculo_falhou")
    prazos_banco.parear_processo(conn, A)
    prazos_banco.parear_processo(conn, A)
    assert _ev(conn, "prazo_recalculo_falhou") == antes


def test_r2_pareamento_sem_mudanca_nao_escreve_de_novo(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.parear_processo(conn, A)
    antes = prazos_banco.obter(conn, p["id"])["atualizado_em"]
    conn.execute("UPDATE prazo SET atualizado_em = 'marca' WHERE id = ?", (p["id"],))
    prazos_banco.parear_processo(conn, A)
    assert prazos_banco.obter(conn, p["id"])["atualizado_em"] == "marca" and antes


# --- rodada de correção 3 ----------------------------------------------------------------

def _dois_avisos_e_publicacao(conn, ciencia_ruim, envio_ruim="2026-10-20", envio_bom="2026-10-10"):
    """Publicação de 05/11 e dois avisos no cartão: um com a ciência que não se conta e outro
    de 10/10 (sozinha em 20/10)."""
    _aviso(conn, id_="9", envio=envio_ruim)
    _aviso(conn, id_="10", envio=envio_bom)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    conn.execute("UPDATE prazo SET ciencia = ? WHERE origem_ref = '1grau:9'", (ciencia_ruim,))
    return pid


@pytest.mark.parametrize("ruim", ["9999-12-31", "lixo"])
def test_r3_um_aviso_ruim_nao_esconde_os_outros(conn, ruim):
    pid = _dois_avisos_e_publicacao(conn, ruim)
    prazos_banco.conferir(conn, pid)
    prazos_banco.parear_processo(conn, A)
    q = prazos_banco.obter(conn, pid)
    # o aviso de 10/10 (sozinha em 20/10): 21, 22, 23, 26 e 27/10
    assert q["fatal"] == "2026-10-27" and q["alerta"] != prazos_banco.ALERTA_SEM_CONTA
    assert "não consegue ler" in q["alerta"] and q["estado"] == "a_conferir"
    ruim_aviso = [a for a in conn.execute("SELECT * FROM prazo WHERE origem_ref = '1grau:9'")][0]
    assert ruim_aviso["pareado_com"] is None


def test_r3_aviso_ruim_sem_envio_e_ignorado_mas_avisa(conn):
    pid = _dois_avisos_e_publicacao(conn, "9999-12-31")
    conn.execute("UPDATE prazo SET disponibilizacao = NULL WHERE origem_ref = '1grau:9'")
    prazos_banco.parear_processo(conn, A)
    q = prazos_banco.obter(conn, pid)
    assert q["fatal"] == "2026-10-27" and "não consegue ler" in q["alerta"]


def test_r3_envio_ausente_com_ciencia_informada_entra_na_janela_de_outro_cartao(conn):
    _aviso(conn, envio="")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    prazos_banco.informar_ciencia(conn, aid, "2026-10-22", hoje=HOJE)
    _separar(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = [r["id"] for r in conn.execute(
        "SELECT id FROM prazo WHERE origem_tipo = 'publicacao'")][0]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    q = prazos_banco.obter(conn, pid)
    # zero 22/10; 23, 26, 27, 28 e 29/10 (a mesma data do mesmo cartão)
    assert q["fatal"] == "2026-10-29" and q["fatal_alternativa"] == "2026-11-13"


def test_r3_envio_ausente_sem_ciencia_nao_entra_em_outro_cartao(conn):
    _aviso(conn, envio="")
    quadro.gerar_pendentes(conn, HOJE)
    _separar(conn)
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = [r["id"] for r in conn.execute(
        "SELECT id FROM prazo WHERE origem_tipo = 'publicacao'")][0]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-13"


def test_r3_a_marca_so_segura_o_alerta_a_conta_do_aviso_sempre_roda(conn):
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    prazos_banco.corrigir(conn, aid, ato="Manifestação", dias=5, unidade="uteis")
    conn.execute("UPDATE aviso SET pendente = 0")
    prazos_banco.atualizar_avisos(conn)
    assert prazos_banco.obter(conn, aid)["fatal"] == "2026-10-27"
    conn.execute("UPDATE aviso SET pendente = 1")
    prazos_banco.incluir_dia(conn, "2026-12-11", "TJMT", "", "sem_expediente", "X", "Y")
    assert prazos_banco.obter(conn, aid)["fatal"] == "2026-11-09"  # voltou à sozinha
    prazos_banco.conferir(conn, aid)
    conn.execute("UPDATE aviso SET pendente = 0")
    assert prazos_banco.atualizar_avisos(conn) == 0  # a linha já tem a mensagem
    p = prazos_banco.obter(conn, aid)
    assert p["fatal"] == "2026-10-27" and p["estado"] == "a_conferir"


def test_r3_inicio_informado_do_aviso_entra_na_comparacao(conn):
    _aviso(conn, envio="2026-10-20")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    aviso = _por_origem(conn, "aviso")
    prazos_banco.corrigir(conn, aviso["id"], ato="Manifestação", dias=5, unidade="uteis",
                          inicio="2026-10-22")
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    q = prazos_banco.obter(conn, pid)
    # o aviso conta de 22/10: 22, 23, 26, 27 e 28/10
    assert q["fatal"] == "2026-10-28" and q["fatal_alternativa"] == "2026-11-13"


def test_r3_aviso_a_conferir_com_fatal_informada_que_sai_da_lista_recebe_o_alerta(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    prazos_banco.corrigir(conn, aid, ato="Manifestação", fatal_informada="2026-11-25")
    conn.execute("UPDATE aviso SET pendente = 0")
    prazos_banco.atualizar_avisos(conn)
    p = prazos_banco.obter(conn, aid)
    assert p["estado"] == "a_conferir" and "confirme o dia" in p["alerta"]


def test_r3_ciencia_ruim_achada_antes_do_corrigir_tambem_avisa_uma_vez(conn):
    _aviso(conn, envio="2026-10-20")
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    conn.execute("UPDATE prazo SET ciencia = 'lixo' WHERE origem_tipo = 'aviso'")
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    q = prazos_banco.obter(conn, pid)
    assert "não consegue ler" in q["alerta"] and _ev(conn, "prazo_ciencia_ilegivel") == 1
    prazos_banco.conferir(conn, pid)
    prazos_banco.parear_processo(conn, A)
    assert _ev(conn, "prazo_ciencia_ilegivel") == 1
    assert prazos_banco.obter(conn, pid)["estado"] == "conferido"


def test_r3_alerta_de_conta_impossivel_some_quando_a_conta_volta(conn):
    p = _cartao_com_os_dois(conn)
    prazos_banco.conferir(conn, p["id"])
    conn.execute("UPDATE prazo SET dias = 400 WHERE id = ?", (p["id"],))
    prazos_banco.parear_processo(conn, A)
    assert prazos_banco.obter(conn, p["id"])["alerta"] == prazos_banco.ALERTA_SEM_CONTA
    conn.execute("UPDATE prazo SET dias = 5 WHERE id = ?", (p["id"],))
    prazos_banco.parear_processo(conn, A)
    q = prazos_banco.obter(conn, p["id"])
    assert q["alerta"] == "" and q["fatal"] == "2026-11-09"


def test_r3_aviso_que_chega_com_o_prazo_cumprido_nao_fala_de_calendario(conn):
    _publicacao(conn)
    quadro.gerar_pendentes(conn, HOJE)
    pid = _por_origem(conn, "publicacao")["id"]
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=5, unidade="uteis")
    prazos_banco.conferir(conn, pid)
    prazos_banco.cumprir(conn, pid)
    _aviso(conn, envio="2026-10-20")
    quadro.gerar_pendentes(conn, HOJE)
    prazos_banco.desfazer_cumprido(conn, pid)
    q = prazos_banco.obter(conn, pid)
    assert q["fatal"] == "2026-11-09" and q["estado"] == "a_conferir"
    assert "calendário" not in q["alerta"] and "09/11/2026" in q["alerta"]


def test_r3_corrigir_recusa_ciencia_do_futuro_no_aviso(conn):
    _aviso(conn, envio="2026-11-04")
    quadro.gerar_pendentes(conn, HOJE)
    aid = _por_origem(conn, "aviso")["id"]
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, aid, ato="X", dias=5, unidade="uteis", ciencia="2030-01-10",
                              hoje=HOJE)
    prazos_banco.corrigir(conn, aid, ato="X", dias=5, unidade="uteis", ciencia="2026-11-07",
                          hoje=HOJE)
    assert prazos_banco.obter(conn, aid)["ciencia"] == "2026-11-07"
