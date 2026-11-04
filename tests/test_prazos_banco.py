"""O prazo no banco: calendário que vem pronto, estados, correção e eventos."""

import json
from datetime import date

import pytest

from nucleo import banco, calendario_semente, eventos, prazos, prazos_banco

NUMERO = "00012345620248110041"


@pytest.fixture
def conn(tmp_path):
    c = banco.conectar(str(tmp_path / "b.db"))
    c.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua, cliente) "
              "VALUES (?, 'TJMT', '1ª Vara Cível de Cuiabá', 1, 'MARIA FICTÍCIA')", (NUMERO,))
    yield c
    c.close()


def _a_conferir(conn, dias=15, unidade="uteis", disp="2026-11-05", ato="Contestação"):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", disp)
    prazos_banco.corrigir(conn, pid, ato=ato, dias=dias, unidade=unidade)
    return pid


def _fatal(conn, tribunal, orgao, disp, dias):
    cal = prazos_banco.carregar_calendario(conn, tribunal, orgao)
    return prazos.contar(disp, dias, prazos.UTEIS, cal).fatal


# --- calendário ------------------------------------------------------------------------

def test_toda_linha_da_semente_tem_fonte_e_tipo_valido():
    linhas = calendario_semente.linhas()
    assert len(linhas) > 100
    for l in linhas:
        assert l["fonte"].strip() and l["descricao"].strip()
        assert l["tipo"] in ("sem_expediente", "reduzido", "suspensao")
        assert l["origem"] in ("lei", "ato")
        assert date.fromisoformat(l["data"]).year in (2026, 2027, 2028)


def test_exemplo_a14_com_o_calendario_que_vem_pronto(conn):
    assert _fatal(conn, "TJMT", "1ª Vara Cível de Cuiabá", date(2026, 11, 5), 15) == date(2026, 11, 30)


@pytest.mark.parametrize("tribunal, orgao, fatal", [
    ("TJMT", "1ª Vara de Cuiabá", date(2026, 6, 8)),
    # 04/06 sai da tabela do TRF1 (ponto facultativo, D6) e do TRF6 (feriado municipal de BH,
    # D6): na dúvida, a data mais cedo. O 05/06 de ambos fica.
    ("TRF1", "1ª Vara Federal", date(2026, 6, 4)),
    ("TRF6", "1ª Vara Federal", date(2026, 6, 4)),
    ("TJMG", "2ª Vara Cível de Belo Horizonte", date(2026, 6, 8)),
    ("TJMG", "Vara Única de Uberlândia", date(2026, 6, 4)),  # só onde for feriado municipal
    ("TJSP", "1ª Vara", date(2026, 6, 4)),  # tribunal sem tabela: só o nacional
])
def test_corpus_christi_por_tribunal(conn, tribunal, orgao, fatal):
    assert _fatal(conn, tribunal, orgao, date(2026, 6, 1), 2) == fatal


@pytest.mark.parametrize("tribunal, fatal", [
    ("TJMT", date(2026, 4, 6)), ("TJMG", date(2026, 4, 7)), ("TRF1", date(2026, 4, 7)),
    ("TRF6", date(2026, 4, 7)), ("TJSP", date(2026, 4, 2)),
])
def test_sexta_da_paixao_por_tribunal(conn, tribunal, fatal):
    assert _fatal(conn, tribunal, "", date(2026, 3, 30), 2) == fatal


def test_28_10_do_tjmt_fora_e_30_10_ponto_facultativo(conn):
    """Na dúvida, a data mais cedo: só o 30/10 (E2/E3) entra, sem o 28/10."""
    assert _fatal(conn, "TJMT", "", date(2026, 10, 26), 15) == date(2026, 11, 19)
    cal = prazos_banco.carregar_calendario(conn, "TJMT", "")
    assert date(2026, 10, 28) not in cal.sem_expediente
    assert "Dia do Servidor" in cal.sem_expediente[date(2026, 10, 30)]


@pytest.mark.parametrize("tribunal", ["TJMT", "TJMG", "TRF1", "TRF6"])
def test_ano_de_2027_avisa_nos_quatro_tribunais(conn, tribunal):
    cal = prazos_banco.carregar_calendario(conn, tribunal, "")
    c = prazos.contar(date(2027, 3, 1), 5, prazos.UTEIS, cal)
    assert f"calendário de 2027 do {tribunal} ainda não cadastrado: confira" in c.avisos


def test_dia_reduzido_vem_pronto(conn):
    assert _fatal(conn, "TJMT", "", date(2026, 2, 11), 2) == date(2026, 2, 19)


def test_virada_2026_2027_e_ano_seguinte_sem_calendario(conn):
    cal = prazos_banco.carregar_calendario(conn, "TJMT", "")
    c = prazos.contar(date(2026, 12, 16), 5, prazos.UTEIS, cal)
    assert c.fatal == date(2027, 1, 26)
    assert "calendário de 2027 do TJMT ainda não cadastrado: confira" in c.avisos
    assert not any("feriados nacionais de 2027" in a for a in c.avisos)
    assert date(2027, 3, 26) not in cal.sem_expediente  # data móvel de 2027 não entra
    assert date(2027, 9, 7) in cal.sem_expediente


def test_comarca_casa_pelo_texto_do_orgao():
    assert prazos_banco.comarca_casa("2ª VARA CÍVEL DE BELO HORIZONTE", "Belo Horizonte")
    assert prazos_banco.comarca_casa("Vara de Várzea Grande", "VARZEA GRANDE")
    assert not prazos_banco.comarca_casa("Vara de Horizonte Alto", "Belo Horizonte")
    assert not prazos_banco.comarca_casa("", "Cuiabá")


@pytest.mark.parametrize("orgao, comarca", [
    ("1ª Vara Cível de Cuiabá", "Cuiabá"),
    ("Comarca de Cuiabá/MT", "Cuiabá"),
    ("Cuiabá, 1ª Vara", "Cuiabá"),
    ("Vara (Cuiabá)", "Cuiabá"),
    ("Vara de São João del-Rei", "São João del-Rei"),
    ("Vara de São João del Rei", "São João del-Rei"),
    ("Juizado Especial Cível e Criminal - Cuiabá", "CUIABA"),
])
def test_comarca_casa_formas_validas(orgao, comarca):
    assert prazos_banco.comarca_casa(orgao, comarca)


@pytest.mark.parametrize("orgao, comarca", [
    ("Vara de Lagoa da Prata", "Prata"),
    ("Vara de Carmo do Rio Claro", "Rio Claro"),
    ("Vara de Horizonte Alto", "Belo Horizonte"),
    ("Vara de Nova Cuiabá", "Cuiabá"),
    ("Vara de Cuiabá Paulista", "Cuiabá"),
    ("Vara de Santa Cuiabá do Sul", "Cuiabá"),
])
def test_comarca_nao_casa_no_meio_de_outro_nome(orgao, comarca):
    assert not prazos_banco.comarca_casa(orgao, comarca)


def test_calendario_do_processo_usa_tribunal_e_orgao(conn):
    conn.execute("INSERT INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem) "
                 "VALUES ('2026-11-10', 'TJMT', 'Cuiabá', 'sem_expediente', 'Feriado municipal', "
                 "'Lei municipal', 'manual')")
    cal = prazos_banco.calendario_do_processo(conn, NUMERO)
    assert cal.tribunal == "TJMT" and cal.comarca == "Cuiabá"
    assert date(2026, 11, 10) in cal.sem_expediente
    assert date(2026, 11, 10) not in prazos_banco.carregar_calendario(
        conn, "TJMT", "Vara de Sinop").sem_expediente


def test_dia_de_outro_tribunal_ou_outra_comarca_nao_vale(conn):
    """O feriado de um tribunal ou de uma comarca não vaza para os outros."""
    conn.execute("INSERT INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem) "
                 "VALUES ('2026-11-10', 'TJMG', 'Belo Horizonte', 'sem_expediente', 'Local', 'x', "
                 "'manual')")
    conn.execute("INSERT INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem) "
                 "VALUES ('2026-11-11', 'TJMG', '', 'sem_expediente', 'Do tribunal', 'x', 'ato')")
    conn.execute("INSERT INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem) "
                 "VALUES ('2026-11-12', 'TJMT', 'Sinop', 'sem_expediente', 'Local', 'x', 'manual')")
    mt = prazos_banco.carregar_calendario(conn, "TJMT", "1ª Vara de Belo Horizonte")
    assert date(2026, 11, 10) not in mt.sem_expediente
    assert date(2026, 11, 11) not in mt.sem_expediente
    assert date(2026, 11, 12) not in mt.sem_expediente
    mg = prazos_banco.carregar_calendario(conn, "TJMG", "1ª Vara de Belo Horizonte")
    assert date(2026, 11, 10) in mg.sem_expediente and date(2026, 11, 11) in mg.sem_expediente
    assert date(2026, 11, 12) not in mg.sem_expediente
    outra = prazos_banco.carregar_calendario(conn, "TJMG", "1ª Vara de Contagem")
    assert date(2026, 11, 10) not in outra.sem_expediente and outra.comarca == ""


def test_calendario_do_prazo_prefere_o_orgao_da_publicacao(conn):
    """O órgão do ato decide a comarca (F7); sem publicação, vale o do processo."""
    conn.execute("INSERT INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem) "
                 "VALUES ('2026-11-10', 'TJMT', 'Sinop', 'sem_expediente', 'Local', 'x', 'manual')")
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, orgao) "
                 "VALUES (5, ?, 'TJMT', '2026-11-05', 'Vara de Sinop')", (NUMERO,))
    da_publicacao = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "5", "2026-11-05")
    sem_publicacao = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "99", "2026-11-05")
    for pid in (da_publicacao, sem_publicacao):
        prazos_banco.corrigir(conn, pid, ato="X", dias=3, unidade="uteis")
    assert prazos_banco.obter(conn, da_publicacao)["fatal"] == "2026-11-12"
    assert prazos_banco.obter(conn, sem_publicacao)["fatal"] == "2026-11-11"


# --- estados ---------------------------------------------------------------------------

def test_abrir_e_idempotente(conn):
    a = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    assert a is not None
    assert prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05") is None
    p = prazos_banco.obter(conn, a)
    assert p["estado"] == "aguardando_lex" and p["fatal"] is None


def test_obter_desconhecido(conn):
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.obter(conn, 999)


def test_preencher_a_mao_conta_do_mesmo_jeito_e_nao_confere_sozinho(conn):
    p = prazos_banco.obter(conn, _a_conferir(conn))
    assert p["estado"] == "a_conferir" and p["conferido_em"] is None
    assert p["publicacao"] == "2026-11-06" and p["inicio"] == "2026-11-09"
    assert p["fatal"] == "2026-11-30" and p["trabalho"] == "2026-11-30"
    assert p["versao_calendario"] == 1
    calculo = json.loads(p["calculo"])
    assert any("dia 15" in x for x in calculo["passos"])
    assert any("municipais" in x for x in calculo["avisos"])


def test_conferir_precisa_de_data(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.conferir(conn, pid)
    assert prazos_banco.obter(conn, pid)["estado"] == "aguardando_lex"


def test_conferir_registra_evento(conn):
    pid = _a_conferir(conn)
    prazos_banco.conferir(conn, pid)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "conferido" and p["conferido_em"]
    e = eventos.listar(conn, NUMERO)[-1]
    assert e["tipo"] == "prazo_conferido" and e["dados"]["prazo"] == pid


def test_corrigir_guarda_quem_corrigiu_o_que_e_volta_a_conferir(conn):
    pid = _a_conferir(conn)
    prazos_banco.conferir(conn, pid)
    prazos_banco.corrigir(conn, pid, dias=5, por="daniel")
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_conferir" and p["fatal"] == "2026-11-13"
    e = eventos.listar(conn, NUMERO)[-1]
    assert e["tipo"] == "prazo_corrigido" and e["dados"]["por"] == "daniel"
    assert e["dados"]["antes"]["dias"] == 15 and e["dados"]["depois"]["dias"] == 5


def test_corrigir_com_data_de_inicio(conn):
    pid = _a_conferir(conn)
    prazos_banco.corrigir(conn, pid, dias=1, inicio="2026-11-14")
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-16"


@pytest.mark.parametrize("campos", [
    {"dias": 0, "unidade": "uteis"}, {"dias": 400, "unidade": "uteis"},
    {"dias": 5, "unidade": "semanas"}, {"dias": 5, "unidade": "horas"},
    {"dias": 5, "unidade": "uteis", "inicio": "ontem"},
    {"fatal_informada": "12/11/2026"},
])
def test_corrigir_recusa_dado_ruim(conn, campos):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, pid, **campos)
    assert prazos_banco.obter(conn, pid)["estado"] == "aguardando_lex"


def test_prazo_em_horas_so_com_data_informada(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, ato="Preparo", unidade="horas",
                          fatal_informada="2026-11-12")
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-12" and p["estado"] == "a_conferir"
    assert "informada" in json.loads(p["calculo"])["passos"][0]


def test_descartar_nao_apaga(conn):
    pid = _a_conferir(conn)
    prazos_banco.descartar(conn, pid)
    assert prazos_banco.obter(conn, pid)["estado"] == "descartado"
    assert eventos.listar(conn, NUMERO)[-1]["tipo"] == "prazo_descartado"
    assert conn.execute("SELECT COUNT(*) FROM prazo").fetchone()[0] == 1


@pytest.mark.parametrize("d, data", [(0, "2026-11-30"), (1, "2026-11-27"), (3, "2026-11-25")])
def test_data_de_trabalho_por_clique(conn, d, data):
    pid = _a_conferir(conn)
    prazos_banco.escolher_trabalho(conn, pid, d=d)
    p = prazos_banco.obter(conn, pid)
    assert p["trabalho"] == data and p["d_escolhido"] == d


def test_outro_dia_nunca_depois_da_fatal(conn):
    pid = _a_conferir(conn)
    prazos_banco.escolher_trabalho(conn, pid, data="2026-11-11")
    p = prazos_banco.obter(conn, pid)
    assert p["trabalho"] == "2026-11-11" and p["d_escolhido"] == prazos_banco.OUTRO_DIA
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.escolher_trabalho(conn, pid, data="2026-12-01")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.escolher_trabalho(conn, pid, d=9)


def test_padrao_do_escritorio_vale_sem_escolha(conn):
    assert prazos_banco.d_padrao(conn) == 0
    prazos_banco.definir_d_padrao(conn, 2)
    assert prazos_banco.obter(conn, _a_conferir(conn))["trabalho"] == "2026-11-26"
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.definir_d_padrao(conn, 7)


def test_dobro_com_um_clique_refaz_a_conta_e_pede_conferencia(conn):
    pid = _a_conferir(conn, dias=2)
    prazos_banco.conferir(conn, pid)
    prazos_banco.alternar_dobro(conn, pid, True)
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-12" and p["dobro"] == 1 and p["estado"] == "a_conferir"
    prazos_banco.alternar_dobro(conn, pid, False)
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-10"


def test_cumprir_e_desfazer_voltam_ao_estado_anterior(conn):
    pid = _a_conferir(conn)
    prazos_banco.conferir(conn, pid)
    prazos_banco.cumprir(conn, pid)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "cumprido" and p["cumprido_em"]
    prazos_banco.desfazer_cumprido(conn, pid)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "conferido" and p["cumprido_em"] is None
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.desfazer_cumprido(conn, pid)


def test_cumprir_do_cartao_e_desfazer_so_mexem_no_que_marcou(conn):
    conn.execute("INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
                 "VALUES (7, ?, 'revisao', '2026-11-05', 'x', 'x')", (NUMERO,))
    a = prazos_banco.abrir(conn, NUMERO, 7, "publicacao", "1", "2026-11-05")
    b = prazos_banco.abrir(conn, NUMERO, 7, "publicacao", "2", "2026-11-05")
    c = prazos_banco.abrir(conn, NUMERO, 7, "publicacao", "3", "2026-11-05")
    prazos_banco.corrigir(conn, a, ato="X", dias=5, unidade="uteis")
    prazos_banco.cumprir(conn, b)  # à mão: o desfazer do cartão não mexe nele
    prazos_banco.descartar(conn, c)
    assert prazos_banco.cumprir_do_cartao(conn, 7) == 1
    assert prazos_banco.obter(conn, a)["estado"] == "cumprido"
    assert prazos_banco.obter(conn, c)["estado"] == "descartado"
    assert prazos_banco.desfazer_do_cartao(conn, 7) == 1
    assert prazos_banco.obter(conn, a)["estado"] == "a_conferir"
    assert prazos_banco.obter(conn, b)["estado"] == "cumprido"


def test_corrigir_conta_fora_do_limite_vira_prazo_invalido_e_nao_muda_nada(conn):
    pid = _a_conferir(conn)
    antes = prazos_banco.obter(conn, pid)
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, pid, dias=200, unidade="uteis", dobro=True)
    assert prazos_banco.obter(conn, pid) == antes


def test_alternar_dobro_fora_do_limite_vira_prazo_invalido(conn):
    pid = _a_conferir(conn, dias=200)
    antes = prazos_banco.obter(conn, pid)
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.alternar_dobro(conn, pid, True)
    assert prazos_banco.obter(conn, pid) == antes


def test_conta_continua_levantando_prazo_fora_da_conta(conn):
    """A leitura (tarefa seguinte) conta com pegar PrazoForaDaConta de `_conta`."""
    p = {**prazos_banco.obter(conn, _a_conferir(conn, dias=200)), "dobro": 1}
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos_banco._conta(conn, p)


def test_corrigir_nao_ignora_calado_dias_com_fatal_informada(conn):
    pid = _a_conferir(conn)
    prazos_banco.corrigir(conn, pid, fatal_informada="2026-11-20")
    antes, n_eventos = prazos_banco.obter(conn, pid), len(eventos.listar(conn, NUMERO))
    with pytest.raises(prazos_banco.PrazoInvalido, match="data fatal"):
        prazos_banco.corrigir(conn, pid, dias=5)
    assert prazos_banco.obter(conn, pid) == antes
    assert len(eventos.listar(conn, NUMERO)) == n_eventos
    # limpando a fatal informada na mesma chamada, a correção vale
    prazos_banco.corrigir(conn, pid, dias=5, fatal_informada="")
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-13"


def test_corrigir_nao_ignora_calado_inicio_com_ciencia(conn):
    pid = _a_conferir(conn)
    prazos_banco.corrigir(conn, pid, ciencia="2026-11-09", hoje=date(2026, 12, 1))
    antes = prazos_banco.obter(conn, pid)
    with pytest.raises(prazos_banco.PrazoInvalido, match="ciência"):
        prazos_banco.corrigir(conn, pid, inicio="2026-11-14")
    assert prazos_banco.obter(conn, pid) == antes
    prazos_banco.corrigir(conn, pid, inicio="2026-11-14", ciencia="")
    assert prazos_banco.obter(conn, pid)["fatal"] is not None


def test_corrigir_recusa_dias_e_fatal_informada_na_mesma_chamada(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, pid, dias=5, fatal_informada="2026-11-20")


@pytest.mark.parametrize("estado", ["conferido", "cumprido", "inventado"])
def test_abrir_recusa_estado_que_nao_se_abre(conn, estado):
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05", estado=estado)
    assert conn.execute("SELECT COUNT(*) FROM prazo").fetchone()[0] == 0


@pytest.mark.parametrize("estado", ["aguardando_lex", "a_conferir", "a_definir", "sem_prazo"])
def test_abrir_aceita_estados_de_entrada(conn, estado):
    assert prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05",
                              estado=estado) is not None


def test_definir_d_padrao_registra_evento(conn):
    prazos_banco.definir_d_padrao(conn, 2, por="daniel")
    e = eventos.listar(conn)[-1]
    assert e["tipo"] == "prazo_d_padrao" and e["dados"] == {"d": 2, "por": "daniel"}


def test_descartar_limpa_a_conferencia(conn):
    pid = _a_conferir(conn)
    prazos_banco.conferir(conn, pid)
    prazos_banco.descartar(conn, pid)
    assert prazos_banco.obter(conn, pid)["conferido_em"] is None


def test_corrigir_pelo_codigo_da_lista_fechada(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    p = prazos_banco.obter(conn, pid)
    assert (p["ato"], p["dias"], p["unidade"], p["codigo_ato"]) == (
        "Contestação", 15, "uteis", "contestacao")
    assert p["origem_dos_dias"] == "lei" and p["fatal"] == "2026-11-30"
    assert "Termo inicial varia" in json.loads(p["ressalvas"])[0]


def test_codigo_vence_o_que_o_formulario_manda_preenchido(conn):
    pid = _a_conferir(conn)
    prazos_banco.corrigir(conn, pid, codigo="ed_cpc", ato="Contestação", dias=15, unidade="corridos")
    p = prazos_banco.obter(conn, pid)
    assert (p["ato"], p["dias"], p["unidade"]) == ("Embargos de declaração", 5, "uteis")


def test_ato_escrito_a_mao_deixa_de_ser_o_da_lista(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    # o formulário devolve os mesmos valores da lista: continua sendo o ato da lista
    prazos_banco.corrigir(conn, pid, ato="Contestação", dias=15, unidade="uteis")
    p = prazos_banco.obter(conn, pid)
    assert p["codigo_ato"] == "contestacao" and p["origem_dos_dias"] == "lei"
    prazos_banco.corrigir(conn, pid, ato="Manifestação")
    p = prazos_banco.obter(conn, pid)
    assert p["codigo_ato"] == "" and p["origem_dos_dias"] == "manual"
    assert json.loads(p["ressalvas"]) == []


def test_dias_trocados_a_mao_desfazem_a_origem_na_lei(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    prazos_banco.corrigir(conn, pid, dias=20)
    p = prazos_banco.obter(conn, pid)
    assert p["codigo_ato"] == "" and p["origem_dos_dias"] == "manual"
    assert not any("Termo inicial varia" in r for r in json.loads(p["ressalvas"]))
    assert p["dias"] == 20


def test_unidade_trocada_a_mao_desfaz_a_origem_na_lei_e_guarda_outras_ressalvas(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    lista = json.loads(prazos_banco.obter(conn, pid)["ressalvas"]) + ["Indício de Fazenda: conferir dobro"]
    conn.execute("UPDATE prazo SET ressalvas = ? WHERE id = ?",
                 (json.dumps(lista, ensure_ascii=False), pid))
    prazos_banco.corrigir(conn, pid, unidade="corridos")
    p = prazos_banco.obter(conn, pid)
    assert (p["codigo_ato"], p["origem_dos_dias"]) == ("", "manual")
    assert json.loads(p["ressalvas"]) == ["Indício de Fazenda: conferir dobro"]


def test_codigo_com_data_fatal_informada_e_recusado_sem_mudar_nada(conn):
    pid = _a_conferir(conn)
    prazos_banco.corrigir(conn, pid, fatal_informada="2026-12-01")
    antes = prazos_banco.obter(conn, pid)
    n_eventos = len(eventos.listar(conn, NUMERO))
    with pytest.raises(prazos_banco.PrazoInvalido, match="Limpe a data fatal"):
        prazos_banco.corrigir(conn, pid, codigo="ed_cpc")
    assert prazos_banco.obter(conn, pid) == antes
    assert len(eventos.listar(conn, NUMERO)) == n_eventos


def test_codigo_limpando_a_data_fatal_na_mesma_chamada_vale(conn):
    pid = _a_conferir(conn)
    prazos_banco.corrigir(conn, pid, fatal_informada="2026-12-01")
    prazos_banco.corrigir(conn, pid, codigo="ed_cpc", fatal_informada="")
    p = prazos_banco.obter(conn, pid)
    assert p["codigo_ato"] == "ed_cpc" and p["fatal_informada"] is None


def test_codigo_sobre_registro_do_lex_guarda_ressalvas_e_limpa_trecho(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    conn.execute(
        "UPDATE prazo SET ato = 'Manifestação', dias = 10, unidade = 'uteis', "
        "origem_dos_dias = 'decisao', trecho = 'no prazo de 10 dias', fixado_pelo_juiz = 1, "
        "ressalvas = ? WHERE id = ?",
        (json.dumps(["Indício de Fazenda: conferir dobro"], ensure_ascii=False), pid))
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    p = prazos_banco.obter(conn, pid)
    ressalvas = json.loads(p["ressalvas"])
    assert ressalvas[0] == "Indício de Fazenda: conferir dobro"
    assert any("Termo inicial varia" in r for r in ressalvas) and len(ressalvas) == 2
    assert (p["origem_dos_dias"], p["trecho"], p["fixado_pelo_juiz"]) == ("lei", "", 0)
    antes = eventos.listar(conn, NUMERO)[-1]["dados"]["antes"]
    assert antes["trecho"] == "no prazo de 10 dias" and antes["fixado_pelo_juiz"] == 1
    assert antes["origem_dos_dias"] == "decisao" and antes["dias"] == 10
    assert "Indício de Fazenda: conferir dobro" in antes["ressalvas"]
    # de novo o mesmo código: a ressalva da lista não duplica
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    assert len(json.loads(prazos_banco.obter(conn, pid)["ressalvas"])) == 2


def test_dias_trocados_a_mao_num_registro_do_lex_viram_manuais(conn):
    for origem in ("decisao", "texto"):
        pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", origem, "2026-11-05")
        conn.execute(
            "UPDATE prazo SET ato = 'Manifestação', dias = 10, unidade = 'uteis', "
            "origem_dos_dias = ?, trecho = 'no prazo de 10 dias', fixado_pelo_juiz = 1, "
            "ressalvas = ? WHERE id = ?",
            (origem, json.dumps(["Indício de Fazenda: conferir dobro"], ensure_ascii=False), pid))
        # o formulário devolve o mesmo número: a fonte não muda
        prazos_banco.corrigir(conn, pid, ato="Manifestação", dias=10, unidade="uteis")
        assert prazos_banco.obter(conn, pid)["origem_dos_dias"] == origem
        prazos_banco.corrigir(conn, pid, dias=12)
        p = prazos_banco.obter(conn, pid)
        assert (p["origem_dos_dias"], p["dias"]) == ("manual", 12)
        assert p["trecho"] == "no prazo de 10 dias" and p["fixado_pelo_juiz"] == 1
        assert json.loads(p["ressalvas"]) == ["Indício de Fazenda: conferir dobro"]
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "u", "2026-11-05")
    conn.execute("UPDATE prazo SET dias = 10, unidade = 'uteis', origem_dos_dias = 'decisao' "
                 "WHERE id = ?", (pid,))
    prazos_banco.corrigir(conn, pid, unidade="corridos")
    assert prazos_banco.obter(conn, pid)["origem_dos_dias"] == "manual"


def test_trocar_de_codigo_troca_a_ressalva_da_lista(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    prazos_banco.corrigir(conn, pid, codigo="ed_cpc")
    assert json.loads(prazos_banco.obter(conn, pid)["ressalvas"]) == []


def test_corrigir_com_codigo_desconhecido(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, pid, codigo="inventado")


# --- mudou o calendário ----------------------------------------------------------------

def test_incluir_dia_recalcula_e_avisa_quando_a_data_muda(conn):
    pid = _a_conferir(conn, dias=5)
    prazos_banco.conferir(conn, pid)
    versao = prazos_banco.versao_calendario(conn)
    _, mudaram = prazos_banco.incluir_dia(conn, "2026-11-10", "TJMT", "", "sem_expediente",
                                          "Ponto facultativo", "Portaria X")
    assert mudaram == 1
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-16" and p["estado"] == "a_conferir"
    assert p["alerta"] == "O calendário mudou: a data fatal era 13/11/2026 e agora é 16/11/2026."
    assert p["versao_calendario"] == versao + 1
    e = eventos.listar(conn, NUMERO)[-1]
    assert e["tipo"] == "prazo_recalculado" and e["dados"]["de"] == "2026-11-13"


def test_mudanca_que_nao_afeta_a_data_nao_incomoda(conn):
    pid = _a_conferir(conn, dias=5)
    prazos_banco.conferir(conn, pid)
    _, mudaram = prazos_banco.incluir_dia(conn, "2026-12-11", "TJMT", "", "sem_expediente", "X", "Y")
    assert mudaram == 0
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "conferido" and p["alerta"] == ""
    assert p["versao_calendario"] == prazos_banco.versao_calendario(conn)


def test_recalculo_nao_mexe_em_cumprido_descartado_ou_data_informada(conn):
    a, b = _a_conferir(conn, dias=5), prazos_banco.abrir(conn, NUMERO, None, "publicacao", "2",
                                                         "2026-11-05")
    prazos_banco.corrigir(conn, b, dias=5, unidade="uteis")
    c = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "3", "2026-11-05")
    prazos_banco.corrigir(conn, c, unidade="horas", fatal_informada="2026-11-13")
    prazos_banco.cumprir(conn, a)
    prazos_banco.descartar(conn, b)
    assert prazos_banco.incluir_dia(conn, "2026-11-10", "TJMT", "", "sem_expediente", "X", "Y")[1] == 0
    assert prazos_banco.obter(conn, a)["fatal"] == "2026-11-13"
    assert prazos_banco.obter(conn, c)["fatal"] == "2026-11-13"


def test_outro_dia_de_trabalho_nao_passa_da_nova_fatal_quando_ela_recua(conn):
    conn.execute("INSERT INTO dia_forense (data, tribunal, tipo, descricao, fonte, origem) VALUES "
                 "('2026-11-10', 'TJMT', 'sem_expediente', 'X', 'Y', 'manual')")
    pid = _a_conferir(conn, dias=5)
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-16"
    prazos_banco.escolher_trabalho(conn, pid, data="2026-11-16")
    dia = conn.execute("SELECT id FROM dia_forense WHERE data = '2026-11-10'").fetchone()[0]
    assert prazos_banco.desativar_dia(conn, dia) == 1
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-13" and p["trabalho"] == "2026-11-13"


def test_desativar_nao_apaga_e_corrigir_tipo_recalcula(conn):
    pid = _a_conferir(conn, dias=5)
    dia, _ = prazos_banco.incluir_dia(conn, "2026-11-13", "TJMT", "", "sem_expediente", "X", "Y")
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-16"
    assert prazos_banco.corrigir_dia(conn, dia, tipo="reduzido") == 0  # segue passando ao dia 16
    assert prazos_banco.corrigir_dia(conn, dia, descricao="Outro nome", fonte="Outra") == 0
    prazos_banco.desativar_dia(conn, dia)
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-13"
    assert conn.execute("SELECT ativo FROM dia_forense WHERE id = ?", (dia,)).fetchone()[0] == 0


def test_incluir_de_novo_um_dia_desativado_reativa(conn):
    dia, _ = prazos_banco.incluir_dia(conn, "2026-11-13", "TJMT", "", "sem_expediente", "X", "Y")
    prazos_banco.desativar_dia(conn, dia)
    outro, _ = prazos_banco.incluir_dia(conn, "2026-11-13", "TJMT", "", "sem_expediente", "Z", "W")
    assert outro == dia
    linha = conn.execute("SELECT ativo, descricao FROM dia_forense WHERE id = ?", (dia,)).fetchone()
    assert tuple(linha) == (1, "Z")


@pytest.mark.parametrize("data, tipo, descricao, fonte", [
    ("13/11/2026", "sem_expediente", "X", "Y"), ("2026-11-13", "outro", "X", "Y"),
    ("2026-11-13", "suspensao", "", "Y"), ("2026-11-13", "suspensao", "X", ""),
])
def test_incluir_dia_recusa_dado_ruim(conn, data, tipo, descricao, fonte):
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.incluir_dia(conn, data, "TJMT", "", tipo, descricao, fonte)


def test_desfazer_cumprido_conta_de_novo_com_o_calendario_de_agora(conn):
    pid = _a_conferir(conn, dias=5)
    prazos_banco.conferir(conn, pid)
    prazos_banco.cumprir(conn, pid)
    _, mudaram = prazos_banco.incluir_dia(conn, "2026-11-10", "TJMT", "", "sem_expediente", "X", "Y")
    assert mudaram == 0  # cumprido não se mexe
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-13"
    prazos_banco.desfazer_cumprido(conn, pid)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_conferir" and p["fatal"] == "2026-11-16" and p["conferido_em"] is None
    assert p["alerta"] == "O calendário mudou: a data fatal era 13/11/2026 e agora é 16/11/2026."
    assert p["versao_calendario"] == prazos_banco.versao_calendario(conn)
    tipos = [e["tipo"] for e in eventos.listar(conn, NUMERO)]
    assert tipos[-2:] == ["prazo_cumprido_desfeito", "prazo_recalculado"]


def test_desfazer_cumprido_sem_mudar_a_data_volta_ao_estado_anterior(conn):
    pid = _a_conferir(conn, dias=5)
    prazos_banco.conferir(conn, pid)
    prazos_banco.cumprir(conn, pid)
    prazos_banco.incluir_dia(conn, "2026-12-11", "TJMT", "", "sem_expediente", "X", "Y")
    prazos_banco.desfazer_cumprido(conn, pid)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "conferido" and p["alerta"] == ""
    assert p["versao_calendario"] == prazos_banco.versao_calendario(conn)


def test_desfazer_cumprido_de_data_informada_nao_conta(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, unidade="horas", fatal_informada="2026-11-13")
    prazos_banco.conferir(conn, pid)
    prazos_banco.cumprir(conn, pid)
    prazos_banco.incluir_dia(conn, "2026-11-13", "TJMT", "", "sem_expediente", "X", "Y")
    prazos_banco.desfazer_cumprido(conn, pid)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "conferido" and p["fatal"] == "2026-11-13"


def test_incluir_dia_normaliza_o_tribunal(conn):
    pid = _a_conferir(conn, dias=5)
    dia, mudaram = prazos_banco.incluir_dia(conn, "2026-11-10", " tjmt ", "", "sem_expediente",
                                            "X", "Y")
    assert mudaram == 1 and prazos_banco.obter(conn, pid)["fatal"] == "2026-11-16"
    assert conn.execute("SELECT tribunal FROM dia_forense WHERE id = ?", (dia,)).fetchone()[0] == "TJMT"
    outro, _ = prazos_banco.incluir_dia(conn, "2026-11-10", "TJMT", "", "sem_expediente", "Z", "W")
    assert outro == dia


# --- rodada de correção 1 --------------------------------------------------------------

ALERTA_SEM_CONTA = prazos_banco.ALERTA_SEM_CONTA


def _envenenar(conn, pid, **campos):
    conn.execute(f"UPDATE prazo SET {', '.join(f'{c} = ?' for c in campos)} WHERE id = ?",
                 (*campos.values(), pid))


def test_um_prazo_com_conta_impossivel_nao_trava_a_mudanca_do_calendario(conn):
    bom = _a_conferir(conn, dias=5)
    prazos_banco.conferir(conn, bom)
    ruim1, ruim2 = (prazos_banco.abrir(conn, NUMERO, None, "publicacao", ref, "2026-11-05")
                    for ref in ("8", "9"))
    for pid in (ruim1, ruim2):
        prazos_banco.corrigir(conn, pid, ato="X", dias=5, unidade="uteis")
        prazos_banco.conferir(conn, pid)
    _envenenar(conn, ruim1, dias=400)
    _envenenar(conn, ruim2, disponibilizacao="lixo")
    versao = prazos_banco.versao_calendario(conn)
    _, atencao = prazos_banco.incluir_dia(conn, "2026-11-10", "TJMT", "", "sem_expediente", "X", "Y")
    assert atencao == 3  # o bom mudou de data e os dois ruins pedem atenção
    assert prazos_banco.versao_calendario(conn) == versao + 1
    b = prazos_banco.obter(conn, bom)
    assert b["fatal"] == "2026-11-16" and b["estado"] == "a_conferir"
    for pid in (ruim1, ruim2):
        p = prazos_banco.obter(conn, pid)
        assert p["estado"] == "a_conferir" and p["conferido_em"] is None
        assert p["alerta"] == ALERTA_SEM_CONTA
        assert p["fatal"] == "2026-11-13"  # a data antiga fica, mas avisada
    falhas = [e for e in eventos.listar(conn, NUMERO) if e["tipo"] == "prazo_recalculo_falhou"]
    assert {e["dados"]["prazo"] for e in falhas} == {ruim1, ruim2}


def test_desfazer_cumprido_de_prazo_com_conta_impossivel_avisa(conn):
    pid = _a_conferir(conn, dias=5)
    prazos_banco.conferir(conn, pid)
    prazos_banco.cumprir(conn, pid)
    _envenenar(conn, pid, disponibilizacao="lixo")
    prazos_banco.desfazer_cumprido(conn, pid)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_conferir" and p["alerta"] == ALERTA_SEM_CONTA


def test_data_informada_recalcula_so_a_data_de_trabalho(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, unidade="horas", fatal_informada="2026-11-13")
    prazos_banco.conferir(conn, pid)
    prazos_banco.escolher_trabalho(conn, pid, d=1)
    assert prazos_banco.obter(conn, pid)["trabalho"] == "2026-11-12"
    _, mudaram = prazos_banco.incluir_dia(conn, "2026-11-12", "TJMT", "", "sem_expediente", "X", "Y")
    assert mudaram == 0
    p = prazos_banco.obter(conn, pid)
    assert p["trabalho"] == "2026-11-11"
    assert p["fatal"] == "2026-11-13" and p["estado"] == "conferido" and p["alerta"] == ""
    assert p["versao_calendario"] == prazos_banco.versao_calendario(conn)


def test_conferido_que_antecipa_ao_tirar_um_feriado_volta_a_conferir_e_avisa(conn):
    dia, _ = prazos_banco.incluir_dia(conn, "2026-11-10", "TJMT", "", "sem_expediente", "X", "Y")
    pid = _a_conferir(conn, dias=5)
    prazos_banco.conferir(conn, pid)
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-16"
    assert prazos_banco.desativar_dia(conn, dia) == 1
    p = prazos_banco.obter(conn, pid)
    assert p["fatal"] == "2026-11-13" and p["estado"] == "a_conferir" and p["conferido_em"] is None
    assert p["alerta"] == "O calendário mudou: a data fatal era 16/11/2026 e agora é 13/11/2026."


def test_desativar_dia_ja_desativado_nao_muda_nada(conn):
    dia, _ = prazos_banco.incluir_dia(conn, "2026-11-13", "TJMT", "", "sem_expediente", "X", "Y")
    prazos_banco.desativar_dia(conn, dia)
    versao, n_eventos = prazos_banco.versao_calendario(conn), len(eventos.listar(conn, None))
    assert prazos_banco.desativar_dia(conn, dia) == 0
    assert prazos_banco.versao_calendario(conn) == versao
    assert len(eventos.listar(conn, None)) == n_eventos


def test_unidade_fora_da_lista_e_recusada_mesmo_com_data_fatal_informada(conn):
    pid = _a_conferir(conn)
    prazos_banco.corrigir(conn, pid, fatal_informada="2026-12-04")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, pid, unidade="semanas")
    assert prazos_banco.obter(conn, pid)["unidade"] == "uteis"
    prazos_banco.corrigir(conn, pid, unidade="meses")
    assert prazos_banco.obter(conn, pid)["unidade"] == "meses"
