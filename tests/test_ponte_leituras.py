"""Leitura de prazo pela ponte: rotas com a chave, laço próprio, sem tocar no fluxo do squad."""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import painel
from nucleo import banco, ponte, prazos_banco
from ponte_mac import cliente, executor, leitor

NUMERO = "00012345620248110041"
TEXTO = "Fica a parte intimada para, no prazo de 15 (quinze) dias, apresentar réplica."
BOA = {"prazos": [{"ato": "Réplica", "dias": 15, "unidade": "uteis", "origem_dos_dias": "texto",
                   "rito": "civel", "fixado_pelo_juiz": False, "ressalvas": [],
                   "natureza": "processual", "inicio_no_diario": True,
                   "processo_suspenso": False,
                   "trecho": "no prazo de 15 (quinze) dias, apresentar réplica"}]}


@pytest.fixture
def web(tmp_path, monkeypatch):
    caminho = str(tmp_path / "b.db")
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    conn = banco.conectar(caminho)
    with conn:
        chave = ponte.gerar_chave(conn)
        conn.execute("INSERT INTO processo (numero, tribunal, advogado_atua) VALUES (?, 'TJMT', 1)",
                     (NUMERO,))
        conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                     "VALUES (1, ?, 'TJMT', '2026-11-05', ?)", (NUMERO, TEXTO))
        pid = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "1", "2026-11-05")
    conn.close()
    return SimpleNamespace(c=painel.app.test_client(), auth={"Authorization": f"Bearer {chave}"},
                           pid=pid, caminho=caminho)


def _estado(web):
    conn = banco.conectar(web.caminho)
    try:
        return prazos_banco.obter(conn, web.pid)
    finally:
        conn.close()


def test_rotas_exigem_a_chave(web):
    assert web.c.get("/ponte/leituras/proxima").status_code == 401
    assert web.c.post(f"/ponte/leituras/{web.pid}", json={"resposta": "x"}).status_code == 401


def test_proxima_entrega_o_pedido_e_depois_204(web):
    r = web.c.get("/ponte/leituras/proxima", headers=web.auth)
    assert r.status_code == 200 and r.get_json()["id"] == web.pid
    assert TEXTO in r.get_json()["pedido"]
    assert web.c.get("/ponte/leituras/proxima", headers=web.auth).status_code == 204


def test_resposta_grava_e_segunda_resposta_e_409(web):
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"resposta": json.dumps(BOA)})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "estado": "a_conferir"}
    assert _estado(web)["fatal"] == "2026-11-30"
    assert web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                      json={"resposta": json.dumps(BOA)}).status_code == 409


def test_falha_conta_tentativa(web):
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth, json={"falha": "erro"})
    assert r.status_code == 200 and _estado(web)["tentativas"] == 1


@pytest.mark.parametrize("corpo", [{}, {"resposta": 5}, {"falha": "qualquer"}, {"resposta": "x" * 70000}])
def test_corpo_ruim_e_400(web, corpo):
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth, json=corpo)
    assert r.status_code == 400 and _estado(web)["estado"] == "aguardando_lex"


def test_proxima_entrega_a_reserva_e_a_falha_com_reserva_velha_nao_gasta(web):
    pacote = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
    assert pacote["reserva"]
    velha = "2020-01-01T00:00:00+00:00"
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"falha": "erro", "reserva": velha})
    assert r.status_code == 200 and _estado(web)["tentativas"] == 0
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"falha": "erro", "reserva": pacote["reserva"]})
    assert r.status_code == 200 and _estado(web)["tentativas"] == 1


def test_resposta_aceita_a_reserva_e_ela_precisa_ser_texto(web):
    pacote = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
    assert web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                      json={"resposta": "{}", "reserva": 5}).status_code == 400
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"resposta": json.dumps(BOA), "reserva": pacote["reserva"]})
    assert r.status_code == 200 and r.get_json()["estado"] == "a_conferir"


def test_resposta_ou_falha_tardia_depois_de_outro_leitor_e_409_limpo(web):
    web.c.get("/ponte/leituras/proxima", headers=web.auth)
    assert web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                      json={"resposta": json.dumps(BOA)}).status_code == 200
    antes = _estado(web)
    for corpo in ({"resposta": json.dumps(BOA)}, {"falha": "erro", "reserva": "x"}):
        r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth, json=corpo)
        assert r.status_code == 409 and r.get_json()["ok"] is False
    assert _estado(web) == antes


def test_prazo_desconhecido_e_409(web):
    assert web.c.post("/ponte/leituras/999", headers=web.auth,
                      json={"resposta": "{}"}).status_code == 409


# --- a chamada curta -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _sem_procurar_o_claude(monkeypatch):
    """No Windows, `_executavel` procura o `claude` no PATH; aqui o processo é sempre falso."""
    monkeypatch.setattr(executor, "_executavel", lambda env: "claude")
    monkeypatch.setattr(executor, "localizar_claude", lambda env: "claude")
    monkeypatch.setattr(leitor, "_AVISADO", set())


def _rodar(stdout="", stderr="", codigo=0, levanta=None, guardar=None):
    def rodar(cmd, **kw):
        if guardar is not None:
            guardar.update(cmd=cmd, **kw)
        if levanta:
            raise levanta
        return subprocess.CompletedProcess(cmd, codigo, stdout, stderr)
    return rodar


ENVELOPE = json.dumps({"is_error": False, "result": '{"sem_prazo": true}'})


def test_comando_exato():
    assert leitor.comando_leitura("claude") == [
        "claude", "-p", "--output-format", "json", "--model", leitor.MODELO, "--tools", "",
        "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
        "--setting-sources", ""]


def test_comando_nao_libera_ferramenta_nenhuma():
    cmd = leitor.comando_leitura("claude")
    assert "--disable-slash-commands" in cmd and cmd[-2:] == ["--setting-sources", ""]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd and "--allowed-tools" not in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == leitor.MODELO
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_ler_manda_o_pedido_pela_entrada_padrao_na_pasta_vazia(tmp_path):
    visto = {}
    r = leitor.ler("o pedido", "tok", tmp_path, rodar=_rodar(ENVELOPE, guardar=visto))
    assert r == ("ok", '{"sem_prazo": true}')
    assert visto["input"] == "o pedido" and "o pedido" not in visto["cmd"]
    assert visto["cwd"] == str(tmp_path)
    assert visto["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert "stdin" not in visto


@pytest.mark.parametrize("rodar, esperado", [
    (_rodar("", "You have hit your limit", 1), "limite"),
    (_rodar(json.dumps({"is_error": True, "result": "Usage limit reached"}), "", 1), "limite"),
    (_rodar("", "authentication_error", 1), "acesso"),
    (_rodar("", "boom", 1), "erro"),
    (_rodar("não é json"), "erro"),
    (_rodar(json.dumps({"is_error": False, "result": 5})), "erro"),
    (_rodar(levanta=subprocess.TimeoutExpired("c", 1)), "tempo"),
    (_rodar(levanta=FileNotFoundError("claude")), "acesso"),   # CLI ausente: não gasta tentativa
    (_rodar(levanta=PermissionError("claude")), "erro"),
    # a mensagem de limite como texto curto, ancorada, sem JSON, e a CLI sem sinalizar erro
    (_rodar(json.dumps({"is_error": False, "result": "You have hit your limit. Resets 3pm"})),
     "limite"),
    (_rodar(json.dumps({"is_error": False, "result": "Claude usage limit reached|1760000000"})),
     "limite"),
    # mais de uma volta: não foi a chamada sem ferramentas que pedimos
    (_rodar(json.dumps({"is_error": False, "result": '{"sem_prazo": true}', "num_turns": 2})),
     "erro"),
])
def test_ler_classifica_a_falha(tmp_path, rodar, esperado):
    assert leitor.ler("p", "tok", tmp_path, rodar=rodar) == (esperado, "")


# --- uma leitura e o laço --------------------------------------------------------------

class PainelFalso:
    def __init__(self, pacote=None, erro=None, erro_falha=None):
        self.pacote, self.erro, self.erro_falha = pacote, erro, erro_falha
        self.respostas, self.falhas, self.reservas = [], [], []
        self.consultas = 0

    def proxima_leitura(self):
        self.consultas += 1
        return self.pacote

    def responder_leitura(self, prazo_id, resposta, reserva=None):
        if self.erro:
            raise self.erro
        self.respostas.append((prazo_id, resposta))
        self.reservas.append(reserva)

    def falhar_leitura(self, prazo_id, motivo, reserva=None):
        if self.erro_falha:
            raise self.erro_falha
        self.falhas.append((prazo_id, motivo))
        self.reservas.append(reserva)


def test_uma_leitura_sem_nada(tmp_path):
    assert leitor.uma_leitura(PainelFalso(), lambda: "tok", tmp_path) == "vazia"


def test_uma_leitura_devolve_a_resposta_crua(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"})
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE)) == "lida"
    assert p.respostas == [(3, '{"sem_prazo": true}')]


def test_uma_leitura_devolve_a_reserva_na_resposta_e_na_falha(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x", "reserva": "R1"})
    leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE))
    leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar("", "boom", 1))
    assert p.reservas == ["R1", "R1"]


def test_falha_tardia_recusada_com_409_e_ignorada(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"}, erro_falha=cliente.PainelErro(409, "fora"))
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar("", "boom", 1)) == "falha"
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path,
                              rodar=_rodar("", "hit your limit", 1)) == "limite"


def test_resposta_grande_demais_recusada_com_400_vira_falha_e_nao_repete(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"}, erro=cliente.PainelErro(400, "grande"))
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE)) == "falha"
    assert p.falhas == [(3, "erro")]


def test_outro_erro_do_painel_na_resposta_sobe_para_o_laco(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"}, erro=cliente.PainelErro(500, "pane"))
    with pytest.raises(cliente.PainelErro):
        leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE))


def test_sem_acesso_no_mac_nem_consulta_a_fila(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"})
    assert leitor.uma_leitura(p, lambda: None, tmp_path) == "sem_acesso"
    assert p.respostas == p.falhas == []


def test_sem_exigir_token_roda_com_o_login_do_programa(tmp_path):
    p, visto = PainelFalso({"id": 3, "pedido": "x"}), {}
    assert leitor.uma_leitura(p, lambda: "", tmp_path, rodar=_rodar(ENVELOPE, guardar=visto),
                              exigir_token=False) == "lida"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in visto["env"]


@pytest.mark.parametrize("stderr, resultado, motivo", [
    ("hit your limit", "limite", "limite"), ("authentication_error", "acesso", "acesso"),
    ("boom", "falha", "erro"),
])
def test_uma_leitura_avisa_a_falha(tmp_path, stderr, resultado, motivo):
    p = PainelFalso({"id": 3, "pedido": "x"})
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar("", stderr, 1)) == resultado
    assert p.falhas == [(3, motivo)]


def test_resposta_recusada_com_409_e_ignorada(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"}, erro=cliente.PainelErro(409, "fora"))
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE)) == "lida"


def _laco(painel_falso, rodar, voltas, tmp_path, **kw):
    dormidas, n = [], {"v": 0}

    def obter():  # `parar` também é consultado dentro da leitura: as voltas contam por aqui
        n["v"] += 1
        return painel_falso
    leitor.rodar_leituras(obter, lambda: "tok", tmp_path, parar=lambda: n["v"] >= voltas,
                          dormir=dormidas.append, rodar=rodar, **kw)
    return dormidas


def test_laco_espera_um_minuto_sem_nada_e_faz_uma_pausa_curta_depois_de_ler(tmp_path):
    assert leitor.PAUSA_ENTRE_LEITURAS == 20
    assert _laco(PainelFalso(), _rodar(ENVELOPE), 1, tmp_path) == [leitor.INTERVALO_S]
    assert _laco(PainelFalso({"id": 1, "pedido": "x"}), _rodar(ENVELOPE), 1, tmp_path) == [
        leitor.PAUSA_ENTRE_LEITURAS]


def test_a_pausa_entre_leituras_acaba_com_o_sinal_de_parar(tmp_path):
    parar = threading.Event()
    p = PainelFalso({"id": 1, "pedido": "x"})
    t = threading.Thread(target=leitor.rodar_leituras,
                         args=(lambda: p, lambda: "tok", tmp_path),
                         kwargs={"parar": parar.is_set, "dormir": parar.wait,
                                 "rodar": _rodar(ENVELOPE)}, daemon=True)
    t.start()
    fim = time.monotonic() + 5
    while not p.respostas and time.monotonic() < fim:
        time.sleep(0.02)
    assert len(p.respostas) == 1  # leu uma e dorme na pausa
    time.sleep(0.1)
    assert t.is_alive() and p.consultas == 1
    parar.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_laco_pausa_no_limite_e_no_acesso(tmp_path):
    p = PainelFalso({"id": 1, "pedido": "x"})
    assert _laco(p, _rodar("", "hit your limit", 1), 1, tmp_path) == [leitor.PAUSA_LIMITE_S]
    assert _laco(p, _rodar("", "authentication_error", 1), 1, tmp_path) == [leitor.PAUSA_ACESSO_S]


def test_laco_sobrevive_a_painel_fora_do_ar_e_sem_chave(tmp_path):
    class Fora(PainelFalso):
        def proxima_leitura(self):
            raise OSError("sem rede")
    assert _laco(Fora(), _rodar(), 2, tmp_path) == [leitor.INTERVALO_S] * 2
    dormidas = []
    n = iter([False, True])
    leitor.rodar_leituras(lambda: None, lambda: "tok", tmp_path, parar=lambda: next(n),
                          dormir=dormidas.append)
    assert dormidas == [leitor.INTERVALO_S]


def test_em_thread_para_com_o_sinal(tmp_path):
    parar = threading.Event()
    t = leitor.em_thread(lambda: PainelFalso(), lambda: "tok", tmp_path, parar=parar.is_set,
                         dormir=parar.wait)
    assert t.daemon and t.name == "leitura-de-prazos"
    parar.set()
    t.join(timeout=5)
    assert not t.is_alive()


# --- cliente ---------------------------------------------------------------------------

class _Resposta:
    def __init__(self, status, corpo=b""):
        self.status, self.corpo = status, corpo

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.corpo


def test_cliente_fala_as_rotas_novas():
    pedidos = []

    def abrir(pedido, timeout):
        pedidos.append((pedido.get_method(), pedido.full_url, pedido.data))
        return _Resposta(200, b'{"id": 1, "pedido": "x"}')
    p = cliente.Painel("https://painel.exemplo", "chave", abrir=abrir)
    assert p.proxima_leitura() == {"id": 1, "pedido": "x"}
    p.responder_leitura(1, "{}")
    p.falhar_leitura(1, "tempo")
    p.responder_leitura(2, "{}", "R")
    p.falhar_leitura(2, "erro", "R")
    assert pedidos[0][:2] == ("GET", "https://painel.exemplo/ponte/leituras/proxima")
    assert pedidos[1][:2] == ("POST", "https://painel.exemplo/ponte/leituras/1")
    assert json.loads(pedidos[1][2]) == {"resposta": "{}"}
    assert json.loads(pedidos[2][2]) == {"falha": "tempo"}
    assert json.loads(pedidos[3][2]) == {"resposta": "{}", "reserva": "R"}
    assert json.loads(pedidos[4][2]) == {"falha": "erro", "reserva": "R"}


def test_cliente_com_servidor_antigo_nao_quebra():
    import io
    import urllib.error

    def abrir(pedido, timeout):
        raise urllib.error.HTTPError(pedido.full_url, 404, "x", {}, io.BytesIO(b""))
    assert cliente.Painel("https://painel.exemplo", "chave", abrir=abrir).proxima_leitura() is None


def test_cliente_204_e_none():
    p = cliente.Painel("https://painel.exemplo", "c", abrir=lambda pedido, timeout: _Resposta(204))
    assert p.proxima_leitura() is None


# --- o serviço sobe o laço, sem mudar o do squad ---------------------------------------

def test_servico_do_mac_sobe_o_laco_de_leitura(monkeypatch, tmp_path):
    from ponte_mac import __main__ as principal
    subidos, rodados = [], []
    monkeypatch.setattr(principal, "esperar_painel",
                        lambda: ({"painel": "https://x", "casa": str(tmp_path)}, object()))
    monkeypatch.setattr(principal.laco, "rodar", lambda *a, **k: rodados.append(a))
    monkeypatch.setattr(principal.leitor, "em_thread", lambda *a, **k: subidos.append((a, k)))
    assert principal.main(["rodar"]) == 0
    assert len(subidos) == 1 and len(rodados) == 1
    assert subidos[0][1]["exigir_token"] is True
    # a pausa da leitura acaba com o mesmo sinal de parada, não dorme até o fim
    assert subidos[0][1]["dormir"].__self__.is_set == subidos[0][1]["parar"]


def test_modo_local_leitura_espera_o_mentorado_conectar_o_lex(monkeypatch):
    from ponte_mac import local as ponte_local
    subidos = []
    monkeypatch.setattr(ponte_local.leitor, "em_thread", lambda *a, **k: subidos.append((a, k)))
    monkeypatch.setattr(ponte_local, "rodar_local", lambda *a, **k: None)
    parar = threading.Event()
    parar.set()
    t = ponte_local.rodar_em_thread("http://127.0.0.1:5056", None, parar)
    t.join(timeout=5)
    assert subidos and subidos[0][1]["exigir_token"] is True
    assert subidos[0][1]["parar"] == parar.is_set
    assert subidos[0][1]["dormir"] == parar.wait
    assert Path(subidos[0][0][2]).name == "leitura-vazia"


def test_laco_do_squad_nao_conhece_a_leitura():
    fonte = Path(executor.__file__).with_name("laco.py").read_text(encoding="utf-8")
    assert "leitor" not in fonte and "leitura" not in fonte


# --- correções da rodada 1 -------------------------------------------------------------

def test_sem_o_claude_nao_reserva_nada_e_avisa_uma_vez(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(executor, "localizar_claude", lambda env: None)
    p = PainelFalso({"id": 3, "pedido": "x"})
    with caplog.at_level(logging.INFO, logger="ponte_mac.leitor"):
        for _ in range(3):
            assert leitor.uma_leitura(p, lambda: "tok", tmp_path) == "sem_claude"
    assert p.consultas == 0 and p.falhas == [] and p.respostas == []
    assert len([r for r in caplog.records if r.name == "ponte_mac.leitor"]) == 1


def test_laco_sem_o_claude_pausa_dez_minutos(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "localizar_claude", lambda env: None)
    assert _laco(PainelFalso({"id": 1, "pedido": "x"}), _rodar(ENVELOPE), 1,
                 tmp_path) == [leitor.PAUSA_ACESSO_S]


def test_claude_que_volta_e_avisado_de_novo_se_sumir(tmp_path, monkeypatch, caplog):
    achado = {"v": None}
    monkeypatch.setattr(executor, "localizar_claude", lambda env: achado["v"])
    p = PainelFalso()
    with caplog.at_level(logging.INFO, logger="ponte_mac.leitor"):
        leitor.uma_leitura(p, lambda: "tok", tmp_path)
        achado["v"] = "claude"
        assert leitor.uma_leitura(p, lambda: "tok", tmp_path) == "vazia"
        achado["v"] = None
        leitor.uma_leitura(p, lambda: "tok", tmp_path)
    assert len([r for r in caplog.records if r.name == "ponte_mac.leitor"]) == 2


def test_cli_sumida_no_meio_da_chamada_nao_gasta_tentativa(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"})
    r = leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(levanta=FileNotFoundError("c")))
    assert r == "acesso" and p.falhas == [(3, "acesso")]


def test_laco_painel_que_recusa_a_chave_espera_dez_minutos(tmp_path):
    class Recusa(PainelFalso):
        def proxima_leitura(self):
            raise cliente.PainelErro(401, "chave")
    assert _laco(Recusa(), _rodar(), 1, tmp_path) == [leitor.PAUSA_ACESSO_S]


# --- o processo de verdade: tempo e parada chegam à árvore -----------------------------

FILHO_COM_NETO = (
    "import subprocess, sys, time\n"
    "n = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(n.pid))\n"
    "time.sleep(120)\n")


@pytest.fixture
def sonda_curta(monkeypatch):
    monkeypatch.setattr(leitor, "SONDA_S", 0.1)


def _vivo(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _esperar_morrer(pid, ate=5):
    fim = time.monotonic() + ate
    while time.monotonic() < fim:
        if not _vivo(pid):
            return True
        time.sleep(0.05)
    return False


def test_executar_devolve_a_saida_e_manda_o_pedido_pela_entrada(tmp_path, sonda_curta):
    r = leitor._executar([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                         input="olá", cwd=str(tmp_path), env=dict(os.environ), timeout=10)
    assert r.returncode == 0 and r.stdout.strip() == "OLÁ"


def test_tempo_esgotado_encerra_a_arvore(tmp_path, sonda_curta, monkeypatch):
    chamadas = []
    original = executor._encerrar
    monkeypatch.setattr(executor, "_encerrar", lambda proc: (chamadas.append(proc), original(proc)))
    arquivo = tmp_path / "neto"
    inicio = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        leitor._executar([sys.executable, "-c", FILHO_COM_NETO, str(arquivo)], input="",
                         cwd=str(tmp_path), env=dict(os.environ), timeout=1,
                         **executor._opcoes_do_processo())
    assert time.monotonic() - inicio < 10
    assert chamadas and chamadas[0].poll() is not None
    if not executor.no_windows():  # o neto também some (a árvore, não só o filho)
        assert _esperar_morrer(int(arquivo.read_text()))


def test_parar_interrompe_em_poucos_segundos_e_nao_avisa_o_painel(tmp_path, sonda_curta):
    parar = threading.Event()
    threading.Timer(0.4, parar.set).start()
    inicio = time.monotonic()
    arquivo = tmp_path / "neto"
    resultado = leitor.ler("x", "tok", tmp_path, parar=parar.is_set,
                           rodar=lambda cmd, **kw: leitor._executar(
                               [sys.executable, "-c", FILHO_COM_NETO, str(arquivo)], **kw))
    assert resultado == ("parado", "")
    assert time.monotonic() - inicio < 3
    assert not leitor._EM_ANDAMENTO
    if not executor.no_windows():
        assert _esperar_morrer(int(arquivo.read_text()))


def test_uma_leitura_parada_nao_avisa_o_painel(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"})
    r = leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(levanta=leitor.Parado()))
    assert r == "parado" and p.respostas == p.falhas == []


def test_encerrar_em_andamento_derruba_o_filho(tmp_path, monkeypatch):
    monkeypatch.setattr(leitor, "SONDA_S", 30)  # só a morte do filho acorda o fio
    saida, parando = {}, threading.Event()

    def fio():
        try:
            leitor._executar([sys.executable, "-c", "import time; time.sleep(120)"], input="",
                             cwd=str(tmp_path), env=dict(os.environ), timeout=60,
                             parar=parando.is_set, **executor._opcoes_do_processo())
        except Exception as exc:
            saida["erro"] = exc
    t = threading.Thread(target=fio)
    t.start()
    fim = time.monotonic() + 5
    while not leitor._EM_ANDAMENTO and time.monotonic() < fim:
        time.sleep(0.02)
    proc = leitor._EM_ANDAMENTO[0]
    parando.set()
    assert leitor.encerrar_em_andamento() == 1
    t.join(timeout=10)
    assert not t.is_alive() and proc.poll() is not None
    assert isinstance(saida.get("erro"), leitor.Parado)


# --- modo local: a leitura não depende da casa da Banca --------------------------------

def test_modo_local_leitura_pega_a_chave_sem_casa_da_banca(monkeypatch, tmp_path):
    from ponte_mac import local as ponte_local
    subidos = []
    monkeypatch.setattr(ponte_local.leitor, "em_thread", lambda *a, **k: subidos.append((a, k)))
    monkeypatch.setattr(ponte_local, "rodar_local", lambda *a, **k: None)
    monkeypatch.setattr(ponte_local, "casa_da_banca", lambda: "")
    chamadas = []
    monkeypatch.setattr(ponte_local, "_chave_do_banco", lambda: chamadas.append(1) or "chave-nova")
    parar = threading.Event()
    parar.set()
    ponte_local.rodar_em_thread("http://127.0.0.1:5056", None, parar).join(timeout=5)
    obter_painel = subidos[0][0][0]
    painel_a = obter_painel()
    assert painel_a.url == "http://127.0.0.1:5056" and chamadas == [1]
    assert obter_painel() is painel_a and chamadas == [1]   # sem refazer a chave a cada volta
    painel_a.chave_recusada = True
    assert obter_painel() is not painel_a and chamadas == [1, 1]


def test_modo_local_sem_chave_avisa_uma_vez(monkeypatch, caplog):
    from ponte_mac import local as ponte_local
    subidos = []
    monkeypatch.setattr(ponte_local.leitor, "em_thread", lambda *a, **k: subidos.append((a, k)))
    monkeypatch.setattr(ponte_local, "rodar_local", lambda *a, **k: None)
    monkeypatch.setattr(ponte_local, "_chave_do_banco", lambda: None)
    monkeypatch.setattr(ponte_local, "_AVISOU_SEM_CHAVE", False, raising=False)
    parar = threading.Event()
    parar.set()
    ponte_local.rodar_em_thread("http://127.0.0.1:5056", None, parar).join(timeout=5)
    with caplog.at_level(logging.WARNING, logger="ponte_mac.local"):
        assert subidos[0][0][0]() is None and subidos[0][0][0]() is None
    assert len([r for r in caplog.records if r.name == "ponte_mac.local"]) == 1


def test_parar_do_modo_local_encerra_tambem_a_leitura(monkeypatch):
    from ponte_mac import local as ponte_local
    visto = []
    monkeypatch.setattr(ponte_local.leitor, "encerrar_em_andamento", lambda: visto.append(1) or 0)
    monkeypatch.setattr(ponte_local.executor, "encerrar_em_andamento", lambda: 0)
    ponte_local.parar(threading.Event(), None)
    assert visto == [1]


# --- o texto da publicação não vai para o registro -------------------------------------

MARCA = "MARCA-SECRETA-7731"


def test_texto_da_publicacao_nao_aparece_em_registro_nenhum(web, tmp_path, caplog):
    conn = banco.conectar(web.caminho)
    with conn:
        conn.execute("UPDATE publicacao SET texto = ? WHERE id = 1", (TEXTO + " " + MARCA,))
    conn.close()
    with caplog.at_level(logging.DEBUG):
        pacote = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
        assert MARCA in pacote["pedido"]

        class Direto(PainelFalso):
            def responder_leitura(self, prazo_id, resposta, reserva=None):
                web.c.post(f"/ponte/leituras/{prazo_id}", headers=web.auth,
                           json={"resposta": resposta, "reserva": reserva})

        eco = json.dumps({"is_error": False, "result": "texto livre " + MARCA})
        leitor.uma_leitura(Direto(pacote), lambda: "tok", tmp_path, rodar=_rodar(eco))
        # o processo falha dizendo o pedido: a mensagem não pode ir ao registro
        leitor.uma_leitura(PainelFalso(pacote), lambda: "tok", tmp_path,
                           rodar=_rodar(levanta=OSError(MARCA + " " + pacote["pedido"])))
        leitor.rodar_leituras(
            lambda: (_ for _ in ()).throw(RuntimeError(MARCA + " " + pacote["pedido"])), lambda: "tok",
            tmp_path, parar=iter([False, True]).__next__, dormir=lambda s: None)
        web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"resposta": "x" * 70000 + MARCA})
    for r in caplog.records:
        assert MARCA not in r.getMessage()
        assert MARCA not in (r.exc_text or "") and MARCA not in str(r.exc_info)


# --- rodada 2: «limite» só pela CLI ou pela mensagem inteira e ancorada ----------------

@pytest.mark.parametrize("texto", [
    "O prazo cita o usage limit do plano e nada mais.",
    "Aqui está o JSON: " + json.dumps({"sem_prazo": True}) + ", rate limit",
    "rate limit",
    "Aqui vai a resposta. " + "x" * 300 + " You have hit your limit",
])
def test_limite_em_prosa_nao_e_limite_do_plano(tmp_path, texto):
    r = leitor.ler("p", "tok", tmp_path,
                   rodar=_rodar(json.dumps({"is_error": False, "result": texto})))
    assert r == ("ok", texto)   # segue o caminho normal; o servidor confere e recusa o formato


def test_limite_sinalizado_pela_cli_continua_limite(tmp_path):
    corpo = json.dumps({"is_error": True, "result": "5-hour limit reached"})
    assert leitor.ler("p", "tok", tmp_path, rodar=_rodar(corpo, "", 1)) == ("limite", "")
    corpo = json.dumps({"is_error": False, "result": "Aqui: rate limit reached"})
    assert leitor.ler("p", "tok", tmp_path, rodar=_rodar(corpo, "", 1))[0] == "erro"


# --- rodada 2: o filho sai e um neto segura a saída ------------------------------------

FILHO_QUE_DEIXA_NETO = (
    "import subprocess, sys\n"
    "n = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(n.pid))\n"
    "print('{\"is_error\": false, \"result\": \"{}\"}')\n")


@pytest.mark.skipif(sys.platform == "win32", reason="no Windows o communicate do Python levanta sem "
                    "a saída depois da carência; conferência à mão na lista da versão")
def test_neto_segurando_a_saida_nao_queima_a_leitura(tmp_path, monkeypatch):
    monkeypatch.setattr(leitor, "SONDA_S", 0.1)
    monkeypatch.setattr(leitor, "GRACA_S", 0.5)
    arquivo = tmp_path / "neto"
    inicio = time.monotonic()
    r = leitor._executar([sys.executable, "-c", FILHO_QUE_DEIXA_NETO, str(arquivo)], input="",
                         cwd=str(tmp_path), env=dict(os.environ), timeout=60,
                         **executor._opcoes_do_processo())
    assert time.monotonic() - inicio < 10
    assert r.returncode == 0 and json.loads(r.stdout)["is_error"] is False
    if not executor.no_windows():
        assert _esperar_morrer(int(arquivo.read_text()))


# --- rodada 2: parar logo antes do Popen -----------------------------------------------

def test_parar_ja_pedido_impede_o_inicio_do_processo(tmp_path, monkeypatch):
    iniciados = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: iniciados.append(a))
    with pytest.raises(leitor.Parado):
        leitor._executar(["x"], input="", cwd=str(tmp_path), env={}, timeout=5,
                         parar=lambda: True)
    assert iniciados == []


def test_parar_depois_do_inicio_e_visto_pelo_encerrar(tmp_path, monkeypatch):
    """O início e o `encerrar_em_andamento` se excluem pela trava: ou o processo já está na
    lista quando o encerramento passa, ou o início vê a parada."""
    monkeypatch.setattr(leitor, "SONDA_S", 0.05)
    parando = threading.Event()
    estado = {}

    def fio():
        try:
            leitor._executar([sys.executable, "-c", "import time; time.sleep(120)"], input="",
                             cwd=str(tmp_path), env=dict(os.environ), timeout=60,
                             parar=parando.is_set, **executor._opcoes_do_processo())
        except leitor.Parado:
            estado["parado"] = True
    with leitor._TRAVA:   # o encerramento «chegou primeiro»: segura a trava e marca a parada
        parando.set()
        t = threading.Thread(target=fio)
        t.start()
        time.sleep(0.2)
        assert not leitor._EM_ANDAMENTO   # o início esperou a trava
    t.join(timeout=10)
    assert estado == {"parado": True} and not leitor._EM_ANDAMENTO


def test_servico_do_mac_encerra_a_leitura_ao_sair_do_laco(monkeypatch, tmp_path):
    from ponte_mac import __main__ as principal
    subidos, encerrou = [], []
    monkeypatch.setattr(principal, "esperar_painel",
                        lambda: ({"painel": "https://x", "casa": str(tmp_path)}, object()))
    monkeypatch.setattr(principal.leitor, "em_thread", lambda *a, **k: subidos.append((a, k)))
    monkeypatch.setattr(principal.leitor, "encerrar_em_andamento",
                        lambda: encerrou.append(subidos[0][1]["parar"]()))

    def sai(*a, **k):
        raise SystemExit(0)   # o que o tratador de SIGTERM do laço faz
    monkeypatch.setattr(principal.laco, "rodar", sai)
    with pytest.raises(SystemExit):
        principal.main(["rodar"])
    assert encerrou == [True]   # a parada já estava pedida quando o filho foi encerrado
    assert "encerrar_no_sigterm" not in subidos[0][1]


SERVICO = r"""
import sys, time
from pathlib import Path
from ponte_mac import __main__ as principal, executor, laco, leitor

pasta = Path(sys.argv[1])
FILHO = ("import os, sys, time\n"
         "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
         "sys.stdin.read()\n"
         "time.sleep(120)\n")
leitor.comando_leitura = lambda exe='claude': [sys.executable, '-c', FILHO, str(pasta / 'filho')]
executor.localizar_claude = lambda env: 'claude'


class Painel:
    dado = False
    chave_recusada = False
    def proxima_leitura(self):
        if Painel.dado:
            return None
        Painel.dado = True
        return {'id': 1, 'pedido': 'x', 'reserva': 'r'}
    def responder_leitura(self, *a): pass
    def falhar_leitura(self, *a): pass


def rodar_falso(*a, **k):      # o laço de verdade: só o trabalho dele é trocado
    anteriores = laco._instalar_tratadores()
    try:
        (pasta / 'pronto').write_text('1')
        while True:
            time.sleep(0.05)
    finally:
        laco._restaurar_tratadores(anteriores)


principal.esperar_painel = lambda: ({'painel': 'https://x', 'casa': str(pasta)}, Painel())
principal._painel = lambda cfg: Painel()
principal.chaves.ler = lambda *_: 'tok'
principal.laco.rodar = rodar_falso
leitor.PASTA = None
principal.PASTA_LEITURA = pasta / 'leitura'
sys.exit(principal.main(['rodar']))
"""


@pytest.mark.skipif(sys.platform.startswith("win"), reason="SIGTERM do launchd é do Mac")
def test_sigterm_no_servico_de_verdade_derruba_o_filho_da_leitura(tmp_path):
    import signal
    repo = Path(__file__).resolve().parent.parent
    servico = subprocess.Popen([sys.executable, "-c", SERVICO, str(tmp_path)], cwd=str(repo),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    filho = None
    try:
        arquivo = tmp_path / "filho"
        fim = time.monotonic() + 20
        while time.monotonic() < fim and not (arquivo.exists() and arquivo.read_text()):
            time.sleep(0.05)
        assert arquivo.exists() and arquivo.read_text(), servico.poll()
        filho = int(arquivo.read_text())
        assert _vivo(filho)
        servico.send_signal(signal.SIGTERM)
        assert servico.wait(timeout=30) == 0
        assert _esperar_morrer(filho, ate=10)   # o filho da leitura não ficou órfão
    finally:
        servico.kill()
        servico.wait()
        if filho and _vivo(filho):
            os.kill(filho, signal.SIGKILL)


# --- rodada 2: modo local para a leitura e espera o fio --------------------------------

def test_parar_do_modo_local_espera_o_fio_da_leitura(monkeypatch):
    from ponte_mac import local as ponte_local
    visto = []

    class Fio:
        def join(self, timeout=None):
            visto.append(timeout)
    ponte = threading.Thread(target=lambda: None)
    ponte.leitura = Fio()
    ponte.start()
    monkeypatch.setattr(ponte_local.leitor, "encerrar_em_andamento", lambda: 0)
    monkeypatch.setattr(ponte_local.executor, "encerrar_em_andamento", lambda: 0)
    ponte_local.parar(threading.Event(), ponte, espera_s=5)
    assert visto and 0 < visto[0] <= 10


def test_rodar_em_thread_guarda_o_fio_da_leitura(monkeypatch):
    from ponte_mac import local as ponte_local
    fio = object()
    monkeypatch.setattr(ponte_local.leitor, "em_thread", lambda *a, **k: fio)
    monkeypatch.setattr(ponte_local, "rodar_local", lambda *a, **k: None)
    parar = threading.Event()
    parar.set()
    t = ponte_local.rodar_em_thread("http://127.0.0.1:5056", None, parar)
    t.join(timeout=5)
    assert t.leitura is fio


# --- rodada 2: o corpo da resposta, cada conferência com o seu teste -------------------

def test_corpo_sem_tamanho_declarado_e_conferido_pelo_que_foi_lido(web):
    """Corpo em pedaços (sem Content-Length): só a conferência do que foi lido o pega."""
    import io
    grande = json.dumps({"resposta": "x" * 70000}).encode()
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth, input_stream=io.BytesIO(grande),
                   content_type="application/json",
                   environ_overrides={"wsgi.input_terminated": True, "CONTENT_LENGTH": ""})
    assert r.status_code == 400 and _estado(web)["estado"] == "aguardando_lex"


def test_tamanho_declarado_grande_recusa_sem_ler_o_corpo(web, monkeypatch):
    from flask import wrappers

    def nao_ler(self, *a, **k):
        raise AssertionError("o corpo não devia ser lido")
    monkeypatch.setattr(wrappers.Request, "get_data", nao_ler)
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   data=json.dumps({"resposta": "x" * 70000}), content_type="application/json")
    assert r.status_code == 400


# --- rodada 2: a trava da chave --------------------------------------------------------

def test_a_chave_do_banco_nao_e_preparada_por_dois_ao_mesmo_tempo(monkeypatch):
    from ponte_mac import local as ponte_local
    dentro, maximo, trava = [0], [0], threading.Lock()

    def preparar():
        with trava:
            dentro[0] += 1
            maximo[0] = max(maximo[0], dentro[0])
        time.sleep(0.1)
        with trava:
            dentro[0] -= 1
        return "k"
    monkeypatch.setattr(ponte_local, "_chave_do_banco_sem_trava", preparar)
    fios = [threading.Thread(target=ponte_local._chave_do_banco) for _ in range(4)]
    for f in fios:
        f.start()
    for f in fios:
        f.join(timeout=10)
    assert maximo[0] == 1


# --- rodada 3: o limite não pode segurar a fila para sempre ----------------------------

def _segunda_publicacao(web):
    conn = banco.conectar(web.caminho)
    with conn:
        conn.execute("INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, texto) "
                     "VALUES (2, ?, 'TJMT', '2026-11-06', ?)", (NUMERO, TEXTO))
        outro = prazos_banco.abrir(conn, NUMERO, None, "publicacao", "2", "2026-11-06")
    conn.close()
    return outro


def _eventos_de_limite(web, prazo=None):
    conn = banco.conectar(web.caminho)
    try:
        return [e for e in __import__("nucleo.eventos", fromlist=["x"]).listar(conn)
                if e["tipo"] == "prazo_leitura_limite"
                and (prazo is None or e["dados"].get("prazo") == prazo)]
    finally:
        conn.close()


def _rodada_de_limite(web):
    pacote = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
    r = web.c.post(f"/ponte/leituras/{pacote['id']}", headers=web.auth,
                   json={"falha": "limite", "reserva": pacote["reserva"]})
    assert r.status_code == 200
    return pacote["id"]


def test_limite_repetido_vai_a_definir_em_doze_rodadas_e_libera_a_fila(web):
    outro = _segunda_publicacao(web)
    for rodada in range(1, 13):
        assert _rodada_de_limite(web) == web.pid        # a mais antiga volta sempre
        assert _estado(web)["tentativas"] == rodada // 4
        assert len(_eventos_de_limite(web, web.pid)) == rodada
        if rodada < 12:
            assert _estado(web)["estado"] == "aguardando_lex"
    estado = _estado(web)
    assert estado["estado"] == "a_definir" and estado["tentativas"] == 3
    # o evento não leva texto da publicação
    assert all(set(e["dados"]) <= {"prazo"} for e in _eventos_de_limite(web))
    seguinte = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
    assert seguinte["id"] == outro


def test_abaixo_da_quarta_o_limite_nao_gasta_tentativa(web):
    for _ in range(3):
        _rodada_de_limite(web)
    assert _estado(web)["tentativas"] == 0 and len(_eventos_de_limite(web, web.pid)) == 3


def test_limite_de_outra_linha_nao_conta_para_esta(web):
    outro = _segunda_publicacao(web)
    for _ in range(3):
        _rodada_de_limite(web)                           # 3 na primeira
        assert web.c.post(f"/ponte/leituras/{outro}", headers=web.auth,
                          json={"falha": "limite"}).status_code == 200   # 3 na segunda
    assert _estado(web)["tentativas"] == 0
    conn = banco.conectar(web.caminho)
    assert prazos_banco.obter(conn, outro)["tentativas"] == 0
    conn.close()


def test_limite_atrasado_de_reserva_vencida_nao_conta(web):
    web.c.get("/ponte/leituras/proxima", headers=web.auth)
    for _ in range(8):
        r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                       json={"falha": "limite", "reserva": "2020-01-01T00:00:00+00:00"})
        assert r.status_code == 200
    assert _eventos_de_limite(web) == [] and _estado(web)["tentativas"] == 0


def test_ler_de_novo_zera_a_conta_do_limite(web):
    for _ in range(6):
        _rodada_de_limite(web)
    conn = banco.conectar(web.caminho)
    with conn:
        prazos_banco._gravar(conn, web.pid, {"estado": "a_definir"})   # o advogado vê «a definir»
        prazos_banco.ler_de_novo(conn, web.pid)
    conn.close()
    for _ in range(2):
        _rodada_de_limite(web)
    assert _estado(web)["tentativas"] == 0     # 2 depois do «ler de novo», não 8 no total


def test_acesso_recusado_nao_registra_evento_de_limite(web):
    pacote = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
    for _ in range(5):
        web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"falha": "acesso", "reserva": pacote["reserva"]})
    assert _eventos_de_limite(web) == [] and _estado(web)["tentativas"] == 0


def test_o_limite_vai_para_o_registro_so_com_a_linha_e_sem_o_texto(tmp_path, caplog):
    p = PainelFalso({"id": 7, "pedido": "x"})
    corpo = json.dumps({"is_error": True, "result": "usage limit reached MARCA-DO-TEXTO"})
    with caplog.at_level(logging.WARNING, logger="ponte_mac.leitor"):
        assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(corpo, "", 1)) == "limite"
    avisos = [r.getMessage() for r in caplog.records if r.name == "ponte_mac.leitor"]
    assert len(avisos) == 1 and "7" in avisos[0] and "MARCA-DO-TEXTO" not in avisos[0]


@pytest.mark.parametrize("texto", [
    "You have hit your limit. " + "e depois muito texto " * 15,    # longa: não é a mensagem
    'You have hit your limit {"sem_prazo": true}',                # com JSON de objeto
    "You have hit your limit [1, 2]",                             # com JSON de lista
])
def test_a_frase_de_limite_so_vale_curta_e_sem_json(tmp_path, texto):
    assert len(texto) >= 30
    r = leitor.ler("p", "tok", tmp_path,
                   rodar=_rodar(json.dumps({"is_error": False, "result": texto})))
    assert r == ("ok", texto)


def test_a_frase_de_limite_curta_sem_json_e_limite(tmp_path):
    for texto in ("You have hit your limit", "You've hit your limit. Resets 3pm"):
        r = leitor.ler("p", "tok", tmp_path,
                       rodar=_rodar(json.dumps({"is_error": False, "result": texto})))
        assert r == ("limite", "")


# --- onda final: quem fala com a fila de leitura não é o Mac do squad ------------------

def _ponte_agora(web, funcao):
    conn = banco.conectar(web.caminho)
    try:
        return funcao(conn, painel.painel_dados.agora_cuiaba())
    finally:
        conn.close()


def test_f1_o_leitor_sozinho_nao_vira_mac_conectado(web):
    assert not _ponte_agora(web, ponte.leitor_conectado)
    conn = banco.conectar(web.caminho)
    with conn:
        agora = "2026-10-01T10:00:00-04:00"
        d = conn.execute(
            "INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em, "
            "lex_estado) VALUES (?, 'lex', '2026-10-01', ?, ?, 'na_fila')",
            (NUMERO, agora, agora)).lastrowid
    linha = conn.execute("SELECT * FROM demanda WHERE id = ?", (d,)).fetchone()
    rotulo = painel.painel_dados._rotulo_do_lex_bruto(conn, linha, [], None)
    conn.close()
    assert rotulo == "aguardando o Mac"
    pacote = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
    assert web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                      json={"falha": "acesso", "reserva": pacote["reserva"]}).status_code == 200
    assert _ponte_agora(web, ponte.leitor_conectado)
    assert not _ponte_agora(web, ponte.mac_conectado)
    conn = banco.conectar(web.caminho)
    try:
        assert ponte.ultimo_contato(conn) is None
        linha = conn.execute("SELECT * FROM demanda WHERE id = ?", (d,)).fetchone()
        assert painel.painel_dados._rotulo_do_lex_bruto(conn, linha, [], None) == "aguardando o Mac"
    finally:
        conn.close()
    assert "Conectado agora" not in web.c.get("/configuracoes/lex").get_data(as_text=True)


def test_f1_o_contato_do_squad_nao_vira_leitor_conectado(web):
    assert web.c.post("/ponte/contato", json={"claude_ok": 1}, headers=web.auth).status_code == 200
    assert _ponte_agora(web, ponte.mac_conectado)
    assert not _ponte_agora(web, ponte.leitor_conectado)


def test_f1_chave_errada_nao_conta_como_leitor(web):
    assert web.c.get("/ponte/leituras/proxima",
                     headers={"Authorization": "Bearer errada"}).status_code == 401
    assert not _ponte_agora(web, ponte.leitor_conectado)


def test_f4_sem_conectar_nao_gasta_nem_consulta_e_depois_de_conectar_le(tmp_path):
    p = PainelFalso({"id": 3, "pedido": "x"})

    def proibido(*a, **k):
        raise AssertionError("não pode chamar o programa do Lex")
    acesso = {"v": ""}
    assert leitor.uma_leitura(p, lambda: acesso["v"], tmp_path, rodar=proibido) == "sem_acesso"
    assert p.consultas == 0 and p.respostas == p.falhas == []
    acesso["v"] = "guardado"  # «Conectar o Lex» guardou o acesso
    assert leitor.uma_leitura(p, lambda: acesso["v"], tmp_path, rodar=_rodar(ENVELOPE)) == "lida"
    assert p.consultas == 1 and len(p.respostas) == 1


def test_f4_o_fio_local_le_o_mesmo_acesso_que_o_botao_guarda(monkeypatch):
    from ponte_mac import chaves, local as ponte_local
    lidos = []
    monkeypatch.setattr(chaves, "ler", lambda servico, **k: lidos.append(servico) or None)
    assert ponte_local._token() == "" and lidos == [chaves.ACESSO_CLAUDE]


# --- onda final: banco ocupado ---------------------------------------------------------

@pytest.fixture
def banco_ocupado(web, monkeypatch):
    """Outro segura a trava de escrita, e as conexões da rota desistem logo."""
    original = banco.conectar

    def rapido(*a, **k):
        c = original(*a, **k)
        c.execute("PRAGMA busy_timeout = 50")
        return c
    trava = original(web.caminho)
    monkeypatch.setattr(banco, "conectar", rapido)
    trava.execute("BEGIN IMMEDIATE")
    yield web
    trava.rollback()
    trava.close()


def test_f8_as_rotas_do_leitor_respondem_503_com_o_banco_ocupado(banco_ocupado):
    web = banco_ocupado
    assert web.c.get("/ponte/leituras/proxima", headers=web.auth).status_code == 503
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"resposta": json.dumps(BOA)})
    assert r.status_code == 503 and r.get_json()["ok"] is False


def test_f8_503_na_consulta_o_leitor_espera_e_tenta_de_novo(tmp_path):
    class Ocupado(PainelFalso):
        def proxima_leitura(self):
            raise cliente.PainelErro(503, "banco ocupado")
    assert _laco(Ocupado(), _rodar(), 2, tmp_path) == [leitor.INTERVALO_S] * 2


def test_f8_503_na_resposta_nao_perde_a_leitura(tmp_path):
    class Uma503(PainelFalso):
        def __init__(self, *a):
            super().__init__(*a)
            self.vezes = 0

        def responder_leitura(self, prazo_id, resposta, reserva=None):
            self.vezes += 1
            if self.vezes == 1:
                raise cliente.PainelErro(503, "banco ocupado")
            super().responder_leitura(prazo_id, resposta, reserva)
    p, dormidas = Uma503({"id": 3, "pedido": "x"}), []
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE),
                              dormir=dormidas.append) == "lida"
    assert len(p.respostas) == 1 and dormidas == [leitor.ESPERA_BANCO_OCUPADO_S]


def test_f8_503_que_nao_passa_sobe_sem_marcar_falha(tmp_path):
    p, dormidas = PainelFalso({"id": 3, "pedido": "x"},
                              erro=cliente.PainelErro(503, "banco ocupado")), []
    with pytest.raises(cliente.PainelErro):
        leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE),
                           dormir=dormidas.append)
    assert p.falhas == [] and len(dormidas) == leitor.TENTATIVAS_BANCO_OCUPADO - 1


def test_f8_503_na_resposta_para_de_tentar_com_o_sinal_de_parar(tmp_path):
    p, dormidas = PainelFalso({"id": 3, "pedido": "x"},
                              erro=cliente.PainelErro(503, "banco ocupado")), []
    assert leitor.uma_leitura(p, lambda: "tok", tmp_path, rodar=_rodar(ENVELOPE),
                              dormir=dormidas.append, parar=lambda: True) == "parado"
    assert dormidas == []


def test_f7_rota_recusa_quieta_a_resposta_de_reserva_vencida(web):
    pacote = web.c.get("/ponte/leituras/proxima", headers=web.auth).get_json()
    conn = banco.conectar(web.caminho)
    with conn:
        conn.execute("UPDATE prazo SET leitura_reservada_ate = '2099-01-01T00:00:00Z' WHERE id = ?",
                     (web.pid,))  # a reserva venceu e passou a outro leitor
    conn.close()
    r = web.c.post(f"/ponte/leituras/{web.pid}", headers=web.auth,
                   json={"resposta": json.dumps(BOA), "reserva": pacote["reserva"]})
    assert r.status_code == 200 and r.get_json()["estado"] == "aguardando_lex"
    assert _estado(web)["estado"] == "aguardando_lex"
    assert _estado(web)["leitura_reservada_ate"] == "2099-01-01T00:00:00Z"
