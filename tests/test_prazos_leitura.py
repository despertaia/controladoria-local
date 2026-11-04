"""A fila de leituras no banco: reserva, resposta, falhas, sem Lex."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from nucleo import banco, eventos, prazos, prazos_banco

NUMERO = "00012345620248110041"
AGORA = datetime.fromisoformat("2026-11-06T10:00:00-04:00")
TEXTO = ("Fica a parte autora intimada para, no prazo de 15 (quinze) dias, apresentar réplica. "
         "No mesmo ato, deverá, em 5 (cinco) dias, juntar documentos.")


@pytest.fixture
def conn(tmp_path):
    c = banco.conectar(str(tmp_path / "b.db"))
    c.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, advogado_atua) "
              "VALUES (?, 'TJMT', '1ª Vara Cível de Cuiabá', 1)", (NUMERO,))
    c.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
              "VALUES (1, ?, 'TJMT', '2026-11-05', ?)", (NUMERO, TEXTO))
    yield c
    c.close()


def _aberto(conn):
    return prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")


def _replica(**campos):
    return {"ato": "Réplica", "dias": 15, "unidade": "uteis", "origem_dos_dias": "texto",
            "rito": "civel", "trecho": "no prazo de 15 (quinze) dias, apresentar réplica",
            "fixado_pelo_juiz": False, "ressalvas": [], "natureza": "processual",
            "inicio_no_diario": True, "processo_suspenso": False, **campos}


def test_reserva_a_mais_antiga_e_nao_entrega_duas_vezes(conn):
    pid = _aberto(conn)
    pacote = prazos_banco.proxima_leitura(conn, AGORA)
    assert pacote["id"] == pid and TEXTO in pacote["pedido"]
    assert prazos_banco.proxima_leitura(conn, AGORA) is None
    # a reserva vence: volta para a fila
    assert prazos_banco.proxima_leitura(conn, AGORA + timedelta(minutes=11))["id"] == pid


def test_sem_nada_na_fila(conn):
    assert prazos_banco.proxima_leitura(conn, AGORA) is None


def test_publicacao_sem_texto_vira_a_definir_sem_gastar_o_lex(conn):
    conn.execute("UPDATE publicacao SET texto = '   '")
    pid = _aberto(conn)
    assert prazos_banco.proxima_leitura(conn, AGORA) is None
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and "sem texto" in p["motivo"]


def test_resposta_certa_calcula_e_fica_a_conferir(conn):
    pid = _aberto(conn)
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]})) == "a_conferir"
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_conferir" and p["fatal"] == "2026-11-30"
    assert p["ato"] == "Réplica" and p["trecho"] and p["conferido_em"] is None
    assert eventos.listar(conn, NUMERO)[-1]["tipo"] == "prazo_lido"


def test_dois_prazos_na_mesma_publicacao_viram_duas_linhas(conn):
    pid = _aberto(conn)
    segundo = _replica(ato="Juntar documentos", dias=5,
                       trecho="deverá, em 5 (cinco) dias, juntar documentos")
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(), segundo]}))
    linhas = conn.execute("SELECT ordem, ato, fatal, estado FROM prazo ORDER BY ordem").fetchall()
    assert [tuple(l) for l in linhas] == [(0, "Réplica", "2026-11-30", "a_conferir"),
                                          (1, "Juntar documentos", "2026-11-13", "a_conferir")]


def test_sem_prazo(conn):
    conn.execute("UPDATE publicacao SET texto = 'Pauta de julgamento publicada para ciência geral.'")
    pid = _aberto(conn)
    assert prazos_banco.aplicar_leitura(conn, pid, '{"sem_prazo": true}') == "sem_prazo"
    assert prazos_banco.obter(conn, pid)["estado"] == "sem_prazo"


def test_json_invalido_vira_a_definir_com_o_motivo(conn):
    pid = _aberto(conn)
    assert prazos_banco.aplicar_leitura(conn, pid, "não é json") == "a_definir"
    p = prazos_banco.obter(conn, pid)
    assert "fora do formato" in p["motivo"] and p["fatal"] is None


def test_trecho_inventado_vira_a_definir(conn):
    pid = _aberto(conn)
    item = _replica(trecho="no prazo de 30 (trinta) dias, apresentar réplica", dias=30)
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [item]}))
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and "não existe" in p["motivo"]


def test_tribunal_superior_fica_fora_da_conta(conn):
    conn.execute("UPDATE publicacao SET tribunal = 'STJ'")
    pid = _aberto(conn)
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}))
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and "superiores" in p["motivo"]


def test_resposta_tardia_nao_passa_por_cima_do_que_o_advogado_fez(conn):
    pid = _aberto(conn)
    prazos_banco.corrigir(conn, pid, ato="X", dias=5, unidade="uteis")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}))
    assert prazos_banco.obter(conn, pid)["ato"] == "X"


def test_tres_falhas_seguidas_viram_a_definir(conn):
    pid = _aberto(conn)
    for _ in range(2):
        prazos_banco.falhar_leitura(conn, pid, "erro")
        assert prazos_banco.obter(conn, pid)["estado"] == "aguardando_lex"
        assert prazos_banco.obter(conn, pid)["leitura_reservada_ate"] is None
    prazos_banco.falhar_leitura(conn, pid, "tempo")
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and p["tentativas"] == 3 and "3 tentativas" in p["motivo"]


@pytest.mark.parametrize("motivo", ["limite", "acesso"])
def test_lex_fora_do_ar_nao_gasta_tentativa(conn, motivo):
    pid = _aberto(conn)
    prazos_banco.proxima_leitura(conn, AGORA)
    prazos_banco.falhar_leitura(conn, pid, motivo)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "aguardando_lex" and p["tentativas"] == 0
    assert p["leitura_reservada_ate"] is None


def test_falha_com_motivo_desconhecido(conn):
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.falhar_leitura(conn, _aberto(conn), "qualquer")


def test_sem_lex_o_prazo_espera_e_pode_ser_preenchido_a_mao(conn):
    pid = _aberto(conn)
    assert prazos_banco.obter(conn, pid)["estado"] == "aguardando_lex"
    prazos_banco.corrigir(conn, pid, ato="Réplica", dias=15, unidade="uteis")
    assert prazos_banco.obter(conn, pid)["fatal"] == "2026-11-30"
    assert prazos_banco.proxima_leitura(conn, AGORA) is None


def test_ler_de_novo_volta_para_a_fila_e_reaproveita_as_linhas(conn):
    pid = _aberto(conn)
    segundo = _replica(ato="Juntar documentos", dias=5,
                       trecho="deverá, em 5 (cinco) dias, juntar documentos")
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(), segundo]}))
    prazos_banco.ler_de_novo(conn, pid)
    linhas = conn.execute("SELECT ordem, estado FROM prazo ORDER BY ordem").fetchall()
    assert [tuple(l) for l in linhas] == [(0, "aguardando_lex"), (1, "descartado")]
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(), segundo]}))
    linhas = conn.execute("SELECT ordem, estado, ato FROM prazo ORDER BY ordem").fetchall()
    assert [tuple(l) for l in linhas] == [(0, "a_conferir", "Réplica"),
                                          (1, "a_conferir", "Juntar documentos")]


def test_ler_de_novo_recusa_o_conferido(conn):
    pid = _aberto(conn)
    segundo = _replica(ato="Juntar documentos", dias=5,
                       trecho="deverá, em 5 (cinco) dias, juntar documentos")
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(), segundo]}))
    outro = conn.execute("SELECT id FROM prazo WHERE ordem = 1").fetchone()[0]
    prazos_banco.conferir(conn, pid)
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.ler_de_novo(conn, pid)
    with pytest.raises(prazos_banco.PrazoInvalido):  # nem por outro prazo da mesma publicação
        prazos_banco.ler_de_novo(conn, outro)
    assert prazos_banco.obter(conn, pid)["estado"] == "conferido"


def test_ler_de_novo_recusa_se_o_conferido_e_o_segundo_prazo(conn):
    pid = _aberto(conn)
    segundo = _replica(ato="Juntar documentos", dias=5,
                       trecho="deverá, em 5 (cinco) dias, juntar documentos")
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(), segundo]}))
    outro = conn.execute("SELECT id FROM prazo WHERE ordem = 1").fetchone()[0]
    prazos_banco.conferir(conn, outro)
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.ler_de_novo(conn, pid)
    assert prazos_banco.obter(conn, pid)["estado"] == "a_conferir"



# --- rodada de correção 1 ------------------------------------------------------------------

def _duas(conn):
    pid = _aberto(conn)
    segundo = _replica(ato="Juntar documentos", dias=5,
                       trecho="deverá, em 5 (cinco) dias, juntar documentos")
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(), segundo]}))
    return pid, conn.execute("SELECT id FROM prazo WHERE ordem = 1").fetchone()[0], segundo


def test_i1_ler_de_novo_zera_o_que_o_advogado_pos_em_todas_as_linhas(conn):
    pid, outro, segundo = _duas(conn)
    prazos_banco.corrigir(conn, pid, ciencia="2026-11-20", hoje=date(2026, 12, 1))
    prazos_banco.corrigir(conn, outro, fatal_informada="2027-03-01")
    prazos_banco.alternar_dobro(conn, pid, True)
    prazos_banco.ler_de_novo(conn, pid)
    for r in conn.execute("SELECT * FROM prazo").fetchall():
        for campo in ("ciencia", "inicio_informado", "fatal_informada", "fatal", "trabalho",
                      "d_escolhido", "pareado_com"):
            assert r[campo] is None, campo
        assert r["dobro"] == 0 and r["fatal_maior"] == 0 and r["ato"] == "" and r["dias"] is None
        assert r["unidade"] == "" and r["trecho"] == "" and r["alerta"] == ""
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(), segundo]}))
    for r in conn.execute("SELECT * FROM prazo ORDER BY ordem").fetchall():
        assert r["ciencia"] is None and r["fatal_informada"] is None and r["dobro"] == 0
    assert prazos_banco.obter(conn, outro)["fatal"] == "2026-11-13"


def test_i1_ler_de_novo_registra_o_que_havia_em_cada_linha(conn):
    pid, outro, _ = _duas(conn)
    prazos_banco.corrigir(conn, outro, fatal_informada="2027-03-01")
    prazos_banco.ler_de_novo(conn, pid)
    evs = [e for e in eventos.listar(conn, NUMERO) if e["tipo"] == "prazo_ler_de_novo"]
    assert sorted(e["dados"]["prazo"] for e in evs) == sorted([pid, outro])
    antes = {e["dados"]["prazo"]: e["dados"]["antes"] for e in evs}
    assert antes[outro]["fatal_informada"] == "2027-03-01" and antes[pid]["ato"] == "Réplica"


def test_i2_numero_recusado_nao_fica_no_banco(conn):
    pid = _aberto(conn)
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica(dias=30)]}))
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and p["dias"] is None and p["unidade"] == ""
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.corrigir(conn, pid, ato="Réplica")


def test_i2_fora_da_conta_tambem_nao_guarda_o_numero(conn):
    conn.execute("UPDATE publicacao SET tribunal = 'STJ'")
    pid = _aberto(conn)
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}))
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and p["dias"] is None and p["unidade"] == ""


@pytest.mark.parametrize("disp", ["", None, "xx", "9999-12-30"])
def test_i3_disponibilizacao_ruim_vira_a_definir(conn, disp):
    pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", disp)
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}))
    assert prazos_banco.obter(conn, pid)["estado"] == "a_definir"


@pytest.mark.parametrize("bruta", [
    json.dumps({"prazos": [{**{"ato": "A", "unidade": "uteis", "origem_dos_dias": "texto",
                              "trecho": "no prazo de 15 (quinze) dias, apresentar réplica",
                              "natureza": "processual", "inicio_no_diario": True,
                              "processo_suspenso": False}, "dias": 2 ** 70}]}),
    '{"prazos":[{"ato":"\\ud800"}]}', "[" * 200000,
], ids=["dias-gigante", "surrogate", "colchetes-200k"])
def test_i3_veneno_na_resposta_vira_a_definir(conn, bruta):
    pid = _aberto(conn)
    prazos_banco.aplicar_leitura(conn, pid, bruta)
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and p["leitura_reservada_ate"] is None


def test_i3_origem_ref_que_nao_e_numero_nao_trava_a_fila(conn):
    prazos_banco.abrir(conn, NUMERO, None, "publicacao", "abc", "2026-11-04")
    bom = _aberto(conn)
    assert prazos_banco.proxima_leitura(conn, AGORA)["id"] == bom
    ruim = conn.execute("SELECT estado, motivo FROM prazo WHERE origem_ref = 'abc'").fetchone()
    assert ruim["estado"] == "a_definir" and "sem texto" in ruim["motivo"]


def test_i9_sem_texto_e_texto_longo_deixam_evento(conn):
    conn.execute("UPDATE publicacao SET texto = '   '")
    pid = _aberto(conn)
    assert prazos_banco.proxima_leitura(conn, AGORA) is None
    ev = [e for e in eventos.listar(conn, NUMERO) if e["tipo"] == "prazo_lido"]
    assert ev and ev[-1]["dados"]["prazo"] == pid and ev[-1]["dados"]["estado"] == "a_definir"
    conn.execute("UPDATE publicacao SET texto = ?", ("x" * 70_000,))
    conn.execute("UPDATE prazo SET estado = 'aguardando_lex', motivo = ''")
    assert prazos_banco.proxima_leitura(conn, AGORA) is None
    assert "longa demais" in prazos_banco.obter(conn, pid)["motivo"]
    assert len([e for e in eventos.listar(conn, NUMERO) if e["tipo"] == "prazo_lido"]) == 2


def test_i9_prazo_lido_registra_o_estado_de_cada_linha(conn):
    pid, outro, _ = _duas(conn)
    evs = [e["dados"] for e in eventos.listar(conn, NUMERO) if e["tipo"] == "prazo_lido"]
    assert {(d["prazo"], d["estado"]) for d in evs} == {(pid, "a_conferir"), (outro, "a_conferir")}
    assert "Réplica" not in json.dumps(evs)


def test_aplicar_leitura_recusa_prazo_de_aviso(conn):
    pid = prazos_banco.abrir(conn, NUMERO, None, "aviso", "1", "2026-11-05")
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}))


def test_reserva_e_atomica_e_compara_em_utc(conn):
    pid = _aberto(conn)
    primeiro = prazos_banco.proxima_leitura(conn, AGORA)
    # a mesma hora, vista em outro fuso: a reserva continua valendo
    mesma_hora = AGORA.astimezone(timezone(timedelta(hours=1)))
    assert prazos_banco.proxima_leitura(conn, mesma_hora) is None
    depois = datetime.fromisoformat("2026-11-06T14:11:00+00:00")  # 10h11 em Cuiabá
    assert prazos_banco.proxima_leitura(conn, depois)["id"] == pid
    assert primeiro["reserva"] != prazos_banco.obter(conn, pid)["leitura_reservada_ate"]


def test_falha_atrasada_de_leitor_vencido_nao_gasta_a_tentativa_do_novo(conn):
    pid = _aberto(conn)
    velho = prazos_banco.proxima_leitura(conn, AGORA)
    novo = prazos_banco.proxima_leitura(conn, AGORA + timedelta(minutes=11))
    prazos_banco.falhar_leitura(conn, pid, "tempo", reserva=velho["reserva"])
    p = prazos_banco.obter(conn, pid)
    assert p["tentativas"] == 0 and p["leitura_reservada_ate"] == novo["reserva"]
    prazos_banco.falhar_leitura(conn, pid, "tempo", reserva=novo["reserva"])
    assert prazos_banco.obter(conn, pid)["tentativas"] == 1


def test_r3_corridos_do_texto_vira_corridos_na_conta(conn):
    conn.execute("UPDATE publicacao SET texto = ?",
                 ("Fica intimada para, no prazo de 10 (dez) dias corridos, pagar a multa.",))
    pid = _aberto(conn)
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [
        _replica(dias=10, trecho="no prazo de 10 (dez) dias")]}))
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_conferir" and p["unidade"] == "corridos"
    assert p["fatal"] == prazos.contar(date(2026, 11, 5), 10, "corridos",
                                       prazos_banco.calendario_do_prazo(conn, p)).fatal.isoformat()


def test_r7_sem_prazo_de_texto_vazio_ou_sentenca_vira_a_definir(conn):
    for texto in ("", "<script>x</script>", "JULGO PROCEDENTE o pedido. Condeno o réu. P.R.I."):
        conn.execute("UPDATE publicacao SET texto = ?", (texto,))
        conn.execute("DELETE FROM prazo")
        pid = _aberto(conn)
        assert prazos_banco.aplicar_leitura(conn, pid, '{"sem_prazo": true}') == "a_definir"


def test_r12_recusado_nao_fica_com_lista_e_corrigir_pede_a_unidade(conn):
    conn.execute("UPDATE publicacao SET texto = 'Em 5 dias, conteste.'")
    pid = _aberto(conn)
    item = _replica(dias=None, origem_dos_dias="lei", codigo_ato="contestacao",
                    trecho="Em 5 dias, conteste")
    prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [item]}))
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "a_definir" and p["codigo_ato"] == "" and p["origem_dos_dias"] == ""
    with pytest.raises(prazos_banco.PrazoInvalido) as exc:
        prazos_banco.corrigir(conn, pid, dias=5)
    assert "úteis ou corridos" in str(exc.value)


def test_s4_excecao_na_conferencia_vira_a_definir_com_evento(conn, monkeypatch):
    from nucleo import leitura

    def estoura(*a, **k):
        raise RuntimeError("inesperado")
    monkeypatch.setattr(leitura, "validar_resposta", estoura)
    pid = _aberto(conn)
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]})) == "a_definir"
    p = prazos_banco.obter(conn, pid)
    assert "não pôde ser conferida" in p["motivo"]
    assert eventos.listar(conn, NUMERO)[-1]["tipo"] == "prazo_lido"


# --- onda final: o tribunal da própria publicação decide -------------------------------

OUTROS = [("STJ", "superiores"), ("TSE", "eleitoral"), ("TRE-MT", "eleitoral")]
FERIADO_SO_DO_TJMT = "2026-11-10"


def _feriado_do_tjmt(conn):
    conn.execute("INSERT INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem) "
                 "VALUES (?, 'TJMT', '', 'sem_expediente', 'Só do TJMT', 'teste', 'manual')",
                 (FERIADO_SO_DO_TJMT,))


@pytest.mark.parametrize("orgao", ["", "Turma de outro tribunal"], ids=["sem-orgao", "com-orgao"])
@pytest.mark.parametrize("sigla, trecho", OUTROS)
def test_f2_leitura_de_outro_tribunal_em_processo_do_tjmt_fica_a_definir(conn, sigla, trecho, orgao):
    conn.execute("UPDATE publicacao SET tribunal = ?, orgao = ?", (sigla, orgao))
    pid = _aberto(conn)
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]})) == "a_definir"
    p = prazos_banco.obter(conn, pid)
    assert trecho in p["motivo"] and p["fatal"] is None and p["dias"] is None


@pytest.mark.parametrize("orgao", ["", "Turma de outro tribunal"], ids=["sem-orgao", "com-orgao"])
@pytest.mark.parametrize("sigla, _trecho", OUTROS)
def test_f2_corrigir_conta_pelo_calendario_do_tribunal_da_publicacao(conn, sigla, _trecho, orgao):
    """À mão a conta sai, pois é o advogado quem digitou; mas sem o feriado do TJMT, que levaria a
    uma data mais tarde, e com o aviso de que o calendário daquele tribunal não está cadastrado."""
    _feriado_do_tjmt(conn)
    conn.execute("UPDATE publicacao SET tribunal = ?, orgao = ?", (sigla, orgao))
    pid = _aberto(conn)
    prazos_banco.corrigir(conn, pid, ato="Recurso", dias=15, unidade="uteis")
    p = prazos_banco.obter(conn, pid)
    cal_outro = prazos_banco.carregar_calendario(conn, sigla, orgao)
    cal_tjmt = prazos_banco.carregar_calendario(conn, "TJMT", "")
    assert FERIADO_SO_DO_TJMT not in {d.isoformat() for d in cal_outro.sem_expediente}
    disp = date(2026, 11, 5)
    assert p["estado"] == "a_conferir"
    assert p["fatal"] == prazos.contar(disp, 15, "uteis", cal_outro).fatal.isoformat()
    assert p["fatal"] < prazos.contar(disp, 15, "uteis", cal_tjmt).fatal.isoformat()
    assert f"calendário de 2026 do {sigla} ainda não cadastrado" in p["calculo"]


def test_f2_recontar_e_a_tela_seguem_o_tribunal_da_publicacao(conn):
    _feriado_do_tjmt(conn)
    conn.execute("UPDATE publicacao SET tribunal = 'STJ', orgao = ''")
    pid = _aberto(conn)
    prazos_banco.corrigir(conn, pid, ato="Recurso", dias=15, unidade="uteis")
    p = prazos_banco.obter(conn, pid)
    assert prazos_banco.calendario_do_prazo(conn, p).tribunal == "STJ"
    assert prazos_banco.recontar(conn, p, "x") is False
    assert prazos_banco.obter(conn, pid)["fatal"] == p["fatal"]


def test_f2_publicacao_sem_tribunal_vale_o_do_processo(conn):
    conn.execute("UPDATE publicacao SET tribunal = '', orgao = 'Vara X'")
    pid = _aberto(conn)
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]})) == "a_conferir"
    p = prazos_banco.obter(conn, pid)
    assert prazos_banco.calendario_do_prazo(conn, p).tribunal == "TJMT"


def test_f2_mesmo_tribunal_sem_orgao_usa_o_orgao_do_processo(conn):
    conn.execute("UPDATE publicacao SET tribunal = 'TJMT', orgao = ''")
    assert prazos_banco._tribunal_e_orgao(conn, NUMERO, 1) == ("TJMT", "1ª Vara Cível de Cuiabá")


def test_f2_outro_tribunal_sem_orgao_nao_herda_o_orgao_do_processo(conn):
    conn.execute("UPDATE publicacao SET tribunal = 'TJMG', orgao = ''")
    assert prazos_banco._tribunal_e_orgao(conn, NUMERO, 1) == ("TJMG", "")


# --- onda final: a resposta atrasada não passa por cima -------------------------------

def test_f7_reserva_vencida_e_recusada_quieta(conn):
    pid = _aberto(conn)
    velha = prazos_banco.proxima_leitura(conn, AGORA)["reserva"]
    nova = prazos_banco.proxima_leitura(conn, AGORA + timedelta(minutes=11))["reserva"]
    assert nova != velha
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}),
                                        velha) == "aguardando_lex"
    p = prazos_banco.obter(conn, pid)
    assert p["estado"] == "aguardando_lex" and p["leitura_reservada_ate"] == nova
    assert p["fatal"] is None
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}),
                                        nova) == "a_conferir"


def test_f7_a_reserva_certa_vale(conn):
    pid = _aberto(conn)
    reserva = prazos_banco.proxima_leitura(conn, AGORA)["reserva"]
    assert prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}),
                                        reserva) == "a_conferir"


def test_f7_a_correcao_nao_entra_no_meio_da_leitura(tmp_path, monkeypatch):
    """Com a trava tomada antes da conferência, a correção espera: não há janela em que ela seja
    vista como «ainda não aconteceu» e depois sobrescrita."""
    caminho = str(tmp_path / "b.db")
    c = banco.conectar(caminho)
    with c:
        c.execute("INSERT INTO processo (numero, tribunal, advogado_atua) VALUES (?, 'TJMT', 1)",
                  (NUMERO,))
        c.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                  "VALUES (1, ?, 'TJMT', '2026-11-05', ?)", (NUMERO, TEXTO))
        pid = prazos_banco.abrir(c, NUMERO, None, "publicacao", "1", "2026-11-05")
    outra = banco.conectar(caminho)
    outra.execute("PRAGMA busy_timeout = 50")
    visto = {}
    original = prazos_banco._texto_de

    def no_meio(conexao, p):  # entre a conferência do estado e a gravação
        try:
            with outra:
                prazos_banco.corrigir(outra, pid, ato="Do advogado", dias=5, unidade="uteis")
        except sqlite3.OperationalError as exc:
            visto["erro"] = str(exc)
        return original(conexao, p)
    monkeypatch.setattr(prazos_banco, "_texto_de", no_meio)
    with c:
        prazos_banco.aplicar_leitura(c, pid, json.dumps({"prazos": [_replica()]}))
    assert "locked" in visto["erro"]
    assert prazos_banco.obter(c, pid)["ato"] == "Réplica"
    outra.close()
    c.close()


def test_f7_recusa_nao_deixa_a_trava_presa(conn):
    pid = _aberto(conn)
    prazos_banco.corrigir(conn, pid, ato="Do advogado", dias=5, unidade="uteis")
    conn.commit()
    with pytest.raises(prazos_banco.PrazoInvalido):
        prazos_banco.aplicar_leitura(conn, pid, json.dumps({"prazos": [_replica()]}))
    assert not conn.in_transaction


def test_falhar_leitura_trava_antes_de_olhar_o_estado():
    """Sem transação aberta, a trava de escrita vem antes da leitura da linha."""
    from unittest import mock

    from nucleo import prazos_banco as pb
    conn = mock.MagicMock(in_transaction=False)
    ordem = []
    conn.execute.side_effect = lambda sql, *a: ordem.append(sql)
    with mock.patch.object(pb, "obter", side_effect=lambda *a: ordem.append("obter") or {
            "estado": "descartado"}):
        with pytest.raises(pb.PrazoInvalido):
            pb.falhar_leitura(conn, 1, "erro")
    assert ordem[:2] == ["BEGIN IMMEDIATE", "obter"]
