from datetime import date, datetime

import pytest

from nucleo import banco, painel_dados as pd
from scripts import banco_demo

HOJE = date(2026, 10, 6)


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    banco_demo.gerar(c, HOJE)
    yield c
    c.close()


def test_kpis(conn):
    assert pd.kpis(conn, HOJE) == {
        "ativos": 16, "a_conferir": 5, "movimentaram_7d": 6, "parados_90d": 5,
        "publicacoes_30d": 9, "valor_em_causa": "R$ 3,3 mi"}


def test_por_tribunal(conn):
    assert pd.por_tribunal(conn) == [
        {"tribunal": "TJMT", "quantidade": 12, "percentual": 100},
        {"tribunal": "TRF1", "quantidade": 4, "percentual": 33}]


def test_por_situacao(conn):
    assert pd.por_situacao(conn) == [
        {"rotulo": "Conferidos no PJe", "quantidade": 12, "tom": "ok"},
        {"rotulo": "Só pelo DJEN", "quantidade": 4, "tom": "aviso"},
        {"rotulo": "A conferir", "quantidade": 5, "tom": "neutro"}]


def test_por_situacao_inclui_ativo_sem_acesso(conn):
    conn.execute("UPDATE processo SET erro_sincronizacao = 'Processo em segredo de justiça' "
                 "WHERE numero = ?", (banco_demo._tjmt(1),))
    linhas = pd.por_situacao(conn)
    assert linhas == [
        {"rotulo": "Conferidos no PJe", "quantidade": 11, "tom": "ok"},
        {"rotulo": "Só pelo DJEN", "quantidade": 4, "tom": "aviso"},
        {"rotulo": "Sem acesso pelo PJe", "quantidade": 1, "tom": "alerta"},
        {"rotulo": "A conferir", "quantidade": 5, "tom": "neutro"}]
    assert sum(l["quantidade"] for l in linhas[:-1]) == pd.kpis(conn, HOJE)["ativos"] == 16


def test_ultima_varredura_le_so_eventos_de_varredura():
    from nucleo import eventos
    conn = banco.conectar(":memory:")
    with conn:
        eventos.registrar(conn, "processo_sincronizado", "1", quando="2026-10-06T15:00:00-04:00")
    assert pd.ultima_varredura(conn) is None
    with conn:
        eventos.registrar(conn, "varredura_concluida", None,
                          {"erros": ["PJe (1º grau): tempo esgotado"]},
                          quando="2026-10-06T12:01:00-04:00")
    assert pd.ultima_varredura(conn) == {
        "quando": "06/10/2026 12:01", "erros": ["PJe (1º grau): tempo esgotado"],
        "falhou": False}
    with conn:
        eventos.registrar(conn, "varredura_falhou", None, {"erro": "KeyError: x"},
                          quando="2026-10-06T18:00:00-04:00")
    assert pd.ultima_varredura(conn)["falhou"] is True
    conn.close()


def test_atividade_recente_mais_nova_primeiro(conn):
    itens = pd.atividade_recente(conn, limite=5)
    assert len(itens) == 5
    assert itens[0]["data"] == "20261006" and itens[0]["cliente"] == "MARIA APARECIDA SOUZA"
    assert itens[0]["tipo"] == "andamento"
    assert [i["data"] for i in itens] == sorted((i["data"] for i in itens), reverse=True)


def test_linha_do_tempo_junta_andamentos_e_publicacoes(conn):
    itens = pd.linha_do_tempo(conn, banco_demo._tjmt(1))
    assert [i["tipo"] for i in itens] == ["andamento", "publicacao", "aviso", "andamento"]
    assert itens[0]["data_br"] == "06/10/2026" and itens[0]["hora"] == "10:00"


def test_linhas_da_carteira_e_filtros(conn):
    linhas = pd.linhas_da_carteira(conn, "ativa", HOJE)
    assert len(linhas) == 16
    assert linhas[0]["cliente"] == "MARIA APARECIDA SOUZA" and linhas[0]["dias_parado"] == 0
    assert linhas[0]["tom"] == "ok"
    assert len(pd.filtrar(linhas, busca="banco alfa")) == 2
    assert len(pd.filtrar(linhas, tribunal="TRF1")) == 4
    assert len(pd.filtrar(linhas, busca="0000013")) == 1
    assert pd.tribunais(linhas) == ["TJMT", "TRF1"]
    assert pd.contagens(conn) == {"ativa": 16, "a_conferir": 5, "descartados": 0}


def test_linha_do_trf1_mostra_publicacao(conn):
    trf1 = [l for l in pd.linhas_da_carteira(conn, "ativa", HOJE) if l["tribunal"] == "TRF1"]
    assert trf1[0]["ultimo_texto"] == "Publicação no DJEN"
    assert trf1[0]["tom"] == "aviso"


def test_processo_detalhe(conn):
    d = pd.processo_detalhe(conn, banco_demo._tjmt(1))
    assert d["cliente"] == "MARIA APARECIDA SOUZA" and d["situacao"] == "Ativo"
    assert d["instancia_rotulo"] == "1º grau" and len(d["linha_do_tempo"]) == 4
    arquivado = pd.processo_detalhe(conn, banco_demo._tjmt(17))
    assert arquivado["situacao"] == "Arquivado" and arquivado["tom"] == "neutro"
    assert pd.processo_detalhe(conn, "99999999999999999999") is None


@pytest.mark.parametrize("valor,esperado", [
    (3_322_000, "R$ 3,3 mi"), (850_000, "R$ 850 mil"), (0, "R$ 0")])
def test_formatar_compacto(valor, esperado):
    assert pd.formatar_compacto(valor) == esperado


def test_valor_em_reais():
    assert pd.valor_em_reais("R$ 1.500,50") == 1500.5
    assert pd.valor_em_reais("") == 0.0 and pd.valor_em_reais(None) == 0.0


def test_saudacao_e_primeiro_nome():
    assert pd.saudacao(datetime(2026, 10, 6, 9)) == "Bom dia"
    assert pd.saudacao(datetime(2026, 10, 6, 14)) == "Boa tarde"
    assert pd.saudacao(datetime(2026, 10, 6, 20)) == "Boa noite"
    assert pd.primeiro_nome("MARIA EXEMPLO DA SILVA") == "Maria"
    assert pd.primeiro_nome("") == ""


def test_filtrar_digitos_soltos_nao_casam_tudo(conn):
    linhas = pd.linhas_da_carteira(conn, "ativa", HOJE)
    assert pd.filtrar(linhas, busca="joao 2") == []
    assert len(pd.filtrar(linhas, busca="0000013")) == 1
    assert len(pd.filtrar(linhas, busca="13")) < 16


def test_atividade_recente_ignora_processo_arquivado(conn):
    numero = banco_demo._tjmt(17)
    conn.executemany(
        "INSERT INTO andamento (numero, data_ordenavel, codigo, texto) VALUES (?, ?, 1, ?)",
        [(numero, f"202610062{m:02d}{s:02d}"[:14], f"ruído {m}-{s}")
         for m in range(20) for s in range(20)])
    itens = pd.atividade_recente(conn, limite=5)
    assert len(itens) == 5
    assert itens[0]["tipo"] == "andamento" and itens[0]["data"] == "20261006"
    ativos = {l["numero"] for l in pd.linhas_da_carteira(conn, "ativa", HOJE)}
    assert all(i["numero"] in ativos for i in itens)


def test_valor_em_reais_ponto_decimal():
    assert pd.valor_em_reais("1500.50") == 1500.5
    assert pd.valor_em_reais("R$ 1.500") == 1500.0
    assert pd.valor_em_reais("R$ 1.500.000") == 1500000.0


def test_limpar_texto_entidades_e_menor_que():
    assert pd._limpar_texto("Intima&ccedil;&atilde;o&nbsp;da parte A &amp; B") == \
        "Intimação da parte A & B"
    assert "< 100" in pd._limpar_texto("valor < 100 e > 50")
    assert pd._limpar_texto("<p>Olá</p> <b>mundo</b>") == "Olá mundo"


def test_linha_do_tempo_so_aceita_links_http(conn):
    numero = banco_demo._tjmt(1)
    for id_, link in ((900001, "javascript:alert(1)"), (900002, "https://exemplo.invalido/doc")):
        conn.execute(
            "INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, tipo, orgao, "
            "texto, link, oab_confirmada) VALUES (?, ?, 'TJMT', '2026-10-01', 'Intimação', "
            "'Vara X', ?, ?, 1)", (id_, numero, f"texto {id_}", link))
    por_texto = {i["texto"]: i["link"] for i in pd.linha_do_tempo(conn, numero)}
    assert por_texto["texto 900001"] == ""
    assert por_texto["texto 900002"] == "https://exemplo.invalido/doc"


def test_kpis_publicacoes_30d_ignora_processo_arquivado(conn):
    antes = pd.kpis(conn, HOJE)["publicacoes_30d"]
    conn.execute(
        "INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, tipo, orgao, "
        "texto, link, oab_confirmada) VALUES (990001, ?, 'TJMT', ?, 'Intimação', 'Vara X', "
        "'texto', '', 1)", (banco_demo._tjmt(17), HOJE.isoformat()))
    assert pd.kpis(conn, HOJE)["publicacoes_30d"] == antes == 9


def test_linha_do_tempo_publicacao_sem_nada_tem_texto_padrao(conn):
    numero = banco_demo._tjmt(1)
    conn.execute(
        "INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, tipo, orgao, "
        "texto, link, oab_confirmada) VALUES (990002, ?, 'TJMT', '2026-10-02', '', '', '', '', 1)",
        (numero,))
    textos = [i["texto"] for i in pd.linha_do_tempo(conn, numero) if i["tipo"] == "publicacao"]
    assert "Publicação no DJEN" in textos


@pytest.mark.parametrize("valor,esperado", [
    (999_500, "R$ 1,0 mi"), (999_400, "R$ 999 mil"), (999_999, "R$ 1,0 mi"),
    (999.6, "R$ 1 mil"), (999.4, "R$ 999"), (1_000_000, "R$ 1,0 mi")])
def test_formatar_compacto_arredonda_antes_de_escolher_a_unidade(valor, esperado):
    assert pd.formatar_compacto(valor) == esperado


# ---------------------------------------------------------------------------
# Busca por nome na carteira
# ---------------------------------------------------------------------------

def test_normalizar_texto():
    assert pd.normalizar_texto("JOÃO Prudência  Ç") == "joao prudencia  c"
    assert pd.normalizar_texto("") == "" and pd.normalizar_texto(None) == ""


def test_buscar_na_carteira_ativos_primeiro_e_arquivados_rotulados(conn):
    achados = pd.buscar_na_carteira(conn, "banco alfa", HOJE)
    assert len(achados) == 5
    assert [a["situacao"] for a in achados[:2]] != ["Arquivado", "Arquivado"]
    assert all(a["situacao"] != "Arquivado" for a in achados[:2])
    assert [a["situacao"] for a in achados[2:]] == ["Arquivado"] * 3
    assert all(a["tom"] == "neutro" for a in achados[2:])
    # dentro de cada grupo, referência mais recente primeiro
    assert [a["cliente"] for a in achados[:2]] == ["MARIA APARECIDA SOUZA", "EDUARDO LOPES TEIXEIRA"]
    assert [a["cliente"] for a in achados[2:]] == ["HELENA PRADO", "OTÁVIO BRANDÃO", "SÍLVIA MATOS"]
    assert set(achados[0]) == {"numero", "numero_formatado", "tribunal", "orgao", "cliente",
                               "parte_contraria", "ultimo_texto", "referencia_br", "dias_parado",
                               "situacao", "tom"}


def test_buscar_na_carteira_ignora_acento_e_caixa(conn):
    achados = pd.buscar_na_carteira(conn, "joao", HOJE)
    assert [a["cliente"] for a in achados] == ["JOÃO PEDRO LIMA"]


def test_buscar_na_carteira_acha_a_parte_contraria(conn):
    achados = pd.buscar_na_carteira(conn, "prudencia", HOJE)
    assert [a["parte_contraria"] for a in achados] == ["SEGURADORA PRUDÊNCIA S.A."]


def test_buscar_na_carteira_acha_nome_so_nas_partes_do_processo(conn):
    conn.execute("UPDATE processo SET partes_json = ? WHERE numero = ?",
                 ('[{"polo": "Polo Passivo", "nomes": ["TERCEIRO ÚNICO LTDA"], '
                  '"integrantes": [{"nome": "OUTRO INTEGRANTE"}]}]', banco_demo._tjmt(5)))
    assert [a["numero"] for a in pd.buscar_na_carteira(conn, "terceiro unico", HOJE)] == [banco_demo._tjmt(5)]
    assert [a["numero"] for a in pd.buscar_na_carteira(conn, "outro integrante", HOJE)] == [banco_demo._tjmt(5)]


def test_buscar_na_carteira_sem_resultado_e_termo_vazio(conn):
    assert pd.buscar_na_carteira(conn, "zzzzzz", HOJE) == []
    assert pd.buscar_na_carteira(conn, "   ", HOJE) == []


def test_numeros_na_carteira(conn):
    de_fora = "00000990220248110041"
    achados = pd.numeros_na_carteira(conn, [banco_demo._tjmt(1), de_fora, banco_demo._trf1(13)])
    assert achados == {banco_demo._tjmt(1), banco_demo._trf1(13)}
    assert pd.numeros_na_carteira(conn, []) == set()


def test_contagens_inclui_descartados(conn):
    from nucleo import carteira
    antes = pd.contagens(conn)
    numero = carteira.listar(conn, "a_conferir")[0]["numero"]
    with conn:
        carteira.descartar(conn, numero)
    depois = pd.contagens(conn)
    assert depois["a_conferir"] == antes["a_conferir"] - 1
    assert depois["descartados"] == antes["descartados"] + 1 == 1


# --- Quadro de demandas (Task 10): testes independentes, com banco em memória próprio ---

def _quadro_basico():
    from nucleo import quadro
    c = banco.conectar(":memory:")
    a = "00000010220248110041"
    c.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado, cliente, "
              "parte_contraria, orgao_julgador) VALUES (?, 'TJMT', 1, 0, 'MARIA', 'BANCO', '1ª VARA')", (a,))
    c.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
              "VALUES (1, ?, 'TJMT', '2026-10-05', '<p>Fica a parte intimada.</p>')", (a,))
    c.execute("INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, data_disponibilizacao, visto_em) "
              "VALUES ('1grau', '9', ?, 'INT', '2026-10-04', 'x')", (a,))
    quadro.adicionar_item(c, a, "publicacao", "1", "2026-10-05")
    quadro.adicionar_item(c, a, "aviso", "1grau:9", "2026-10-04")
    c.commit()
    return c, a


def test_quadro_monta_colunas_e_cartao():
    c, a = _quadro_basico()
    q = pd.quadro(c, date(2026, 10, 6))
    assert [col["chave"] for col in q["colunas"]] == ["chegou", "acao", "autos", "lex", "revisao"]
    cartao = q["colunas"][0]["cartoes"][0]
    assert cartao["cliente"] == "MARIA" and cartao["parte_contraria"] == "BANCO"
    assert cartao["origem"] == "DJEN + PJe" and cartao["n_itens"] == 2
    assert cartao["data_br"] == "04/10/2026" and cartao["chegou_ha"] == "há 2 dias"
    assert cartao["trecho"] == "Fica a parte intimada."
    assert q["na_triagem"] == 1 and q["versao"] == pd.versao(c) > 0
    assert q["varredura"] == {"ativa": False, "ultima": None}
    c.close()


def test_chegou_ha():
    assert pd.chegou_ha("2026-10-06", date(2026, 10, 6)) == "hoje"
    assert pd.chegou_ha("2026-10-05", date(2026, 10, 6)) == "ontem"
    assert pd.chegou_ha("2026-10-01", date(2026, 10, 6)) == "há 5 dias"


def test_atividade_agora_traduz_eventos():
    from nucleo import quadro
    c, a = _quadro_basico()
    with c:
        quadro.mover(c, 1, "acao", "advogado")
    textos = [i["texto"] for i in pd.atividade_agora(c)]
    assert textos[0] == "marcado: precisa de ação"
    assert "novo cartão" in textos
    c.close()


def test_atividade_agora_ordena_por_quando_e_nao_por_id():
    from nucleo import eventos
    c = banco.conectar(":memory:")
    with c:
        eventos.registrar(c, "varredura_concluida", None, {}, quando="2026-10-06T12:30:00-04:00")
        eventos.registrar(c, "varredura_concluida", None, {}, quando="2026-10-06T06:00:00-04:00")
        eventos.registrar(c, "varredura_concluida", None, {}, quando="2026-10-06T12:00:00-04:00")
    assert [i["hora"] for i in pd.atividade_agora(c)] == ["12:30", "12:00", "06:00"]
    c.close()


def test_linha_do_tempo_mostra_intimacao():
    c, a = _quadro_basico()
    itens = [i for i in pd.linha_do_tempo(c, a) if i["tipo"] == "aviso"]
    assert itens and itens[0]["texto"] == "Intimação no PJe · aguardando ciência"
    c.close()


def test_cartao_de_intimacao_diz_se_a_ciencia_ja_foi_registrada():
    from nucleo import quadro
    c = banco.conectar(":memory:")
    a = "00000010220248110041"
    c.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado) "
              "VALUES (?, 'TJMT', 1, 0)", (a,))
    c.execute("INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, data_disponibilizacao, "
              "visto_em) VALUES ('1grau', '9', ?, 'INT', '2026-10-04', 'x')", (a,))
    quadro.adicionar_item(c, a, "aviso", "1grau:9", "2026-10-04")
    c.commit()
    trecho = lambda: pd.quadro(c, HOJE)["colunas"][0]["cartoes"][0]["trecho"]  # noqa: E731
    assert trecho() == "Intimação no PJe aguardando ciência"
    c.execute("UPDATE aviso SET pendente = 0")
    assert trecho() == "Intimação no PJe · ciência já registrada (o prazo pode estar correndo)"
    c.close()


def test_atividade_agora_mostra_download_dos_autos():
    from nucleo import eventos
    c = banco.conectar(":memory:")
    a = "00000010220248110041"
    with c:
        eventos.registrar(c, "autos_iniciados", a, {"demanda": 1},
                          quando="2026-10-06T10:00:00-04:00")
        eventos.registrar(c, "autos_pedidos_de_novo", a, {"demanda": 1},
                          quando="2026-10-06T11:00:00-04:00")
    textos = [i["texto"] for i in pd.atividade_agora(c)]
    assert textos == ["download dos autos pedido de novo", "baixando autos"]
    c.close()


# --- Fase 3: cartão do Lex, cronômetro, cartão aberto e Resultados ---------------

import json  # noqa: E402

NUM_LEX = "00000010220248110041"
FUSO_T = "-04:00"


def _t(dia, hora, minuto=0):
    return f"2026-10-{dia:02d}T{hora:02d}:{minuto:02d}:00{FUSO_T}"


def _banco_lex():
    c = banco.conectar(":memory:")
    c.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado, cliente, "
              "parte_contraria, orgao_julgador) VALUES (?, 'TJMT', 1, 0, 'MARIA', 'BANCO', "
              "'1ª VARA')", (NUM_LEX,))
    c.commit()
    return c


def _demanda(c, coluna="revisao", criada=None, lex_estado="", **campos):
    cur = c.execute(
        "INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em, "
        "lex_estado) VALUES (?, ?, '2026-10-01', ?, ?, ?)",
        (NUM_LEX, coluna, criada or _t(1, 8), criada or _t(1, 8), lex_estado))
    for k, v in campos.items():
        c.execute(f"UPDATE demanda SET {k} = ? WHERE id = ?", (v, cur.lastrowid))
    c.commit()
    return cur.lastrowid


def _exec(c, demanda, n, inicio, fim, resultado="pronto", squad="contestacao", falhas=0,
          gate="PASS", pasta="", detalhe=""):
    c.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
              "resultado, squad, gate_status, citacoes_total, citacoes_falhas, pasta, detalhe) "
              "VALUES (?, ?, 'novo', ?, ?, ?, ?, ?, 5, ?, ?, ?)",
              (demanda, n, inicio, fim, resultado, squad, gate, falhas, pasta, detalhe))
    c.commit()


@pytest.mark.parametrize("minutos,esperado", [
    (0, "0 min"), (14, "14 min"), (59, "59 min"), (60, "1 h"), (95, "1 h 35 min"),
    (120, "2 h"), (125.4, "2 h 5 min"), (None, "")])
def test_formatar_duracao(minutos, esperado):
    assert pd.formatar_duracao(minutos) == esperado


def _cartao_lex(c, agora):
    q = pd.quadro(c, HOJE, agora)
    return {col["chave"]: col["cartoes"] for col in q["colunas"]}


def test_quadro_tem_chave_do_lex_automatico():
    from nucleo import config
    c = _banco_lex()
    assert pd.quadro(c, HOJE)["lex_automatico"] is False
    with c:
        config.definir_lex_automatico(c, True)
    assert pd.quadro(c, HOJE)["lex_automatico"] is True


def test_rotulo_na_fila_e_aguardando_o_mac():
    from nucleo import ponte
    c = _banco_lex()
    _demanda(c, "lex", lex_estado="na_fila")
    agora = datetime.fromisoformat(_t(6, 10, 0))
    assert _cartao_lex(c, agora)["lex"][0]["lex_rotulo"] == "aguardando o Mac"  # nunca falou
    with c:
        ponte.config.gravar(c, ponte.ULTIMO_CONTATO, _t(6, 9, 58))
    assert _cartao_lex(c, agora)["lex"][0]["lex_rotulo"] == "na fila do Lex"
    with c:
        ponte.config.gravar(c, ponte.ULTIMO_CONTATO, _t(6, 9, 54))
    assert _cartao_lex(c, agora)["lex"][0]["lex_rotulo"] == "aguardando o Mac"
    assert _cartao_lex(c, agora)["lex"][0]["lex_estado"] == "na_fila"


def test_rotulo_do_lex_trabalhando_pausado_e_falhou():
    c = _banco_lex()
    d = _demanda(c, "lex", lex_estado="trabalhando", lex_squad="contestacao",
                 lex_etapa="pesquisa", lex_reservado_ate=_t(6, 10, 5))
    c.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em) VALUES (?, 1, 'novo', ?)",
              (d, _t(6, 9, 40)))
    c.commit()
    agora = datetime.fromisoformat(_t(6, 10, 0))
    cartao = _cartao_lex(c, agora)["lex"][0]
    assert cartao["lex_rotulo"] == "Lex trabalhando há 20 min · Contestação · pesquisa"
    c.execute("UPDATE demanda SET lex_estado = 'pausado_limite' WHERE id = ?", (d,))
    assert _cartao_lex(c, agora)["lex"][0]["lex_rotulo"] == "pausado: limite do plano"
    c.execute("UPDATE demanda SET lex_estado = 'falhou', lex_detalhe = 'sem rede' WHERE id = ?", (d,))
    cartao = _cartao_lex(c, agora)["lex"][0]
    assert cartao["lex_rotulo"] == "falhou: " + pd.FRASE_FALHA_DESCONHECIDA
    assert "sem rede" not in cartao["lex_rotulo"] and cartao["lex_detalhe"] == "sem rede"


def test_cartao_sem_lex_nao_tem_rotulo_nem_cronometro():
    c = _banco_lex()
    _demanda(c, "chegou")
    cartao = _cartao_lex(c, datetime.fromisoformat(_t(6, 10)))["chegou"][0]
    assert cartao["lex_rotulo"] == "" and cartao["tempo_total"] == ""
    assert cartao["tempo_lex"] == "" and cartao["citacoes_falhas"] is None
    assert cartao["gate_status"] == "" and cartao["lex_orientacao"] == ""


def test_cartao_em_revisao_traz_cronometro_gate_e_citacoes():
    c = _banco_lex()
    d = _demanda(c, "revisao", criada=_t(1, 8, 0), lex_estado="pronto",
                 lex_orientacao="Contestar por prescrição")
    _exec(c, d, 1, _t(1, 8, 30), _t(1, 8, 50), falhas=2, gate="BLOCK")      # 20 min
    _exec(c, d, 2, _t(1, 9, 0), _t(1, 9, 5), falhas=0, gate="PASS")        # 5 min
    cartao = _cartao_lex(c, datetime.fromisoformat(_t(6, 10)))["revisao"][0]
    # total = primeira pronta (8:50) − criada (8:00); Lex = 20 + 5
    assert cartao["tempo_total"] == "50 min" and cartao["tempo_lex"] == "25 min"
    assert cartao["gate_status"] == "PASS" and cartao["citacoes_falhas"] == 0  # última pronta
    assert cartao["lex_orientacao"] == "Contestar por prescrição"


def test_cronometro_ignora_execucoes_que_nao_ficaram_prontas():
    c = _banco_lex()
    d = _demanda(c, "revisao", criada=_t(1, 8, 0), lex_estado="pronto")
    _exec(c, d, 1, _t(1, 8, 0), _t(1, 8, 40), resultado="falhou", detalhe="x")
    _exec(c, d, 2, _t(1, 9, 0), _t(1, 10, 35))
    cartao = _cartao_lex(c, datetime.fromisoformat(_t(6, 10)))["revisao"][0]
    assert cartao["tempo_total"] == "2 h 35 min" and cartao["tempo_lex"] == "1 h 35 min"


def _pacote(tmp_path, d, n, gate=None, nota="Confira o prazo.", pdf=True, docx=True):
    pasta = tmp_path / str(d) / str(n)
    pasta.mkdir(parents=True)
    if pdf:
        (pasta / "peca.pdf").write_bytes(b"%PDF-1.4")
    if docx:
        (pasta / "peca.docx").write_bytes(b"PK")
    (pasta / "nota-ao-revisor.md").write_text(nota, encoding="utf-8")
    if gate is not None:
        (pasta / "citation-gate.json").write_text(json.dumps(gate), encoding="utf-8")
    return pasta


def test_demanda_detalhe_inexistente():
    c = _banco_lex()
    assert pd.demanda_detalhe(c, 99, HOJE, "dados/pecas") is None


def test_demanda_detalhe_le_pacote_e_historico(tmp_path):
    from nucleo import eventos
    c = _banco_lex()
    d = _demanda(c, "revisao", criada=_t(1, 8, 0), lex_estado="pronto", lex_squad="contestacao",
                 lex_orientacao="Contestar", lex_ajuste="")
    gate = {"gate_status": "PASS",
            "citations": [{"title": "Súmula 297 do STJ", "status": "VERIFIED",
                           "source_url": "https://exemplo.test/s297"},
                          {"title": "Tema 1", "status": "NOT_FOUND",
                           "source_url": "javascript:alert(1)"}],
            "pendencias_do_profissional": [{"marcador": "[CONFERIR 1]", "onde": "fls. 10",
                                            "diligencia": "Confirmar a data da citação"}]}
    pasta = _pacote(tmp_path, d, 1, gate)
    _exec(c, d, 1, _t(1, 8, 30), _t(1, 8, 50), pasta=str(pasta))
    with c:
        eventos.registrar(c, "demanda_criada", NUM_LEX, {"demanda": d}, quando=_t(1, 8, 0))
        eventos.registrar(c, "demanda_movida", NUM_LEX,
                          {"demanda": d, "de": "autos", "para": "lex", "por": "advogado"},
                          quando=_t(1, 8, 20))
        eventos.registrar(c, "lex_pronto", NUM_LEX,
                          {"demanda": d, "n": 1, "squad": "contestacao", "minutos_lex": 20},
                          quando=_t(1, 8, 50))
        eventos.registrar(c, "demanda_criada", "outro", {"demanda": d + 7})  # de outro cartão
    det = pd.demanda_detalhe(c, d, HOJE, str(tmp_path), datetime.fromisoformat(_t(6, 10)))
    assert det["numero_formatado"] == formatar_cnj_demo() and det["cliente"] == "MARIA"
    assert det["coluna"] == "revisao" and det["coluna_titulo"] == "Sua revisão"
    assert det["orientacao"] == "Contestar" and det["squad"] == "contestacao"
    assert det["cronometro"]["tempo_total"] == "50 min"
    assert det["cronometro"]["tempo_lex"] == "20 min"
    assert det["execucoes"] == [{
        "n": 1, "modo": "novo", "inicio": "01/10/2026 08:30", "fim": "01/10/2026 08:50",
        "resultado": "pronto", "resultado_rotulo": "peça pronta", "minutos": 20,
        "squad": "contestacao", "squad_rotulo": "Contestação", "frase": "", "detalhe": ""}]
    p = det["pronta"]
    assert p["tem_pdf"] and p["tem_docx"] and not p["tem_termo"]
    assert p["nota"] == "Confira o prazo." and p["gate_status"] == "PASS"
    assert p["citacoes"] == [
        {"title": "Súmula 297 do STJ", "status": "VERIFIED", "source_url": "https://exemplo.test/s297"},
        {"title": "Tema 1", "status": "NOT_FOUND", "source_url": ""}]
    assert p["pendencias"] == [{"marcador": "[CONFERIR 1]", "onde": "fls. 10",
                                "diligencia": "Confirmar a data da citação"}]
    assert [h["quando"] for h in det["historico"]] == [
        "01/10/2026 08:00", "01/10/2026 08:20", "01/10/2026 08:50"]
    assert [h["texto"] for h in det["historico"]] == [
        "novo cartão", "movido para “Lex minutando” por você",
        "Lex terminou a peça · Contestação · 20 min"]


def formatar_cnj_demo():
    from captura.processo_parser import formatar_numero_cnj
    return formatar_numero_cnj(NUM_LEX)


def test_demanda_detalhe_sem_pacote_ou_com_gate_quebrado(tmp_path):
    c = _banco_lex()
    d = _demanda(c, "revisao", lex_estado="pronto")
    _exec(c, d, 1, _t(1, 8, 30), _t(1, 8, 50))
    det = pd.demanda_detalhe(c, d, HOJE, str(tmp_path))
    p = det["pronta"]
    assert not p["tem_pdf"] and not p["tem_docx"] and p["nota"] == ""
    assert p["citacoes"] == [] and p["pendencias"] == [] and p["gate_status"] == "PASS"
    pasta = _pacote(tmp_path, d, 1, None)
    (pasta / "citation-gate.json").write_text("{não é json", encoding="utf-8")
    p = pd.demanda_detalhe(c, d, HOJE, str(tmp_path))["pronta"]
    assert p["tem_pdf"] and p["citacoes"] == [] and p["pendencias"] == []


def test_demanda_detalhe_ignora_pasta_gravada_fora_de_pecas_dir(tmp_path):
    c = _banco_lex()
    d = _demanda(c, "revisao", lex_estado="pronto")
    fora = tmp_path / "fora"
    fora.mkdir()
    (fora / "peca.pdf").write_bytes(b"%PDF")
    _exec(c, d, 1, _t(1, 8, 30), _t(1, 8, 50), pasta=str(fora))
    pecas = tmp_path / "pecas"
    pecas.mkdir()
    assert pd.demanda_detalhe(c, d, HOJE, str(pecas))["pronta"]["tem_pdf"] is False


def test_demanda_detalhe_cartao_sem_execucao():
    c = _banco_lex()
    d = _demanda(c, "chegou")
    det = pd.demanda_detalhe(c, d, HOJE, "dados/pecas")
    assert det["pronta"] is None and det["execucoes"] == []
    assert det["cronometro"]["tempo_total"] == "" and det["historico"] == []


def test_resultados_numeros_por_periodo():
    c = _banco_lex()
    agora = datetime.fromisoformat(_t(20, 12))
    # A: total 50 min, Lex 20+5 (2ª execução ajuste), contestacao, 1ª limpa
    a = _demanda(c, criada=_t(1, 8, 0))
    _exec(c, a, 1, _t(1, 8, 30), _t(1, 8, 50), squad="contestacao", falhas=0)
    _exec(c, a, 2, _t(1, 9, 0), _t(1, 9, 5), squad="contestacao", falhas=3)
    # B: total 2 h, Lex 40, recurso, 1ª com falha
    b = _demanda(c, criada=_t(3, 8, 0))
    _exec(c, b, 1, _t(3, 9, 20), _t(3, 10, 0), squad="recurso", falhas=1)
    # C: no mês anterior (setembro), total 30 min, Lex 10, contestacao, limpa
    cdem = _demanda(c, criada="2026-09-10T08:00:00-04:00")
    _exec(c, cdem, 1, "2026-09-10T08:20:00-04:00", "2026-09-10T08:30:00-04:00",
          squad="contestacao", falhas=0)
    # D: só falhou — não conta
    dd = _demanda(c, criada=_t(4, 8))
    _exec(c, dd, 1, _t(4, 8, 10), _t(4, 8, 20), resultado="falhou", squad="recurso")

    r = pd.resultados(c, "tudo", agora)
    assert r["total_pecas"] == 3
    assert r["tempo_medio_total_minutos"] == round((50 + 120 + 30) / 3)  # 67
    assert r["tempo_medio_total"] == "1 h 7 min"
    assert r["tempo_medio_lex_minutos"] == round((25 + 40 + 10) / 3)  # 25
    assert r["tempo_medio_lex"] == "25 min"
    assert r["pecas_por_mes"] == [("2026-09", 1), ("2026-10", 2)]
    assert r["pecas_por_tipo"] == [("Contestação", 2), ("Recurso", 1)]
    assert [(m["squad"], m["minutos"], m["data"]) for m in r["melhores"]] == [
        ("Contestação", 30, "10/09/2026"), ("Contestação", 50, "01/10/2026"),
        ("Recurso", 120, "03/10/2026")]
    assert r["citacoes_de_primeira"] == 67  # 2 de 3

    r = pd.resultados(c, "30d", agora)  # corte 20/set: sem a peça de setembro
    assert r["total_pecas"] == 2 and r["pecas_por_mes"] == [("2026-10", 2)]
    assert r["tempo_medio_total_minutos"] == 85 and r["citacoes_de_primeira"] == 50
    assert pd.resultados(c, "12m", agora)["total_pecas"] == 3


def test_resultados_vazio():
    c = _banco_lex()
    r = pd.resultados(c, "tudo")
    assert r["total_pecas"] == 0 and r["pecas_por_mes"] == [] and r["pecas_por_tipo"] == []
    assert r["melhores"] == [] and r["tempo_medio_total"] == "" and r["tempo_medio_lex"] == ""
    assert r["citacoes_de_primeira"] is None


def _pronta_com_gate(tmp_path, conteudo):
    c = _banco_lex()
    d = _demanda(c, "revisao", lex_estado="pronto")
    _exec(c, d, 1, _t(1, 8, 30), _t(1, 8, 50))
    pasta = _pacote(tmp_path, d, 1, None)
    (pasta / "citation-gate.json").write_text(conteudo, encoding="utf-8")
    return pd.demanda_detalhe(c, d, HOJE, str(tmp_path))["pronta"]


@pytest.mark.parametrize("conteudo", [
    '{"citations": 5, "pendencias_do_profissional": true}',
    '{"citations": "x", "pendencias_do_profissional": {"a": 1}}',
    '[1, 2, 3]', '"texto"', 'null',
    '{"citations": [5, null, "x"], "pendencias_do_profissional": [1, []]}',
    '[' * 100_000,
], ids=["numeros", "tipos", "lista", "texto", "null", "listas-mistas", "aninhado"])
def test_gate_com_tipos_errados_ou_aninhado_nao_quebra(tmp_path, conteudo):
    p = _pronta_com_gate(tmp_path, conteudo)
    assert p["citacoes"] == [] and p["pendencias"] == [] and p["tem_pdf"]


def test_gate_com_url_que_nao_e_texto(tmp_path):
    p = _pronta_com_gate(tmp_path, '{"citations": [{"title": "T", "source_url": 5}]}')
    assert p["citacoes"] == [{"title": "T", "status": "", "source_url": ""}]


def test_gate_acima_do_limite_conta_como_vazio(tmp_path):
    valido = json.dumps({"citations": [{"title": "T", "status": "OK", "source_url": ""}],
                         "pad": "x" * pd.LIMITE_GATE})
    assert len(valido) > pd.LIMITE_GATE
    assert _pronta_com_gate(tmp_path / "a", valido)["citacoes"] == []
    pequeno = json.dumps({"citations": [{"title": "T", "status": "OK", "source_url": ""}]})
    assert len(_pronta_com_gate(tmp_path / "b", pequeno)["citacoes"]) == 1


def test_historico_nao_repete_o_passo_de_mover_para_lex_e_protocolado():
    from nucleo import quadro
    c = _banco_lex()
    d = _demanda(c, "autos")
    with c:
        quadro.mover(c, d, "lex", "advogado", orientacao="x")
        c.execute("UPDATE demanda SET coluna = 'revisao', lex_estado = 'pronto' WHERE id = ?", (d,))
        quadro.mover(c, d, "protocolado", "advogado")
        quadro.mover(c, d, "revisao", "advogado")
    textos = [h["texto"] for h in pd.demanda_detalhe(c, d, HOJE, "x")["historico"]]
    assert textos == ["enviado à fila do Lex", "marcado como protocolado",
                      "protocolo desfeito; de volta à sua revisão"]


# --- onda final -------------------------------------------------------------------

@pytest.mark.parametrize("slug,esperado", [
    ("replica-a-contestacao", "Réplica à contestação"),
    ("EMBARGOS-DE-DECLARACAO", "Embargos de declaração"),
    ("peticao_inicial", "Petição inicial"),
    ("apelacao-civel", "Apelação civel"),
    ("impugnacao-ao-cumprimento-de-sentenca", "Impugnação ao cumprimento de sentença"),
    ("manifestacao-sobre-laudo", "Manifestação sobre laudo"),
    ("embargos-a-execucao", "Embargos a execução"),
    ("acao-de-cobranca", "Ação de cobranca"),
    ("", ""), (None, ""),
])
def test_humanizar_squad(slug, esperado):
    assert pd.humanizar_squad(slug) == esperado


def test_nome_do_squad_prefere_o_nome_enviado():
    assert pd.nome_do_squad("Réplica  (TJMT)", "replica") == "Réplica (TJMT)"
    assert pd.nome_do_squad("", "replica-a-contestacao") == "Réplica à contestação"


@pytest.mark.parametrize("motivo,detalhe,frase", [
    ("limite", "pausado: limite do Claude", "o limite do plano acabou"),
    ("tempo", "o Lex passou de 2 h", "passou do tempo máximo"),
    ("sem_tipo", "", "segurança sobre o tipo de peça"),
    ("falhou", "o squad não é deste caso; pacote recusado", "peça de outro caso"),
    ("erro", "pacote da peça não encontrado em squads/x/output/pacote", "não apareceu na pasta"),
    ("falhou", "autos indisponíveis", "autos não puderam ser entregues"),
    ("erro", "Traceback /Users/daniel/segredo.py", "detalhe técnico no histórico"),
    ("falhou", "", "detalhe técnico no histórico"),
])
def test_frase_da_falha(motivo, detalhe, frase):
    saida = pd.frase_da_falha(motivo, detalhe)
    assert frase in saida and "/Users" not in saida and "squads/" not in saida


@pytest.mark.parametrize("de,para,por,texto", [
    ("lex", "autos", "advogado", "tirado do Lex"),
    ("lex", "autos", "mac", "Lex não escolheu o tipo de peça"),
    ("protocolado", "revisao", "advogado", "protocolo desfeito"),
    ("lex", "revisao", "advogado", "ajuste desistido; a peça anterior voltou à revisão"),
    ("lex", "revisao", "mac", "peça pronta para a sua revisão"),
    ("revisao", "lex", "advogado", "devolvido ao Lex com ajuste"),
    ("autos", "lex", "advogado", "enviado ao Lex"),
    ("acao", "autos", "trabalhador", "autos baixados"),
    ("autos", "chegou", "advogado", "voltou para a triagem"),
    ("x", "y", "z", "cartão movido"),
])
def test_texto_do_movimento(de, para, por, texto):
    assert pd.texto_do_movimento(de, para, por) == texto


def test_markdown_minimo_seguro():
    saida = str(pd.markdown_minimo(
        "# Título\n## Sub\n### Menor\n\nLinha *um*\ncontinua **dois**.\n\n"
        "1. primeiro\n2. segundo\n- solto\n\n<script>x</script> [a](javascript:y) `<i>`"))
    assert "<h3>Título</h3><h4>Sub</h4><h5>Menor</h5>" in saida
    assert "<p>Linha <em>um</em> continua <strong>dois</strong>.</p>" in saida
    assert "<ol><li>primeiro</li><li>segundo</li></ol><ul><li>solto</li></ul>" in saida
    assert "&lt;script&gt;" in saida and "<script>" not in saida
    assert "<a " not in saida and "<code>&lt;i&gt;</code>" in saida
    assert str(pd.markdown_minimo("")) == "" and str(pd.markdown_minimo(None)) == ""


def test_resultados_agrupam_pelo_slug_e_mostram_o_nome():
    c = _banco_lex()
    agora = datetime.fromisoformat(_t(20, 12))
    a = _demanda(c, criada=_t(1, 8, 0))
    _exec(c, a, 1, _t(1, 8, 30), _t(1, 8, 50), squad="replica-a-contestacao")
    b = _demanda(c, criada=_t(2, 8, 0))
    _exec(c, b, 1, _t(2, 8, 30), _t(2, 8, 50), squad="replica-a-contestacao")
    c.execute("UPDATE execucao_lex SET squad_nome = 'Réplica (modelo TJMT)' WHERE demanda_id = ?",
              (b,))
    c.commit()
    r = pd.resultados(c, "tudo", agora)
    assert r["pecas_por_tipo"] == [("Réplica (modelo TJMT)", 2)]
    assert {m["squad"] for m in r["melhores"]} == {"Réplica (modelo TJMT)"}


def test_rotulo_na_fila_com_mac_sem_plano():
    from nucleo import ponte
    c = _banco_lex()
    _demanda(c, "lex", lex_estado="na_fila")
    agora = datetime.fromisoformat(_t(6, 10, 0))
    with c:
        ponte.config.gravar(c, ponte.ULTIMO_CONTATO, _t(6, 9, 58))
        ponte.config.gravar(c, ponte.CLAUDE_OK, "0")
        ponte.config.gravar(c, ponte.CLAUDE_OK_EM, _t(6, 9, 58))
    assert _cartao_lex(c, agora)["lex"][0]["lex_rotulo"] == (
        "na fila · Mac sem acesso ao plano do Lex")
    assert pd.quadro(c, HOJE, agora)["mac_sem_plano"] is True


def test_rotulo_com_reserva_vencida():
    c = _banco_lex()
    _demanda(c, "lex", lex_estado="reservado", lex_reservado_ate=_t(6, 9, 59))
    agora = datetime.fromisoformat(_t(6, 10, 0))
    cartao = _cartao_lex(c, agora)["lex"][0]
    assert cartao["lex_rotulo"] == "o Mac parou de responder" and cartao["reserva_vencida"]
    c.execute("UPDATE demanda SET lex_reservado_ate = ?", (_t(6, 10, 1),))
    assert _cartao_lex(c, agora)["lex"][0]["reserva_vencida"] is False


def test_cartoes_com_lex_do_processo():
    c = _banco_lex()
    _demanda(c, "chegou")
    d = _demanda(c, "protocolado")
    _exec(c, d, 1, _t(1, 8, 30), _t(1, 8, 50), squad="apelacao-civel")
    assert pd.cartoes_com_lex(c, NUM_LEX) == [
        {"id": d, "coluna_titulo": "Protocolado", "squad_rotulo": "Apelação civel",
         "data_br": "01/10/2026"}]


# --- onda final, correção 1 ------------------------------------------------------------

def test_markdown_negrito_em_tempo_linear():
    import time as _time
    inicio = _time.perf_counter()
    pd.markdown_minimo(" **a" * 25000)
    pd.markdown_minimo(" *a" * 25000)
    assert _time.perf_counter() - inicio < 0.5


def test_markdown_bloco_de_codigo_e_codigo_em_negrito():
    saida = str(pd.markdown_minimo(
        "Antes\n```python\n  x = '<b>' ** 2\n*não* é itálico\n```\nDepois **`npx banca`** fim"))
    assert ("<p>Antes</p><pre><code>  x = &#39;&lt;b&gt;&#39; ** 2\n*não* é itálico"
            "</code></pre>") in saida
    assert "<p>Depois <strong><code>npx banca</code></strong> fim</p>" in saida
    sem_fim = str(pd.markdown_minimo("```\n<script>"))
    assert sem_fim == "<pre><code>&lt;script&gt;</code></pre>"


def test_frases_do_detalhe_cobrem_todas_as_frases_do_mac():
    from ponte_mac import laco
    esperadas = {
        "o painel recusou o pacote": "o painel recusou a peça enviada",
        laco.executor_mod.DETALHE_ACESSO: "o Mac perdeu o acesso ao plano do Lex",
        "o MANIFESTO do pacote é de outro run; pacote recusado":
            "a peça encontrada não era desta execução",
        "o Lex terminou sem a linha final": "o Lex parou antes de concluir",
        "erro no Mac": "erro no Mac",
    }
    for detalhe, frase in esperadas.items():
        assert pd.frase_da_falha("erro", detalhe) == frase, detalhe
    for detalhe in laco._FRASES_DE_ERRO:  # nenhuma frase do Mac cai na genérica
        assert pd.frase_da_falha("erro", detalhe) != pd.FRASE_FALHA_DESCONHECIDA, detalhe
    assert pd.frase_da_falha("erro", "o painel recusou o pacote: pacote inválido") == \
        "o painel recusou a peça enviada"
    assert pd.frase_da_falha("erro", "o acesso do Claude no Mac foi recusado") == \
        "o Mac perdeu o acesso ao plano do Lex"  # frase do Mac antigo
    assert "Claude" not in laco.executor_mod.DETALHE_ACESSO
