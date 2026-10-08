"""Modo apresentação: nomes fictícios, números CNJ mascarados, textos livres ocultos
e downloads/busca indisponíveis (para mostrar o sistema sem expor clientes)."""

import json
import pathlib
import re
import time

import pytest

import painel
from nucleo import apresentacao, banco, painel_dados
from scripts import banco_demo

OCULTO = "texto oculto no modo apresentação"
INDISPONIVEL = "Indisponível no modo apresentação."


# --- funções puras -------------------------------------------------------------------

def test_nome_ficticio_e_deterministico_e_normaliza():
    a = apresentacao.nome_ficticio("MARIA APARECIDA SOUZA")
    assert a == apresentacao.nome_ficticio("Maria  Aparecida   Souza")
    assert a != "MARIA APARECIDA SOUZA"
    assert len(a.split()) == 3 and a == a.upper()
    assert apresentacao.nome_ficticio("JOÃO PEDRO LIMA") != a


@pytest.mark.parametrize("nome", ["BANCO ALFA S.A.", "CONSTRUTORA HORIZONTE LTDA",
                                  "COMERCIO XYZ S/A", "PADARIA BOA EIRELI", "LOJA PEQUENA ME",
                                  "BANCO DO ESTADO DE MATO GROSSO"])
def test_nome_ficticio_de_empresa(nome):
    f = apresentacao.nome_ficticio(nome)
    assert re.fullmatch(r"EMPRESA \S+ LTDA", f), f


@pytest.mark.parametrize("nome", ["MUNICÍPIO DE VÁRZEA ALTA", "ESTADO DE MATO GROSSO"])
def test_nome_ficticio_de_ente_publico(nome):
    assert re.fullmatch(r"ENTE PÚBLICO \S+", apresentacao.nome_ficticio(nome))


def test_mascarar_cnj_formatado_e_cru():
    texto = "Proc. 0000001-02.2024.8.11.0041 e 00000130220244013600; 123456789012345678901"
    saida = apresentacao.mascarar_cnj(texto)
    assert re.search(r"\b\d{7}-••\.2024\.8\.11\.••••", saida)
    assert re.search(r"\b\d{7}-••\.2024\.4\.01\.••••", saida)
    assert "0000001-02.2024.8.11.0041" not in saida and "00000130220244013600" not in saida
    assert "123456789012345678901" in saida  # 21 dígitos não é número CNJ


def test_mascara_cnj_distinguivel_estavel_e_pela_chave():
    a, b = "00000010220248110041", "00000020220248110041"
    formatado = f"{a[:7]}-{a[7:9]}.{a[9:13]}.{a[13]}.{a[14:16]}.{a[16:]}"
    assert apresentacao.mascarar_cnj(a) == apresentacao.mascarar_cnj(formatado)
    assert apresentacao.mascarar_cnj(a) != apresentacao.mascarar_cnj(b)
    assert apresentacao.mascarar_cnj(a, b"k1") != apresentacao.mascarar_cnj(a, b"k2")
    assert re.fullmatch(r"\d{7}-••\.2024\.8\.11\.••••", apresentacao.mascarar_cnj(a, b"k1"))


@pytest.mark.parametrize("texto", [
    "MT0054321A", "OAB/MT 54321", "OAB-SP nº 123.456-A", "OAB/MT54321", "54321/MT",
    "54.321/MT", "MT-54321-A", "MT12345-A", "MT 54321", "oab/mt 54321", "OAB MT 54321",
    "MT54321", "mt0054321a"])
def test_mascarar_oab(texto):
    uf = "SP" if "SP" in texto else "MT"
    assert apresentacao.mascarar_oab(f"adv. ({texto}).") == f"adv. (OAB/{uf} •••••)."


def test_mascarar_oab_sem_uf():
    assert apresentacao.mascarar_oab("adv. (13408/B).") == "adv. (OAB •••••)."


def test_mascarar_oab_nao_pega_outras_coisas():
    texto = ("MT 2024 · p217958c482f7 · TJMT · 1ª Vara · TRF1 · se 2024 · Lei 8.078/90 · "
             "Art. 1015/CPC · 01/10/2026 · R$ 1.250.000,00 · color:#ac1234 · 06:00 · "
             + apresentacao.mascarar_cnj("00000010220248110041"))
    assert apresentacao.mascarar_oab(texto) == texto


def test_mascarar_html_sem_acento_e_espacos_html():
    nomes = ["JOÃO DA CONCEIÇÃO"]
    falso = apresentacao.nome_ficticio("JOÃO DA CONCEIÇÃO")
    saida = apresentacao.mascarar_html("<p>Joao da Conceicao · JOÃO&nbsp;DA  CONCEIÇÃO</p>", nomes)
    assert saida == f"<p>{falso} · {falso}</p>"


def test_mascarar_html_preserva_script_meta_e_campo_escondido():
    nomes = ["ANA"]
    html = ('<meta name="csrf" content="x-ANA-0000001-02.2024.8.11.0041">'
            '<input type="hidden" name="csrf" value="ANA">'
            "<script>var a = 'ANA';</script><p>ANA</p>")
    saida = apresentacao.mascarar_html(html, nomes)
    assert '<meta name="csrf" content="x-ANA-0000001-02.2024.8.11.0041">' in saida
    assert '<input type="hidden" name="csrf" value="ANA">' in saida
    assert "<script>var a = 'ANA';</script>" in saida
    assert f"<p>{apresentacao.nome_ficticio('ANA')}</p>" in saida


def test_mascarar_html_troca_forma_escapada_e_respeita_palavras():
    nomes = ["D'ÁVILA & FILHOS LTDA", "ANA", "MARIA SOUZA LIMA", "MARIA SOUZA"]
    html = ("<p>D&#39;ÁVILA &amp; FILHOS LTDA</p><p>ANA · MARIANA · Análise</p>"
            "<p title=\"Maria Souza Lima\">MARIA SOUZA</p>")
    saida = apresentacao.mascarar_html(html, nomes)
    assert "ÁVILA" not in saida
    assert apresentacao.nome_ficticio("D'ÁVILA & FILHOS LTDA") in saida
    assert "MARIANA" in saida and "Análise" in saida  # só a palavra inteira
    assert f"<p>{apresentacao.nome_ficticio('ANA')} ·" in saida
    assert f'title="{apresentacao.nome_ficticio("MARIA SOUZA LIMA")}"' in saida
    assert f">{apresentacao.nome_ficticio('MARIA SOUZA')}</p>" in saida


def test_mascarar_html_nao_retroalimenta_e_mascara_cnj():
    nomes = ["JOSE SILVA"]
    saida = apresentacao.mascarar_html("JOSE SILVA 0000001-02.2024.8.11.0041", nomes)
    assert saida == (f"{apresentacao.nome_ficticio('JOSE SILVA')} "
                     f"{apresentacao.mascarar_cnj('0000001-02.2024.8.11.0041')}")


def test_mascarar_html_rapido_com_muitos_nomes():
    nomes = [f"PESSOA NUMERO {i} DA SILVA" for i in range(800)]
    html = ("<tr><td>texto qualquer de tabela com acentuação</td></tr>" * 6000
            + "PESSOA NUMERO 799 DA SILVA")
    apresentacao.mascarar_html("x", nomes)  # compila e guarda o padrão
    inicio = time.perf_counter()
    saida = apresentacao.mascarar_html(html, nomes)
    assert time.perf_counter() - inicio < 1.5
    assert "PESSOA NUMERO 799" not in saida


# --- com o banco de demonstração -------------------------------------------------------

@pytest.fixture
def caminho_demo(tmp_path, monkeypatch):
    caminho = tmp_path / "demo.db"
    conn = banco.conectar(str(caminho))
    banco_demo.gerar(conn, painel_dados.hoje_cuiaba())
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(caminho))
    monkeypatch.setenv("CONTROLADORIA_PECAS", str(tmp_path / "pecas"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    return str(caminho)


@pytest.fixture
def cliente(caminho_demo):
    return painel.app.test_client()


def _ligar(cliente):
    assert cliente.post("/apresentacao").status_code == 302
    with cliente.session_transaction() as sessao:
        assert sessao["apresentacao"] is True


def _demo(caminho):
    conn = banco.conectar(caminho)
    try:
        nomes = apresentacao.nomes_conhecidos(conn)
        numeros = [r[0] for r in conn.execute("SELECT numero FROM processo")]
        demandas = [r[0] for r in conn.execute("SELECT id FROM demanda")]
    finally:
        conn.close()
    return nomes, numeros, demandas


def test_nomes_conhecidos(caminho_demo):
    nomes, _, _ = _demo(caminho_demo)
    for esperado in ("MARIA APARECIDA SOUZA", "BANCO ALFA S.A.", "HELENA PRADO",
                     "ADVOGADO FICTÍCIO", "ADVOGADA DEMONSTRAÇÃO", "ESTADO DE MATO GROSSO"):
        assert esperado in nomes
    assert all(n.strip() for n in nomes)
    assert len(nomes) == len(set(nomes))
    assert [len(n) for n in nomes] == sorted((len(n) for n in nomes), reverse=True)


def _paginas(numeros, demandas):
    return (["/", "/carteira", "/carteira?aba=a_conferir", "/quadro/fragmento", "/resultados"]
            + [f"/processo/{n}" for n in numeros] + [f"/demanda/{i}" for i in demandas])


def _formas_do_numero(n):
    return (n, f"{n[:7]}-{n[7:9]}.{n[9:13]}.{n[13]}.{n[14:16]}.{n[16:]}")


def test_teste_chave_nada_da_demo_vaza_com_o_modo_ligado(cliente, caminho_demo):
    nomes, numeros, demandas = _demo(caminho_demo)
    assert nomes and numeros and demandas
    paginas = _paginas(numeros, demandas)

    vistos_nomes, vistos_numeros = set(), set()
    for url in paginas:  # desligado: os dados reais aparecem
        r = cliente.get(url)
        assert r.status_code == 200, url
        html = r.get_data(as_text=True)
        vistos_nomes.update(n for n in nomes if n in html)
        vistos_numeros.update(n for n in numeros if any(f in html for f in _formas_do_numero(n)))
    assert vistos_nomes == set(nomes)
    assert vistos_numeros == set(numeros)

    _ligar(cliente)
    for url in paginas:
        r = cliente.get(url)
        assert r.status_code == 200, url
        html = r.get_data(as_text=True)
        alto = html.upper()
        for nome in nomes:
            assert nome.upper() not in alto, (url, nome)
        for numero in numeros:
            for forma in _formas_do_numero(numero):
                assert forma not in html, (url, forma)
        if url != "/quadro/fragmento":
            assert "Modo apresentação ligado" in html


def test_nomes_ficticios_consistentes_entre_paginas(cliente, caminho_demo):
    _ligar(cliente)
    falso = apresentacao.nome_ficticio("MARIA APARECIDA SOUZA",
                                       painel._chave_derivada(b"apresentacao-nome"))
    assert falso in cliente.get("/carteira").get_data(as_text=True)
    assert falso in cliente.get(f"/processo/{banco_demo._tjmt(1)}").get_data(as_text=True)
    assert re.search(r"\d{7}-••\.2024\.8\.11\.••••", cliente.get("/carteira").get_data(as_text=True))


def test_botao_e_faixa(cliente):
    html = cliente.get("/carteira").get_data(as_text=True)
    assert "Modo apresentação" in html and 'aria-pressed="false"' in html
    assert "Modo apresentação ligado" not in html
    _ligar(cliente)
    html = cliente.get("/carteira").get_data(as_text=True)
    assert "Modo apresentação ligado" in html
    assert re.search(r'action="/apresentacao"[\s\S]{0,400}aria-pressed="true"', html)
    cliente.post("/apresentacao")
    with cliente.session_transaction() as sessao:
        assert sessao["apresentacao"] is False
    assert "Modo apresentação ligado" not in cliente.get("/carteira").get_data(as_text=True)


def test_alternar_exige_csrf(cliente):
    painel.app.config["CSRF_EXIGIDO"] = True
    r = cliente.post("/apresentacao")
    assert r.status_code in (302, 400)
    with cliente.session_transaction() as sessao:
        assert not sessao.get("apresentacao")
        token = sessao.get("csrf")
    if not token:
        cliente.get("/carteira")
        with cliente.session_transaction() as sessao:
            token = sessao["csrf"]
    r = cliente.post("/apresentacao", data={"csrf": token, "voltar": "/carteira"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/carteira")
    with cliente.session_transaction() as sessao:
        assert sessao["apresentacao"] is True


def test_alternar_ignora_voltar_externo(cliente):
    r = cliente.post("/apresentacao", data={"voltar": "//site.test/x"})
    assert r.headers["Location"].endswith("/")


def test_links_usam_apelido_que_abre_o_processo(cliente):
    _ligar(cliente)
    html = cliente.get("/carteira").get_data(as_text=True)
    apelidos = re.findall(r'href="/processo/(p[0-9a-f]{12})"', html)
    assert apelidos
    r = cliente.get(f"/processo/{apelidos[0]}")
    assert r.status_code == 200
    assert "Linha do tempo" in r.get_data(as_text=True)


def test_descartar_pelo_apelido(cliente, caminho_demo):
    _ligar(cliente)
    html = cliente.get("/carteira?aba=a_conferir").get_data(as_text=True)
    m = re.search(r'name="numero" value="(p[0-9a-f]{12})"', html)
    assert m
    antes = cliente.get("/carteira?aba=a_conferir").get_data(as_text=True).count("<tr data-href")
    r = cliente.post("/carteira/descartar", data={"numero": m.group(1),
                                                  "voltar": "/carteira?aba=a_conferir"})
    assert r.status_code == 302
    depois = cliente.get("/carteira?aba=a_conferir").get_data(as_text=True).count("<tr data-href")
    assert depois == antes - 1


def test_redirecionamento_nao_mostra_o_numero(cliente):
    _ligar(cliente)
    numero = banco_demo._tjmt(1)
    r = cliente.post("/carteira/descartar", data={"numero": numero,
                                                  "voltar": f"/processo/{numero}"})
    assert numero not in r.headers["Location"]
    assert re.search(r"/processo/p[0-9a-f]{12}$", r.headers["Location"])


def test_textos_livres_ocultos(cliente):
    url = f"/processo/{banco_demo._tjmt(1)}"
    assert "Fica a parte intimada" in cliente.get(url).get_data(as_text=True)
    assert "Fica a parte intimada" in cliente.get("/quadro/fragmento").get_data(as_text=True)
    _ligar(cliente)
    for pagina in (url, "/quadro/fragmento", "/"):
        html = cliente.get(pagina).get_data(as_text=True)
        assert "Fica a parte intimada" not in html, pagina
        assert OCULTO in html, pagina
    assert "Conclusos para decisão" not in cliente.get(url).get_data(as_text=True)


@pytest.mark.parametrize("url", [
    "/buscar?q=maria", "/carteira/exportar", f"/baixar-zip/{banco_demo._tjmt(1)}",
    "/demanda/1/peca/1.pdf", "/demanda/1/peca/1.docx",
    f"/documento/{banco_demo._tjmt(1)}/123", "/grupos/x/exportar", "/grupos/x/zip"])
def test_rotas_indisponiveis(cliente, url):
    _ligar(cliente)
    r = cliente.get(url)
    assert r.status_code == 403
    assert INDISPONIVEL in r.get_data(as_text=True)


def test_downloads_em_fluxo_indisponiveis(cliente):
    _ligar(cliente)
    for url in ("/baixar-lote/stream", f"/baixar-processo/{banco_demo._tjmt(1)}/stream"):
        r = cliente.post(url)
        assert r.status_code == 403 and INDISPONIVEL in r.get_data(as_text=True)


def test_exportar_funciona_com_o_modo_desligado(cliente):
    assert cliente.get("/carteira/exportar").status_code == 200


def test_json_fica_como_esta(cliente):
    _ligar(cliente)
    r = cliente.post("/varrer", headers={"Accept": "application/json"})
    assert r.is_json and r.get_json()["ok"] is True


def test_busca_do_topo_e_exportar_somem(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert 'id="busca-topo-form"' in html and "/carteira/exportar" in html
    _ligar(cliente)
    html = cliente.get("/").get_data(as_text=True)
    assert 'id="busca-topo-form"' not in html and "/carteira/exportar" not in html


# --- cartão com peça pronta ------------------------------------------------------------

@pytest.fixture
def cliente_com_peca(caminho_demo, tmp_path):
    conn = banco.conectar(caminho_demo)
    d = conn.execute("SELECT id FROM demanda ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE demanda SET coluna = 'revisao', lex_estado = 'pronto', "
                 "lex_orientacao = 'Contestar por prescrição', lex_ajuste = 'Trocar o fato 2' "
                 "WHERE id = ?", (d,))
    conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em, terminada_em, "
                 "resultado, squad, gate_status, detalhe) VALUES (?, 1, 'novo', "
                 "'2026-10-07T08:23:00-04:00', '2026-10-07T09:35:00-04:00', 'pronto', "
                 "'contestacao', 'aprovado', 'detalhe livre do Mac')", (d,))
    conn.commit()
    conn.close()
    pasta = tmp_path / "pecas" / str(d) / "1"
    pasta.mkdir(parents=True)
    (pasta / "peca.pdf").write_bytes(b"%PDF-1.4 fake")
    (pasta / "peca.docx").write_bytes(b"PK fake")
    (pasta / "nota-ao-revisor.md").write_text("Nota secreta do caso.", encoding="utf-8")
    (pasta / "citation-gate.json").write_text(json.dumps({
        "gate_status": "aprovado",
        "citations": [{"title": "Súmula 297 do STJ", "status": "verificada"}],
        "pendencias_do_profissional": [{"marcador": "[CONFERIR DATA]", "onde": "Fato 3",
                                        "diligencia": "Pedir o extrato"}]}), encoding="utf-8")
    c = painel.app.test_client()
    c.demanda = d
    return c


def test_cartao_aberto_no_modo(cliente_com_peca):
    c = cliente_com_peca
    url = f"/demanda/{c.demanda}"
    html = c.get(url).get_data(as_text=True)
    for texto in ("Nota secreta do caso.", "Pedir o extrato", "Contestar por prescrição",
                  "Trocar o fato 2", "<iframe", "Baixar Word"):
        assert texto in html
    with c.get(f"/demanda/{c.demanda}/peca/1.pdf") as r:
        assert r.status_code == 200
    _ligar(c)
    html = c.get(url).get_data(as_text=True)
    for texto in ("Nota secreta do caso.", "Pedir o extrato", "[CONFERIR DATA]",
                  "Contestar por prescrição", "Trocar o fato 2", "<iframe", "Baixar Word",
                  "Fica a parte intimada", "detalhe livre do Mac"):
        assert texto not in html, texto
    assert OCULTO in html
    assert "Súmula 297 do STJ" in html  # citação é norma, não dado do cliente
    assert c.get(f"/demanda/{c.demanda}/peca/1.pdf").status_code == 403


def test_templates_sem_safe_novo():
    raiz = pathlib.Path(painel.app.root_path) / "templates"
    for nome in ("base.html", "_quadro.html", "demanda.html", "cockpit.html",
                 "indisponivel.html"):
        texto = (raiz / nome).read_text()
        assert "|safe" not in texto and "| safe" not in texto, nome


# --- rodada de correção 1 ----------------------------------------------------------

def _sql(caminho, sql, *params):
    conn = banco.conectar(caminho)
    try:
        with conn:
            conn.execute(sql, params)
    finally:
        conn.close()


def test_processo_so_no_cache_fica_indisponivel(cliente, monkeypatch):
    numero = "12345670220248110041"
    dados = {"numero_formatado": "1234567-02.2024.8.11.0041", "partes": [
        {"polo": "Polo Ativo", "nomes": ["CLIENTE SÓ DO CACHE"], "integrantes": []}],
        "andamentos": [], "documentos": []}
    monkeypatch.setattr(painel.cache, "ler", lambda n: dados if n == numero else None)
    assert "CLIENTE SÓ DO CACHE" in cliente.get(f"/processo/{numero}").get_data(as_text=True)
    _ligar(cliente)
    r = cliente.get(f"/processo/{numero}")
    html = r.get_data(as_text=True)
    assert r.status_code == 403 and INDISPONIVEL in html
    assert "CLIENTE SÓ DO CACHE" not in html and "1234567-02" not in html


def test_processo_fora_da_carteira_e_do_cache_mostra_o_bloco_sem_formulario(cliente, monkeypatch):
    numero = "12345670220248110041"
    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    _ligar(cliente)
    r = cliente.get(f"/processo/{numero}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Este processo não está na sua carteira" in html
    assert "desligue o modo apresentação" in html and "Adicionar e acompanhar" not in html
    # O número só fica no "voltar" do botão do modo (é a própria URL que se abriu).
    sem_voltar = html.replace(f'name="voltar" value="/processo/{numero}"', "")
    assert "1234567-02" not in html and numero not in sem_voltar


def test_processo_do_banco_sem_partes_nao_usa_as_do_cache(cliente, caminho_demo, monkeypatch):
    numero = banco_demo._tjmt(1)
    _sql(caminho_demo, "UPDATE processo SET partes_json = '[]' WHERE numero = ?", numero)
    dados = {"partes": [{"polo": "Polo Ativo", "nomes": ["PARTE SÓ DO CACHE"],
                         "integrantes": []}], "andamentos": [], "documentos": []}
    monkeypatch.setattr(painel.cache, "ler", lambda n: dados if n == numero else None)
    assert "PARTE SÓ DO CACHE" in cliente.get(f"/processo/{numero}").get_data(as_text=True)
    _ligar(cliente)
    html = cliente.get(f"/processo/{numero}").get_data(as_text=True)
    assert "PARTE SÓ DO CACHE" not in html
    assert f"Partes e advogados: {OCULTO}" in html


def test_consulta_avulsa_some_no_modo(cliente):
    assert 'action="/consultar"' in cliente.get("/carteira").get_data(as_text=True)
    _ligar(cliente)
    assert 'action="/consultar"' not in cliente.get("/carteira").get_data(as_text=True)


@pytest.fixture
def com_grupo(cliente, tmp_path, monkeypatch):
    from captura import grupos
    monkeypatch.setattr(grupos, "PASTA_GRUPOS", str(tmp_path / "grupos"))
    g = grupos.criar("Carteira MARIA APARECIDA", "cliente antiga do banco",
                     [banco_demo._tjmt(1), banco_demo._tjmt(2)])
    return g


def test_grupos_no_modo(cliente, com_grupo):
    slug = com_grupo["slug"]
    html = cliente.get("/grupos").get_data(as_text=True)
    assert "Carteira MARIA APARECIDA" in html
    _ligar(cliente)
    html = cliente.get("/grupos").get_data(as_text=True)
    assert "Grupos ficam indisponíveis no modo apresentação." in html
    assert "Grupo 1" in html
    assert "MARIA APARECIDA" not in html.upper() and "cliente antiga" not in html
    assert slug not in html and "+ Novo grupo" not in html
    for url in (f"/grupos/{slug}", f"/grupos/{slug}/status", "/grupos/novo"):
        r = cliente.get(url)
        assert r.status_code == 403 and INDISPONIVEL in r.get_data(as_text=True), url
    for url in (f"/grupos/{slug}/baixar", f"/grupos/{slug}/atualizar",
                f"/grupos/{slug}/excluir"):
        assert cliente.post(url).status_code == 403, url
    from captura import grupos
    assert grupos.ler(slug) is not None  # excluir não passou


def test_historico_esconde_o_detalhe_da_falha(caminho_demo):
    from nucleo import eventos
    conn = banco.conectar(caminho_demo)
    d, numero = conn.execute("SELECT id, numero FROM demanda ORDER BY id LIMIT 1").fetchone()
    with conn:
        eventos.registrar(conn, "lex_falhou", numero,
                          {"demanda": d, "motivo": "tempo", "detalhe": "falha com nome secreto"})
    conn.close()
    c = painel.app.test_client()
    assert "falha com nome secreto" in c.get(f"/demanda/{d}").get_data(as_text=True)
    _ligar(c)
    html = c.get(f"/demanda/{d}").get_data(as_text=True)
    assert "falha com nome secreto" not in html
    assert "Lex não concluiu (tempo esgotado)" in html


def test_html_no_modo_nao_fica_em_cache_e_base_recarrega_no_voltar(cliente):
    assert "no-store" not in (cliente.get("/carteira").headers.get("Cache-Control") or "")
    html = cliente.get("/carteira").get_data(as_text=True)
    assert 'data-apresentacao="0"' in html and "back_forward" in html
    _ligar(cliente)
    r = cliente.get("/carteira")
    assert "no-store" in r.headers["Cache-Control"]
    assert 'data-apresentacao="1"' in r.get_data(as_text=True)


def test_oab_mascarada_na_pagina(cliente):
    url = f"/processo/{banco_demo._tjmt(1)}"
    assert "MT0012345A" in cliente.get(url).get_data(as_text=True)
    _ligar(cliente)
    html = cliente.get(url).get_data(as_text=True)
    assert "MT0012345A" not in html and "MT0099999A" not in html
    assert "(OAB •••••)" in html


def test_numeros_mascarados_distinguiveis_na_carteira(cliente):
    _ligar(cliente)
    html = cliente.get("/carteira").get_data(as_text=True)
    mascaras = re.findall(r"\d{7}-••\.\d{4}\.\d\.\d{2}\.••••", html)
    assert len(set(mascaras)) >= 10


def test_apelido_usa_chave_derivada(cliente):
    import hashlib
    import hmac
    numero = banco_demo._tjmt(1)
    direto = "p" + hmac.new(str(painel.app.secret_key).encode(), numero.encode(),
                            hashlib.sha256).hexdigest()[:12]
    assert painel._apelido(numero) != direto


def test_cache_dos_nomes_por_versao(cliente, caminho_demo, monkeypatch):
    _ligar(cliente)
    chamadas = []
    original = apresentacao.nomes_conhecidos
    monkeypatch.setattr(apresentacao, "nomes_conhecidos",
                        lambda conn: chamadas.append(1) or original(conn))
    monkeypatch.setattr(painel, "_cache_apresentacao", {})
    cliente.get("/carteira")
    cliente.get("/resultados")
    assert len(chamadas) == 1
    from nucleo import eventos
    conn = banco.conectar(caminho_demo)
    with conn:
        conn.execute("UPDATE processo SET cliente = 'NOVO CLIENTE SECRETO' WHERE numero = ?",
                     (banco_demo._tjmt(1),))
        eventos.registrar(conn, "processo_sincronizado", banco_demo._tjmt(1), {})
    conn.close()
    html = cliente.get("/carteira").get_data(as_text=True)
    assert len(chamadas) == 2 and "NOVO CLIENTE SECRETO" not in html


# --- rodada de correção 2 ----------------------------------------------------------

def test_oab_do_advogado_nao_e_renderizada(cliente, caminho_demo):
    numero = banco_demo._tjmt(1)
    partes = [{"polo": "Polo Ativo", "nomes": ["MARIA APARECIDA SOUZA"], "integrantes": [
        {"nome": "MARIA APARECIDA SOUZA", "advogados": [
            {"nome": "ADVOGADA DEMONSTRAÇÃO", "oab": "54.321/MT"},
            {"nome": "ADVOGADO FICTÍCIO", "oab": "13408/B"},
            {"nome": "OUTRO ADVOGADO", "oab": "inscrição esquisita 99"}]}]}]
    _sql(caminho_demo, "UPDATE processo SET partes_json = ? WHERE numero = ?",
         json.dumps(partes, ensure_ascii=False), numero)
    url = f"/processo/{numero}"
    html = cliente.get(url).get_data(as_text=True)
    assert "54.321/MT" in html and "inscrição esquisita 99" in html
    _ligar(cliente)
    html = cliente.get(url).get_data(as_text=True)
    assert "54.321" not in html and "13408" not in html and "esquisita" not in html
    assert "(OAB •••••)" in html


def _cartao_que_falhou(caminho, coluna, **campos):
    conn = banco.conectar(caminho)
    d = conn.execute("SELECT id FROM demanda ORDER BY id LIMIT 1").fetchone()[0]
    sets = ", ".join(f"{k} = ?" for k in campos)
    with conn:
        conn.execute(f"UPDATE demanda SET coluna = ?, {sets} WHERE id = ?",
                     (coluna, *campos.values(), d))
    conn.close()
    return d


def test_rotulo_do_lex_que_falhou_sem_detalhe(cliente, caminho_demo):
    d = _cartao_que_falhou(caminho_demo, "lex", lex_estado="falhou",
                           lex_detalhe="erro técnico com caminho /Users/x")
    # O detalhe cru nunca vai para o quadro nem para o cartão: só a frase fixa.
    for url in ("/quadro/fragmento", f"/demanda/{d}"):
        html = cliente.get(url).get_data(as_text=True)
        assert "falhou: o Lex não terminou a peça" in html, url
        assert "erro técnico" not in html and "/Users/x" not in html, url
    _ligar(cliente)
    for url in ("/quadro/fragmento", f"/demanda/{d}"):
        html = cliente.get(url).get_data(as_text=True)
        assert "erro técnico" not in html and "/Users/x" not in html, url
        assert "falhou: o Lex não terminou a peça" in html, url


def test_falha_dos_autos_sem_detalhe(cliente, caminho_demo):
    _cartao_que_falhou(caminho_demo, "acao", autos_estado="falhou",
                       autos_detalhe="SOAP fault em host interno 10.0.0.5")
    assert "SOAP fault" in cliente.get("/quadro/fragmento").get_data(as_text=True)
    _ligar(cliente)
    html = cliente.get("/quadro/fragmento").get_data(as_text=True)
    assert "SOAP fault" not in html and "Falha ao baixar os autos." in html


def test_documentos_do_cache_sem_descricao(cliente, monkeypatch):
    numero = banco_demo._tjmt(1)
    dados = {"partes": [], "andamentos": [], "documentos": [
        {"id": "9", "descricao": "Procuração de FULANO DO CACHE", "data": "01/10/2026",
         "mimetype": "application/pdf"}]}
    monkeypatch.setattr(painel.cache, "ler", lambda n: dados if n == numero else None)
    url = f"/processo/{numero}"
    assert "Procuração de FULANO DO CACHE" in cliente.get(url).get_data(as_text=True)
    _ligar(cliente)
    html = cliente.get(url).get_data(as_text=True)
    assert "FULANO" not in html and "Procuração" not in html
    assert '<span class="doc-nome">Documento</span>' in html


def test_cache_trocado_de_uma_vez(cliente, caminho_demo):
    _ligar(cliente)
    cliente.get("/carteira")
    antigo = painel._cache_apresentacao
    copia = dict(antigo)
    _sql(caminho_demo, "UPDATE processo SET cliente = 'OUTRO' WHERE numero = ?",
         banco_demo._tjmt(2))
    from nucleo import eventos
    conn = banco.conectar(caminho_demo)
    with conn:
        eventos.registrar(conn, "processo_sincronizado", banco_demo._tjmt(2), {})
    conn.close()
    cliente.get("/carteira")
    assert painel._cache_apresentacao is not antigo
    assert antigo == copia  # o dicionário antigo não foi esvaziado nem alterado


# --- onda final -------------------------------------------------------------------

def test_nome_ficticio_depende_da_chave():
    nome = "MARIA APARECIDA SOUZA"
    assert apresentacao.nome_ficticio(nome, b"k1") == apresentacao.nome_ficticio(nome, b"k1")
    # com muitos nomes, ao menos um muda de fictício ao trocar a chave
    nomes = [f"PESSOA NUMERO {i}" for i in range(20)] + ["BANCO ALFA S.A.", "ESTADO DE MT"]
    assert any(apresentacao.nome_ficticio(n, b"k1") != apresentacao.nome_ficticio(n, b"k2")
               for n in nomes)


def test_painel_usa_hmac_com_chave_derivada_nos_nomes(cliente):
    _ligar(cliente)
    html = cliente.get("/carteira").get_data(as_text=True)
    chave = painel._chave_derivada(b"apresentacao-nome")
    assert apresentacao.nome_ficticio("JOÃO PEDRO LIMA", chave) in html
    assert chave != painel._chave_derivada(b"apresentacao-cnj")


@pytest.mark.parametrize("texto", [
    "Processo de MT 2024 em diante", "valor 12345/SP do contrato", "Lei MT 54321",
    "protocolo 54.321/MT", "código MT-54321-A"])
def test_oab_solta_sem_contexto_nao_e_mascarada(texto):
    assert apresentacao.mascarar_oab(texto) == texto


@pytest.mark.parametrize("texto", [
    "Advogado: FULANO (54321/MT)", "adv. MT 54321", "OAB 12345/SP",
    "ADVOGADA FULANA - MT-54321-A"])
def test_oab_solta_com_contexto_e_mascarada(texto):
    saida = apresentacao.mascarar_oab(texto)
    assert "54321" not in saida and "12345" not in saida and "•••••" in saida


def test_oab_no_formato_do_pje_vale_sozinha():
    assert apresentacao.mascarar_oab("parte MT0054321A") == "parte OAB/MT •••••"


def test_mascarar_oab_lista_longa_de_advogados_numa_linha():
    texto = ("Advogados: MARIA APARECIDA DOS SANTOS FICTÍCIA (54321/MT), JOÃO PEDRO DE "
             "ALMEIDA EXEMPLO (12345/MT), ANA BEATRIZ CARVALHO DEMONSTRAÇÃO (54321/MT), "
             "CARLOS EDUARDO NUNES TESTE (99887/MT)")
    saida = apresentacao.mascarar_oab(texto)
    assert saida.count("OAB/MT •••••") == 4
    for numero in ("54321", "12345", "54321", "99887"):
        assert numero not in saida
    # sem "OAB"/"adv" por perto, a corrente não começa
    solto = "Processo 2024 · ver 12345/SP e 54321/SP"
    assert apresentacao.mascarar_oab(solto) == solto
