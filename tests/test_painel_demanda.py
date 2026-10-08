import json
import pathlib

import pytest

import painel
from nucleo import banco, quadro

A = "00000010220248110041"
GATE = {
    "gate_status": "aprovado",
    "citations": [
        {"title": "Súmula 297 do STJ", "status": "verificada",
         "source_url": "https://exemplo.test/sumula-297"},
        {"title": "REsp 1.000.000", "status": "nao_encontrada",
         "source_url": "javascript:alert(1)"},
    ],
    "pendencias_do_profissional": [
        {"marcador": "[CONFERIR DATA]", "onde": "Fato 3",
         "diligencia": "Pedir o extrato ao cliente"}],
}


def _sql(cliente, sql, *params):
    conn = banco.conectar(cliente.caminho)
    try:
        with conn:
            return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    caminho = str(tmp_path / "d.db")
    pecas = tmp_path / "pecas"
    conn = banco.conectar(caminho)
    conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua, arquivado, cliente, "
                 "parte_contraria, orgao_julgador) VALUES (?, 'TJMT', 1, 0, 'MARIA FICTÍCIA', "
                 "'BANCO X', '1ª VARA CÍVEL')", (A,))
    quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    conn.execute("UPDATE demanda SET coluna = 'revisao', lex_estado = 'pronto', "
                 "lex_orientacao = 'Contestar por prescrição', "
                 "criada_em = '2026-10-07T08:00:00-04:00' WHERE id = 1")
    conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
                 "resultado, squad, gate_status, citacoes_total, citacoes_falhas) "
                 "VALUES (1, 1, 'novo', '2026-10-07T08:23:00-04:00', "
                 "'2026-10-07T09:35:00-04:00', 'pronto', 'contestacao', 'aprovado', 2, 1)")
    conn.commit()
    conn.close()
    pasta = pecas / "1" / "1"
    pasta.mkdir(parents=True)
    (pasta / "peca.pdf").write_bytes(b"%PDF-1.4 fake")
    (pasta / "peca.docx").write_bytes(b"PK fake")
    (pasta / "citation-gate.json").write_text(json.dumps(GATE), encoding="utf-8")
    (pasta / "nota-ao-revisor.md").write_text("Confira <b>o prazo</b>.\nLinha 2.", encoding="utf-8")
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_PECAS", str(pecas))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    c = painel.app.test_client()
    c.caminho = caminho
    return c


def test_pagina_com_peca_pronta(cliente):
    r = cliente.get("/demanda/1")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Pronta em 1 h 35 min desde que a publicação chegou" in html
    assert "Lex trabalhou 1 h 12 min" in html
    assert '<iframe class="peca-pdf" src="/demanda/1/peca/1.pdf" title="Peça para leitura"' in html
    assert "sandbox" not in html
    assert "/demanda/1/peca/1.docx" in html and "Baixar Word" in html
    assert "O que conferir" in html and "<strong>[CONFERIR DATA]</strong>" in html
    assert "Fato 3" in html and "Pedir o extrato ao cliente" in html
    assert "Súmula 297 do STJ" in html and "REsp 1.000.000" in html
    assert "MARIA FICTÍCIA" in html and "BANCO X" in html and "1ª VARA CÍVEL" in html
    assert "Contestar por prescrição" in html and "Contestação" in html
    assert "Protocolei" in html and "Devolver ao Lex com ajuste" in html
    assert 'name="ajuste"' in html and "required" in html
    assert 'name="voltar" value="/demanda/1"' in html


def test_nota_ao_revisor_escapada(cliente):
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Confira &lt;b&gt;o prazo&lt;/b&gt;." in html and "<b>o prazo</b>" not in html


def test_link_de_fonte_so_http(cliente):
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert 'href="https://exemplo.test/sumula-297"' in html
    assert "javascript:" not in html


def test_pdf_inline_com_cabecalhos(cliente):
    r = cliente.get("/demanda/1/peca/1.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Content-Security-Policy"] == (
        "default-src 'none'; object-src 'self'; frame-ancestors 'self'")
    assert "attachment" not in (r.headers.get("Content-Disposition") or "")
    assert r.data == b"%PDF-1.4 fake"
    r.close()


def test_docx_como_anexo(cliente):
    r = cliente.get("/demanda/1/peca/1.docx")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    disp = r.headers["Content-Disposition"]
    assert "attachment" in disp and f"peca-{A}-v1.docx" in disp
    assert r.data == b"PK fake"
    r.close()


@pytest.mark.parametrize("url", ["/demanda/99", "/demanda/99/peca/1.pdf",
                                 "/demanda/1/peca/2.pdf", "/demanda/1/peca/2.docx",
                                 "/demanda/99/peca/1.docx"])
def test_inexistente_404(cliente, url):
    assert cliente.get(url).status_code == 404


def test_pasta_gravada_fora_das_pecas_nao_e_servida(cliente, tmp_path):
    fora = tmp_path / "fora"
    fora.mkdir()
    (fora / "peca.pdf").write_bytes(b"SEGREDO")
    _sql(cliente, "UPDATE execucao_lex SET pasta = ?", str(fora))
    (tmp_path / "pecas" / "1" / "1" / "peca.pdf").unlink()
    assert cliente.get("/demanda/1/peca/1.pdf").status_code == 404


def test_sem_pacote_nao_mostra_iframe(cliente, tmp_path):
    (tmp_path / "pecas" / "1" / "1" / "peca.pdf").unlink()
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "<iframe" not in html


def test_historico_aparece(cliente):
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Histórico" in html


def test_cartao_no_lex_mostra_tirar_e_tentar(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'falhou', "
                  "lex_detalhe = 'sem rede'")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Tirar do Lex" in html and "Tentar de novo" in html
    assert "Protocolei" not in html and "Devolver ao Lex" not in html
    assert 'action="/demanda/1/lex/tentar"' in html
    assert 'name="voltar" value="/demanda/1"' in html


def test_devolver_com_voltar_redireciona_para_o_cartao(cliente):
    r = cliente.post("/demanda/1/mover", data={"para": "lex", "ajuste": "Cortar a preliminar",
                                               "voltar": "/demanda/1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/demanda/1")
    assert _sql(cliente, "SELECT coluna, lex_ajuste FROM demanda WHERE id = 1")[0][:] == (
        "lex", "Cortar a preliminar")


def test_voltar_externo_ignorado_e_json_inalterado(cliente):
    r = cliente.post("/demanda/1/mover", data={"para": "protocolado",
                                               "voltar": "https://evil.test/"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert "evil.test" not in r.headers["Location"]
    _sql(cliente, "UPDATE demanda SET coluna = 'revisao'")
    r = cliente.post("/demanda/1/mover", data={"para": "protocolado", "voltar": "/demanda/1"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_tentar_de_novo_com_voltar(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'falhou'")
    r = cliente.post("/demanda/1/lex/tentar", data={"voltar": "/demanda/1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/demanda/1")


def test_quadro_abre_o_cartao(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert 'href="/demanda/1"' in html


def test_template_sem_safe():
    texto = (pathlib.Path(painel.app.root_path) / "templates" / "demanda.html").read_text()
    assert "|safe" not in texto and "| safe" not in texto


@pytest.mark.parametrize("status,tom", [
    ("verificada", "ok"), ("verificada_no_acervo", "ok"), ("nao_encontrada", "alerta"),
    ("divergente", "alerta"), ("desconhecido", "aviso"), ("", "aviso"), (None, "aviso")])
def test_selo_citacao(status, tom):
    from nucleo import painel_dados
    assert painel_dados.selo_citacao(status) == tom


def test_selo_na_pagina_para_verificada_no_acervo(cliente, tmp_path):
    gate = dict(GATE, citations=[{"title": "Tema 1", "status": "verificada_no_acervo"}])
    (tmp_path / "pecas" / "1" / "1" / "citation-gate.json").write_text(json.dumps(gate))
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert 'selo selo-ok">verificada_no_acervo' in html


def test_selo_do_relatorio_so_ok_se_aprovado_e_sem_falhas(cliente):
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert 'selo-alerta">aprovado' in html  # 1 citação falhou
    _sql(cliente, "UPDATE execucao_lex SET citacoes_falhas = 0")
    assert 'selo-ok">aprovado' in cliente.get("/demanda/1").get_data(as_text=True)
    _sql(cliente, "UPDATE execucao_lex SET gate_status = 'reprovado'")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert 'selo-alerta">reprovado' in html and 'selo-ok">reprovado' not in html


def test_pdf_so_se_a_execucao_existe(cliente, tmp_path):
    outra = tmp_path / "pecas" / "1" / "7"
    outra.mkdir(parents=True)
    (outra / "peca.pdf").write_bytes(b"%PDF")
    (outra / "peca.docx").write_bytes(b"PK")
    assert cliente.get("/demanda/1/peca/7.pdf").status_code == 404
    assert cliente.get("/demanda/1/peca/7.docx").status_code == 404


def test_pdf_aceita_range(cliente):
    r = cliente.get("/demanda/1/peca/1.pdf", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.data == b"%PDF"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
    r.close()


def test_symlink_para_fora_das_pecas_da_404(cliente, tmp_path):
    fora = tmp_path / "fora"
    fora.mkdir()
    (fora / "peca.pdf").write_bytes(b"SEGREDO")
    pasta = tmp_path / "pecas" / "1" / "1"
    (pasta / "peca.pdf").unlink()
    (pasta / "peca.pdf").symlink_to(fora / "peca.pdf")
    assert cliente.get("/demanda/1/peca/1.pdf").status_code == 404


@pytest.mark.parametrize("voltar", ["//evil.test", "/\\evil.test"])
def test_voltar_malicioso_cai_no_quadro(cliente, voltar):
    r = cliente.post("/demanda/1/mover", data={"para": "protocolado", "voltar": voltar})
    assert r.status_code == 302
    assert r.headers["Location"] in ("/", "http://localhost/")


# --- onda final -------------------------------------------------------------------

def test_protocolado_tem_desfazer_e_a_peca(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'protocolado', protocolado_em = 'x'")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Desfazer: voltar para Sua revisão" in html
    assert 'name="para" value="revisao"' in html
    assert "/demanda/1/peca/1.pdf" in html and "/demanda/1/peca/1.docx" in html
    r = cliente.post("/demanda/1/mover", data={"para": "revisao", "voltar": "/demanda/1"})
    assert r.status_code == 302
    assert _sql(cliente, "SELECT coluna, protocolado_em FROM demanda")[0][:] == ("revisao", None)


def test_ajuste_mostra_a_peca_anterior_e_desistir(cliente):
    cliente.post("/demanda/1/mover", data={"para": "lex", "ajuste": "Cortar a preliminar"})
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Peça anterior" in html and "versão 1" in html
    assert "/demanda/1/peca/1.pdf" in html and "Baixar Word" in html
    assert "Desistir do ajuste" in html
    r = cliente.post("/demanda/1/mover", data={"para": "revisao", "voltar": "/demanda/1"})
    assert r.status_code == 302
    assert _sql(cliente, "SELECT coluna, lex_estado, lex_ajuste FROM demanda")[0][:] == (
        "revisao", "pronto", "")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Desistir do ajuste" not in html and "Protocolei" in html


def test_desistir_some_com_o_lex_trabalhando(cliente):
    cliente.post("/demanda/1/mover", data={"para": "lex", "ajuste": "Cortar"})
    _sql(cliente, "UPDATE demanda SET lex_estado = 'trabalhando', "
                  "lex_reservado_ate = '2999-01-01T00:00:00-04:00'")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Desistir do ajuste" not in html and "Tirar do Lex" not in html
    r = cliente.post("/demanda/1/mover", data={"para": "revisao"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 409


def test_reserva_vencida_rotulo_e_tirar_do_lex(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'trabalhando', "
                  "lex_reservado_ate = '2020-01-01T00:00:00-04:00'")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "o Mac parou de responder" in html and "Tirar do Lex" in html
    r = cliente.post("/demanda/1/mover", data={"para": "autos"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert _sql(cliente, "SELECT coluna FROM demanda")[0][0] == "autos"


def test_falha_mostra_frase_fixa_e_detalhe_so_no_historico(cliente):
    from nucleo import eventos
    _sql(cliente, "UPDATE demanda SET coluna = 'lex', lex_estado = 'falhou', "
                  "lex_detalhe = 'o squad não é deste caso; pacote recusado'")
    _sql(cliente, "INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
                  "resultado, detalhe) VALUES (1, 2, 'ajuste', 'a', 'b', 'falhou', "
                  "'o squad não é deste caso; pacote recusado')")
    conn = banco.conectar(cliente.caminho)
    with conn:
        eventos.registrar(conn, "lex_falhou", A, {"demanda": 1, "n": 2, "motivo": "erro",
                                                  "detalhe": "o squad não é deste caso; pacote recusado"})
    conn.close()
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "O Lex achou uma peça de outro caso e a recusou." in html
    assert html.count("pacote recusado") == 1  # só no histórico


def test_nota_ao_revisor_em_markdown(cliente, tmp_path):
    (tmp_path / "pecas" / "1" / "1" / "nota-ao-revisor.md").write_text(
        "# Nota\n\n**Atenção** ao *prazo* e `art. 335`.\n\n- item um\n- item [dois](javascript:x)\n",
        encoding="utf-8")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "<h3>Nota</h3>" in html and "<strong>Atenção</strong>" in html
    assert "<em>prazo</em>" in html and "<code>art. 335</code>" in html
    assert "<li>item um</li>" in html and "# Nota" not in html
    assert 'href="javascript' not in html


def test_nome_do_squad_no_cartao_e_fora_do_sobretitulo(cliente):
    _sql(cliente, "UPDATE execucao_lex SET squad = 'replica-a-contestacao', squad_nome = ''")
    _sql(cliente, "UPDATE demanda SET lex_squad = 'replica-a-contestacao'")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "Réplica à contestação" in html and "replica-a-contestacao" not in html
    assert '<p class="sobretitulo">Sua revisão</p>' in html
    _sql(cliente, "UPDATE execucao_lex SET squad_nome = 'Réplica (nome do squad.yaml)'")
    _sql(cliente, "UPDATE demanda SET lex_squad_nome = 'Réplica (nome do squad.yaml)'")
    assert "Réplica (nome do squad.yaml)" in cliente.get("/demanda/1").get_data(as_text=True)


def test_meta_do_cartao_com_separadores(cliente):
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert '1ª VARA CÍVEL</span><span class="meta-sep" aria-hidden="true"> · </span>' in html
    assert 'peça pronta</span><span class="meta-sep" aria-hidden="true"> · </span>' in html


def test_processo_liga_ao_cartao_aberto(cliente):
    html = cliente.get(f"/processo/{A}").get_data(as_text=True)
    assert "Peças do Lex" in html and 'href="/demanda/1"' in html and "Contestação" in html


def test_sem_tipo_mostra_a_explicacao_do_lex_e_oculta_na_apresentacao(cliente):
    _sql(cliente, "UPDATE demanda SET coluna = 'autos', lex_estado = 'sem_tipo', "
                  "lex_detalhe = 'a publicação não diz se cabe réplica ou embargos'")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert ("O Lex não escolheu o tipo de peça: a publicação não diz se cabe réplica ou "
            "embargos") in html
    cliente.post("/apresentacao")
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert "réplica ou embargos" not in html
    assert ('O Lex não escolheu o tipo de peça: <span class="texto-oculto">texto oculto no '
            'modo apresentação</span>') in html


@pytest.mark.parametrize("estado,frase", [
    ("na_fila", "a última que ficou pronta; ajuste na fila do Lex."),
    ("reservado", "a última que ficou pronta; o Lex está trabalhando no ajuste."),
    ("trabalhando", "a última que ficou pronta; o Lex está trabalhando no ajuste."),
    ("falhou", "a última que ficou pronta; o ajuste falhou."),
    ("pausado_limite", "a última que ficou pronta; ajuste pausado: limite do plano."),
])
def test_ajuste_com_o_rotulo_do_estado_do_lex(cliente, estado, frase):
    cliente.post("/demanda/1/mover", data={"para": "lex", "ajuste": "Cortar a preliminar"})
    _sql(cliente, "UPDATE demanda SET lex_estado = ?, "
                  "lex_reservado_ate = '2999-01-01T00:00:00-04:00'", estado)
    html = cliente.get("/demanda/1").get_data(as_text=True)
    assert frase in html
    if estado not in ("reservado", "trabalhando"):
        assert "o Lex está trabalhando no ajuste" not in html
