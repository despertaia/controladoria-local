import json
from datetime import datetime, timedelta

import pytest

from nucleo import banco, eventos, ponte, quadro

AGORA = datetime(2026, 10, 7, 10, 0, tzinfo=eventos.FUSO)
_SEQ = iter(range(1, 10_000))


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _no_lex(conn, referencia="2026-10-01", *, ajuste="", orientacao=""):
    """Cartão já na fila do Lex (coluna lex / na_fila), com uma publicação e um aviso."""
    k = next(_SEQ)
    numero = f"{k:07d}2024811004"[:20].ljust(20, "1")
    conn.execute("INSERT INTO processo (numero, tribunal, orgao_julgador, cliente, "
                 "parte_contraria, advogado_atua) VALUES (?, 'TJMT', 'Vara X', 'Maria', "
                 "'Banco Y', 1)", (numero,))
    agora = eventos.agora()
    d = conn.execute(
        "INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em, "
        "lex_estado, lex_ajuste, lex_orientacao) VALUES (?, 'lex', ?, ?, ?, 'na_fila', ?, ?)",
        (numero, referencia, agora, agora, ajuste, orientacao)).lastrowid
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, tipo, "
                 "orgao, texto, link) VALUES (?, ?, 'TJMT', ?, 'Intimação', 'Vara X', "
                 "'Texto da publicação', 'https://exemplo.test/p')", (k, numero, referencia))
    conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) "
                 "VALUES (?, 'publicacao', ?, ?)", (d, str(k), referencia))
    conn.execute("INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, "
                 "data_disponibilizacao, visto_em) VALUES ('1g', ?, ?, 'Intimação', ?, ?)",
                 (str(k), numero, referencia, agora))
    conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) "
                 "VALUES (?, 'aviso', ?, ?)", (d, f"1g:{k}", referencia))
    conn.commit()
    return d


def _estado(conn, d):
    return conn.execute("SELECT lex_estado FROM demanda WHERE id = ?", (d,)).fetchone()[0]


def _campos(conn, d, *campos):
    r = conn.execute(f"SELECT {', '.join(campos)} FROM demanda WHERE id = ?", (d,)).fetchone()
    return tuple(r)


def test_chave_gerada_valida_e_revogada(conn):
    assert ponte.chave_valida(conn, "qualquer") is False
    chave = ponte.gerar_chave(conn)
    assert len(chave) >= 40 and ponte.chave_valida(conn, chave)
    assert chave not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM config")])
    assert ponte.chave_valida(conn, chave + "x") is False
    ponte.revogar_chave(conn)
    assert ponte.chave_valida(conn, chave) is False


def test_chave_vazia_ou_nao_texto_nunca_vale(conn):
    ponte.gerar_chave(conn)
    assert ponte.chave_valida(conn, "") is False
    assert ponte.chave_valida(conn, None) is False


def test_contato(conn):
    assert ponte.ultimo_contato(conn) is None
    ponte.registrar_contato(conn)
    assert ponte.ultimo_contato(conn)


def test_proximo_reserva_o_mais_antigo_e_monta_o_pacote(conn):
    a = _no_lex(conn, referencia="2026-10-03"); b = _no_lex(conn, referencia="2026-10-01")
    pac = ponte.proximo(conn, AGORA)
    assert pac["demanda"] == b and pac["n"] == 1 and pac["modo"] == "novo"
    assert {"numero", "publicacoes", "intimacoes", "orientacao"} <= set(pac)
    assert pac["publicacoes"][0]["texto"] == "Texto da publicação"
    assert pac["intimacoes"][0]["tipo"] == "Intimação"
    assert pac["numero_formatado"] != pac["numero"] and pac["cliente"] == "Maria"
    assert pac["squad_anterior"] == "" and pac["run_anterior"] == ""
    assert _estado(conn, b) == "reservado"
    assert ponte.proximo(conn, AGORA)["demanda"] == a
    assert ponte.proximo(conn, AGORA) is None


def test_proximo_em_ajuste_traz_o_squad_anterior(conn):
    d = _no_lex(conn)
    ponte.proximo(conn, AGORA)
    ponte.concluir(conn, d, 1, squad="peticao-x", run_id="r", gate_status="aprovado",
                   citacoes_total=1, citacoes_falhas=0, pasta="p", agora=AGORA)
    conn.execute("UPDATE demanda SET coluna = 'lex', lex_estado = 'na_fila', "
                 "lex_ajuste = 'Reduza' WHERE id = ?", (d,))
    pac = ponte.proximo(conn, AGORA + timedelta(hours=1))
    assert pac["modo"] == "ajuste" and pac["n"] == 2
    assert pac["squad_anterior"] == "peticao-x" and pac["ajuste"] == "Reduza"
    assert pac["run_anterior"] == "r"


def test_reserva_vencida_volta_para_a_fila(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    assert ponte.vencer_reservas(conn, AGORA + timedelta(minutes=9)) == 0
    assert ponte.vencer_reservas(conn, AGORA + timedelta(minutes=11)) == 1
    assert _estado(conn, d) == "na_fila"
    assert conn.execute("SELECT resultado FROM execucao_lex").fetchone()[0] == "reserva_vencida"
    assert ponte.proximo(conn, AGORA + timedelta(minutes=12))["n"] == 2


def test_batida_renova_e_mostra_etapa(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.batida(conn, d, 1, squad="peticao-inicial-jec", etapa="pesquisa jurídica (8/13)",
                 agora=AGORA + timedelta(minutes=9))
    assert ponte.vencer_reservas(conn, AGORA + timedelta(minutes=15)) == 0
    assert _campos(conn, d, "lex_estado", "lex_etapa") == ("trabalhando", "pesquisa jurídica (8/13)")
    with pytest.raises(ponte.PonteInvalida):
        ponte.batida(conn, d, 7, agora=AGORA)


def test_batida_recusa_cartao_fora_do_lex(conn):
    d = _no_lex(conn)
    with pytest.raises(ponte.PonteInvalida):  # ainda na fila, não reservado
        ponte.batida(conn, d, 1, agora=AGORA)


def test_concluir_leva_a_revisao(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.concluir(conn, d, 1, squad="s", run_id="r", gate_status="aprovado", citacoes_total=25,
                   citacoes_falhas=0, pasta="dados/pecas/1/1", agora=AGORA + timedelta(minutes=95))
    assert _campos(conn, d, "coluna", "lex_estado") == ("revisao", "pronto")
    ex = conn.execute("SELECT resultado, citacoes_total, terminada_em FROM execucao_lex").fetchone()
    assert ex[0] == "pronto" and ex[1] == 25 and ex[2]
    ev = [e for e in eventos.listar(conn) if e["tipo"] == "lex_pronto"][0]
    assert ev["dados"]["minutos_lex"] == 95 and ev["dados"]["n"] == 1
    with pytest.raises(ponte.PonteInvalida):  # não conclui duas vezes
        ponte.concluir(conn, d, 1, squad="s", run_id="r", gate_status="aprovado",
                       citacoes_total=1, citacoes_falhas=0, pasta="p", agora=AGORA)


def test_falhas(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.falhar(conn, d, 1, "limite", "limite do plano", AGORA)
    assert _estado(conn, d) == "pausado_limite"
    assert ponte.proximo(conn, AGORA + timedelta(minutes=10)) is None
    assert ponte.proximo(conn, AGORA + timedelta(minutes=31))["n"] == 2
    ponte.falhar(conn, d, 2, "sem_tipo", "", AGORA)
    assert _campos(conn, d, "coluna", "lex_estado") == ("autos", "sem_tipo")
    with pytest.raises(ponte.PonteInvalida):
        ponte.falhar(conn, d, 2, "inventado", "", AGORA)


def test_erro_e_tempo_marcam_falhou(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.falhar(conn, d, 1, "erro", "quebrou", AGORA)
    assert _campos(conn, d, "lex_estado", "lex_detalhe") == ("falhou", "quebrou")
    assert conn.execute("SELECT resultado, detalhe FROM execucao_lex").fetchone()[:] == ("falhou", "quebrou")
    assert ponte.proximo(conn, AGORA + timedelta(hours=1)) is None  # não volta sozinho
    e = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.falhar(conn, e, 1, "tempo", "passou de 3 h", AGORA)
    assert _estado(conn, e) == "falhou"
    assert conn.execute("SELECT resultado FROM execucao_lex WHERE demanda_id = ?",
                        (e,)).fetchone()[0] == "tempo"


def test_funcoes_publicas_aceitam_agora_sem_fuso(conn):
    ingenuo = datetime(2026, 10, 7, 10, 0)
    d = _no_lex(conn)
    pac = ponte.proximo(conn, ingenuo)
    assert pac["n"] == 1
    ponte.batida(conn, d, 1, etapa="x", agora=ingenuo + timedelta(minutes=1))
    assert ponte.vencer_reservas(conn, ingenuo + timedelta(minutes=5)) == 0
    assert ponte.vencer_reservas(conn, ingenuo + timedelta(minutes=30)) == 1
    assert ponte.liberar_pausados(conn, ingenuo) == 0
    ponte.proximo(conn, ingenuo + timedelta(minutes=31))
    ponte.concluir(conn, d, 2, squad="s", run_id="r", gate_status="aprovado",
                   citacoes_total=1, citacoes_falhas=0, pasta="p",
                   agora=ingenuo + timedelta(minutes=40))
    assert _estado(conn, d) == "pronto"


def test_pacote_sem_processo_ou_com_tribunal_vazio_vira_texto(conn):
    d = _no_lex(conn)
    conn.execute("UPDATE processo SET tribunal = '' WHERE numero = "
                 "(SELECT numero FROM demanda WHERE id = ?)", (d,))
    pac = ponte.proximo(conn, AGORA)
    assert pac["tribunal"] == "" and pac["cliente"] == "Maria"
    linha = conn.execute("SELECT * FROM demanda WHERE id = ?", (d,)).fetchone()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM processo WHERE numero = ?", (linha["numero"],))
    assert ponte._pacote(conn, linha, 1, "novo")["tribunal"] == ""


def test_tentar_lex_de_novo_em_pausado_limite_volta_para_a_fila(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.falhar(conn, d, 1, "limite", "limite do plano", AGORA)
    assert _estado(conn, d) == "pausado_limite"
    quadro.tentar_lex_de_novo(conn, d)
    assert _campos(conn, d, "lex_estado", "lex_tentar_depois") == ("na_fila", None)


def test_vencer_reserva_com_linha_velha_nao_fecha_a_execucao_nova(tmp_path):
    caminho = str(tmp_path / "corrida.db")
    a = banco.conectar(caminho)
    d = _no_lex(a); ponte.proximo(a, AGORA); a.commit()
    velha = a.execute("SELECT * FROM demanda WHERE id = ?", (d,)).fetchone()  # lida antes
    # Outro processo vence a reserva e reserva de novo (execução 2, reserva válida).
    b = banco.conectar(caminho)
    depois = AGORA + timedelta(minutes=30)
    with b:
        assert ponte.vencer_reservas(b, depois) == 1
        assert ponte.proximo(b, depois)["n"] == 2
    b.close()
    # A linha velha ainda diz "reservado, vencida": a atualização condicional não deve agir.
    with a:
        assert ponte.vencer_reserva(a, velha, depois) is False
    ex = a.execute("SELECT n, resultado, terminada_em FROM execucao_lex ORDER BY n").fetchall()
    assert [(e["n"], e["resultado"]) for e in ex] == [(1, "reserva_vencida"), (2, "")]
    assert ex[1]["terminada_em"] is None
    assert _estado(a, d) == "reservado"
    a.close()


def test_vencer_reserva_devolve_true_e_registra(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    linha = conn.execute("SELECT * FROM demanda WHERE id = ?", (d,)).fetchone()
    assert ponte.vencer_reserva(conn, linha, AGORA + timedelta(minutes=11)) is True
    assert _estado(conn, d) == "na_fila"
    assert ponte.vencer_reserva(conn, linha, AGORA + timedelta(minutes=11)) is False


def test_reserva_sem_data_conta_como_vencida_em_vencer_reservas(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    conn.execute("UPDATE demanda SET lex_reservado_ate = NULL WHERE id = ?", (d,))
    assert ponte.vencer_reservas(conn, AGORA) == 1 and _estado(conn, d) == "na_fila"


# --- onda final -------------------------------------------------------------------

def test_batida_e_conclusao_guardam_o_nome_do_squad(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.batida(conn, d, 1, squad="replica-a-contestacao", squad_nome="Réplica à contestação",
                 etapa="pesquisa", agora=AGORA)
    assert _campos(conn, d, "lex_squad", "lex_squad_nome") == (
        "replica-a-contestacao", "Réplica à contestação")
    ponte.batida(conn, d, 1, etapa="redação", agora=AGORA)  # batida sem squad mantém o nome
    assert _campos(conn, d, "lex_squad_nome") == ("Réplica à contestação",)
    ponte.concluir(conn, d, 1, squad="replica-a-contestacao", run_id="r", gate_status="aprovado",
                   citacoes_total=1, citacoes_falhas=0, pasta="p", agora=AGORA)
    ex = conn.execute("SELECT squad, squad_nome FROM execucao_lex").fetchone()
    assert tuple(ex) == ("replica-a-contestacao", "Réplica à contestação")
    ev = [e for e in eventos.listar(conn) if e["tipo"] == "lex_pronto"][0]
    assert ev["dados"]["squad_nome"] == "Réplica à contestação"


def test_squad_trocado_sem_nome_nao_herda_o_nome_antigo(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.batida(conn, d, 1, squad="a", squad_nome="Nome A", agora=AGORA)
    ponte.batida(conn, d, 1, squad="b", agora=AGORA)
    assert _campos(conn, d, "lex_squad", "lex_squad_nome") == ("b", "")


def test_textos_do_mac_cortados_e_numa_linha(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.batida(conn, d, 1, squad="s" * 500, squad_nome="Nome\ncom\tquebra\x00" + "n" * 500,
                 etapa="e\r\n" + "e" * 500, agora=AGORA)
    squad, nome, etapa = _campos(conn, d, "lex_squad", "lex_squad_nome", "lex_etapa")
    assert len(squad) == 120 and len(nome) == 120 and len(etapa) == 200
    assert nome.startswith("Nome com quebra") and "\n" not in etapa and "\x00" not in nome
    ponte.falhar(conn, d, 1, "erro", "linha 1\nlinha 2 " + "x" * 500, AGORA)
    detalhe = _campos(conn, d, "lex_detalhe")[0]
    assert len(detalhe) == 300 and detalhe.startswith("linha 1 linha 2")
    assert conn.execute("SELECT detalhe FROM execucao_lex").fetchone()[0] == detalhe


def test_episodio_e_o_envio_ao_lex_mais_recente(conn):
    from nucleo import quadro
    d = _no_lex(conn)
    conn.execute("UPDATE demanda SET coluna = 'autos', lex_estado = '' WHERE id = ?", (d,))
    quadro.mover(conn, d, "lex", "advogado")
    envio = max(e["id"] for e in eventos.listar(conn) if e["tipo"] == "lex_enfileirado")
    pac = ponte.proximo(conn, AGORA)
    assert pac["episodio"] == envio
    ponte.falhar(conn, d, 1, "erro", "quebrou", AGORA)
    quadro.tentar_lex_de_novo(conn, d)  # mesmo pedido: mesmo episódio
    assert ponte.proximo(conn, AGORA)["episodio"] == envio
    ponte.falhar(conn, d, 2, "limite", "", AGORA)
    assert ponte.proximo(conn, AGORA + timedelta(minutes=31))["episodio"] == envio
    ponte.concluir(conn, d, 3, squad="s", run_id="r", gate_status="aprovado", citacoes_total=0,
                   citacoes_falhas=0, pasta="p", agora=AGORA + timedelta(minutes=40))
    quadro.mover(conn, d, "lex", "advogado", ajuste="trocar o pedido")  # pedido novo
    novo = ponte.proximo(conn, AGORA + timedelta(minutes=50))["episodio"]
    assert novo > envio
    assert novo == max(e["id"] for e in eventos.listar(conn) if e["tipo"] == "lex_enfileirado")


def test_episodio_ignora_envio_de_outro_cartao_do_mesmo_processo(conn):
    d = _no_lex(conn)
    numero = _campos(conn, d, "numero")[0]
    outro = eventos.registrar(conn, "lex_enfileirado", numero, {"demanda": d + 1000})
    assert ponte.proximo(conn, AGORA)["episodio"] is None
    assert outro


@pytest.mark.parametrize("acao", ["batida", "concluir", "falhar"])
def test_estado_conferido_dentro_da_transacao(conn, monkeypatch, acao):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    conn.commit()
    vistos = []
    original = ponte._ativa

    def espiao(c, *args):
        vistos.append(c.in_transaction)
        return original(c, *args)

    monkeypatch.setattr(ponte, "_ativa", espiao)
    with conn:
        if acao == "batida":
            ponte.batida(conn, d, 1, etapa="x", agora=AGORA)
        elif acao == "concluir":
            ponte.concluir(conn, d, 1, squad="s", run_id="r", gate_status="aprovado",
                           citacoes_total=0, citacoes_falhas=0, pasta="p", agora=AGORA)
        else:
            ponte.falhar(conn, d, 1, "erro", "x", AGORA)
    assert vistos == [True]


def test_recusa_dentro_da_trava_nao_deixa_transacao_aberta(conn):
    d = _no_lex(conn)
    conn.commit()
    with pytest.raises(ponte.PonteInvalida):
        ponte.batida(conn, d, 1, agora=AGORA)
    assert conn.in_transaction is False


def test_campos_do_gate_pela_regra_do_mac():
    gate = {"gate_status": " reprovado\n", "citations": [
        {"status": "verificada"}, {"status": "Verified ok"}, {"status": "não encontrada"},
        {"status": ""}, "lixo"]}
    assert ponte.campos_do_gate(gate) == {"gate_status": "reprovado", "citacoes_total": 4,
                                          "citacoes_falhas": 2}
    assert ponte.campos_do_gate({"citations": "x"}) == {
        "gate_status": "desconhecido", "citacoes_total": 0, "citacoes_falhas": 0}


def test_chave_invalida_conta_sem_evento(conn):
    assert ponte.chaves_invalidas(conn) == (0, None)
    ponte.registrar_chave_invalida(conn)
    ponte.registrar_chave_invalida(conn)
    n, quando = ponte.chaves_invalidas(conn)
    assert n == 2 and quando
    assert not [e for e in eventos.listar(conn) if "chave" in e["tipo"]]


def test_chave_nova_zera_o_contador_de_chave_invalida(conn):
    ponte.registrar_chave_invalida(conn)
    ponte.registrar_chave_invalida(conn)
    ponte.gerar_chave(conn)
    assert ponte.chaves_invalidas(conn) == (0, None)


def test_mac_conectado_e_sem_plano(conn, monkeypatch):
    monkeypatch.setattr(eventos, "agora", lambda: AGORA.isoformat())
    assert ponte.mac_conectado(conn, AGORA) is False
    ponte.registrar_contato(conn)
    assert ponte.mac_conectado(conn, AGORA + timedelta(minutes=5))
    assert not ponte.mac_conectado(conn, AGORA + timedelta(minutes=6))
    assert ponte.mac_sem_plano(conn, AGORA) is False
    ponte.registrar_contato(conn, claude_ok=False)
    assert ponte.mac_sem_plano(conn, AGORA + timedelta(minutes=2))
    assert not ponte.mac_sem_plano(conn, AGORA + timedelta(minutes=10))  # Mac sumiu
    ponte.registrar_contato(conn, claude_ok=True)
    assert ponte.mac_sem_plano(conn, AGORA) is False


def test_sem_tipo_guarda_a_explicacao_do_lex(conn):
    d = _no_lex(conn); ponte.proximo(conn, AGORA)
    ponte.falhar(conn, d, 1, "sem_tipo", "a publicação não diz se cabe réplica\nou embargos",
                 AGORA)
    assert _campos(conn, d, "lex_estado", "lex_detalhe") == (
        "sem_tipo", "a publicação não diz se cabe réplica ou embargos")
    e = _no_lex(conn); ponte.proximo(conn, AGORA)  # Mac antigo: só a frase padrão
    ponte.falhar(conn, e, 1, "sem_tipo", "o Lex não teve segurança sobre o tipo de peça", AGORA)
    assert _campos(conn, e, "lex_detalhe") == ("",)
