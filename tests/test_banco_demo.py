from datetime import date

import pytest

from nucleo import banco, carteira
from scripts import banco_demo

HOJE = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def _cofre_da_demo_isolado(tmp_path, monkeypatch):
    """A demo guarda credenciais fictícias no cofre do servidor: cada teste usa uma chave
    sua (nunca a de dados/ nem o cofre local do computador)."""
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "dados" / "chave-do-teste.key"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)


def test_gerar_preenche_a_carteira_ficticia():
    conn = banco.conectar(":memory:")
    banco_demo.gerar(conn, HOJE)
    assert carteira.resumo(conn) == {
        "processos": 25, "carteira_ativa": 17, "arquivados": 3,
        "a_conferir": 5, "falhas_pje": 1, "publicacoes": 19}


def test_demo_tem_processo_do_trf1_a_conferir_com_integra_e_partes():
    from nucleo import painel_dados
    conn = banco.conectar(":memory:")
    banco_demo.gerar(conn, HOJE)
    numero = banco_demo._trf1(banco_demo.TRF1_A_CONFERIR)
    d = painel_dados.processo_detalhe(conn, numero)
    assert d["monitoramento"]["estado"] == "a_conferir"
    assert [g["rotulo"] for g in d["partes_publicadas"]] == ["polo ativo", "polo passivo"]
    cartao = conn.execute("SELECT id, coluna FROM demanda WHERE numero = ?", (numero,)).fetchone()
    assert cartao["coluna"] == "chegou"
    det = painel_dados.demanda_detalhe(conn, cartao["id"], HOJE, "/nao-existe")
    assert len(det["publicacoes"]) == 2 and len(det["publicacoes"][0]["integra"]) >= 10


def test_numeros_ficticios_tem_20_digitos_e_tribunal_certo():
    assert banco_demo._tjmt(1) == "00000010220248110041"
    assert banco_demo._trf1(13) == "00000130220244013600"
    assert carteira.tribunal_do_numero(banco_demo._trf1(13)) == "TRF1"


def test_main_cria_arquivo_e_recusa_existente(tmp_path, capsys):
    caminho = tmp_path / "demo.db"
    assert banco_demo.main([str(caminho)]) == 0
    assert caminho.exists()
    assert banco_demo.main([str(caminho)]) == 1
    assert "já existe" in capsys.readouterr().err


def test_demo_tem_cartoes_em_todas_as_colunas():
    from datetime import date
    from nucleo import banco, painel_dados
    from scripts import banco_demo
    conn = banco.conectar(":memory:")
    banco_demo.gerar(conn, date(2026, 10, 6))
    q = painel_dados.quadro(conn, date(2026, 10, 6))
    contagem = {c["chave"]: len(c["cartoes"]) for c in q["colunas"]}
    assert contagem["chegou"] >= 3 and contagem["acao"] >= 2 and contagem["autos"] >= 2
    estados = {c["autos_estado"] for c in q["colunas"][1]["cartoes"]}
    assert {"baixando", "falhou"} <= estados
    assert q["varredura"]["ultima"]["quando"].endswith("06:00")
    assert conn.execute("SELECT COUNT(*) FROM tarefa WHERE estado IN ('na_fila', 'rodando')"
                        ).fetchone()[0] == 0  # demo não deixa trabalho pendente


# --- demonstração do Lex (Fase 3) -------------------------------------------------------

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from nucleo import config, eventos, painel_dados, ponte

AGORA = datetime(2026, 10, 6, 10, 0, tzinfo=eventos.FUSO)


def _demo(tmp_path, nome="pecas"):
    conn = banco.conectar(":memory:")
    banco_demo.gerar(conn, HOJE, pecas_dir=str(tmp_path / nome), agora=AGORA)
    return conn, str(tmp_path / nome)


def _cartoes(conn, coluna):
    q = painel_dados.quadro(conn, HOJE, AGORA)
    return next(c for c in q["colunas"] if c["chave"] == coluna)["cartoes"]


def test_quadro_demo_tem_as_cinco_colunas_com_o_lex(tmp_path):
    conn, _ = _demo(tmp_path)
    q = painel_dados.quadro(conn, HOJE, AGORA)
    contagem = {c["chave"]: len(c["cartoes"]) for c in q["colunas"]}
    assert contagem["lex"] == 2 and contagem["revisao"] == 2
    assert contagem["chegou"] >= 3 and contagem["acao"] >= 2 and contagem["autos"] >= 2
    assert q["lex_automatico"] is False
    lex = {c["lex_estado"]: c for c in _cartoes(conn, "lex")}
    assert set(lex) == {"trabalhando", "na_fila"}
    assert "pesquisa jurídica (8/13)" in lex["trabalhando"]["lex_rotulo"]
    assert lex["trabalhando"]["lex_rotulo"].startswith("Lex trabalhando há 21 min")
    assert lex["na_fila"]["lex_orientacao"]
    assert lex["na_fila"]["lex_rotulo"] == "na fila do Lex"  # o Mac fez contato há pouco
    assert ponte.ultimo_contato(conn)
    assert config.ler(conn, "demo_versao") == str(banco_demo.DEMO_VERSAO)


def test_revisao_tem_uma_peca_limpa_e_uma_com_citacao_a_conferir(tmp_path):
    conn, _ = _demo(tmp_path)
    limpa, conferir = sorted(_cartoes(conn, "revisao"), key=lambda c: c["citacoes_falhas"])
    assert (limpa["citacoes_falhas"], limpa["gate_status"]) == (0, "aprovado")
    assert limpa["tempo_total"] and limpa["tempo_lex"]
    assert conferir["citacoes_falhas"] == 1 and conferir["gate_status"] != "aprovado"


def test_cartao_aberto_mostra_pacote_com_citacoes_e_pendencias(tmp_path):
    conn, pecas = _demo(tmp_path)
    limpa = next(c for c in _cartoes(conn, "revisao") if c["citacoes_falhas"] == 0)
    d = painel_dados.demanda_detalhe(conn, limpa["id"], HOJE, pecas, AGORA)
    p = d["pronta"]
    assert (p["citacoes_total"], p["citacoes_falhas"]) == (25, 0)
    assert p["tem_pdf"] and p["nota"].strip()
    assert len(p["citacoes"]) == 3 and len(p["pendencias"]) == 2
    assert all(c["source_url"].startswith("https://") for c in p["citacoes"])
    assert [e["n"] for e in d["execucoes"]] == [1]
    assert d["cronometro"]["lex_minutos"] == 47
    pdf = Path(pecas, str(d["id"]), "1", "peca.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-1.") and pdf.rstrip().endswith(b"%%EOF")


def test_peca_com_citacao_a_conferir_teve_uma_pausa_antes(tmp_path):
    conn, pecas = _demo(tmp_path)
    c = next(c for c in _cartoes(conn, "revisao") if c["citacoes_falhas"] == 1)
    d = painel_dados.demanda_detalhe(conn, c["id"], HOJE, pecas, AGORA)
    assert [(e["n"], e["resultado"]) for e in d["execucoes"]] == [(1, "limite"), (2, "pronto")]
    assert d["pronta"]["n"] == 2
    assert [x["status"] for x in d["pronta"]["citacoes"]].count("não encontrada") == 1
    assert d["historico"]


def test_resultados_tem_quatro_pecas_em_meses_diferentes(tmp_path):
    conn, _ = _demo(tmp_path)
    r = painel_dados.resultados(conn, "tudo", AGORA)
    assert r["total_pecas"] == 4 and len(r["pecas_por_tipo"]) == 4
    assert len(r["pecas_por_mes"]) >= 3 and r["tempo_medio_total"] and r["tempo_medio_lex"]
    assert r["citacoes_de_primeira"] == 75
    assert painel_dados.resultados(conn, "30d", AGORA)["total_pecas"] == 2
    protocolados = conn.execute(
        "SELECT COUNT(*) FROM demanda WHERE coluna = 'protocolado' AND protocolado_em IS NOT NULL"
    ).fetchone()[0]
    assert protocolados == 2


def test_pdf_minimo_e_valido_e_tem_o_texto():
    pdf = banco_demo._pdf_minimo("Peça de demonstração — caso fictício")
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    inicio = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert pdf[inicio:inicio + 4] == b"xref"
    linhas = pdf[inicio:].split(b"\n")
    n = int(linhas[1].split()[1])
    for i in range(1, n):  # cada entrada aponta para "<i> 0 obj"
        deslocamento = int(linhas[2 + i].split()[0])
        assert pdf[deslocamento:].startswith(f"{i} 0 obj".encode())
    assert "Peça de demonstração — caso fictício".encode("cp1252") in pdf


def test_geracao_e_deterministica_para_o_mesmo_dia(tmp_path):
    a, pa = _demo(tmp_path, "a")
    b, pb = _demo(tmp_path, "b")
    for tabela in ("demanda", "demanda_item", "execucao_lex", "evento", "config", "tarefa"):
        ordem = "id" if tabela not in ("demanda_item", "config") else "1"
        # texto cifrado (Fernet) muda a cada gravação; o conteúdo é conferido à parte
        filtro = " WHERE chave != 'pje_cpf_cifrado'" if tabela == "config" else ""
        sql = f"SELECT * FROM {tabela}{filtro} ORDER BY {ordem}"
        linhas_a = [tuple(r) for r in a.execute(sql)]
        linhas_b = [tuple(r) for r in b.execute(sql)]
        # a pasta gravada na execução aponta para o diretório de cada geração
        if tabela == "execucao_lex":
            linhas_a = [tuple(str(x).replace(pa, "") for x in r) for r in linhas_a]
            linhas_b = [tuple(str(x).replace(pb, "") for x in r) for r in linhas_b]
        assert linhas_a == linhas_b, tabela
    from nucleo import credenciais
    assert credenciais.cpf_guardado(a) == credenciais.cpf_guardado(b) == banco_demo.CPF_DEMO
    for raiz, _, arquivos in os.walk(pa):
        for nome in arquivos:
            rel = os.path.relpath(os.path.join(raiz, nome), pa)
            assert Path(raiz, nome).read_bytes() == Path(pb, rel).read_bytes()


def test_pasta_das_pecas_vem_do_ambiente_quando_nao_informada(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_PECAS", str(tmp_path / "do-ambiente"))
    conn = banco.conectar(":memory:")
    banco_demo.gerar(conn, HOJE, agora=AGORA)
    assert os.path.isdir(tmp_path / "do-ambiente")


def test_situacao_distingue_demo_atual_demo_antiga_e_outro_banco(tmp_path):
    atual = tmp_path / "atual.db"
    conn = banco.conectar(str(atual))
    banco_demo.gerar(conn, HOJE, pecas_dir=str(tmp_path / "p"), agora=AGORA)
    conn.close()
    assert banco_demo.situacao(str(atual)) == "atual"
    antiga = tmp_path / "antiga.db"
    conn = banco.conectar(str(antiga))
    banco_demo.gerar(conn, HOJE, pecas_dir=str(tmp_path / "p"), agora=AGORA)
    conn.execute("DELETE FROM config WHERE chave = 'demo_versao'")
    conn.commit()
    conn.close()
    assert banco_demo.situacao(str(antiga)) == "antiga"
    real = tmp_path / "real.db"
    banco.conectar(str(real)).close()
    assert banco_demo.situacao(str(real)) == "outro"
    assert banco_demo.situacao(str(tmp_path / "nao-existe.db")) == "outro"
    assert banco_demo.main(["--situacao", str(atual)]) == 0
    assert banco_demo.main(["--situacao", str(antiga)]) == 3
    assert banco_demo.main(["--situacao", str(real)]) == 4


def test_relogio_de_verdade_volta_depois_de_gerar(tmp_path):
    original = eventos.agora
    _demo(tmp_path)
    assert eventos.agora is original


@pytest.mark.parametrize("agora", [
    datetime(2026, 10, 7, 3, 10, tzinfo=eventos.FUSO),
    datetime(2026, 10, 7, 18, 40, tzinfo=eventos.FUSO)], ids=["madrugada", "fim-do-dia"])
def test_telas_do_painel_abrem_com_a_demo_do_lex(tmp_path, monkeypatch, agora):
    import painel
    # O painel compara com o relógio; a demo é ancorada no mesmo relógio (qualquer horário).
    monkeypatch.setattr(painel_dados, "agora_cuiaba", lambda: agora)
    caminho = tmp_path / "demo.db"
    conn = banco.conectar(str(caminho))
    banco_demo.gerar(conn, agora.date(), pecas_dir=str(tmp_path / "pecas"), agora=agora)
    ids = {r["coluna"]: r["id"] for r in conn.execute(
        "SELECT coluna, id FROM demanda WHERE coluna IN ('lex', 'revisao')")}
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(caminho))
    monkeypatch.setenv("CONTROLADORIA_PECAS", str(tmp_path / "pecas"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    c = painel.app.test_client()
    quadro = c.get("/").get_data(as_text=True)
    assert "Lex minutando" in quadro and "pesquisa jurídica (8/13)" in quadro
    assert "citações conferidas" in quadro and "citação a conferir" in quadro
    for demanda in ids.values():
        assert c.get(f"/demanda/{demanda}").status_code == 200
    pronta = c.get(f"/demanda/{ids['revisao']}").get_data(as_text=True)
    assert "Código de Processo Civil" in pronta and "CONFERIR" in pronta
    with c.get(f"/demanda/{ids['revisao']}/peca/1.pdf") as r:
        assert r.status_code == 200
    resultados = c.get("/resultados?periodo=tudo")
    assert resultados.status_code == 200 and "47 min" in resultados.get_data(as_text=True)
    lex = c.get("/configuracoes/lex")
    assert lex.status_code == 200 and "Conectado há" in lex.get_data(as_text=True)


def test_demo_desatualizada_e_pasta_sumida_pedem_nova_geracao(tmp_path):
    caminho = tmp_path / "demo.db"
    conn = banco.conectar(str(caminho))
    banco_demo.gerar(conn, AGORA.date(), pecas_dir=str(tmp_path / "p"), agora=AGORA)
    conn.close()
    assert not banco_demo.desatualizada(str(caminho), AGORA + timedelta(minutes=59))
    assert banco_demo.desatualizada(str(caminho), AGORA + timedelta(minutes=61))
    assert banco_demo.desatualizada(str(caminho), AGORA + timedelta(days=1))
    assert banco_demo.desatualizada(str(tmp_path / "nao-existe.db"))
    assert banco_demo.situacao(str(caminho)) == "atual"
    shutil.rmtree(tmp_path / "p")
    assert banco_demo.situacao(str(caminho)) == "antiga"


def test_pasta_padrao_das_pecas_nao_depende_do_diretorio_atual():
    assert Path(banco_demo.PECAS_PADRAO).is_absolute()
    assert Path(banco_demo.PECAS_PADRAO).parent.parent == Path(__file__).resolve().parents[1]


def test_demo_grava_o_nome_dos_squads(tmp_path):
    conn, _ = _demo(tmp_path)
    assert banco_demo.DEMO_VERSAO >= 3
    lex = {c["lex_estado"]: c for c in _cartoes(conn, "lex")}
    assert "Manifestação sobre o laudo pericial" in lex["trabalhando"]["lex_rotulo"]
    assert "manifestacao-sobre-laudo" not in lex["trabalhando"]["lex_rotulo"]
    nomes = {r[0] for r in conn.execute("SELECT squad_nome FROM execucao_lex "
                                         "WHERE resultado = 'pronto'")}
    assert nomes == {"Contestação cível", "Apelação cível", "Embargos de declaração",
                     "Réplica à contestação"}
    tipos = {nome for nome, _ in painel_dados.resultados(conn, "tudo", AGORA)["pecas_por_tipo"]}
    assert tipos == nomes


def test_demo_fala_em_plano_e_lex_nas_telas(tmp_path):  # o nome do teste vai no tmp_path
    """O que a demonstração grava e mostra fala em "plano"/"Lex", nunca em "Claude"."""
    conn, pecas = _demo(tmp_path)
    textos = []
    for (tabela,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        textos += [repr(tuple(r)) for r in conn.execute(f"SELECT * FROM {tabela}")]
    for (demanda_id,) in conn.execute("SELECT id FROM demanda"):
        textos.append(repr(painel_dados.demanda_detalhe(conn, demanda_id, HOJE, pecas, AGORA)))
    textos.append(repr(painel_dados.quadro(conn, HOJE, AGORA)))
    textos.append(repr(painel_dados.atividade_agora(conn, 50)))
    assert not [t for t in textos if "claude" in t.lower()]
    assert banco_demo.DEMO_VERSAO >= 4


# --- tribunais, faixa e pastas do caso (Projeto 1) ----------------------------------------

def test_demo_guarda_credenciais_ficticias_no_cofre(tmp_path):
    from nucleo import credenciais
    conn, _ = _demo(tmp_path)
    assert credenciais.cadastradas(conn) == {("TJMT", "1grau"), ("TJMT", "2grau")}
    assert credenciais.cpf_guardado(conn) == banco_demo.CPF_DEMO
    assert credenciais.ler(conn, "TJMT", "1grau")["senha"] == banco_demo.SENHA_DEMO
    # nada legível no banco: só texto cifrado
    for tabela in ("credencial", "config"):
        despejo = repr([tuple(r) for r in conn.execute(f"SELECT * FROM {tabela}")])
        assert banco_demo.SENHA_DEMO not in despejo and banco_demo.CPF_DEMO not in despejo
    assert (tmp_path / "dados" / "chave-do-teste.key").is_file()


def test_demo_nao_toca_o_cofre_local_do_computador(tmp_path, monkeypatch):
    from nucleo import cofre
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")

    def _proibido(*a, **k):
        raise AssertionError("a demo não pode gravar no cofre local")
    monkeypatch.setattr(cofre, "gravar", _proibido)
    conn, _ = _demo(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM credencial").fetchone()[0] == 2
    assert os.environ["CONTROLADORIA_LOCAL"] == "1"  # o ambiente volta como estava
    assert os.environ["CONTROLADORIA_CHAVE_COFRE"] == str(tmp_path / "dados" / "chave-do-teste.key")


def test_conexoes_da_demo_dao_tres_selos_e_uma_faixa(tmp_path):
    from nucleo import conexao
    conn, _ = _demo(tmp_path)
    selos = {(s["sigla"], s["instancia"]): s["selo"] for s in conexao.situacao(conn)}
    assert selos == {("TJMT", "1grau"): "ok", ("TJMT", "2grau"): "recusada",
                     ("TJMG", "1grau"): "nao_cadastrado"}
    assert [a["sigla"] for a in conexao.avisos(conn)] == ["TJMT"]


def test_pastas_do_caso_na_demo(tmp_path):
    conn, _ = _demo(tmp_path)
    faltam = conn.execute("SELECT * FROM demanda WHERE pasta_estado = 'faltam'").fetchall()
    na_pasta = conn.execute("SELECT * FROM demanda WHERE pasta_estado = 'na_pasta'").fetchall()
    assert len(faltam) == 1 and len(na_pasta) == 1
    from ponte_mac import pastas
    for d in (faltam[0], na_pasta[0]):
        assert d["coluna"] == "autos" and d["pasta_nome"]
        cliente = conn.execute("SELECT cliente FROM processo WHERE numero = ?",
                               (d["numero"],)).fetchone()[0]
        assert d["pasta_nome"] == pastas.nome_da_pasta(d["numero"], cliente)  # como o Mac cria
        assert carteira.tribunal_do_numero(d["numero"]) == "TRF1"
    assert len(json.loads(na_pasta[0]["pasta_arquivos"])) == 2
    assert conn.execute("SELECT COUNT(*) FROM tarefa WHERE estado IN ('na_fila', 'rodando')"
                        ).fetchone()[0] == 0


def test_situacao_pede_nova_geracao_quando_a_chave_do_cofre_some(tmp_path):
    caminho = tmp_path / "demo.db"
    conn = banco.conectar(str(caminho))
    banco_demo.gerar(conn, HOJE, pecas_dir=str(tmp_path / "p"), agora=AGORA)
    conn.close()
    assert banco_demo.situacao(str(caminho)) == "atual"
    (tmp_path / "dados" / "chave-do-teste.key").unlink()
    assert banco_demo.situacao(str(caminho)) == "antiga"


def test_chave_padrao_da_demo_fica_em_dados():
    assert Path(banco_demo.COFRE_PADRAO).is_absolute()
    assert Path(banco_demo.COFRE_PADRAO).parts[-2:] == ("dados", "demo-cofre.key")


@pytest.mark.parametrize("agora", [
    datetime(2026, 10, 7, 3, 10, tzinfo=eventos.FUSO),
    datetime(2026, 10, 7, 18, 40, tzinfo=eventos.FUSO)], ids=["madrugada", "fim-do-dia"])
def test_telas_de_tribunais_faixa_e_pastas_abrem_com_a_demo(tmp_path, monkeypatch, agora):
    import painel
    monkeypatch.setattr(painel_dados, "agora_cuiaba", lambda: agora)
    caminho = tmp_path / "demo.db"
    conn = banco.conectar(str(caminho))
    banco_demo.gerar(conn, agora.date(), pecas_dir=str(tmp_path / "pecas"), agora=agora)
    ids = {r["pasta_estado"]: r["id"] for r in conn.execute(
        "SELECT pasta_estado, id FROM demanda WHERE pasta_estado != ''")}
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(caminho))
    monkeypatch.setenv("CONTROLADORIA_PECAS", str(tmp_path / "pecas"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    c = painel.app.test_client()
    quadro = c.get("/").get_data(as_text=True)
    assert "foi recusada" in quadro and "Configurações › Tribunais" in quadro  # a faixa
    assert "Faltam os autos" in quadro and "Autos na pasta (2 arquivos)" in quadro
    telas = c.get("/configuracoes/tribunais")
    assert telas.status_code == 200
    html = telas.get_data(as_text=True)
    assert "Conectado · conferido" in html and "Senha recusada" in html
    assert "Não cadastrado" in html
    assert banco_demo.SENHA_DEMO not in html and banco_demo.CPF_DEMO not in html
    assert "guardado (final 09)" in html
    for demanda in ids.values():
        assert c.get(f"/demanda/{demanda}").status_code == 200


@pytest.mark.parametrize("fora", ["/etc/controladoria/cofre.key", "chave-solta.key"])
def test_demo_ignora_chave_do_ambiente_fora_de_dados(monkeypatch, tmp_path, fora):
    """banco_demo.py rodado com o ambiente de produção nunca cifra com a chave real."""
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", fora)
    assert banco_demo._chave_do_cofre() == banco_demo.COFRE_PADRAO
    dentro = str(tmp_path / "dados" / "outra.key")
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", dentro)
    assert banco_demo._chave_do_cofre() == dentro
