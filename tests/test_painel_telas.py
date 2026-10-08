import json
import pytest

import painel
from nucleo import banco, painel_dados
from scripts import banco_demo


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    """Painel com banco de demonstração fictício e login desligado."""
    caminho = tmp_path / "demo.db"
    conn = banco.conectar(str(caminho))
    banco_demo.gerar(conn, painel_dados.hoje_cuiaba())
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(caminho))
    monkeypatch.setenv("CARTEIRA_ADVOGADO_NOME", "MARIA EXEMPLO DA SILVA")
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    return painel.app.test_client()


def test_base_tem_tema_fontes_e_navegacao(cliente):
    html = cliente.get("/grupos").get_data(as_text=True)
    assert 'id="btn-tema"' in html
    assert "fonts.googleapis.com" in html and "Cormorant+Garamond" in html
    assert 'class="navegacao"' in html and ">Cockpit" in html  # o contador da triagem pode vir depois
    assert "localStorage" in html


def test_login_esconde_a_navegacao(monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    html = painel.app.test_client().get("/login").get_data(as_text=True)
    assert 'class="navegacao"' not in html
    assert 'id="btn-tema"' in html


def test_carteira_lista_a_aba_ativa(cliente):
    html = cliente.get("/carteira").get_data(as_text=True)
    assert "MARIA APARECIDA SOUZA" in html and "BANCO ALFA S.A." in html
    assert "HELENA PRADO" not in html  # arquivado não aparece
    assert "selo-ok" in html and "Atua (pelo DJEN; situação não conferida)" in html


def test_carteira_tem_lote_e_rotulos_no_celular(cliente):
    html = cliente.get("/carteira").get_data(as_text=True)
    assert "Baixar em lote" in html and "/baixar-lote" in html
    assert 'data-rotulo="Cliente"' in html


def test_carteira_aba_a_conferir(cliente):
    html = cliente.get("/carteira?aba=a_conferir").get_data(as_text=True)
    assert "Sem acesso pelo PJe" in html
    assert "MARIA APARECIDA SOUZA" not in html


def test_carteira_filtra_por_busca_e_tribunal(cliente):
    html = cliente.get("/carteira?q=construtora").get_data(as_text=True)
    assert "JOÃO PEDRO LIMA" in html and "MARIA APARECIDA SOUZA" not in html
    html = cliente.get("/carteira?tribunal=TRF1").get_data(as_text=True)
    assert "0000013-02.2024.4.01.3600" in html and "MARIA APARECIDA SOUZA" not in html


def test_cockpit(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert "Seu escritório hoje" in html
    assert any(s in html for s in ("Bom dia, Maria", "Boa tarde, Maria", "Boa noite, Maria"))
    assert "Processos ativos" in html and ">16<" in html
    assert "R$ 3,3 mi" in html
    assert "Carteira por tribunal" in html and "TRF1" in html
    assert "Atividade recente" in html and "MARIA APARECIDA SOUZA" in html
    assert "Situação da carteira" in html and "Conferidos no PJe" in html
    assert "06:00" in html


def test_rotas_da_planilha_antiga_sairam(cliente):
    assert cliente.get("/exportar").status_code == 404
    assert cliente.post("/atualizar-todos").status_code in (404, 405)


def test_processo_do_banco_sem_cache(cliente):
    html = cliente.get(f"/processo/{banco_demo._tjmt(1)}").get_data(as_text=True)
    assert "MARIA APARECIDA SOUZA" in html and "BANCO ALFA S.A." in html
    assert "Linha do tempo" in html and "Publicação no DJEN" in html
    assert "selo-ok" in html and "1º grau" in html


def test_processo_arquivado(cliente):
    html = cliente.get(f"/processo/{banco_demo._tjmt(17)}").get_data(as_text=True)
    assert "Arquivado" in html


def test_processo_desconhecido(cliente):
    html = cliente.get("/processo/99999999999999999999").get_data(as_text=True)
    assert "ainda não foi sincronizado" in html


# ---------------------------------------------------------------------------
# Fase 1B: onda final de correções
# ---------------------------------------------------------------------------

from captura.mni_client import MNIError, ProcessoNaoEncontradoError  # noqa: E402


def test_atualizar_tjmt_segue_para_o_banco_mesmo_se_o_cache_do_1grau_falha(cliente, monkeypatch):
    monkeypatch.setenv("CARTEIRA_OAB_NUMERO", "12345")
    monkeypatch.setenv("CARTEIRA_OAB_UF", "MT")
    chamadas = []

    def falha(_cliente, _numero):
        raise ProcessoNaoEncontradoError("Processo não encontrado no 1º grau. Detalhe técnico longo.")

    def sincronizar(conn, numero, clientes, adv):
        chamadas.append(numero)
        return True

    monkeypatch.setattr(painel, "cliente_pje", lambda instancia="1grau": object())
    monkeypatch.setattr(painel, "sincronizar_um", falha)
    monkeypatch.setattr(painel.nucleo_carteira, "sincronizar_processo", sincronizar)
    numero = banco_demo._tjmt(1)
    resposta = cliente.post(f"/atualizar/{numero}")
    assert chamadas == [numero]
    assert resposta.status_code == 302 and resposta.headers["Location"].endswith(f"/processo/{numero}")
    html = cliente.get(f"/processo/{numero}").get_data(as_text=True)
    assert "Peças do 1º grau não atualizadas: Processo não encontrado no 1º grau" in html
    assert "Detalhe técnico longo" not in html


def test_atualizar_cria_o_cliente_do_2grau_so_se_precisar(cliente, monkeypatch):
    monkeypatch.setenv("CARTEIRA_OAB_NUMERO", "12345")
    monkeypatch.setenv("CARTEIRA_OAB_UF", "MT")
    pedidos = []

    def fabrica(instancia="1grau"):
        pedidos.append(instancia)
        return object()

    def sincronizar(conn, numero, clientes, adv):
        clientes["1grau"]  # o 2º grau nunca é tocado
        return True

    monkeypatch.setattr(painel, "cliente_pje", fabrica)
    monkeypatch.setattr(painel, "sincronizar_um",
                        lambda c, n: {"andamentos": [], "documentos": []})
    monkeypatch.setattr(painel.nucleo_carteira, "sincronizar_processo", sincronizar)
    cliente.post(f"/atualizar/{banco_demo._tjmt(1)}")
    assert "2grau" not in pedidos and "1grau" in pedidos


def test_atualizar_nao_tjmt_nao_fala_com_o_pje(cliente, monkeypatch):
    def proibido(*a, **k):
        raise AssertionError("não deve chamar o PJe")

    monkeypatch.setattr(painel, "cliente_pje", proibido)
    monkeypatch.setattr(painel, "sincronizar_um", proibido)
    numero = banco_demo._trf1(13)
    resposta = cliente.post(f"/atualizar/{numero}")
    assert resposta.status_code == 302
    html = cliente.get(f"/processo/{numero}").get_data(as_text=True)
    assert "Este processo não é do TJMT; os movimentos chegam pelo DJEN." in html


def test_processo_nao_tjmt_nao_oferece_botoes_do_pje(cliente):
    html = cliente.get(f"/processo/{banco_demo._trf1(13)}").get_data(as_text=True)
    assert "Atualizar do PJe" not in html and "Baixar do TJMT" not in html
    assert "Baixar .zip" not in html
    assert "Neste tribunal os movimentos chegam pelas publicações do DJEN." in html


def test_processo_tjmt_mantem_os_botoes(cliente):
    html = cliente.get(f"/processo/{banco_demo._tjmt(1)}").get_data(as_text=True)
    assert "Atualizar do PJe" in html and "Baixar do TJMT" in html
    assert "Neste tribunal os movimentos chegam" not in html


def test_processo_nao_sincronizado_nao_tjmt_sem_levantamento(cliente):
    outro = "00000990220244013600"  # TRF1, fora do banco
    html = cliente.get(f"/processo/{outro}").get_data(as_text=True)
    assert "ainda não foi sincronizado" in html
    assert "Fazer levantamento inicial" not in html
    html = cliente.get(f"/processo/{banco_demo._tjmt(50)}").get_data(as_text=True)
    assert "Fazer levantamento inicial" in html  # TJMT ainda não sincronizado


def test_consultar_erro_volta_para_a_carteira(cliente):
    resposta = cliente.post("/consultar", data={"numero": "123"})
    assert resposta.status_code == 302 and resposta.headers["Location"].endswith("/carteira")


def test_navegacao_acessivel(cliente):
    html = cliente.get("/carteira").get_data(as_text=True)
    assert 'aria-label="Seções do painel"' in html
    assert 'aria-label="Abas da carteira"' in html
    assert html.count('aria-current="page"') == 2  # Carteira (menu) + aba Ativa
    assert 'aria-pressed="false"' in html
    cockpit = cliente.get("/").get_data(as_text=True)
    assert cockpit.count('aria-current="page"') == 1


def test_login_nao_redireciona_para_fora(monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    monkeypatch.setattr(painel, "PAINEL_USUARIO", "dr")
    dados = {"usuario": "dr", "senha": "segredo"}
    c = painel.app.test_client()
    r = c.post("/login?proximo=//evil.com", data=dados)
    assert r.status_code == 302 and r.headers["Location"] == "/"
    c = painel.app.test_client()
    r = c.post("/login?proximo=/%5Cevil.com", data=dados)
    assert r.headers["Location"] == "/"
    for perigoso in ("/%09/evil.com", "/%0A/evil.com", "/%0D%0A/evil.com", "/a%5Cb"):
        c = painel.app.test_client()
        r = c.post(f"/login?proximo={perigoso}", data=dados)
        assert r.status_code == 302 and r.headers["Location"] == "/", perigoso
    c = painel.app.test_client()
    r = c.post("/login?proximo=/carteira", data=dados)
    assert r.headers["Location"] == "/carteira"


def test_cookie_de_sessao_samesite():
    assert painel.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


@pytest.mark.parametrize("valor", [
    "/\t/evil.com", "/\n/evil.com", "/\r\n/x", "//evil.com", "/\\evil.com", "/a\\b",
    "/\x7f", "https://evil.com", "evil.com", "", None])
def test_destino_seguro_recusa_o_que_pode_sair_do_site(valor):
    assert painel._destino_seguro(valor, "/padrao") == "/padrao"


@pytest.mark.parametrize("valor", ["/carteira", "/processo/123?aba=a_conferir", "/"])
def test_destino_seguro_aceita_caminhos_internos(valor):
    assert painel._destino_seguro(valor, "/padrao") == valor


# ---------------------------------------------------------------------------
# Busca por nome e por número (campo único no topo)
# ---------------------------------------------------------------------------

from nucleo import djen  # noqa: E402

NOVO_TJMT = "00000770220248110041"
NOVO_TRF1 = "00000880220244013600"


def _grupo(numero, tribunal="TJMT", partes=None, link="https://exemplo.invalido/pub", n=2):
    return {"numero": numero, "numero_formatado": numero, "tribunal": tribunal,
            "orgao": "5ª VARA DE TESTE", "classe": "CLASSE DE TESTE",
            "partes": partes or [{"nome": "CONSTRUTORA HORIZONTE LTDA", "polo": "Polo Passivo"},
                                 {"nome": "FULANO DE TAL", "polo": "Polo Ativo"}],
            "ultima_publicacao": "2026-09-15", "n_publicacoes": n, "link": link}


def _publicacoes_falsas(monkeypatch, grupos, truncado=False, erro=None):
    """Troca djen.buscar_por_parte/agrupar_por_processo: nunca fala com o DJEN."""
    chamadas = []

    def falso(nome, tribunal, inicio, fim, **kw):
        chamadas.append({"nome": nome, "tribunal": tribunal, "inicio": inicio, "fim": fim})
        if erro:
            raise erro
        return {"publicacoes": grupos, "truncado": truncado}

    monkeypatch.setattr(djen, "buscar_por_parte", falso)
    monkeypatch.setattr(djen, "agrupar_por_processo", lambda pubs: pubs)
    return chamadas


def test_topo_tem_o_campo_de_busca(cliente):
    html = cliente.get("/grupos").get_data(as_text=True)
    assert 'name="q"' in html and 'class="busca busca-topo"' in html
    assert 'action="/buscar"' in html and 'type="search"' in html
    assert "Buscar nome ou nº do processo\"" in html


def test_topo_sem_busca_na_tela_de_login(monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    html = painel.app.test_client().get("/login").get_data(as_text=True)
    assert 'name="q"' not in html and "busca-topo" not in html


def test_buscar_vazio_mostra_instrucoes(cliente, monkeypatch):
    chamadas = _publicacoes_falsas(monkeypatch, [])
    html = cliente.get("/buscar").get_data(as_text=True)
    assert "Digite um nome" in html and chamadas == []


def test_buscar_com_poucas_letras_avisa(cliente, monkeypatch):
    chamadas = _publicacoes_falsas(monkeypatch, [])
    html = cliente.get("/buscar?q=ab").get_data(as_text=True)
    assert "Digite pelo menos 4 letras do nome ou os 20 dígitos do processo." in html
    assert chamadas == []


def test_buscar_por_nome_mostra_carteira_e_djen(cliente, monkeypatch):
    chamadas = _publicacoes_falsas(monkeypatch, [
        _grupo(banco_demo._tjmt(2)), _grupo(NOVO_TJMT), _grupo(NOVO_TRF1, "TRF1")])
    html = cliente.get("/buscar?q=construtora").get_data(as_text=True)
    assert "Na sua carteira" in html and "JOÃO PEDRO LIMA" in html
    assert "Em publicações do DJEN" in html and NOVO_TJMT in html and NOVO_TRF1 in html
    assert html.count("selo-na-carteira") == 1  # só o número que já existe
    assert html.count("Adicionar e acompanhar") == 2  # os dois novos, nunca o existente
    assert html.count('name="origem" value="busca"') == 2
    assert f'value="{banco_demo._tjmt(2)}"' not in html.split("Em publicações do DJEN")[1]
    # TJMT abre no PJe (busca pelo número); outros tribunais levam à publicação
    assert f"/buscar?q={NOVO_TJMT}" in html and "Abrir no PJe" in html
    assert "ver publicação" in html and 'href="https://exemplo.invalido/pub"' in html
    assert "Polo Ativo" in html and "FULANO DE TAL" in html
    assert "15/09/2026" in html and "2 publicações" in html
    assert chamadas[0]["tribunal"] == "TJMT" and chamadas[0]["nome"] == "construtora"
    assert (chamadas[0]["fim"] - chamadas[0]["inicio"]).days == 360


def test_buscar_repassa_tribunal_e_meses_validos(cliente, monkeypatch):
    chamadas = _publicacoes_falsas(monkeypatch, [])
    cliente.get("/buscar?q=construtora&tribunal=&meses=24")
    cliente.get("/buscar?q=construtora&tribunal=TRF3&meses=3")
    cliente.get("/buscar?q=construtora&tribunal=XYZ&meses=7")
    assert [c["tribunal"] for c in chamadas] == ["", "TRF3", "TJMT"]
    assert [(c["fim"] - c["inicio"]).days for c in chamadas] == [720, 90, 360]


def test_buscar_avisa_quando_truncado(cliente, monkeypatch):
    _publicacoes_falsas(monkeypatch, [_grupo(NOVO_TJMT)], truncado=True)
    html = cliente.get("/buscar?q=construtora").get_data(as_text=True)
    assert "Mostrando os processos das 500 publicações mais recentes. Refine pelo período ou tribunal." in html
    _publicacoes_falsas(monkeypatch, [_grupo(NOVO_TJMT)], truncado=False)
    html = cliente.get("/buscar?q=construtora").get_data(as_text=True)
    assert "500 publicações mais recentes" not in html


def test_buscar_com_djen_fora_do_ar_ainda_mostra_a_carteira(cliente, monkeypatch):
    _publicacoes_falsas(monkeypatch, [], erro=djen.DJENError("fora"))
    html = cliente.get("/buscar?q=construtora")
    assert html.status_code == 200
    html = html.get_data(as_text=True)
    assert "O DJEN não respondeu agora. Tente de novo em instantes." in html
    assert "JOÃO PEDRO LIMA" in html and "fora" not in html.replace("fora do ar", "")


def test_buscar_nome_sem_resultados(cliente, monkeypatch):
    _publicacoes_falsas(monkeypatch, [])
    html = cliente.get("/buscar?q=zzzzzz").get_data(as_text=True)
    assert "Nenhum processo seu com esse nome." in html


def test_buscar_nao_marca_html_do_djen_como_seguro(cliente, monkeypatch):
    _publicacoes_falsas(monkeypatch, [_grupo(NOVO_TJMT, partes=[
        {"nome": "<script>alert(1)</script>", "polo": ""}], link="javascript:alert(1)")])
    html = cliente.get("/buscar?q=construtora").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_buscar_numero_da_carteira_vai_direto_ao_processo_sem_pje(cliente, monkeypatch):
    def proibido(*a, **k):
        raise AssertionError("não deve chamar o PJe")

    monkeypatch.setattr(painel, "cliente_pje", proibido)
    monkeypatch.setattr(painel, "sincronizar_um", proibido)
    numero = banco_demo._tjmt(1)
    for q in (numero, "0000001-02.2024.8.11.0041"):
        resposta = cliente.get("/buscar", query_string={"q": q})
        assert resposta.status_code == 302
        assert resposta.headers["Location"].endswith(f"/processo/{numero}")


def test_buscar_numero_desconhecido_nao_chama_o_pje_e_oferece_consulta(cliente, monkeypatch):
    def proibido(*a, **k):
        raise AssertionError("GET não pode chamar o PJe")

    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    monkeypatch.setattr(painel, "cliente_pje", proibido)
    monkeypatch.setattr(painel, "sincronizar_um", proibido)
    resposta = cliente.get(f"/buscar?q={NOVO_TJMT}")
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert "Este processo ainda não está no painel." in html
    assert 'action="/consultar"' in html and 'name="numero"' in html
    assert f'value="{NOVO_TJMT}"' in html and "Consultar no PJe" in html
    assert "Adicionar e acompanhar" not in html


def test_buscar_numero_desconhecido_fora_do_tjmt_oferece_adicionar(cliente, monkeypatch):
    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    monkeypatch.setattr(painel, "cliente_pje", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    html = cliente.get(f"/buscar?q={NOVO_TRF1}").get_data(as_text=True)
    assert "Este processo não é do TJMT; os movimentos chegam pelo DJEN." in html
    assert "Consultar no PJe" not in html
    assert 'action="/carteira/adicionar"' in html and "Adicionar e acompanhar" in html
    assert "Adicionar à carteira" not in html


def test_buscar_numero_so_no_cache_abre_o_processo(cliente, monkeypatch):
    monkeypatch.setattr(painel.cache, "ler", lambda n: {"numero": n})
    monkeypatch.setattr(painel, "cliente_pje", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    resposta = cliente.get(f"/buscar?q={NOVO_TJMT}")
    assert resposta.status_code == 302 and resposta.headers["Location"].endswith(f"/processo/{NOVO_TJMT}")


def test_consultar_numero_novo_sincroniza_e_abre(cliente, monkeypatch):
    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    chamadas = []
    monkeypatch.setattr(painel, "cliente_pje", lambda instancia="1grau": "cliente-falso")
    monkeypatch.setattr(painel, "sincronizar_um", lambda c, n: chamadas.append((c, n)))
    resposta = cliente.post("/consultar", data={"numero": NOVO_TJMT})
    assert chamadas == [("cliente-falso", NOVO_TJMT)]
    assert resposta.status_code == 302 and resposta.headers["Location"].endswith(f"/processo/{NOVO_TJMT}")


def test_consultar_numero_novo_com_falha_volta_a_carteira(cliente, monkeypatch):
    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    monkeypatch.setattr(painel, "cliente_pje", lambda instancia="1grau": object())

    def falha(c, n):
        raise MNIError("Processo não encontrado.")

    monkeypatch.setattr(painel, "sincronizar_um", falha)
    resposta = cliente.post("/consultar", data={"numero": NOVO_TJMT})
    assert resposta.status_code == 302 and resposta.headers["Location"].endswith("/carteira")


def test_buscar_usa_o_buscador_interativo(cliente, monkeypatch):
    capturado = {}

    def falso(nome, tribunal, inicio, fim, **kw):
        capturado.update(kw)
        return {"publicacoes": [], "truncado": False}

    monkeypatch.setattr(djen, "buscar_por_parte", falso)
    cliente.get("/buscar?q=construtora")
    assert capturado["obter"] is djen.obter_interativo and capturado["pausa"] == 0.2


def test_buscar_data_vazia_nao_mostra_barras(cliente, monkeypatch):
    _publicacoes_falsas(monkeypatch, [{**_grupo(NOVO_TJMT), "ultima_publicacao": ""}])
    html = cliente.get("/buscar?q=construtora").get_data(as_text=True)
    assert "Última publicação em //" not in html and "Última publicação em" not in html


def test_formularios_de_busca_dao_retorno_de_carregamento(cliente, monkeypatch):
    _publicacoes_falsas(monkeypatch, [])
    topo = cliente.get("/grupos").get_data(as_text=True)
    assert "aria-busy" in topo and "buscando" in topo
    pagina = cliente.get("/buscar?q=construtora").get_data(as_text=True)
    assert "Buscando…" in pagina


def test_consultar_continua_igual_e_pula_o_pje_se_ja_esta_no_banco(cliente, monkeypatch):
    monkeypatch.setattr(painel, "cliente_pje", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    numero = banco_demo._tjmt(1)
    resposta = cliente.post("/consultar", data={"numero": numero})
    assert resposta.status_code == 302 and resposta.headers["Location"].endswith(f"/processo/{numero}")


def _linha(numero):
    conn = banco.conectar()
    try:
        return conn.execute("SELECT * FROM processo WHERE numero = ?", (numero,)).fetchone()
    finally:
        conn.close()


ADICIONADO = ("Processo adicionado à sua carteira. A Controladoria passa a acompanhá-lo "
              "nas próximas conferências.")


def test_adicionar_vindo_da_busca_confirma_e_abre_o_processo(cliente, monkeypatch):
    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    resposta = cliente.post("/carteira/adicionar", data={
        "numero": NOVO_TJMT, "origem": "busca", "voltar": "/buscar?q=construtora"})
    assert resposta.status_code == 302
    assert resposta.headers["Location"] == f"/processo/{NOVO_TJMT}"
    linha = _linha(NOVO_TJMT)
    assert linha["tribunal"] == "TJMT" and json.loads(linha["fontes"]) == ["busca"]
    assert linha["confirmado_em"] and linha["descartado_em"] is None
    html = cliente.get(f"/processo/{NOVO_TJMT}").get_data(as_text=True)
    assert ADICIONADO in html
    assert "Este processo não está na sua carteira" not in html
    carteira = cliente.get("/carteira").get_data(as_text=True)
    assert "0000077-02.2024.8.11.0041" in carteira  # já na aba Ativa


@pytest.mark.parametrize("digitado", [NOVO_TRF1, "0000088-02.2024.4.01.3600",
                                      " 0000088-02.2024.4.01.3600 "])
def test_adicionar_pelo_numero_com_ou_sem_mascara(cliente, digitado):
    resposta = cliente.post("/carteira/adicionar", data={"numero": digitado, "voltar": "/carteira"})
    assert resposta.headers["Location"] == f"/processo/{NOVO_TRF1}"
    linha = _linha(NOVO_TRF1)
    assert linha["tribunal"] == "TRF1" and json.loads(linha["fontes"]) == ["manual"]
    assert linha["confirmado_em"]


def test_adicionar_com_origem_desconhecida_vale_manual(cliente):
    cliente.post("/carteira/adicionar", data={"numero": NOVO_TRF1, "origem": "<x>"})
    assert json.loads(_linha(NOVO_TRF1)["fontes"]) == ["manual"]


def test_adicionar_duas_vezes_avisa_que_ja_esta(cliente):
    cliente.post("/carteira/adicionar", data={"numero": NOVO_TRF1, "voltar": "/carteira"})
    cliente.get("/carteira")  # consome a mensagem da 1ª vez
    resposta = cliente.post("/carteira/adicionar", data={"numero": NOVO_TRF1, "voltar": "/carteira"})
    assert resposta.headers["Location"] == f"/processo/{NOVO_TRF1}"
    html = cliente.get("/carteira").get_data(as_text=True)
    assert "Este processo já está na sua carteira." in html and ADICIONADO not in html


def test_adicionar_processo_a_conferir_confirma(cliente):
    numero = _numero_a_conferir()
    cliente.post("/carteira/adicionar", data={"numero": numero})
    assert _linha(numero)["confirmado_em"]
    html = cliente.get("/carteira").get_data(as_text=True)
    assert ADICIONADO in html


def test_adicionar_processo_descartado_volta_para_a_ativa(cliente):
    numero = _numero_a_conferir()
    cliente.post("/carteira/descartar", data={"numero": numero})
    assert _linha(numero)["descartado_em"]
    cliente.post("/carteira/adicionar", data={"numero": numero})
    linha = _linha(numero)
    assert linha["descartado_em"] is None and linha["confirmado_em"]
    html = cliente.get("/carteira?aba=descartados").get_data(as_text=True)
    assert ADICIONADO in html


def test_adicionar_numero_invalido_recusa_destino_externo(cliente):
    for voltar in ("//evil.com", "https://evil.com", "/\\evil.com"):
        resposta = cliente.post("/carteira/adicionar", data={"numero": "123", "voltar": voltar})
        assert resposta.status_code == 302 and resposta.headers["Location"].endswith("/carteira")


def test_adicionar_a_carteira_com_numero_invalido(cliente):
    resposta = cliente.post("/carteira/adicionar", data={"numero": "123", "voltar": "/carteira"})
    assert resposta.status_code == 302 and resposta.headers["Location"] == "/carteira"
    html = cliente.get("/carteira").get_data(as_text=True)
    assert "Número inválido — informe os 20 dígitos do número CNJ" in html
    conn = banco.conectar()
    try:
        assert conn.execute("SELECT COUNT(*) FROM processo WHERE numero = '123'").fetchone()[0] == 0
    finally:
        conn.close()


def test_atualizar_consulta_o_pje_uma_vez_so(cliente, monkeypatch):
    from types import SimpleNamespace as NS
    from tests.fixtures_mni import processo_do_cliente
    numero = "00000010220248110041"
    consultas = []

    class PJe:
        def consultar_processo(self, n, incluir_movimentos=False):
            consultas.append(n)
            return NS(processo=processo_do_cliente())

    monkeypatch.setattr(painel, "cliente_pje", lambda instancia="1grau": PJe())
    monkeypatch.setattr(painel.cache, "gravar", lambda dados: None)
    monkeypatch.setenv("CARTEIRA_OAB_NUMERO", "54321")
    monkeypatch.setenv("CARTEIRA_OAB_UF", "MT")
    cliente.post(f"/atualizar/{numero}")
    assert consultas == [numero]


def _um_a_conferir(cliente):
    html = cliente.get("/carteira?aba=a_conferir").get_data(as_text=True)
    import re
    return re.search(r'name="numero" value="(\d{20})"', html).group(1)


def test_descartar_e_restaurar_pela_tela(cliente):
    html = cliente.get("/carteira?aba=a_conferir").get_data(as_text=True)
    # A aba Descartados só aparece quando há algo nela (o banco de demonstração começa sem nenhum).
    assert "Descartar" in html and 'aba=descartados' not in html
    numero = _um_a_conferir(cliente)
    r = cliente.post("/carteira/descartar",
                     data={"numero": numero, "voltar": "/carteira?aba=a_conferir"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/carteira?aba=a_conferir")
    html = cliente.get("/carteira?aba=a_conferir").get_data(as_text=True)
    assert numero not in html and "Descartados" in html
    html = cliente.get("/carteira?aba=descartados").get_data(as_text=True)
    assert numero in html and "Restaurar" in html
    r = cliente.post("/carteira/restaurar", data={"numero": numero, "voltar": "//evil.com"})
    assert r.headers["Location"].endswith("/carteira") and "evil" not in r.headers["Location"]
    assert numero in cliente.get("/carteira?aba=a_conferir").get_data(as_text=True)


def test_aba_desconhecida_vira_ativa(cliente):
    assert cliente.get("/carteira?aba=lixo").status_code == 200


@pytest.mark.parametrize("erro", [MNIError("WSDL do TJMT fora do ar. Detalhe."),
                                  ValueError("CPF inválido.")])
def test_atualizar_com_falha_ao_criar_o_cliente_vira_aviso(cliente, monkeypatch, erro):
    monkeypatch.setenv("CARTEIRA_OAB_NUMERO", "12345")
    monkeypatch.setenv("CARTEIRA_OAB_UF", "MT")

    def fabrica(instancia="1grau"):
        raise erro
    monkeypatch.setattr(painel, "cliente_pje", fabrica)
    monkeypatch.setattr(painel, "sincronizar_um",
                        lambda *a: pytest.fail("não há cliente para consultar"))
    numero = banco_demo._tjmt(1)
    resposta = cliente.post(f"/atualizar/{numero}")
    assert resposta.status_code == 302
    html = cliente.get(f"/processo/{numero}").get_data(as_text=True)
    assert "Peças do 1º grau não atualizadas:" in html


def test_atualizar_sem_credencial_continua_indo_para_conectar(cliente, monkeypatch):
    def fabrica(instancia="1grau"):
        raise painel.CredencialPJeAusente("Conecte-se.")
    monkeypatch.setattr(painel, "cliente_pje", fabrica)
    resposta = cliente.post(f"/atualizar/{banco_demo._tjmt(1)}")
    assert resposta.status_code == 302 and "/conectar" in resposta.headers["Location"]


# --- onda final: regras visuais (CSS e quadro.js) -----------------------------------

def _estatico(nome):
    import pathlib
    import painel as _painel
    return (pathlib.Path(_painel.app.static_folder) / nome).read_text(encoding="utf-8")


def _regra(css, seletor):
    import re as _re
    m = _re.search(_re.escape(seletor) + r"\s*\{([^}]*)\}", css)
    assert m, seletor
    return m.group(1)


def test_css_quadro_rompe_o_container_e_rola_com_sombra():
    css = _estatico("style.css")
    secao = _regra(css, ".quadro-secao")
    assert "1560px" in secao and "margin-left" in secao
    colunas = _regra(css, ".quadro-colunas")
    assert "minmax(210px, 1fr)" in colunas and "scroll-snap-type" in colunas
    assert "scroll-snap-align" in _regra(css, ".quadro-coluna")
    assert "linear-gradient" in _regra(css, ".quadro-faixa::after")
    assert "opacity: 1" in _regra(css, ".quadro-faixa.tem-mais::after")


def test_css_numero_do_cartao_quebra():
    regra = _regra(_estatico("style.css"), ".cartao-demanda .numero")
    assert "overflow-wrap: anywhere" in regra and "white-space: normal" in regra


def test_css_topo_ate_1180_busca_em_linha_propria():
    css = _estatico("style.css")
    bloco = css[css.index("@media (max-width: 1180px) {\n  /* A busca"):]
    bloco = bloco[:bloco.index("\n}\n")]
    assert ".busca-form { order: 4; flex: 1 1 100%" in bloco
    assert ".btn-apresentacao .rotulo-longo { display: none; }" in bloco


def test_css_chave_desligada_com_contraste():
    css = _estatico("style.css")
    assert "background: var(--cinza)" in _regra(css, ".chave-bola")
    assert "background: var(--cartao)" in _regra(css, '.chave[aria-checked="true"] .chave-bola')


def test_css_kpis_4_vence_a_media_query():
    css = _estatico("style.css")
    assert ".kpis.kpis-4 { grid-template-columns: repeat(4, 1fr); }" in css
    assert "\n.kpis-4 {" not in css and "  .kpis-4 {" not in css


def test_quadro_js_solta_o_foco_da_chave_se_a_atualizacao_falhar():
    js = _estatico("quadro.js")
    assert ".catch(function () { focoNaChave = false; })" in js
    assert "if (r.status !== 204) focoNaChave = false" in js
    assert "tem-mais" in js


def test_botao_apresentacao_compacto(tmp_path):
    import pathlib
    import painel as _painel
    base = (pathlib.Path(_painel.app.root_path) / "templates" / "base.html").read_text()
    assert '<span class="rotulo-curto" aria-hidden="true">Apresentação</span>' in base
    assert 'aria-label="Modo apresentação"' in base


def _numero_a_conferir():
    conn = banco.conectar()
    try:
        return conn.execute("SELECT numero FROM processo WHERE arquivado = 0 AND confirmado_em IS NULL "
                            "AND descartado_em IS NULL AND (advogado_atua IS NULL OR advogado_atua = 0) "
                            "ORDER BY numero LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def test_carteira_tem_formulario_de_adicionar_pelo_numero(cliente):
    html = cliente.get("/carteira").get_data(as_text=True)
    form = html.split('class="adicionar-numero"')[1].split("</form>")[0]
    assert 'action="/carteira/adicionar"' in html.split('class="adicionar-numero"')[0][-200:]
    assert '<label for="adicionar-numero">Adicionar processo pelo número</label>' in form
    assert 'inputmode="numeric"' in form and 'name="numero"' in form
    assert 'name="csrf"' in form and 'name="origem" value="manual"' in form
    assert "Adicionar e acompanhar" in form
    for aba in ("a_conferir", "descartados"):
        assert 'class="adicionar-numero"' not in cliente.get(f"/carteira?aba={aba}").get_data(as_text=True)


def test_processo_fora_da_carteira_oferece_adicionar(cliente, monkeypatch):
    monkeypatch.setattr(painel.cache, "ler", lambda n: None)
    html = cliente.get(f"/processo/{NOVO_TRF1}").get_data(as_text=True)
    assert "Este processo não está na sua carteira" in html
    bloco = html.split("Este processo não está na sua carteira")[1].split("</form>")[0]
    assert 'action="/carteira/adicionar"' in bloco and 'name="csrf"' in bloco
    assert f'name="numero" value="{NOVO_TRF1}"' in bloco and "Adicionar e acompanhar" in bloco
    assert "Este processo é seu?" not in html


def test_processo_so_no_cache_tambem_oferece_adicionar(cliente, monkeypatch):
    dados = {"numero_formatado": "0000077-02.2024.8.11.0041", "partes": [],
             "andamentos": [], "documentos": []}
    monkeypatch.setattr(painel.cache, "ler", lambda n: dados if n == NOVO_TJMT else None)
    html = cliente.get(f"/processo/{NOVO_TJMT}").get_data(as_text=True)
    assert "Este processo não está na sua carteira" in html and "andamentos no PJe" in html


def test_processo_da_carteira_nao_mostra_o_bloco_de_adicionar(cliente):
    html = cliente.get(f"/processo/{banco_demo._tjmt(1)}").get_data(as_text=True)
    assert "Este processo não está na sua carteira" not in html
    html = cliente.get(f"/processo/{_numero_a_conferir()}").get_data(as_text=True)
    assert "Este processo não está na sua carteira" not in html
