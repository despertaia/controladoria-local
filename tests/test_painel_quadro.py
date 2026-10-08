import pytest

import painel
from nucleo import banco, quadro, tarefas_fila

A = "00000010220248110041"
JSON = {"Accept": "application/json"}


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    caminho = str(tmp_path / "q.db")
    conn = banco.conectar(caminho)
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado, cliente) "
                 "VALUES (?, 'TJMT', 1, 0, 'MARIA FICTÍCIA')", (A,))
    quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    conn.commit()
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    c = painel.app.test_client()
    c.caminho = caminho
    return c


def _coluna(cliente):
    conn = banco.conectar(cliente.caminho)
    try:
        return conn.execute("SELECT coluna FROM demanda WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


def test_cockpit_mostra_o_quadro(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert 'id="quadro"' in html and "Chegou do PJe" in html and "MARIA FICTÍCIA" in html
    assert "Precisa de ação" in html and "Só acompanhar" in html


def test_fragmento_com_versao(cliente):
    r = cliente.get("/quadro/fragmento")
    assert r.status_code == 200 and "MARIA FICTÍCIA" in r.get_data(as_text=True)
    versao = r.headers["X-Versao"]
    assert cliente.get(f"/quadro/fragmento?versao={versao}").status_code == 204


def test_mover_por_json_e_desfazer(cliente):
    r = cliente.post("/demanda/1/mover", data={"para": "acao"}, headers=JSON)
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "mensagem": "Movido para “Precisa de ação”.",
                            "desfazer": "chegou"}
    assert _coluna(cliente) == "acao"
    cliente.post("/demanda/1/mover", data={"para": "chegou"}, headers=JSON)
    assert _coluna(cliente) == "chegou"


def test_mover_invalido_da_409(cliente):
    r = cliente.post("/demanda/1/mover", data={"para": "autos"}, headers=JSON)
    assert r.status_code == 409 and r.get_json()["ok"] is False


def test_mover_sem_js_redireciona(cliente):
    r = cliente.post("/demanda/1/mover", data={"para": "acompanhar"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def test_varrer_agora_nao_duplica(cliente):
    assert cliente.post("/varrer", headers=JSON).status_code == 200
    r = cliente.post("/varrer", headers=JSON)
    assert r.status_code == 409 and "Já há uma varredura" in r.get_json()["erro"]


def test_contador_na_navegacao(cliente):
    html = cliente.get("/carteira").get_data(as_text=True)
    assert 'class="contador contador-nav"' in html


def test_cockpit_carrega_o_js_e_o_aviso(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert "quadro.js" in html and 'id="quadro-aviso"' in html
    assert 'data-url="/quadro/fragmento"' in html


def test_cartao_com_falha_de_autos_tem_tentar_de_novo(cliente):
    conn = banco.conectar(cliente.caminho)
    with conn:
        quadro.mover(conn, 1, "acao", "advogado")
        quadro.marcar_autos(conn, 1, "falhou", "Tempo esgotado no TJMT.")
    conn.close()
    html = cliente.get("/quadro/fragmento").get_data(as_text=True)
    assert "cartao-falhou" in html and "Tentar de novo" in html
    assert "Tempo esgotado no TJMT." in html


def test_fragmento_carrega_a_contagem_da_triagem(cliente):
    html = cliente.get("/quadro/fragmento").get_data(as_text=True)
    assert 'data-na-triagem="' in html


def test_corrida_no_indice_do_cartao_novo_vira_mensagem_e_nao_500(cliente, monkeypatch):
    import sqlite3

    def corrida(*_a, **_k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: demanda.numero")
    monkeypatch.setattr(painel.nucleo_quadro, "mover", corrida)
    r = cliente.post("/demanda/1/mover", data={"para": "chegou"}, headers=JSON)
    assert r.status_code == 409
    assert r.get_json() == {"ok": False,
                            "erro": "Já há um cartão novo deste processo esperando triagem."}


def test_cartao_com_autos_na_fila_pode_voltar_a_triagem(cliente):
    conn = banco.conectar(cliente.caminho)
    with conn:
        quadro.mover(conn, 1, "acao", "advogado")
    conn.close()
    html = cliente.get("/quadro/fragmento").get_data(as_text=True)
    assert "Autos na fila para baixar" in html and "Voltar à triagem" in html
    r = cliente.post("/demanda/1/mover", data={"para": "chegou"}, headers=JSON)
    assert r.status_code == 200 and _coluna(cliente) == "chegou"


def test_cartao_baixando_nao_oferece_voltar(cliente):
    conn = banco.conectar(cliente.caminho)
    with conn:
        quadro.mover(conn, 1, "acao", "advogado")
        quadro.marcar_autos(conn, 1, "baixando")
    conn.close()
    html = cliente.get("/quadro/fragmento").get_data(as_text=True)
    assert "Baixando autos…" in html and "Voltar à triagem" not in html


# --- Fase 3: colunas do Lex, chave e botões ---------------------------------------

def _sql(cliente, sql, *args):
    conn = banco.conectar(cliente.caminho)
    try:
        with conn:
            conn.execute(sql, args)
    finally:
        conn.close()


def _campos(cliente, *nomes):
    conn = banco.conectar(cliente.caminho)
    try:
        return tuple(conn.execute(f"SELECT {', '.join(nomes)} FROM demanda WHERE id = 1").fetchone())
    finally:
        conn.close()


def _fragmento(cliente):
    return cliente.get("/quadro/fragmento").get_data(as_text=True)


def test_fragmento_mostra_as_cinco_colunas(cliente):
    html = _fragmento(cliente)
    for titulo in ("Chegou do PJe", "Precisa de ação", "Autos baixados", "Lex minutando",
                   "Sua revisão"):
        assert f'aria-label="{titulo}"' in html
    assert "O Lex está livre." in html and "Nenhuma peça esperando sua revisão." in html
    assert "Nenhum cartão com autos prontos." in html


def test_chave_do_lex_automatico_no_cabecalho(cliente):
    html = _fragmento(cliente)
    assert 'role="switch" aria-checked="false"' in html and "Lex automático" in html
    assert 'name="ligado" value="1"' in html
    cliente.post("/lex/automatico", data={"ligado": "1"})
    html = _fragmento(cliente)
    assert 'aria-checked="true"' in html and 'name="ligado" value="0"' in html


def test_mandar_pro_lex_aparece_com_a_chave_desligada_e_ligada(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos' WHERE id = 1")
    html = _fragmento(cliente)
    assert "Mandar pro Lex" in html and 'maxlength="600"' in html
    assert "Orientação para o Lex (opcional)" in html and "Resolvido" in html
    # Com a chave ligada o cartão pode estar em "Autos baixados" (chegou antes de ligar,
    # foi tirado do Lex): o botão continua lá.
    _sql(cliente, "INSERT INTO config (chave, valor) VALUES ('lex_automatico', '1')")
    html = _fragmento(cliente)
    assert "Mandar pro Lex" in html and "Resolvido" in html


def test_mandar_pro_lex_aparece_com_sem_tipo_mesmo_com_chave_ligada(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos', lex_estado = 'sem_tipo', "
                  "lex_detalhe = 'O Lex não teve segurança sobre o tipo.' WHERE id = 1")
    _sql(cliente, "INSERT INTO config (chave, valor) VALUES ('lex_automatico', '1')")
    html = _fragmento(cliente)
    assert "Mandar pro Lex" in html
    assert ('class="cartao-autos tom-aviso">O Lex não escolheu o tipo de peça: '
            'escreva a orientação e mande de novo') in html  # frase antiga: sem explicação


def test_sem_tipo_mostra_a_explicacao_do_lex_e_oculta_na_apresentacao(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos', lex_estado = 'sem_tipo', "
                  "lex_detalhe = 'a intimação não diz se cabe réplica ou embargos' WHERE id = 1")
    html = _fragmento(cliente)
    assert ("O Lex não escolheu o tipo de peça: a intimação não diz se cabe réplica ou "
            "embargos") in html
    cliente.post("/apresentacao")
    html = _fragmento(cliente)
    assert "réplica ou embargos" not in html
    assert ('O Lex não escolheu o tipo de peça: <span class="texto-oculto">texto oculto no '
            'modo apresentação</span>') in html


def test_mover_com_orientacao_grava(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos' WHERE id = 1")
    r = cliente.post("/demanda/1/mover", headers=JSON,
                     data={"para": "lex", "orientacao": "  contestar só a preliminar "})
    assert r.status_code == 200
    assert _campos(cliente, "coluna", "lex_estado", "lex_orientacao") == (
        "lex", "na_fila", "contestar só a preliminar")
    html = _fragmento(cliente)
    assert "contestar só a preliminar" in html and "Tirar do Lex" in html


def test_mover_sem_orientacao_aceita_campo_ausente(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos' WHERE id = 1")
    assert cliente.post("/demanda/1/mover", data={"para": "lex"}, headers=JSON).status_code == 200
    assert _campos(cliente, "lex_orientacao") == ("",)


def test_orientacao_longa_e_cortada_em_600(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos' WHERE id = 1")
    cliente.post("/demanda/1/mover", data={"para": "lex", "orientacao": "x" * 900}, headers=JSON)
    assert len(_campos(cliente, "lex_orientacao")[0]) == 600


def test_lex_automatico_liga_e_enfileira(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos' WHERE id = 1")
    r = cliente.post("/lex/automatico", data={"ligado": "1"}, headers=JSON)
    assert r.get_json()["mensagem"] == "Lex automático ligado: 1 cartão(ões) foram para o Lex."
    assert _campos(cliente, "coluna", "lex_estado") == ("lex", "na_fila")
    r = cliente.post("/lex/automatico", data={"ligado": "0"}, headers=JSON)
    assert r.get_json()["mensagem"] == "Lex automático desligado."
    assert _campos(cliente, "coluna") == ("lex",)  # o que já está no Lex continua lá


def test_lex_automatico_sem_js_redireciona(cliente):
    r = cliente.post("/lex/automatico", data={"ligado": "1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def test_coluna_do_lex_rotulos_e_botoes(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'falhou', "
                  "lex_detalhe = 'quebrou' WHERE id = 1")
    html = _fragmento(cliente)
    assert "falhou: o Lex não terminou a peça (detalhe técnico no histórico)" in html
    assert "quebrou" not in html and "Tentar de novo" in html and "Tirar do Lex" in html
    assert "cartao-falhou" in html
    _sql(cliente, "UPDATE demanda SET lex_estado = 'trabalhando', lex_detalhe = '', "
                  "lex_reservado_ate = '2999-01-01T00:00:00-04:00' WHERE id = 1")
    html = _fragmento(cliente)
    assert "Lex trabalhando" in html
    assert "Tirar do Lex" not in html and "Tentar de novo" not in html


def test_tentar_lex_de_novo_pela_rota(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'falhou' WHERE id = 1")
    r = cliente.post("/demanda/1/lex/tentar", headers=JSON)
    assert r.status_code == 200 and _campos(cliente, "lex_estado") == ("na_fila",)
    r = cliente.post("/demanda/1/lex/tentar", headers=JSON)
    assert r.status_code == 409 and r.get_json()["ok"] is False


def test_tentar_lex_de_novo_em_pausado_limite_pela_rota(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'pausado_limite' WHERE id = 1")
    assert cliente.post("/demanda/1/lex/tentar", headers=JSON).status_code == 200
    assert _campos(cliente, "lex_estado") == ("na_fila",)


def _peca_pronta(cliente, falhas, gate):
    _sql(cliente, "UPDATE demanda SET coluna = 'revisao', lex_estado = 'pronto', "
                  "criada_em = '2026-10-07T08:00:00-04:00' WHERE id = 1")
    _sql(cliente, "INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
                  "resultado, squad, gate_status, citacoes_total, citacoes_falhas) "
                  "VALUES (1, 1, 'novo', '2026-10-07T08:23:00-04:00', "
                  "'2026-10-07T09:35:00-04:00', 'pronto', 'peticao-x', ?, 3, ?)", gate, falhas)


def test_revisao_mostra_tempos_selo_abrir_e_protocolei(cliente):
    _peca_pronta(cliente, 0, "aprovado")
    html = _fragmento(cliente)
    assert "pronta em 1 h 35 min · Lex 1 h 12 min" in html
    assert "citações conferidas" in html and "citação a conferir" not in html
    assert 'href="/demanda/' in html and ">Abrir<" in html and "Protocolei" in html


def test_revisao_com_citacao_a_conferir(cliente):
    _peca_pronta(cliente, 2, "aprovado")
    assert "citação a conferir" in _fragmento(cliente)
    _sql(cliente, "UPDATE execucao_lex SET citacoes_falhas = 0, gate_status = 'reprovado'")
    html = _fragmento(cliente)
    assert "citação a conferir" in html and "citações conferidas" not in html


def test_protocolei_leva_a_protocolado(cliente):
    _peca_pronta(cliente, 0, "aprovado")
    r = cliente.post("/demanda/1/mover", data={"para": "protocolado"}, headers=JSON)
    assert r.status_code == 200 and r.get_json()["desfazer"] == "revisao"
    assert _campos(cliente, "coluna") == ("protocolado",)
    assert "Protocolei" not in _fragmento(cliente)


def test_todo_form_do_quadro_leva_csrf(cliente):
    import re
    _sql(cliente, "UPDATE demanda SET coluna = 'autos' WHERE id = 1")
    html = _fragmento(cliente)
    forms = re.findall(r"<form\b.*?</form>", html, re.S)
    assert len(forms) >= 5
    assert all('name="csrf"' in f for f in forms)


def test_quadro_nao_tem_atributo_morto_de_detalhe(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos' WHERE id = 1")
    assert "data-cartao-detalhe" not in _fragmento(cliente)


# --- onda final -------------------------------------------------------------------

def _execucao(cliente, resultado="falhou"):
    _sql(cliente, "INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
                  "resultado) VALUES (1, 1, 'novo', '2026-10-05T10:00:00-04:00', "
                  "'2026-10-05T10:30:00-04:00', ?)", resultado)


def test_abrir_em_todo_cartao(cliente):
    # Também sem execução: a página do cartão traz a íntegra e o "Este processo é seu?".
    assert 'href="/demanda/1">Abrir</a>' in _fragmento(cliente)
    _execucao(cliente)
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'falhou' WHERE id = 1")
    assert 'href="/demanda/1">Abrir</a>' in _fragmento(cliente)
    _sql(cliente, "UPDATE demanda SET coluna = 'autos', lex_estado = 'sem_tipo', "
                  "lex_detalhe = 'sem segurança' WHERE id = 1")
    html = _fragmento(cliente)
    assert 'href="/demanda/1">Abrir</a>' in html and "Mandar pro Lex" in html


def test_reserva_vencida_no_quadro(cliente):
    _execucao(cliente, "")
    _sql(cliente, "UPDATE execucao_lex SET terminada_em = NULL")
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'trabalhando', "
                  "lex_reservado_ate = '2020-01-01T00:00:00-04:00' WHERE id = 1")
    html = _fragmento(cliente)
    assert "o Mac parou de responder" in html and "Tirar do Lex" in html
    assert "Lex trabalhando" not in html


def test_mac_sem_plano_aparece_no_quadro(cliente):
    from nucleo import ponte
    conn = banco.conectar(cliente.caminho)
    with conn:
        ponte.registrar_contato(conn, claude_ok=False)
    conn.close()
    assert "Mac conectado, sem acesso ao plano do Lex" in _fragmento(cliente)
    conn = banco.conectar(cliente.caminho)
    with conn:
        ponte.registrar_contato(conn, claude_ok=True)
    conn.close()
    assert "sem acesso ao plano" not in _fragmento(cliente)


def test_nome_do_squad_no_cartao_de_revisao(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'revisao', lex_estado = 'pronto', "
                  "lex_squad = 'EMBARGOS-DE-DECLARACAO' WHERE id = 1")
    html = _fragmento(cliente)
    assert '<p class="cartao-tipo">Embargos de declaração</p>' in html
    assert "EMBARGOS-DE-DECLARACAO" not in html


def test_faixa_do_quadro_para_a_sombra_de_rolagem(cliente):
    html = _fragmento(cliente)
    assert '<div class="quadro-faixa">' in html


@pytest.mark.parametrize("de,para,por,texto", [
    ("lex", "autos", "advogado", "tirado do Lex"),
    ("lex", "autos", "mac", "Lex não escolheu o tipo de peça"),
    ("protocolado", "revisao", "advogado", "protocolo desfeito"),
    ("lex", "revisao", "mac", "peça pronta para a sua revisão"),
    ("autos", "lex", "sistema", "enviado ao Lex automaticamente"),
])
def test_atividade_agora_pelo_movimento(cliente, de, para, por, texto):
    from nucleo import eventos
    conn = banco.conectar(cliente.caminho)
    with conn:
        eventos.registrar(conn, "demanda_movida", A, {"demanda": 1, "de": de, "para": para,
                                                       "por": por})
    conn.close()
    html = _fragmento(cliente)
    atividade = html[html.index("Atividade agora"):]
    assert texto in atividade
