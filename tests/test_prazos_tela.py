"""Os dados das telas de prazo, com datas fixas."""

import dataclasses
from datetime import date, timedelta

import pytest

from nucleo import (banco, carteira, painel_dados, prazos, prazos_banco, prazos_legais,
                    prazos_tela)

HOJE = date(2026, 11, 10)  # terça
A = "00012345620248110041"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    c.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua, cliente) "
              "VALUES (?, 'TJMT', '1ª Vara Cível de Cuiabá', 1, 'MARIA FICTÍCIA')", (A,))
    yield c
    c.close()


def _prazo(conn, ref, *, fatal, conferir=True, ato="Réplica", numero=A, demanda=None):
    pid = prazos_banco.abrir(conn, numero, demanda, "publicacao", ref, "2026-11-05")
    prazos_banco.corrigir(conn, pid, ato=ato, unidade="horas", fatal_informada=fatal)
    if conferir:
        prazos_banco.conferir(conn, pid)
    return pid


def _ids(linhas):
    return [l["id"] for l in linhas]


def test_lista_separa_por_data_e_estado(conn):
    atrasado = _prazo(conn, "1", fatal="2026-11-09")
    de_hoje = _prazo(conn, "2", fatal="2026-11-10")
    semana = _prazo(conn, "3", fatal="2026-11-13")
    depois = _prazo(conn, "4", fatal="2026-11-16")
    conferir = _prazo(conn, "5", fatal="2026-11-11", conferir=False)
    definir = prazos_banco.abrir(conn, A, None, "publicacao", "6", "2026-11-05")
    cumprido = _prazo(conn, "7", fatal="2026-11-11")
    prazos_banco.cumprir(conn, cumprido)
    descartado = _prazo(conn, "8", fatal="2026-11-11")
    prazos_banco.descartar(conn, descartado)
    g = prazos_tela.lista(conn, HOJE)
    assert _ids(g["atrasados"]) == [atrasado] and _ids(g["hoje"]) == [de_hoje]
    assert _ids(g["semana"]) == [semana] and _ids(g["depois"]) == [depois]
    assert _ids(g["a_conferir"]) == [conferir] and _ids(g["a_definir"]) == [definir]


def test_linha_mostra_o_que_a_tela_precisa(conn):
    pid = _prazo(conn, "1", fatal="2026-11-13")
    prazos_banco.escolher_trabalho(conn, pid, d=1)
    l = prazos_tela.lista(conn, HOJE)["semana"][0]
    assert l["fatal_br"] == "13/11/2026" and l["trabalho_br"] == "12/11/2026"
    assert l["numero_formatado"] == "0001234-56.2024.8.11.0041"
    assert l["cliente"] == "MARIA FICTÍCIA" and l["estado_rotulo"] == "Conferido"
    assert l["urgente"] is False


@pytest.mark.parametrize("fatal, urgente", [("2026-11-09", True), ("2026-11-10", True),
                                            ("2026-11-12", True), ("2026-11-13", False)])
def test_urgente_em_ate_dois_dias_uteis(conn, fatal, urgente):
    _prazo(conn, "1", fatal=fatal, conferir=False)
    assert prazos_tela.lista(conn, HOJE)["a_conferir"][0]["urgente"] is urgente


def test_prazo_de_processo_arquivado_segue_na_lista_com_o_selo(conn):
    _prazo(conn, "1", fatal="2026-11-13")
    carteira.arquivar(conn, A)
    l = prazos_tela.lista(conn, HOJE)["semana"][0]
    assert l["selo_processo"] == carteira.ARQUIVADO_POR_VOCE
    carteira.excluir(conn, A)
    assert prazos_tela.lista(conn, HOJE)["semana"][0]["selo_processo"] == carteira.NA_LIXEIRA


def test_aviso_coberto_nao_aparece_a_parte(conn):
    conn.execute("INSERT INTO demanda (id, numero, coluna, referencia_em, criada_em, atualizada_em) "
                 "VALUES (1, ?, 'chegou', '2026-11-05', 'x', 'x')", (A,))
    aviso = prazos_banco.abrir(conn, A, 1, "aviso", "1grau:9", "2026-10-20", estado="a_definir",
                               motivo="abra no PJe")
    assert _ids(prazos_tela.lista(conn, HOJE)["a_definir"]) == [aviso]
    pub = prazos_banco.abrir(conn, A, 1, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pub, ato="Réplica", dias=5, unidade="uteis")
    g = prazos_tela.lista(conn, HOJE)
    assert g["a_definir"] == [] and _ids(g["a_conferir"]) == [pub]
    assert g["a_conferir"][0]["fatal_alternativa_br"]


def test_grade_da_semana_mostra_trabalho_e_fatal(conn):
    pid = _prazo(conn, "1", fatal="2026-11-13")
    prazos_banco.escolher_trabalho(conn, pid, d=1)
    g = prazos_tela.grade(conn, HOJE, HOJE, "semana")
    assert [d["data"] for d in g["dias"]][0] == "2026-11-09" and len(g["dias"]) == 7
    assert g["anterior"] == "2026-11-03" and g["proximo"] == "2026-11-17"
    por_dia = {d["data"]: d for d in g["dias"]}
    assert por_dia["2026-11-10"]["e_hoje"] is True
    assert [i["tipo"] for i in por_dia["2026-11-12"]["itens"]] == ["trabalho"]
    assert [i["tipo"] for i in por_dia["2026-11-13"]["itens"]] == ["fatal"]


def test_grade_do_mes_cobre_semanas_inteiras(conn):
    _prazo(conn, "1", fatal="2026-11-13", conferir=False)
    g = prazos_tela.grade(conn, HOJE, HOJE, "mes")
    assert g["dias"][0]["data"] == "2026-10-26" and g["dias"][-1]["data"] == "2026-12-06"
    assert g["dias"][0]["fora_do_mes"] is True
    assert g["anterior"] == "2026-10-01" and g["proximo"] == "2026-12-01"
    item = [d for d in g["dias"] if d["data"] == "2026-11-13"][0]["itens"][0]
    assert item["a_conferir"] is True
    assert g["titulo"] == "Novembro de 2026"


def test_faixa_conta_sem_contar_o_que_nao_corre(conn):
    assert prazos_tela.faixa(conn, HOJE) is None
    _prazo(conn, "1", fatal="2026-11-09")
    _prazo(conn, "2", fatal="2026-11-11")
    _prazo(conn, "3", fatal="2026-11-11")
    _prazo(conn, "4", fatal="2026-11-20", conferir=False)
    texto = prazos_tela.faixa(conn, HOJE)["texto"]
    assert "2 prazos vencem até amanhã" in texto
    assert "1 atrasado" in texto and "1 a conferir" in texto


def test_faixa_na_sexta_olha_o_proximo_dia_util(conn):
    _prazo(conn, "1", fatal="2026-11-16")
    texto = prazos_tela.faixa(conn, date(2026, 11, 13))["texto"]
    assert "1 prazo vence até 16/11" in texto


def test_contador_do_menu_em_ate_dois_dias_uteis(conn):
    _prazo(conn, "1", fatal="2026-11-09")
    _prazo(conn, "2", fatal="2026-11-12", conferir=False)
    _prazo(conn, "3", fatal="2026-11-13")
    assert prazos_tela.contador_nav(conn, HOJE) == 2


def _cartao(conn):
    conn.execute("INSERT OR IGNORE INTO demanda (id, numero, coluna, referencia_em, criada_em, "
                 "atualizada_em) VALUES (1, ?, 'autos', '2026-11-05', 'x', 'x')", (A,))
    return 1


def test_selos_do_cartao(conn):
    d = _cartao(conn)
    assert prazos_tela.selo_do_cartao(conn, d, HOJE) is None
    pid = prazos_banco.abrir(conn, A, d, "publicacao", "1", "2026-11-05")
    assert prazos_tela.selo_do_cartao(conn, d, HOJE)["texto"] == "Prazo a definir"
    prazos_banco.corrigir(conn, pid, ato="Réplica", unidade="horas", fatal_informada="2026-11-20")
    assert prazos_tela.selo_do_cartao(conn, d, HOJE)["texto"] == "Prazo a conferir"
    prazos_banco.conferir(conn, pid)
    prazos_banco.escolher_trabalho(conn, pid, d=2)
    s = prazos_tela.selo_do_cartao(conn, d, HOJE)
    assert s == {"texto": "Fatal 20/11 · trabalho 18/11", "tom": "neutro"}
    prazos_banco.descartar(conn, pid)
    assert prazos_tela.selo_do_cartao(conn, d, HOJE)["texto"] == "Sem prazo"


def test_selo_vermelho_em_ate_dois_dias_uteis(conn):
    d = _cartao(conn)
    pid = _prazo(conn, "1", fatal="2026-11-12", demanda=d)
    assert prazos_tela.selo_do_cartao(conn, d, HOJE)["tom"] == "alerta"
    prazos_banco.cumprir(conn, pid)
    assert prazos_tela.selo_do_cartao(conn, d, HOJE) is None


def test_cartao_do_quadro_leva_o_selo(conn):
    d = _cartao(conn)
    _prazo(conn, "1", fatal="2026-11-12", demanda=d)
    linha = conn.execute("SELECT d.*, p.cliente, p.parte_contraria, p.orgao_julgador FROM demanda d "
                         "LEFT JOIN processo p ON p.numero = d.numero WHERE d.id = ?", (d,)).fetchone()
    c = painel_dados._cartao(conn, linha, HOJE)
    assert c["selo_prazo"]["tom"] == "alerta"
    assert c["aviso_prazos"] == "1 prazo aberto: próximo 12/11. Continuam na aba Prazos."


def test_aviso_de_arquivar(conn):
    assert prazos_tela.aviso_de_arquivar(conn, [A], HOJE) == ""
    _prazo(conn, "1", fatal="2026-11-20")
    _prazo(conn, "2", fatal="2026-11-12", conferir=False)
    assert prazos_tela.aviso_de_arquivar(conn, [A], HOJE) == (
        "2 prazos abertos: próximo 12/11. Continuam na aba Prazos.")
    p = conn.execute("SELECT * FROM processo WHERE numero = ?", (A,)).fetchone()
    assert painel_dados.estado_no_arquivo(conn, p, HOJE)["aviso_prazos"] == (
        "2 prazos abertos: próximo 12/11. Continuam na aba Prazos.")


def test_detalhe_traz_conta_ressalvas_e_opcoes(conn):
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                 "VALUES (1, ?, 'TJMT', '2026-11-05', 'x')", (A,))
    pid = prazos_banco.abrir(conn, A, None, "publicacao", "1", "2026-11-05")
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert d["fatal_br"] == "30/11/2026" and d["pode_conferir"] is True
    assert any("dia 15" in x for x in d["passos"])
    assert any("municipais" in x for x in d["avisos"])
    assert any("Termo inicial" in x for x in d["ressalvas"])
    assert [(o["d"], o["data_br"]) for o in d["opcoes_d"]] == [
        (0, "30/11/2026"), (1, "27/11/2026"), (2, "26/11/2026"), (3, "25/11/2026")]
    assert d["versao_calendario"] == 1
    assert "contestacao" in [c["codigo"] for c in d["lista_fechada"]]


def test_dias_do_calendario_e_lembrete(conn):
    linhas = prazos_tela.dias_do_calendario(conn, "TJMT", 2026)
    assert linhas and all(l["tribunal"] in ("", "TJMT") for l in linhas)
    assert all(l["data"].startswith("2026") for l in linhas)
    assert linhas == sorted(linhas, key=lambda l: l["data"])
    assert "2027" in prazos_tela.lembrete_calendario(conn, date(2026, 11, 3), ["TJMT"])
    assert prazos_tela.lembrete_calendario(conn, date(2026, 10, 3), ["TJMT"]) == ""
    prazos_banco.incluir_dia(conn, "2027-02-08", "TJMT", "", "sem_expediente", "Carnaval",
                             "Portaria Y", origem="ato")
    assert prazos_tela.lembrete_calendario(conn, date(2026, 11, 3), ["TJMT"]) == ""


# --- o que o controlador mandou cumprir nesta tela ---------------------------------------

def _demanda(conn, id_=1, coluna="autos"):
    conn.execute("INSERT OR IGNORE INTO demanda (id, numero, coluna, referencia_em, criada_em, "
                 "atualizada_em) VALUES (?, ?, ?, '2026-11-05', 'x', 'x')", (id_, A, coluna))
    return id_


def _aviso_pje(conn, demanda, ref="1grau:9", envio="2026-10-20", pendente=1,
               motivo="Intimação no PJe, sem texto aqui: abra no PJe e preencha à mão."):
    instancia, _, id_ = ref.partition(":")
    conn.execute("INSERT OR IGNORE INTO aviso (instancia, id, numero, tipo_comunicacao, "
                 "data_disponibilizacao, visto_em, pendente) VALUES (?, ?, ?, 'INT', ?, "
                 "'2026-11-04T10:00:00-04:00', ?)", (instancia, id_, A, envio, pendente))
    return prazos_banco.abrir(conn, A, demanda, "aviso", ref, envio, estado="a_definir",
                              motivo=motivo)


def _publicacao(conn, id_=1, dias=15, ato="Contestação", demanda=None, dispo="2026-11-05"):
    conn.execute("INSERT OR IGNORE INTO publicacao (id, numero, tribunal, data_disponibilizacao, "
                 "texto) VALUES (?, ?, 'TJMT', ?, 'x')", (id_, A, dispo))
    pid = prazos_banco.abrir(conn, A, demanda, "publicacao", str(id_), dispo)
    prazos_banco.corrigir(conn, pid, ato=ato, dias=dias, unidade="uteis")
    return pid


def _descartar_processo(conn, atua=0):
    conn.execute("UPDATE processo SET descartado_em = '2026-11-01', advogado_atua = ? "
                 "WHERE numero = ?", (atua, A))


def test_detalhe_com_suspensao_traz_a_data_sem_ela(conn):
    prazos_banco.incluir_dia(conn, "2026-11-17", "", "", "suspensao", "Suspensão de teste",
                             "Portaria Z", origem="ato")
    pid = _publicacao(conn)
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert prazos.AVISO_SUSPENSAO in d["avisos"] and d["com_suspensao"] is True
    cal = prazos_banco.carregar_calendario(conn, "TJMT", "1ª Vara Cível de Cuiabá")
    sem = dataclasses.replace(cal, suspensao={})
    esperada = prazos.contar(date(2026, 11, 5), 15, "uteis", sem).fatal
    assert d["fatal_sem_suspensao"] == esperada.isoformat()
    assert d["fatal_sem_suspensao_br"] == prazos.dia_br(esperada)
    assert d["fatal_sem_suspensao"] < d["fatal"]


def test_detalhe_sem_suspensao_nao_inventa_a_outra_data(conn):
    d = prazos_tela.detalhe(conn, _publicacao(conn), HOJE)
    assert d["com_suspensao"] is False
    assert d["fatal_sem_suspensao"] is None and d["fatal_sem_suspensao_br"] == ""


def test_detalhe_com_suspensao_e_duas_datas_traz_as_duas_sem_ela(conn):
    prazos_banco.incluir_dia(conn, "2026-11-17", "", "", "suspensao", "Suspensão de teste",
                             "Portaria Z", origem="ato")
    cartao = _demanda(conn)
    pid = _publicacao(conn, demanda=cartao)
    _aviso_pje(conn, cartao)
    prazos_banco.parear_processo(conn, A)
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert d["fatal_alternativa"] and d["fatal_sem_suspensao"]
    assert d["fatal_alternativa_sem_suspensao_br"]


def test_origem_dos_dias_chega_com_o_ato_da_lista(conn):
    pid = _publicacao(conn)
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert d["origem_dos_dias"] == "lei" and "lei" in d["origem_rotulo"].lower()
    assert d["ato_da_lei"] == prazos_legais.PRAZOS["contestacao"].ato
    assert d["artigo_da_lei"] == prazos_legais.PRAZOS["contestacao"].artigo
    prazos_banco.corrigir(conn, pid, dias=7)
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert d["origem_dos_dias"] == "manual" and d["ato_da_lei"] == ""
    assert "você" in d["origem_rotulo"]


def test_origem_texto_mostra_o_trecho_da_publicacao(conn):
    pid = _publicacao(conn)
    conn.execute("UPDATE prazo SET origem_dos_dias = 'texto', trecho = 'prazo de quinze dias' "
                 "WHERE id = ?", (pid,))
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert "publicação" in d["origem_rotulo"] and d["ato_da_lei"] == ""
    assert d["trecho"] == "prazo de quinze dias"


def test_processo_descartado_nao_espera_o_lex_nem_fica_a_definir(conn):
    cartao = _demanda(conn)
    espera = prazos_banco.abrir(conn, A, cartao, "publicacao", "1", "2026-11-05")
    definir = prazos_banco.abrir(conn, A, cartao, "publicacao", "2", "2026-11-05",
                                 estado="a_definir", motivo="Sem texto.")
    dated = _prazo(conn, "3", fatal="2026-11-13", conferir=False, demanda=cartao)
    assert _ids(prazos_tela.lista(conn, HOJE)["a_definir"]) == [espera, definir]
    _descartar_processo(conn)
    g = prazos_tela.lista(conn, HOJE)
    assert g["a_definir"] == [] and _ids(g["a_conferir"]) == [dated]
    assert prazos_banco.contar_sem_data_do_cartao(conn, cartao) == 0
    assert prazos_tela.selo_do_cartao(conn, cartao, HOJE)["texto"] == "Prazo a conferir"
    outro = _demanda(conn, 2)  # um cartão só com o que esperava o Lex: não diz nada
    prazos_banco.abrir(conn, A, outro, "publicacao", "4", "2026-11-05")
    assert prazos_tela.selo_do_cartao(conn, outro, HOJE) is None


def test_quem_atua_no_processo_descartado_ainda_ve_o_que_espera(conn):
    cartao = _demanda(conn)
    espera = prazos_banco.abrir(conn, A, cartao, "publicacao", "1", "2026-11-05")
    _descartar_processo(conn, atua=1)
    assert _ids(prazos_tela.lista(conn, HOJE)["a_definir"]) == [espera]
    assert prazos_banco.contar_sem_data_do_cartao(conn, cartao) == 1
    assert prazos_tela.selo_do_cartao(conn, cartao, HOJE)["texto"] == "Prazo a definir"


def test_detalhe_de_processo_descartado_nao_promete_leitura(conn):
    espera = prazos_banco.abrir(conn, A, None, "publicacao", "1", "2026-11-05")
    definir = prazos_banco.abrir(conn, A, None, "publicacao", "2", "2026-11-05",
                                 estado="a_definir", motivo="Sem texto.")
    d = prazos_tela.detalhe(conn, espera, HOJE)
    assert d["lex_vai_ler"] is True and "Lex ainda não leu" in d["motivo"]
    assert prazos_tela.detalhe(conn, definir, HOJE)["pode_ler_de_novo"] is True
    _descartar_processo(conn)
    d = prazos_tela.detalhe(conn, espera, HOJE)
    assert d["lex_vai_ler"] is False and "Lex ainda não leu" not in d["motivo"]
    assert "não seu" in d["motivo"]
    assert prazos_tela.detalhe(conn, definir, HOJE)["pode_ler_de_novo"] is False


def test_aviso_que_nao_foi_coberto_continua_visivel(conn):
    cartao = _demanda(conn)
    # envio longe de qualquer publicação: nada o cobre
    solto = _aviso_pje(conn, cartao, envio="2026-05-04")
    g = prazos_tela.lista(conn, HOJE)
    assert _ids(g["a_definir"]) == [solto] and "abra no PJe" in g["a_definir"][0]["motivo"]


def test_aviso_pedindo_o_dia_da_ciencia_nao_some_com_a_publicacao(conn):
    cartao = _demanda(conn)
    pub = _publicacao(conn, demanda=cartao)
    aviso = _aviso_pje(conn, cartao, pendente=0)
    prazos_banco.atualizar_avisos(conn)
    g = prazos_tela.lista(conn, HOJE)
    assert aviso in _ids(g["a_definir"]) and pub in _ids(g["a_conferir"])
    assert "informe a data" in prazos_tela.detalhe(conn, aviso, HOJE)["motivo"].lower()


def test_aviso_com_ciencia_ilegivel_continua_visivel_e_a_publicacao_diz_por_que(conn):
    cartao = _demanda(conn)
    pub = _publicacao(conn, demanda=cartao)
    aviso = _aviso_pje(conn, cartao)
    conn.execute("UPDATE prazo SET ciencia = 'ontem' WHERE id = ?", (aviso,))
    prazos_banco.parear_processo(conn, A)
    g = prazos_tela.lista(conn, HOJE)
    assert aviso in _ids(g["a_definir"])
    d = prazos_tela.detalhe(conn, pub, HOJE)
    assert prazos_banco.NOTA_CIENCIA_ILEGIVEL in d["avisos"]
    assert prazos_banco.ALERTA_CIENCIA_ILEGIVEL in d["alerta"]


def test_aviso_coberto_volta_a_lista_quando_a_publicacao_sai(conn):
    cartao = _demanda(conn)
    pub = _publicacao(conn, demanda=cartao)
    aviso = _aviso_pje(conn, cartao)
    prazos_banco.parear_processo(conn, A)
    assert aviso not in _ids(prazos_tela.lista(conn, HOJE)["a_definir"])
    prazos_banco.cumprir(conn, pub)
    assert aviso in _ids(prazos_tela.lista(conn, HOJE)["a_definir"])


def test_aviso_coberto_nao_faz_o_cartao_dizer_a_definir(conn):
    cartao = _demanda(conn)
    pub = _publicacao(conn, demanda=cartao)
    _aviso_pje(conn, cartao)
    prazos_banco.parear_processo(conn, A)
    assert prazos_tela.selo_do_cartao(conn, cartao, HOJE)["texto"] == "Prazo a conferir"
    prazos_banco.conferir(conn, pub)
    assert prazos_tela.selo_do_cartao(conn, cartao, HOJE)["texto"].startswith("Fatal ")


def test_data_menor_vale_e_a_maior_aparece_ao_lado_com_o_porque(conn):
    cartao = _demanda(conn)
    pub = _publicacao(conn, demanda=cartao)
    prazos_banco.conferir(conn, pub)
    assert _ids(prazos_tela.lista(conn, HOJE)["depois"]) == [pub]
    _aviso_pje(conn, cartao)
    prazos_banco.parear_processo(conn, A, prazos_banco.ALERTA_INTIMACAO)
    g = prazos_tela.lista(conn, HOJE)
    # a conferida voltou a «A conferir» e saiu dos grupos por data
    assert _ids(g["a_conferir"]) == [pub]
    assert all(pub not in _ids(g[k]) for k in ("atrasados", "hoje", "semana", "depois"))
    l = g["a_conferir"][0]
    assert l["fatal"] < prazos_banco.obter(conn, pub)["fatal_alternativa"]
    assert l["fatal_alternativa_br"] and l["fatal_maior"] is False
    assert "Chegou intimação" in l["alerta"]
    d = prazos_tela.detalhe(conn, pub, HOJE)
    assert prazos_banco.AVISO_DUAS_DATAS in d["avisos"] and d["pode_escolher_fatal"] is True
    assert d["alternativa_passos"]
    prazos_banco.escolher_fatal(conn, pub, True)
    l = prazos_tela.lista(conn, HOJE)["a_conferir"][0]
    assert l["fatal_maior"] is True and l["fatal_br"] > "" and l["fatal_alternativa_br"]
    assert l["fatal"] > prazos_banco.obter(conn, pub)["fatal_alternativa"]


def test_protocolado_deixa_o_que_nao_tem_data_nas_listas(conn):
    cartao = _demanda(conn)
    datado = _prazo(conn, "1", fatal="2026-11-13", demanda=cartao)
    espera = prazos_banco.abrir(conn, A, cartao, "publicacao", "2", "2026-11-05")
    definir = prazos_banco.abrir(conn, A, cartao, "publicacao", "3", "2026-11-05",
                                 estado="a_definir", motivo="Sem texto.")
    assert prazos_banco.cumprir_do_cartao(conn, cartao) == 1
    g = prazos_tela.lista(conn, HOJE)
    assert _ids(g["a_definir"]) == [espera, definir] and datado not in _ids(g["semana"])
    assert prazos_tela.selo_do_cartao(conn, cartao, HOJE)["texto"] == "Prazo a definir"
    assert prazos_banco.contar_sem_data_do_cartao(conn, cartao) == 2


def test_a_conferir_com_data_proxima_aparece_nomeado_na_faixa(conn):
    _prazo(conn, "1", fatal="2026-11-11", conferir=False)
    _prazo(conn, "2", fatal="2026-11-20", conferir=False)
    assert prazos_tela.faixa(conn, HOJE)["texto"] == "1 a conferir com data urgente · 1 a conferir"
    assert prazos_tela.contador_nav(conn, HOJE) == 1


def test_a_conferir_sem_data_conta_como_a_conferir(conn):
    pid = prazos_banco.abrir(conn, A, None, "publicacao", "1", "2026-11-05", estado="a_conferir")
    assert prazos_banco.obter(conn, pid)["fatal"] is None
    assert prazos_tela.faixa(conn, HOJE)["texto"] == "1 a conferir"
    assert prazos_tela.lista(conn, HOJE)["a_conferir"][0]["urgente"] is False


@pytest.mark.parametrize("hoje", [date(2026, 11, 13), date(2026, 11, 18), date(2026, 12, 18),
                                  date(2026, 12, 31), date(2027, 1, 5)])
def test_urgente_concorda_com_a_conta_dos_dias_uteis(conn, hoje):
    cal = prazos_banco.carregar_calendario(conn, "TJMT", "1ª Vara Cível de Cuiabá")
    cals = prazos_tela._Calendarios(conn)
    for i in range(-3, 45):
        alvo = hoje + timedelta(days=i)
        p = {"numero": A, "origem_tipo": "aviso", "origem_ref": "x", "fatal": alvo.isoformat()}
        assert prazos_tela._urgente(p, hoje, cals) is (
            prazos.uteis_ate(hoje, alvo, cal) <= prazos_tela.URGENTE_UTEIS), (hoje, alvo)


def test_as_telas_so_leem_nao_refazem_a_conta_nem_gravam(conn, monkeypatch):
    cartao = _demanda(conn)
    _prazo(conn, "1", fatal="2026-11-13", demanda=cartao)
    _prazo(conn, "2", fatal="2026-11-11", conferir=False, demanda=cartao)
    pub = _publicacao(conn, id_=7, demanda=cartao)

    def proibido(*a, **k):
        raise AssertionError("a tela refez a conta")
    for nome in ("contar", "contar_da_ciencia"):
        monkeypatch.setattr(prazos, nome, proibido)
    for nome in ("_conta", "recontar", "recontar_seguro", "parear_processo"):
        monkeypatch.setattr(prazos_banco, nome, proibido)
    conn.commit()
    antes = conn.total_changes
    prazos_tela.lista(conn, HOJE), prazos_tela.faixa(conn, HOJE)
    prazos_tela.contador_nav(conn, HOJE), prazos_tela.selo_do_cartao(conn, cartao, HOJE)
    prazos_tela.grade(conn, HOJE, HOJE, "mes")
    prazos_tela.aviso_de_arquivar(conn, [A], HOJE)
    assert conn.total_changes == antes and pub


def test_faixa_e_contador_nao_consultam_prazo_distante_nem_um_a_um(conn):
    for i in range(60):
        numero = f"{i:07d}20248110041"
        conn.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua) "
                     "VALUES (?, 'TJMT', ?, 1)", (numero, f"{i}ª Vara de Cuiabá"))
        pid = prazos_banco.abrir(conn, numero, None, "publicacao", str(i), "2026-11-05")
        prazos_banco.corrigir(conn, pid, ato="Réplica", unidade="horas",
                              fatal_informada="2027-03-10")
        prazos_banco.conferir(conn, pid)
    consultas = []
    conn.set_trace_callback(consultas.append)
    assert prazos_tela.faixa(conn, HOJE) is None
    assert prazos_tela.contador_nav(conn, HOJE) == 0
    conn.set_trace_callback(None)
    assert len(consultas) <= 14, consultas[:16]  # as mesmas, com 60 ou com 6000 processos


def test_quadro_do_painel_leva_o_selo_de_cada_cartao(conn):
    cartao = _demanda(conn, coluna="chegou")
    _prazo(conn, "1", fatal="2026-11-12", demanda=cartao)
    colunas = painel_dados.quadro(conn, HOJE)["colunas"]
    cartoes = [c for col in colunas for c in col["cartoes"]]
    assert cartoes[0]["selo_prazo"]["tom"] == "alerta"
    assert "próximo 12/11" in cartoes[0]["aviso_prazos"]


# --- rodada 1 de correções ---------------------------------------------------------------

def _vencidos(conn, n, conferir=False, marca="v"):
    return [_prazo(conn, f"{marca}{i}", fatal=(HOJE - timedelta(days=1 + i)).isoformat(),
                   conferir=conferir) for i in range(n)]


def test_atraso_acumulado_nao_enterra_o_que_vence_hoje(conn):
    vencidos = _vencidos(conn, 12)
    hoje_ = _prazo(conn, "h", fatal="2026-11-10", conferir=False)
    amanha = _prazo(conn, "a", fatal="2026-11-11", conferir=False)
    g = prazos_tela.lista(conn, HOJE)
    assert _ids(g["a_conferir_proximos"]) == [hoje_, amanha]
    assert _ids(g["a_conferir_vencidos"]) == vencidos  # o mais recente primeiro
    assert _ids(g["a_conferir"]) == [hoje_, amanha] + vencidos
    assert [l["atrasado"] for l in g["a_conferir"]] == [False, False] + [True] * 12
    assert prazos_tela.faixa(conn, HOJE)["texto"] == (
        "2 a conferir com data urgente · 12 vencidos a conferir")


def test_faixa_tem_um_grupo_para_cada_numero(conn):
    _vencidos(conn, 2, conferir=True)
    _prazo(conn, "h", fatal="2026-11-10")
    _prazo(conn, "p", fatal="2026-11-20", conferir=False)
    _vencidos(conn, 1, marca="w")
    texto = prazos_tela.faixa(conn, HOJE)["texto"]
    assert texto == ("1 prazo vence hoje · 1 a conferir · 2 atrasados · "
                     "1 vencido a conferir")
    g = prazos_tela.lista(conn, HOJE)
    assert (len(g["atrasados"]), len(g["hoje"]), len(g["a_conferir_proximos"]),
            len(g["a_conferir_vencidos"])) == (2, 1, 1, 1)


def test_o_que_vence_hoje_entra_na_faixa_e_no_contador(conn):
    _prazo(conn, "1", fatal="2026-11-10")
    assert "1 prazo vence hoje" in prazos_tela.faixa(conn, HOJE)["texto"]
    assert prazos_tela.contador_nav(conn, HOJE) == 1


def test_a_conferir_atrasado_entra_na_faixa_e_no_contador(conn):
    _prazo(conn, "1", fatal="2026-11-09", conferir=False)
    assert prazos_tela.faixa(conn, HOJE)["texto"] == "1 vencido a conferir"
    assert prazos_tela.contador_nav(conn, HOJE) == 1


def test_domingo_ainda_e_a_semana_e_a_segunda_e_depois(conn):
    domingo = _prazo(conn, "1", fatal="2026-11-15")
    segunda = _prazo(conn, "2", fatal="2026-11-16")
    g = prazos_tela.lista(conn, HOJE)
    assert _ids(g["semana"]) == [domingo] and _ids(g["depois"]) == [segunda]


def test_calendarios_diferentes_a_faixa_e_o_contador_usam_a_mesma_regra(conn):
    prazos_banco.incluir_dia(conn, "2026-11-16", "TJMG", "", "sem_expediente", "Dia de teste",
                             "Portaria W", origem="ato")
    outro = "00022345620248130024"
    conn.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua) "
                 "VALUES (?, 'TJMG', '1ª Vara Cível de Belo Horizonte', 1)", (outro,))
    sexta = date(2026, 11, 13)
    _prazo(conn, "1", fatal="2026-11-17")
    _prazo(conn, "2", fatal="2026-11-17", numero=outro)
    assert prazos_tela.faixa(conn, sexta)["texto"] == "2 prazos vencem até 17/11"
    assert prazos_tela.contador_nav(conn, sexta) == 2


def _sem_suspensao_de_fim_de_ano(conn):
    pid = _publicacao(conn, dias=5, ato="Réplica", dispo="2026-12-18")
    prazos_banco.corrigir(conn, pid, dias=5, unidade="corridos")
    return prazos_tela.detalhe(conn, pid, HOJE)


def test_data_sem_a_suspensao_diz_o_que_e_e_o_que_ficou_de_fora(conn):
    d = _sem_suspensao_de_fim_de_ano(conn)
    assert d["com_suspensao"] is True
    assert d["fatal_sem_suspensao_rotulo"] == (
        f"Sem a suspensão de fim de ano: {d['fatal_sem_suspensao_br']}. O recesso e os feriados "
        "continuam fora desta conta; em caso de réu preso, Maria da Penha, urgência ou "
        "alimentos, confira.")


def test_lados_sem_a_suspensao_pela_data_e_nao_pela_ordem():
    f = prazos_tela._lados_sem_suspensao
    # a vista é a maior (26/11); sem a suspensão a outra conta passou a ser a maior
    assert f("2026-11-26", "2026-11-17", "2026-11-17", "2026-11-25") == ("2026-11-25", "2026-11-17")
    assert f("2026-11-17", "2026-11-26", "2026-11-17", "2026-11-25") == ("2026-11-17", "2026-11-25")
    assert f("2026-11-26", "2026-11-17", "2026-11-17", "2026-11-26") == ("2026-11-26", "2026-11-17")
    assert f("2026-11-17", "2026-11-26", "2026-11-17", None) == ("2026-11-17", "2026-11-17")
    assert f("2026-11-17", None, "2026-11-16", None) == ("2026-11-16", None)


def test_data_ilegivel_nao_derruba_a_tela(conn):
    bom = _prazo(conn, "1", fatal="2026-11-13")
    conf = _prazo(conn, "2", fatal="2026-11-12", conferir=False)
    ruim = _prazo(conn, "3", fatal="2026-11-14")
    sem = prazos_banco.abrir(conn, A, None, "publicacao", "4", "05/11/2026", estado="a_definir",
                             motivo="Sem texto.")
    cartao = _demanda(conn)
    conn.execute("UPDATE prazo SET fatal = '14/11/2026', trabalho = '14/11/2026', demanda_id = ?, "
                 "ciencia = 'ontem' WHERE id = ?", (cartao, ruim))
    g = prazos_tela.lista(conn, HOJE)
    assert _ids(g["semana"]) == [bom] and _ids(g["a_conferir"]) == [conf]
    assert sorted(_ids(g["a_definir"])) == sorted([ruim, sem])
    linha = [l for l in g["a_definir"] if l["id"] == ruim][0]
    assert linha["fatal_br"] == "14/11/2026" and linha["motivo"]
    d = prazos_tela.detalhe(conn, sem, HOJE)
    assert d["disponibilizacao_br"] == "05/11/2026"
    d = prazos_tela.detalhe(conn, ruim, HOJE)
    assert d["ciencia_br"] == "ontem" and d["opcoes_d"] == []
    assert prazos_tela.grade(conn, HOJE, HOJE, "mes")["dias"]
    assert "2 sem data" in prazos_tela.faixa(conn, HOJE)["texto"]  # a ilegível e a que espera o Lex
    assert prazos_tela.contador_nav(conn, HOJE) >= 1
    assert prazos_tela.selo_do_cartao(conn, cartao, HOJE) == {
        "texto": "Prazo com data ilegível", "tom": "alerta"}
    assert "próximo 12/11" in prazos_tela.aviso_de_arquivar(conn, [A], HOJE)
    assert prazos_tela._br("x" * 80) == "data ilegível"


def test_opcao_marcada_e_a_que_bate_com_o_trabalho_gravado(conn):
    pid = _prazo(conn, "1", fatal="2026-11-30")
    assert [o["escolhido"] for o in prazos_tela.detalhe(conn, pid, HOJE)["opcoes_d"]] == [
        True, False, False, False]
    prazos_banco.definir_d_padrao(conn, 2)  # só vale para o que ainda não tem escolha
    assert [o["escolhido"] for o in prazos_tela.detalhe(conn, pid, HOJE)["opcoes_d"]] == [
        True, False, False, False]
    prazos_banco.escolher_trabalho(conn, pid, d=3)
    assert [o["escolhido"] for o in prazos_tela.detalhe(conn, pid, HOJE)["opcoes_d"]] == [
        False, False, False, True]


def test_aviso_de_arquivar_diz_o_proximo_o_atrasado_e_o_sem_data(conn):
    _prazo(conn, "1", fatal="2026-09-15")
    _prazo(conn, "2", fatal="2026-11-20")
    prazos_banco.abrir(conn, A, None, "publicacao", "3", "2026-11-05", estado="a_definir",
                       motivo="Sem texto.")
    assert prazos_tela.aviso_de_arquivar(conn, [A], HOJE) == (
        "3 prazos abertos: próximo 20/11; 1 atrasado desde 15/09; 1 sem data. "
        "Continuam na aba Prazos.")


def test_aviso_de_arquivar_so_atrasado_e_so_sem_data(conn):
    _prazo(conn, "1", fatal="2026-09-15")
    assert prazos_tela.aviso_de_arquivar(conn, [A], HOJE) == (
        "1 prazo aberto: 1 atrasado desde 15/09. Continuam na aba Prazos.")
    conn.execute("UPDATE prazo SET estado = 'cumprido'")
    prazos_banco.abrir(conn, A, None, "publicacao", "3", "2026-11-05", estado="a_definir",
                       motivo="Sem texto.")
    assert prazos_tela.aviso_de_arquivar(conn, [A], HOJE) == (
        "1 prazo aberto: 1 sem data. Continuam na aba Prazos.")
    _descartar_processo(conn)  # não é meu: nada espera ninguém
    assert prazos_tela.aviso_de_arquivar(conn, [A], HOJE) == ""


def test_grade_mostra_o_dia_de_trabalho_mesmo_com_a_fatal_fora_da_vista(conn):
    pid = _prazo(conn, "1", fatal="2026-11-30")
    prazos_banco.escolher_trabalho(conn, pid, d=3)
    g = prazos_tela.grade(conn, date(2026, 11, 25), HOJE, "semana")
    achados = [(d["data"], i["tipo"]) for d in g["dias"] for i in d["itens"]]
    assert achados == [("2026-11-25", "trabalho")]


def test_selo_mostra_a_data_que_vence_primeiro(conn):
    cartao = _demanda(conn)
    _prazo(conn, "1", fatal="2026-11-30", demanda=cartao)
    _prazo(conn, "2", fatal="2026-11-20", demanda=cartao)
    assert prazos_tela.selo_do_cartao(conn, cartao, HOJE)["texto"].startswith("Fatal 20/11")


def test_o_calendario_do_prazo_e_o_do_orgao_da_publicacao(conn):
    belo = "00032345620248130024"
    conn.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua) "
                 "VALUES (?, 'TJMG', '2ª Vara Cível de Belo Horizonte', 1)", (belo,))
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto, "
                 "orgao) VALUES (70, ?, 'TJMG', '2026-06-01', 'x', 'Vara Única de Uberlândia')",
                 (belo,))
    pid = prazos_banco.abrir(conn, belo, None, "publicacao", "70", "2026-06-01")
    prazos_banco.corrigir(conn, pid, ato="Réplica", unidade="horas", fatal_informada="2026-06-05")
    prazos_banco.conferir(conn, pid)
    terca = date(2026, 6, 2)
    # 04/06 é feriado em Belo Horizonte, mas a publicação é de Uberlândia: 05/06 é o terceiro
    # dia útil depois de terça, e ainda não urgente
    assert prazos_tela.lista(conn, terca)["semana"][0]["urgente"] is False
    conn.execute("UPDATE publicacao SET orgao = '' WHERE id = 70")
    assert prazos_tela.lista(conn, terca)["semana"][0]["urgente"] is True


def test_dias_do_texto_nao_mostram_artigo_de_lei_velho(conn):
    pid = _publicacao(conn)
    conn.execute("UPDATE prazo SET codigo_ato = 'contestacao', origem_dos_dias = 'texto' "
                 "WHERE id = ?", (pid,))
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert d["ato_da_lei"] == "" and d["artigo_da_lei"] == ""


def test_primeiro_preenchimento_a_mao_diz_que_foi_informado_por_voce(conn):
    pid = _publicacao(conn)
    assert prazos_banco.obter(conn, pid)["origem_dos_dias"] == ""
    assert "você" in prazos_tela.detalhe(conn, pid, HOJE)["origem_rotulo"]
    sem_dias = prazos_banco.abrir(conn, A, None, "publicacao", "9", "2026-11-05")
    assert prazos_tela.detalhe(conn, sem_dias, HOJE)["origem_rotulo"] == ""


def test_data_digitada_nao_leva_o_artigo_da_lista(conn):
    pid = _publicacao(conn)
    prazos_banco.corrigir(conn, pid, codigo="contestacao")
    assert prazos_tela.detalhe(conn, pid, HOJE)["artigo_da_lei"]
    prazos_banco.corrigir(conn, pid, fatal_informada="2026-12-01")
    d = prazos_tela.detalhe(conn, pid, HOJE)
    assert d["artigo_da_lei"] == "" and d["ato_da_lei"] == ""
    assert "digitada" in d["origem_rotulo"] or "informada" in d["origem_rotulo"]


@pytest.mark.parametrize("link, esperado", [
    ("https://tj.example/p?x=1", "https://tj.example/p?x=1"), ("  HTTP://tj.example/a ", "HTTP://tj.example/a"),
    ("javascript:alert(1)", ""), ("java\tscript:alert(1)", ""), ("data:text/html,x", ""),
    ("//tj.example/a", ""), ("", ""), (None, "")])
def test_so_link_http_ou_https_vira_link(conn, link, esperado):
    pid = _publicacao(conn)
    conn.execute("UPDATE publicacao SET link = ? WHERE id = 1", (link,))
    assert prazos_tela.detalhe(conn, pid, HOJE)["publicacao_link"] == esperado


def test_dias_do_calendario_diz_se_o_dia_esta_ativo(conn):
    dia, _ = prazos_banco.incluir_dia(conn, "2027-02-08", "TJMT", "", "sem_expediente",
                                      "Carnaval", "Portaria Y", origem="ato")
    achado = lambda: [l for l in prazos_tela.dias_do_calendario(conn, "TJMT", 2027)
                      if l["id"] == dia][0]
    assert achado()["ativo"] is True
    prazos_banco.desativar_dia(conn, dia)
    assert achado()["ativo"] is False


# --- o que a Task 11 pede aos dados das telas -------------------------------------------


def test_linha_diz_se_a_data_se_le_e_nomeia_a_intimacao_sem_ato(conn):
    pid = _prazo(conn, "1", fatal="2026-11-13")
    conn.execute("UPDATE prazo SET fatal = '13/11/2026' WHERE id = ?", (pid,))
    ruim = [l for l in prazos_tela.lista(conn, HOJE)["a_definir"] if l["id"] == pid][0]
    assert ruim["fatal_legivel"] is False
    bom = _prazo(conn, "2", fatal="2026-11-13")
    assert [l for l in prazos_tela.lista(conn, HOJE)["semana"] if l["id"] == bom][0]["fatal_legivel"]
    aviso = _aviso_pje(conn, _demanda(conn))
    assert prazos_tela.detalhe(conn, aviso, HOJE)["ato"] == "Intimação no PJe"
    assert prazos_tela.detalhe(conn, aviso, HOJE)["ato_gravado"] == ""


def test_dias_do_calendario_traz_a_origem_em_palavras(conn):
    dias = {l["origem"]: l["origem_rotulo"] for l in prazos_tela.dias_do_calendario(conn, "TJMT", 2026)}
    assert dias["lei"] == "Lei" and dias["ato"] == "Ato do tribunal"
    dia, _ = prazos_banco.incluir_dia(conn, "2026-12-01", "TJMT", "", "sem_expediente", "X", "Y")
    assert [l for l in prazos_tela.dias_do_calendario(conn, "TJMT", 2026)
            if l["id"] == dia][0]["origem_rotulo"] == "Anotação sua"


# --- rodada 1 de correções da Task 11 (M3: cada número da faixa é um grupo da aba) ----------


def test_a_faixa_soma_o_que_a_aba_mostra_em_cada_grupo(conn):
    outro = "00099999920248110041"
    conn.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua, "
                 "descartado_em) VALUES (?, 'TJMT', 'x', 0, '2026-11-01')", (outro,))
    _prazo(conn, "1", fatal="2026-11-10")                                  # conferido, vence hoje
    _prazo(conn, "2", fatal="2026-11-25")                                  # conferido, longe
    _prazo(conn, "3", fatal="2026-11-05")                                  # conferido, atrasado
    _prazo(conn, "4", fatal="2026-11-11", conferir=False)                  # a conferir, urgente
    _prazo(conn, "5", fatal="2026-11-27", conferir=False)                  # a conferir
    _prazo(conn, "6", fatal="2027-03-01", conferir=False)                  # a conferir, mês longe
    _prazo(conn, "7", fatal="2026-11-03", conferir=False)                  # vencido a conferir
    prazos_banco.abrir(conn, A, None, "publicacao", "8", "2026-11-05")      # espera o Lex
    ruim = _prazo(conn, "9", fatal="2026-11-14")
    conn.execute("UPDATE prazo SET fatal = '14/11/2026' WHERE id = ?", (ruim,))  # data ilegível
    prazos_banco.abrir(conn, outro, None, "publicacao", "10", "2026-11-05")  # «Não é meu»: fora
    cartao = _demanda(conn)
    _aviso_pje(conn, cartao, ref="1grau:7", envio="2026-11-05")             # coberto: fora
    conn.execute("UPDATE prazo SET pareado_com = 1 WHERE origem_ref = '1grau:7'")
    texto = prazos_tela.faixa(conn, HOJE)["texto"]
    assert texto == ("1 prazo vence hoje · 1 a conferir com data urgente · 2 a conferir · "
                     "2 sem data · 1 atrasado · 1 vencido a conferir")
    g = prazos_tela.lista(conn, HOJE)
    assert len(g["a_conferir_proximos"]) == 3
    assert (len(g["hoje"]), len(g["atrasados"]), len(g["a_conferir_vencidos"]),
            len(g["a_definir"])) == (1, 1, 1, 2)
    urgentes = [l for l in g["a_conferir_proximos"] if l["urgente"]]
    assert len(urgentes) == 1 and len(g["a_conferir_proximos"]) - len(urgentes) == 2


def test_a_conferir_de_um_mes_adiante_nao_e_data_urgente(conn):
    _prazo(conn, "1", fatal="2026-12-20", conferir=False)
    assert prazos_tela.faixa(conn, HOJE)["texto"] == "1 a conferir"


# --- o calendário em linhas, com os dias seguidos juntos (só na tela) --------------------


def _dia(data, **campos):
    base = {"id": int(data.replace("-", "")), "data": data, "data_br": data[8:] + "/" + data[5:7]
            + "/" + data[:4], "tribunal": "", "comarca": "", "tipo": "suspensao",
            "descricao": "Prazos suspensos no fim de ano", "fonte": "CPC", "origem": "lei",
            "ativo": True}
    return {**base, **campos}


def test_dias_seguidos_iguais_viram_uma_linha_de_um_dia_a_outro():
    dias = [_dia(f"2026-12-{d}") for d in range(20, 32)] + [_dia("2027-01-01"), _dia("2027-01-02")]
    linhas = prazos_tela.agrupar_dias(dias)
    assert len(linhas) == 1 and linhas[0]["grupo"] is True
    assert (linhas[0]["de_br"], linhas[0]["ate_br"]) == ("20/12/2026", "02/01/2027")
    assert [d["data"] for d in linhas[0]["dias"]] == [d["data"] for d in dias]


def test_dia_sozinho_fica_como_esta():
    linhas = prazos_tela.agrupar_dias([_dia("2026-11-02", descricao="Finados")])
    assert len(linhas) == 1 and linhas[0]["grupo"] is False
    assert linhas[0]["de_br"] == linhas[0]["ate_br"] == "02/11/2026"


@pytest.mark.parametrize("campo,valor", [
    ("tribunal", "TJMT"), ("comarca", "Cuiabá"), ("tipo", "sem_expediente"),
    ("descricao", "Outra coisa"), ("fonte", "Portaria"), ("origem", "ato")])
def test_qualquer_diferenca_separa_os_dias(campo, valor):
    dias = [_dia("2026-12-20"), _dia("2026-12-21", **{campo: valor}), _dia("2026-12-22")]
    assert [l["grupo"] for l in prazos_tela.agrupar_dias(dias)] == [False, False, False]


def test_falta_de_um_dia_separa_o_grupo():
    dias = [_dia("2026-12-20"), _dia("2026-12-21"), _dia("2026-12-23"), _dia("2026-12-24")]
    linhas = prazos_tela.agrupar_dias(dias)
    assert [(l["de_br"], l["ate_br"]) for l in linhas] == [
        ("20/12/2026", "21/12/2026"), ("23/12/2026", "24/12/2026")]


def test_dia_retirado_quebra_o_grupo_e_fica_na_linha_dele():
    dias = [_dia("2026-12-20"), _dia("2026-12-21", ativo=False), _dia("2026-12-22"), _dia("2026-12-23")]
    linhas = prazos_tela.agrupar_dias(dias)
    assert [(l["de_br"], l["ate_br"], l["grupo"]) for l in linhas] == [
        ("20/12/2026", "20/12/2026", False), ("21/12/2026", "21/12/2026", False),
        ("22/12/2026", "23/12/2026", True)]
    assert linhas[1]["dias"][0]["ativo"] is False


def test_o_que_esta_no_meio_nao_atrapalha_a_ordem_nem_quebra_o_grupo():
    """A tabela vem ordenada por data: outro dia na mesma data fica entre os do grupo."""
    dias = [_dia("2026-12-24", tribunal="TJMT", tipo="sem_expediente", descricao="Recesso"),
            _dia("2026-12-24"), _dia("2026-12-25"), _dia("2026-12-26")]
    linhas = prazos_tela.agrupar_dias(dias)
    assert [(l["grupo"], l["de_br"]) for l in linhas] == [(False, "24/12/2026"),
                                                          (True, "24/12/2026")]
    assert len(linhas[1]["dias"]) == 3


def test_agrupar_nao_perde_nem_repete_nenhum_dia_do_calendario_de_verdade(conn):
    dias = prazos_tela.dias_do_calendario(conn, "TJMT", 2026)
    linhas = prazos_tela.agrupar_dias(dias)
    assert sorted(d["id"] for l in linhas for d in l["dias"]) == sorted(d["id"] for d in dias)
    assert len(linhas) < len(dias) and max(len(l["dias"]) for l in linhas) >= 10
