"""A agenda assinada: token, sem cliente nem parte nem trecho, «[A CONFERIR]» (spec, seção 6)."""

import hashlib
import logging
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

import painel
from nucleo import agenda, banco, config, eventos, prazos_banco

A = "00012345620248110041"
B = "00054321520248110041"
AGORA = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)
RAIZ = pathlib.Path(__file__).resolve().parent.parent
MARCA = "ZZMARCAZZ"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    caminho = str(tmp_path / "b.db")
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    c = banco.conectar(caminho)
    c.execute("INSERT INTO processo (numero, tribunal, advogado_atua, cliente, parte_contraria) "
              "VALUES (?, 'TJMT', 1, 'MARIA FICTICIA', 'BANCO ALFA')", (A,))
    c.commit()
    yield c
    c.close()


def _prazo(conn, ref, *, codigo="contestacao", ato=None, conferir=True, d=None, numero=A):
    pid = prazos_banco.abrir(conn, numero, None, "publicacao", ref, "2026-11-05")
    if codigo:
        prazos_banco.corrigir(conn, pid, codigo=codigo)
    else:
        prazos_banco.corrigir(conn, pid, ato=ato, dias=15, unidade="uteis")
    conn.execute("UPDATE prazo SET trecho = 'TRECHO SIGILOSO' WHERE id = ?", (pid,))
    if conferir:
        prazos_banco.conferir(conn, pid)
    if d is not None:
        prazos_banco.escolher_trabalho(conn, pid, d=d)
    conn.commit()
    return pid


def _desdobrar(ics):
    return ics.replace("\r\n ", "")


def _eventos(ics):
    """Os VEVENT como {propriedade: valor}, já desdobrados (sem os parâmetros no nome)."""
    saida, atual = [], None
    for linha in _desdobrar(ics).split("\r\n"):
        if linha == "BEGIN:VEVENT":
            atual = {}
        elif linha == "END:VEVENT":
            saida.append(atual)
            atual = None
        elif atual is not None and ":" in linha and not linha.startswith(("BEGIN", "END")):
            nome, valor = linha.split(":", 1)
            atual.setdefault(nome, valor)
    return saida


def test_token_so_o_hash_fica_no_banco_e_trocar_invalida_o_anterior(conn):
    t1 = agenda.gerar_token(conn)
    assert len(t1) >= 40 and agenda.tem_token(conn)
    assert t1 not in repr([tuple(r) for r in conn.execute("SELECT * FROM config")])
    assert t1 not in repr([tuple(r) for r in conn.execute("SELECT * FROM evento")])
    assert config.ler(conn, agenda.CHAVE_HASH) == hashlib.sha256(t1.encode()).hexdigest()
    assert agenda.token_valido(conn, t1)
    t2 = agenda.gerar_token(conn)
    assert not agenda.token_valido(conn, t1) and agenda.token_valido(conn, t2)
    agenda.revogar_token(conn)
    assert not agenda.tem_token(conn) and not agenda.token_valido(conn, t2)


@pytest.mark.parametrize("ruim", ["", None, "x", "çãõ", 5, b"abc", "\ud800", "a" * 5000])
def test_token_invalido(conn, ruim):
    agenda.gerar_token(conn)
    assert agenda.token_valido(conn, ruim) is False


def test_token_sem_nenhum_gerado_nunca_vale(conn):
    assert not agenda.tem_token(conn)
    for t in ("", "x", hashlib.sha256(b"").hexdigest()):
        assert agenda.token_valido(conn, t) is False


def test_token_compara_em_tempo_constante(conn, monkeypatch):
    chamadas = []
    real = agenda.hmac.compare_digest
    monkeypatch.setattr(agenda.hmac, "compare_digest",
                        lambda a, b: chamadas.append(1) or real(a, b))
    t = agenda.gerar_token(conn)
    assert agenda.token_valido(conn, t) and agenda.token_valido(conn, "outro") is False
    assert len(chamadas) == 2


def test_eventos_de_dia_inteiro_na_fatal_e_no_trabalho(conn):
    _prazo(conn, "1", d=2)
    ics = _desdobrar(agenda.montar_ics(conn, AGORA))
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == 2
    assert "DTSTART;VALUE=DATE:20261130" in ics and "DTEND;VALUE=DATE:20261201" in ics
    assert "DTSTART;VALUE=DATE:20261126" in ics and "DTEND;VALUE=DATE:20261127" in ics
    assert "SUMMARY:Contestação · 0001234-56.2024.8.11.0041 · fatal 30/11" in ics
    assert "SUMMARY:Trabalhar: Contestação · 0001234-56.2024.8.11.0041 · fatal 30/11" in ics
    assert ics.count("BEGIN:VALARM") == 2 and ics.count("TRIGGER:-PT15H") == 2
    assert "DTSTAMP:20261106T130000Z" in ics


def test_o_alarme_de_dia_inteiro_toca_as_9h_do_dia_anterior(conn):
    """O evento de dia inteiro começa à meia-noite; -PT15H cai às 9h do dia anterior, nunca
    no próprio dia nem dois dias antes."""
    _prazo(conn, "1")
    gatilho = re.search(r"TRIGGER:(-PT(\d+)H)", agenda.montar_ics(conn, AGORA))
    toca = datetime(2026, 11, 30) - timedelta(hours=int(gatilho.group(2)))
    assert toca == datetime(2026, 11, 29, 9, 0)


def test_trabalho_igual_a_fatal_vira_um_evento_so(conn):
    _prazo(conn, "1")
    assert agenda.montar_ics(conn, AGORA).count("BEGIN:VEVENT") == 1


def test_trabalho_depois_da_fatal_nunca_vira_evento(conn):
    pid = _prazo(conn, "1", d=2)
    conn.execute("UPDATE prazo SET trabalho = '2026-12-15' WHERE id = ?", (pid,))
    conn.commit()
    ics = _desdobrar(agenda.montar_ics(conn, AGORA))
    assert ics.count("BEGIN:VEVENT") == 1 and "20261215" not in ics and "Trabalhar" not in ics


def test_trabalho_ilegivel_deixa_so_a_fatal(conn):
    pid = _prazo(conn, "1", d=2)
    conn.execute("UPDATE prazo SET trabalho = 'quarta' WHERE id = ?", (pid,))
    conn.commit()
    ics = agenda.montar_ics(conn, AGORA)
    assert ics.count("BEGIN:VEVENT") == 1 and "quarta" not in ics


def test_nao_conferido_entra_marcado_e_nunca_parece_confirmado(conn):
    _prazo(conn, "1", conferir=False)
    ics = _desdobrar(agenda.montar_ics(conn, AGORA))
    assert "SUMMARY:[A CONFERIR] Contestação" in ics
    [ev] = _eventos(ics)
    assert ev["STATUS"] == "TENTATIVE" and "confirmado" not in ev["SUMMARY"].lower()
    assert "não conferido" in ev["DESCRIPTION"].lower() and "painel" in ev["DESCRIPTION"].lower()
    # o texto do lembrete também leva a marca
    assert re.search(r"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:\[A CONFERIR\]", ics)


def test_conferido_diz_para_conferir_no_painel_e_vira_confirmado(conn):
    _prazo(conn, "1")
    [ev] = _eventos(agenda.montar_ics(conn, AGORA))
    assert ev["STATUS"] == "CONFIRMED" and "[A CONFERIR]" not in ev["SUMMARY"]
    assert "painel" in ev["DESCRIPTION"].lower()


def test_sem_cliente_sem_parte_sem_trecho_nem_com_ato_escrito_a_mao(conn):
    _prazo(conn, "1")
    _prazo(conn, "2", codigo="", ato="Manifestação de MARIA FICTICIA contra BANCO ALFA")
    ics = _desdobrar(agenda.montar_ics(conn, AGORA))
    for proibido in ("MARIA", "FICTICIA", "BANCO", "ALFA", "SIGILOSO", "Manifestação"):
        assert proibido not in ics
    assert "SUMMARY:Prazo · 0001234-56.2024.8.11.0041 · fatal 30/11" in ics


def test_nenhum_texto_livre_chega_a_agenda(conn):
    """Marca em cada campo de texto livre do prazo e do processo: nenhuma sai no .ics."""
    pid = _prazo(conn, "1", codigo="", ato=f"Ato {MARCA}")
    conn.execute(
        "UPDATE prazo SET motivo = ?, alerta = ?, trecho = ?, ressalvas = ?, rito = ?, "
        "origem_dos_dias = ?, calculo = ? WHERE id = ?",
        tuple(f"{c} {MARCA}" for c in ("motivo", "alerta", "trecho")) + (
            f'["ressalva {MARCA}"]', f"rito {MARCA}", f"origem {MARCA}",
            f'{{"x": "calculo {MARCA}"}}', pid))
    conn.execute("UPDATE processo SET cliente = ?, parte_contraria = ?, orgao_julgador = ?, "
                 "classe = ?, partes_json = ?, ultimo_andamento_texto = ? WHERE numero = ?",
                 tuple(f"{c} {MARCA}" for c in ("cli", "par", "org", "cla", "pj", "ult")) + (A,))
    conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, orgao, "
                 "classe, texto) VALUES (1, ?, 'TJMT', '2026-11-05', ?, ?, ?)",
                 (A, f"orgao {MARCA}", f"classe {MARCA}", f"texto {MARCA}"))
    conn.commit()
    ics = agenda.montar_ics(conn, AGORA)
    assert "BEGIN:VEVENT" in ics and MARCA not in _desdobrar(ics) and MARCA[:6] not in ics


def test_cumprido_descartado_e_sem_data_saem(conn):
    a, b = _prazo(conn, "1"), _prazo(conn, "2")
    prazos_banco.cumprir(conn, a)
    prazos_banco.descartar(conn, b)
    prazos_banco.abrir(conn, A, None, "publicacao", "3", "2026-11-05")
    assert "BEGIN:VEVENT" not in agenda.montar_ics(conn, AGORA)


def test_so_conferido_e_a_conferir_entram(conn):
    pid = _prazo(conn, "1")
    for estado in ("aguardando_lex", "a_definir", "sem_prazo", "cumprido", "descartado"):
        conn.execute("UPDATE prazo SET estado = ? WHERE id = ?", (estado, pid))
        assert "BEGIN:VEVENT" not in agenda.montar_ics(conn, AGORA), estado


def test_processo_arquivado_segue_na_agenda(conn):
    _prazo(conn, "1")
    conn.execute("UPDATE processo SET arquivado_por_voce_em = 'x'")
    assert "BEGIN:VEVENT" in agenda.montar_ics(conn, AGORA)
    conn.execute("UPDATE processo SET excluido_em = 'x'")
    assert "BEGIN:VEVENT" in agenda.montar_ics(conn, AGORA)


def test_prazo_com_data_de_processo_nao_e_meu_segue_na_agenda(conn):
    """A agenda acompanha o painel: o prazo com data fica na aba mesmo com o processo marcado
    «Não é meu», então fica também na agenda, qualquer que seja a marca do processo."""
    _prazo(conn, "1")
    for atua in (0, None, 1):
        conn.execute("UPDATE processo SET descartado_em = 'x', advogado_atua = ?", (atua,))
        assert "BEGIN:VEVENT" in agenda.montar_ics(conn, AGORA), atua


def test_data_ilegivel_nao_derruba_a_agenda_inteira(conn):
    ruim = _prazo(conn, "1")
    _prazo(conn, "2")
    for lixo in ("lixo", "2026-13-45", "9999-12-31", "0001-01-01", "", "30/11/2026"):
        conn.execute("UPDATE prazo SET fatal = ? WHERE id = ?", (lixo, ruim))
        ics = agenda.montar_ics(conn, AGORA)
        assert ics.count("BEGIN:VEVENT") == 1, lixo
        assert f"prazo-{ruim}-" not in ics


def test_fatal_maior_usa_a_data_guardada(conn):
    """A data da conta já é a que o advogado escolheu (a menor, ou a maior se clicou); a agenda
    mostra a guardada e nunca a alternativa."""
    pid = _prazo(conn, "1")
    conn.execute("UPDATE prazo SET fatal = '2026-12-02', fatal_alternativa = '2026-11-30', "
                 "fatal_maior = 1, trabalho = '2026-12-02' WHERE id = ?", (pid,))
    ics = _desdobrar(agenda.montar_ics(conn, AGORA))
    assert "DTSTART;VALUE=DATE:20261202" in ics and "20261130" not in ics
    conn.execute("UPDATE prazo SET fatal = '2026-11-30', fatal_alternativa = '2026-12-02', "
                 "fatal_maior = 0, trabalho = '2026-11-30' WHERE id = ?", (pid,))
    ics = _desdobrar(agenda.montar_ics(conn, AGORA))
    assert "DTSTART;VALUE=DATE:20261130" in ics and "20261202" not in ics


def test_so_datas_nenhum_fuso_muda_o_dia(conn):
    _prazo(conn, "1", d=2)
    # 23h30 em Cuiabá (UTC-4) já é o dia seguinte em UTC: só o DTSTAMP anda
    cuiaba = timezone(timedelta(hours=-4))
    ics = _desdobrar(agenda.montar_ics(conn, datetime(2026, 11, 29, 23, 30, tzinfo=cuiaba)))
    assert "DTSTAMP:20261130T033000Z" in ics
    assert "DTSTART;VALUE=DATE:20261130" in ics and "DTSTART;VALUE=DATE:20261126" in ics
    assert not re.search(r"DT(START|END)[^:]*:\d{8}T", ics)
    # relógio sem fuso: é lido como UTC, não como o fuso da máquina
    ingenuo = _desdobrar(agenda.montar_ics(conn, datetime(2026, 11, 6, 13, 0)))
    assert "DTSTAMP:20261106T130000Z" in ingenuo


def test_agora_padrao_e_utc(conn):
    _prazo(conn, "1")
    assert re.search(r"DTSTAMP:\d{8}T\d{6}Z", agenda.montar_ics(conn))


def test_mudar_a_data_atualiza_o_mesmo_evento_e_sair_da_agenda_some(conn):
    pid = _prazo(conn, "1", d=2)
    conn.execute("UPDATE prazo SET atualizado_em = '2026-11-06T10:00:00-04:00' WHERE id = ?", (pid,))
    conn.commit()
    antes = {e["UID"]: e for e in _eventos(agenda.montar_ics(conn, AGORA))}
    assert set(antes) == {f"prazo-{pid}-fatal@controladoria", f"prazo-{pid}-trabalho@controladoria"}
    prazos_banco.escolher_trabalho(conn, pid, d=1)
    conn.execute("UPDATE prazo SET atualizado_em = '2026-11-07T10:00:00-04:00' WHERE id = ?", (pid,))
    conn.commit()
    depois = {e["UID"]: e for e in _eventos(agenda.montar_ics(conn, AGORA))}
    assert set(depois) == set(antes)  # mesmo UID: atualiza, não duplica
    uid = f"prazo-{pid}-trabalho@controladoria"
    assert depois[uid]["DTSTART;VALUE=DATE"] != antes[uid]["DTSTART;VALUE=DATE"]
    assert int(depois[uid]["SEQUENCE"]) > int(antes[uid]["SEQUENCE"])
    assert depois[uid]["LAST-MODIFIED"] == "20261107T140000Z"
    prazos_banco.cumprir(conn, pid)
    conn.commit()
    assert "BEGIN:VEVENT" not in agenda.montar_ics(conn, AGORA)


def test_o_uid_nao_depende_do_estado(conn):
    pid = _prazo(conn, "1", conferir=False)
    antes = [e["UID"] for e in _eventos(agenda.montar_ics(conn, AGORA))]
    prazos_banco.conferir(conn, pid)
    depois = [e["UID"] for e in _eventos(agenda.montar_ics(conn, AGORA))]
    assert antes == depois == [f"prazo-{pid}-fatal@controladoria"]


def test_montar_a_agenda_nao_grava_nada(conn):
    _prazo(conn, "1", d=2)
    antes = conn.total_changes
    agenda.montar_ics(conn, AGORA)
    assert conn.total_changes == antes and not conn.in_transaction


def test_agenda_vazia_e_um_calendario_valido(conn):
    ics = agenda.montar_ics(conn, AGORA)
    assert "BEGIN:VEVENT" not in ics
    _validar(ics)


def test_linhas_dobradas_em_75_octetos_e_escape(conn):
    pid = _prazo(conn, "1", codigo="replica_fato_impeditivo")
    ics = agenda.montar_ics(conn, AGORA)
    for linha in ics.split("\r\n"):
        assert len(linha.encode("utf-8")) <= 75
    assert "impeditivo\\, modificativo" in _desdobrar(ics)
    assert f"UID:prazo-{pid}-fatal@controladoria" in _desdobrar(ics)
    assert "\n" not in ics.replace("\r\n", "")


def test_dobra_conta_octetos_e_nao_parte_caractere():
    texto = "SUMMARY:" + "ção·" * 60
    pedacos = agenda._dobrar(texto)
    assert len(pedacos) > 1 and all(len(p.encode("utf-8")) <= 75 for p in pedacos)
    assert all(p.startswith(" ") for p in pedacos[1:])
    assert "".join([pedacos[0], *(p[1:] for p in pedacos[1:])]) == texto
    for p in pedacos:
        p.encode("utf-8").decode("utf-8")
    assert agenda._dobrar("X") == ["X"] and agenda._dobrar("a" * 75) == ["a" * 75]
    assert len(agenda._dobrar("a" * 76)) == 2


def test_escape_de_virgula_ponto_e_virgula_barra_e_quebra():
    assert agenda._escapar("a,b;c\\d\ne\r\nf") == "a\\,b\\;c\\\\d\\ne\\nf"
    assert agenda._escapar("a\x00b\x07c\x7fd") == "abcd"


# --- um validador de iCalendar feito à mão (não há analisador no ambiente) ----------------

_DURACAO = re.compile(r"^[+-]?P(T\d+H(\d+M)?(\d+S)?|T\d+M(\d+S)?|T\d+S|\d+D|\d+W)$")


def _validar(ics: str) -> list[dict]:
    """RFC 5545 no que a agenda usa: CRLF, 75 octetos, aninhamento, propriedades obrigatórias,
    datas reais, DTEND exclusivo, escape de texto, UID único. Devolve os eventos."""
    assert ics.endswith("\r\n") and "\r\n\r\n" not in ics
    assert not re.search(r"(?<!\r)\n|\r(?!\n)", ics)
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", ics)
    brutas = ics[:-2].split("\r\n")
    for linha in brutas:
        assert len(linha.encode("utf-8")) <= 75, linha
    linhas = []
    for linha in brutas:
        if linha.startswith(" "):
            assert linhas, "continuação sem linha"
            linhas[-1] += linha[1:]
        else:
            linhas.append(linha)
    assert linhas[0] == "BEGIN:VCALENDAR" and linhas[-1] == "END:VCALENDAR"
    pilha, eventos_, atual, uids = [], [], None, set()
    cal = {}
    for linha in linhas:
        assert ":" in linha, linha
        nome_param, valor = linha.split(":", 1)
        nome, *params = nome_param.split(";")
        assert re.fullmatch(r"[A-Z0-9-]+", nome), nome
        if nome == "BEGIN":
            pilha.append(valor)
            if valor == "VEVENT":
                atual = {"alarmes": []}
            if valor == "VALARM":
                atual["alarmes"].append({})
            continue
        if nome == "END":
            assert pilha.pop() == valor
            if valor == "VEVENT":
                eventos_.append(atual)
                atual = None
            continue
        alvo = (atual["alarmes"][-1] if pilha[-1] == "VALARM" else atual
                if pilha[-1] == "VEVENT" else cal)
        assert nome not in alvo, f"propriedade repetida: {nome}"
        alvo[nome] = (params, valor)
        if nome in ("SUMMARY", "DESCRIPTION", "X-WR-CALNAME"):
            assert not re.search(r"(?<!\\)[,;]", valor.replace("\\\\", "")), valor
            assert not re.search(r"\\[^,;\\nN]", valor), valor
    assert pilha == []
    assert cal["VERSION"][1] == "2.0" and cal["PRODID"][1]
    for ev in eventos_:
        for obrigatoria in ("UID", "DTSTAMP", "DTSTART", "SUMMARY"):
            assert obrigatoria in ev, obrigatoria
        uid = ev["UID"][1]
        assert uid not in uids
        uids.add(uid)
        assert re.fullmatch(r"\d{8}T\d{6}Z", ev["DTSTAMP"][1])
        datetime.strptime(ev["DTSTAMP"][1], "%Y%m%dT%H%M%SZ")
        assert ev["DTSTART"][0] == ["VALUE=DATE"] and ev["DTEND"][0] == ["VALUE=DATE"]
        ini = datetime.strptime(ev["DTSTART"][1], "%Y%m%d").date()
        fim = datetime.strptime(ev["DTEND"][1], "%Y%m%d").date()
        assert fim - ini == timedelta(days=1)  # DTEND é exclusivo
        for alarme in ev["alarmes"]:
            assert alarme["ACTION"][1] == "DISPLAY" and alarme["DESCRIPTION"][1]
            assert _DURACAO.match(alarme["TRIGGER"][1]), alarme["TRIGGER"]
        if "SEQUENCE" in ev:
            assert ev["SEQUENCE"][1].isdigit()
    return eventos_


def test_o_calendario_inteiro_passa_no_validador(conn):
    _prazo(conn, "1", d=2)
    _prazo(conn, "2", conferir=False, codigo="replica_fato_impeditivo")
    _prazo(conn, "3", codigo="", ato="qualquer")
    eventos_ = _validar(agenda.montar_ics(conn, AGORA))
    assert len(eventos_) == 4


def test_o_validador_pega_defeitos_conhecidos():
    base = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//x//PT\r\n%s"
            "END:VCALENDAR\r\n")
    evento = ("BEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20261106T130000Z\r\nDTSTART;VALUE=DATE:20261130\r\n"
              "DTEND;VALUE=DATE:20261201\r\nSUMMARY:%s\r\nEND:VEVENT\r\n")
    _validar(base % (evento % "ok"))
    for ruim in (base.replace("\r\n", "\n") % (evento % "ok"),
                 base % (evento % "a, b"), base % (evento % ("x" * 80)),
                 base % (evento.replace("20261201", "20261130") % "ok")):
        with pytest.raises(AssertionError):
            _validar(ruim)


# --- rotas -------------------------------------------------------------------------------

def test_rota_com_token(conn):
    token = agenda.gerar_token(conn)
    conn.commit()
    _prazo(conn, "1")
    r = painel.app.test_client().get(f"/agenda.ics?t={token}")
    assert r.status_code == 200 and r.mimetype == "text/calendar"
    assert r.headers["Cache-Control"] == "no-store"
    assert "Contestação" in r.get_data(as_text=True)
    _validar(r.get_data(as_text=True))


def test_rota_nao_pede_login_mas_exige_token(conn, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    token = agenda.gerar_token(conn)
    conn.commit()
    c = painel.app.test_client()
    assert c.get(f"/agenda.ics?t={token}").status_code == 200
    assert c.get("/agenda.ics?t=errado").status_code == 403
    assert c.get("/agenda.ics").status_code == 403
    r = c.get("/agenda.ics?t=errado")
    assert r.headers["Cache-Control"] == "no-store" and "Contest" not in r.get_data(as_text=True)


def test_403_nao_diz_o_motivo(conn):
    agenda.gerar_token(conn)
    conn.commit()
    c = painel.app.test_client()
    corpos = {c.get(u).get_data() for u in ("/agenda.ics", "/agenda.ics?t=errado",
                                           "/agenda.ics?t=", "/agenda.ics?t=a&t=b")}
    assert len(corpos) == 1  # sem token, token errado: a mesma resposta
    corpo = corpos.pop().decode().lower()
    for palavra in ("revog", "token", "inválid", "expir", "errado"):
        assert palavra not in corpo


def test_token_so_na_consulta_nao_no_formulario_nem_no_cabecalho(conn):
    token = agenda.gerar_token(conn)
    conn.commit()
    c = painel.app.test_client()
    assert c.post("/agenda.ics", data={"t": token}).status_code == 405
    assert c.get("/agenda.ics", headers={"Authorization": f"Bearer {token}",
                                         "X-Token": token}).status_code == 403


def test_token_no_caminho_nao_existe(conn):
    token = agenda.gerar_token(conn)
    conn.commit()
    assert painel.app.test_client().get(f"/agenda.ics/{token}").status_code == 404


def test_revogado_na_rota(conn):
    token = agenda.gerar_token(conn)
    conn.commit()
    c = painel.app.test_client()
    assert c.post("/configuracoes/prazos/agenda/revogar").status_code == 302
    assert c.get(f"/agenda.ics?t={token}").status_code == 403


def test_a_rota_no_modo_apresentacao_fica_indisponivel_e_sem_agenda(conn):
    """Quem abre o endereço no navegador com o modo ligado não baixa números reais. O serviço
    de calendário não tem a sessão do navegador, então não é afetado."""
    token = agenda.gerar_token(conn)
    conn.commit()
    _prazo(conn, "1")
    c = painel.app.test_client()
    with c.session_transaction() as s:
        s["apresentacao"] = True
    r = c.get(f"/agenda.ics?t={token}")
    assert r.status_code == 403 and r.mimetype != "text/calendar"
    assert "0001234" not in r.get_data(as_text=True) and token not in r.get_data(as_text=True)
    with c.session_transaction() as s:
        s["apresentacao"] = False
    assert c.get(f"/agenda.ics?t={token}").status_code == 200


def test_o_endereco_nunca_vai_ao_redirecionamento_de_login(conn, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    r = painel.app.test_client().get("/carteira?t=SEGREDOSEGREDO&agenda=SEGREDOSEGREDO")
    assert r.status_code == 302 and "SEGREDOSEGREDO" not in r.headers["Location"]


def test_o_token_nao_vai_a_nenhum_registro(conn, caplog):
    token = agenda.gerar_token(conn)
    conn.commit()
    _prazo(conn, "1")
    c = painel.app.test_client()
    with caplog.at_level(logging.DEBUG):
        assert c.get(f"/agenda.ics?t={token}").status_code == 200
        assert c.get("/agenda.ics?t=ERRADOERRADO").status_code == 403
        c.post("/configuracoes/prazos/agenda/gerar")
        c.post("/configuracoes/prazos/agenda/revogar")
    texto = caplog.text + repr([tuple(r) for r in conn.execute("SELECT * FROM evento")])
    assert token not in texto and "ERRADOERRADO" not in texto
    assert [e["tipo"] for e in eventos.listar(conn) if e["tipo"].startswith("agenda_")] == [
        "agenda_token_gerado", "agenda_token_gerado", "agenda_token_revogado"]
    assert all(e["dados"] == {} for e in eventos.listar(conn) if e["tipo"].startswith("agenda_"))


def test_o_registro_do_servidor_nao_guarda_a_consulta():
    """O nginx da produção registra o caminho, sem a consulta nem o referer, e o gunicorn não
    liga o registro de acessos. Se alguém mudar isso, o endereço da agenda iria para o disco."""
    arquivo = RAIZ / "deploy" / "provisionar.sh"
    if not arquivo.exists():  # a distribuição dos mentorados não leva a pasta deploy/
        pytest.skip("deploy/ só existe no repositório privado")
    texto = arquivo.read_text(encoding="utf-8")
    formato = re.search(r"log_format controladoria_sem_consulta\s+(.+?);", texto, re.S).group(1)
    for proibido in ("$request ", '$request"', "$args", "$query_string", "$request_uri",
                     "$http_referer", "$is_args"):
        assert proibido not in formato + " ", proibido
    assert "access_log /var/log/nginx/$SUBDOMINIO.access.log controladoria_sem_consulta;" in texto
    assert "access_log" not in texto.replace("access_log /var/log/nginx/$SUBDOMINIO.access.log "
                                             "controladoria_sem_consulta;", "")
    assert "access-logfile" not in texto and "--log-level debug" not in texto


def test_gerar_mostra_o_endereco_uma_vez_sem_cache(conn):
    c = painel.app.test_client()
    r = c.post("/configuracoes/prazos/agenda/gerar")
    t = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert "/agenda.ics?t=" in t and "única vez" in t
    token = t.split("/agenda.ics?t=")[1].split('"')[0]
    assert c.get(f"/agenda.ics?t={token}").status_code == 200
    depois = c.get("/configuracoes/prazos").get_data(as_text=True)
    assert token not in depois and "Revogar" in depois and "pode levar horas" in depois
    with c.session_transaction() as s:
        assert token not in repr(dict(s))  # nem no cookie de sessão (que o navegador lê)


def test_a_tela_avisa_quem_tem_o_endereco_ve_e_o_servico_demora(conn):
    pagina = painel.app.test_client().get("/configuracoes/prazos").get_data(as_text=True)
    texto = " ".join(re.sub(r"<[^>]+>", " ", pagina).split())
    assert "Quem tiver o endereço vê os números dos processos e os atos" in texto
    assert "pode levar horas" in texto and "Confira sempre no painel" in texto
    assert "Gerar endereço" in texto and "Revogar" not in texto


def test_gerar_outro_revoga_o_anterior_pela_rota(conn):
    c = painel.app.test_client()
    pegar = lambda: c.post("/configuracoes/prazos/agenda/gerar").get_data(as_text=True)  # noqa: E731
    um = pegar().split("/agenda.ics?t=")[1].split('"')[0]
    dois = pegar().split("/agenda.ics?t=")[1].split('"')[0]
    assert um != dois
    assert c.get(f"/agenda.ics?t={um}").status_code == 403
    assert c.get(f"/agenda.ics?t={dois}").status_code == 200


def test_endereco_em_https_na_producao(conn, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    c = painel.app.test_client()
    with c.session_transaction() as s:
        s["logado"] = True
    t = c.post("/configuracoes/prazos/agenda/gerar").get_data(as_text=True)
    assert "https://localhost/agenda.ics?t=" in t


def test_a_secao_some_no_modo_apresentacao(conn):
    c = painel.app.test_client()
    with c.session_transaction() as s:
        s["apresentacao"] = True
    pagina = c.get("/configuracoes/prazos").get_data(as_text=True)
    assert "Gerar endereço" not in pagina and "agenda.ics" not in pagina


@pytest.mark.parametrize("rota", ["/configuracoes/prazos/agenda/gerar",
                                  "/configuracoes/prazos/agenda/revogar"])
def test_acoes_do_token_com_csrf_login_e_apresentacao(conn, rota, monkeypatch):
    c = painel.app.test_client()
    painel.app.config["CSRF_EXIGIDO"] = True
    assert c.post(rota).status_code == 302 and not agenda.tem_token(conn)
    painel.app.config["CSRF_EXIGIDO"] = False
    with c.session_transaction() as s:
        s["apresentacao"] = True
    assert c.post(rota).status_code == 403
    assert not agenda.tem_token(conn)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    c2 = painel.app.test_client()
    r = c2.post(rota)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert not agenda.tem_token(conn)


def test_a_agenda_mostra_o_que_a_conferencia_ainda_nao_viu_so_com_a_marca(conn):
    """O que muda de a_conferir para conferido troca o título, e a marca nunca fica num
    prazo conferido."""
    pid = _prazo(conn, "1", conferir=False)
    assert "[A CONFERIR]" in agenda.montar_ics(conn, AGORA)
    prazos_banco.conferir(conn, pid)
    assert "[A CONFERIR]" not in agenda.montar_ics(conn, AGORA)


def test_a_consulta_so_le_as_colunas_do_evento(conn):
    """O texto livre nem chega ao módulo: o banco confere quais colunas foram lidas."""
    import sqlite3
    _prazo(conn, "1", d=2)
    lidas = set()

    def vigia(acao, tabela, coluna, *_):
        if acao == sqlite3.SQLITE_READ:
            lidas.add((tabela, coluna))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(vigia)
    try:
        agenda.montar_ics(conn, AGORA)
    finally:
        conn.set_authorizer(None)
    assert {c for t, c in lidas if t == "prazo"} <= {
        "id", "numero", "estado", "codigo_ato", "fatal", "trabalho", "atualizado_em"}
    assert {c for t, c in lidas if t == "processo"} <= {"numero", "descartado_em", "advogado_atua"}
    assert {t for t, _ in lidas} <= {"prazo", "processo"}


def test_atualizado_em_absurdo_nao_derruba_a_agenda(conn):
    pid = _prazo(conn, "1")
    _prazo(conn, "2")
    for lixo in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00", "ontem", ""):
        conn.execute("UPDATE prazo SET atualizado_em = ? WHERE id = ?", (lixo, pid))
        ics = agenda.montar_ics(conn, AGORA)
        assert ics.count("BEGIN:VEVENT") == 2, lixo
        ev = {e["UID"]: e for e in _eventos(ics)}[f"prazo-{pid}-fatal@controladoria"]
        assert ev.get("SEQUENCE", "0") == "0" and "LAST-MODIFIED" not in ev, lixo
        _validar(ics)


def test_qualquer_falha_na_rota_e_503_sem_detalhe_e_sem_cache(conn, monkeypatch, caplog):
    token = agenda.gerar_token(conn)
    conn.commit()

    def quebra(*a, **k):
        raise RuntimeError(f"detalhe secreto {token}")

    monkeypatch.setattr(agenda, "montar_ics", quebra)
    with caplog.at_level(logging.DEBUG):
        r = painel.app.test_client().get(f"/agenda.ics?t={token}")
    assert r.status_code == 503 and r.headers["Cache-Control"] == "no-store"
    assert "secreto" not in r.get_data(as_text=True) and token not in r.get_data(as_text=True)
    assert "RuntimeError" in caplog.text and "secreto" not in caplog.text and token not in caplog.text


def test_se_a_tela_falha_o_endereco_antigo_continua_valendo(conn, monkeypatch):
    """Nada fica pela metade: o endereço novo só vale se a resposta que o mostra foi montada."""
    antigo = agenda.gerar_token(conn)
    conn.commit()

    def quebra(*a, **k):
        raise RuntimeError("tela")

    monkeypatch.setattr(painel, "_contexto_das_configuracoes_de_prazos", quebra)
    c = painel.app.test_client()
    r = c.post("/configuracoes/prazos/agenda/gerar")
    assert r.status_code == 302 and "/agenda.ics" not in r.get_data(as_text=True)
    assert c.get(f"/agenda.ics?t={antigo}").status_code == 200
    assert [e["tipo"] for e in eventos.listar(conn)].count("agenda_token_gerado") == 1


@pytest.mark.parametrize("estranho", ["MARIA ZZMARCAZZ", "0001234-56.2024.8.11.0041 ZZMARCAZZ",
                                       "", "123 ZZMARCAZZ"])
def test_numero_que_nao_e_so_digitos_fica_fora_do_titulo(conn, estranho):
    pid = _prazo(conn, "1")
    conn.execute("UPDATE prazo SET numero = ? WHERE id = ?", (estranho, pid))
    ics = _desdobrar(agenda.montar_ics(conn, AGORA))
    assert "ZZMARCAZZ" not in ics and "MARIA" not in ics
    assert "SUMMARY:Contestação · fatal 30/11" in ics and "· ·" not in ics
    _validar(ics)


def test_numero_so_de_digitos_sai_formatado_ou_como_esta(conn):
    pid = _prazo(conn, "1")
    conn.execute("UPDATE prazo SET numero = '12345' WHERE id = ?", (pid,))
    assert "SUMMARY:Contestação · 12345 · fatal 30/11" in _desdobrar(agenda.montar_ics(conn, AGORA))


def test_conferido_muito_antigo_sai_da_agenda_e_a_conferir_fica(conn):
    """Janela do tamanho do arquivo (não é prazo de lei): conferido com fatal de mais de um
    ano atrás sai; no limite fica; o que ainda espera conferência nunca sai por velho."""
    limite = AGORA.date() - timedelta(days=agenda.JANELA_DA_AGENDA_DIAS)
    velho = _prazo(conn, "1")
    no_limite = _prazo(conn, "2")
    esperando = _prazo(conn, "3", conferir=False)
    for pid, dia in ((velho, limite - timedelta(days=1)), (no_limite, limite),
                     (esperando, limite - timedelta(days=400))):
        conn.execute("UPDATE prazo SET fatal = ?, trabalho = ? WHERE id = ?",
                     (dia.isoformat(), dia.isoformat(), pid))
    ids = {e["UID"] for e in _eventos(agenda.montar_ics(conn, AGORA))}
    assert ids == {f"prazo-{no_limite}-fatal@controladoria", f"prazo-{esperando}-fatal@controladoria"}


def test_a_tela_diz_que_conferido_muito_antigo_sai(conn):
    pagina = painel.app.test_client().get("/configuracoes/prazos").get_data(as_text=True)
    assert f"mais de {agenda.JANELA_DA_AGENDA_DIAS} dias" in pagina


# --- versão local ----------------------------------------------------------------------

@pytest.fixture
def local(conn, monkeypatch):
    import painel_local
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setattr(painel_local, "_configurado", True)
    return painel.app.test_client()


def _texto(pagina):
    return " ".join(re.sub(r"<[^>]+>", " ", pagina).split())


def test_tela_local_oferece_baixar_e_diz_o_limite_do_celular(local):
    t = local.get("/configuracoes/prazos").get_data(as_text=True)
    assert "Baixar agenda" in t and "/agenda/baixar" in t
    texto = _texto(t)
    assert "o celular não alcança" in texto and "não se atualiza sozinho" in texto
    assert "só neste computador" in texto
    assert "Confira sempre no painel" in texto


def test_tela_local_nao_promete_assinatura_no_google_nem_no_celular(local):
    """O endereço assinado é 127.0.0.1: nenhum serviço de fora o alcança. A tela não oferece o
    que não funciona."""
    texto = _texto(local.get("/configuracoes/prazos").get_data(as_text=True))
    assert "Assine no Google" not in texto


def test_producao_nao_oferece_baixar(conn):
    t = painel.app.test_client().get("/configuracoes/prazos").get_data(as_text=True)
    assert "Baixar agenda" not in t and "o celular não alcança" not in t
    assert "/agenda/baixar" not in t
    assert "Assine no Google ou na Apple" in _texto(t)


def test_baixar_agenda_devolve_o_arquivo(conn, local):
    _prazo(conn, "1")
    r = local.get("/agenda/baixar")
    assert r.status_code == 200 and r.mimetype == "text/calendar"
    assert "attachment" in r.headers["Content-Disposition"]
    assert "filename=prazos.ics" in r.headers["Content-Disposition"]
    assert r.headers["Cache-Control"] == "no-store"
    assert "Contestação" in r.get_data(as_text=True)


def test_baixar_leva_as_mesmas_regras_de_conteudo_da_agenda(conn, local):
    """Número do processo e ato da lista, sem nome nem trecho: é o mesmo `montar_ics`."""
    _prazo(conn, "1")
    corpo = _desdobrar(local.get("/agenda/baixar").get_data(as_text=True))
    assert "MARIA" not in corpo and "BANCO ALFA" not in corpo and "TRECHO SIGILOSO" not in corpo
    assert "0001234-56.2024.8.11.0041" in corpo
    _validar(corpo)


def test_a_rota_existe_tambem_fora_do_modo_local_e_com_login(conn, monkeypatch):
    _prazo(conn, "1")
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    c = painel.app.test_client()
    with c.session_transaction() as s:
        s["logado"] = True
    r = c.get("/agenda/baixar")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"


def test_baixar_exige_login_e_some_na_apresentacao(conn, monkeypatch):
    _prazo(conn, "1")
    c = painel.app.test_client()
    with c.session_transaction() as s:
        s["apresentacao"] = True
    r = c.get("/agenda/baixar")
    assert r.status_code == 403 and r.mimetype != "text/calendar"
    assert "0001234" not in r.get_data(as_text=True)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    r = painel.app.test_client().get("/agenda/baixar")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert "0001234" not in r.get_data(as_text=True)


def test_baixar_nao_aceita_o_token_da_assinatura_no_lugar_do_login(conn, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    token = agenda.gerar_token(conn)
    conn.commit()
    r = painel.app.test_client().get(f"/agenda/baixar?t={token}")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_falha_ao_baixar_e_mensagem_simples_sem_cache_e_sem_detalhe(conn, monkeypatch, caplog):
    def quebra(*a, **k):
        raise RuntimeError("detalhe secreto 0001234")

    monkeypatch.setattr(agenda, "montar_ics", quebra)
    with caplog.at_level(logging.DEBUG):
        r = painel.app.test_client().get("/agenda/baixar")
    assert r.status_code == 503 and r.mimetype == "text/plain"
    assert r.headers["Cache-Control"] == "no-store"
    corpo = r.get_data(as_text=True)
    assert "secreto" not in corpo and "Traceback" not in corpo and "Não foi possível" in corpo
    assert "RuntimeError" in caplog.text and "secreto" not in caplog.text
