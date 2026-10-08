"""Executor e laço da ponte fora do Mac: ambiente do Windows, localização do `claude`,
processo sem janela, encerramento da árvore e parada pedida pelo lançador."""

import json
import subprocess
import sys
import threading

import pytest

from ponte_mac import executor, laco

PACOTE = {
    "demanda": 7, "n": 1, "episodio": 501, "modo": "novo", "numero": "10000011120268110001",
    "numero_formatado": "1000001-11.2026.8.11.0001", "tribunal": "TJMT", "orientacao": "",
    "publicacoes": [], "intimacoes": [],
}

PERFIL = "C:\\Users\\Fulano"


@pytest.fixture
def windows(monkeypatch):
    """Simula o Windows: plataforma, ambiente herdado (com sujeira do app desktop) e o
    PATH do registro."""
    for nome in list(__import__("os").environ):
        monkeypatch.delenv(nome)
    herdado = {
        "USERPROFILE": PERFIL, "APPDATA": PERFIL + "\\AppData\\Roaming",
        "LOCALAPPDATA": PERFIL + "\\AppData\\Local", "TEMP": PERFIL + "\\AppData\\Local\\Temp",
        "SystemRoot": "C:\\Windows", "ComSpec": "C:\\Windows\\system32\\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD", "ProgramFiles": "C:\\Program Files",
        "USERNAME": "Fulano", "HOMEDRIVE": "C:", "HOMEPATH": "\\Users\\Fulano",
        "PATH": "C:\\app-desktop\\bin;C:\\Windows\\system32",
        # o que o app desktop deixaria no ambiente do lançador
        "HTTPS_PROXY": "http://127.0.0.1:9999", "HTTP_PROXY": "http://127.0.0.1:9999",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999", "CLAUDE_CODE_OAUTH_TOKEN": "do-app",
        "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
        "CLAUDE_CODE_GIT_BASH_PATH": "C:\\Program Files\\Git\\bin\\bash.exe",
    }
    for nome, valor in herdado.items():
        monkeypatch.setenv(nome, valor)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(executor, "_path_do_registro",
                        lambda: ["C:\\Windows\\system32", "C:\\Program Files\\Git\\cmd",
                                 "C:\\Windows\\System32"])
    monkeypatch.setattr(executor, "_parada", threading.Event())
    return herdado


def test_no_windows_segue_a_plataforma(windows):
    assert executor.no_windows()


def test_ambiente_windows_tem_o_que_o_sistema_precisa_e_nada_do_app(windows):
    amb = executor.ambiente("tok")
    assert amb["USERPROFILE"] == PERFIL and amb["HOME"] == PERFIL
    assert amb["APPDATA"] == PERFIL + "\\AppData\\Roaming"
    assert amb["LOCALAPPDATA"] == PERFIL + "\\AppData\\Local"
    assert amb["TEMP"] == amb["TMP"] == PERFIL + "\\AppData\\Local\\Temp"
    assert amb["SystemRoot"] == "C:\\Windows"
    assert amb["COMSPEC"] == "C:\\Windows\\system32\\cmd.exe"  # achado sem olhar a caixa
    assert amb["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    assert amb["HOMEDRIVE"] == "C:" and amb["HOMEPATH"] == "\\Users\\Fulano"
    assert amb["CLAUDE_CODE_GIT_BASH_PATH"].endswith("bash.exe")
    assert amb["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert amb["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    for sujeira in ("HTTPS_PROXY", "HTTP_PROXY", "ANTHROPIC_BASE_URL",
                    "CLAUDE_CODE_ENTRYPOINT"):
        assert sujeira not in amb
    partes = amb["PATH"].split(";")
    assert partes[:3] == [PERFIL + "\\.local\\bin", PERFIL + "\\AppData\\Roaming\\npm",
                          "C:\\Program Files\\nodejs"]
    assert "C:\\Program Files\\Git\\cmd" in partes
    assert "C:\\app-desktop\\bin" not in partes  # o PATH vem do registro, não do app
    assert len({p.lower() for p in partes}) == len(partes)  # sem repetição


def test_ambiente_windows_sem_token_usa_o_login_do_usuario(windows):
    amb = executor.ambiente(None)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in amb  # nem o herdado do app
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in executor.ambiente("")


def test_ambiente_windows_sem_registro_usa_o_path_herdado(windows, monkeypatch):
    monkeypatch.setattr(executor, "_path_do_registro", lambda: [])
    partes = executor.ambiente("t")["PATH"].split(";")
    assert "C:\\app-desktop\\bin" in partes and "C:\\Windows" in partes


def test_ambiente_windows_com_o_minimo(windows, monkeypatch):
    for nome in ("APPDATA", "LOCALAPPDATA", "TEMP", "SystemRoot", "ComSpec", "PATHEXT",
                 "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(nome)
    amb = executor.ambiente("t")
    assert amb["APPDATA"] == PERFIL + "\\AppData\\Roaming"
    assert amb["TEMP"] == PERFIL + "\\AppData\\Local\\Temp"
    assert amb["SystemRoot"] == "C:\\Windows"
    assert amb["COMSPEC"] == "C:\\Windows\\System32\\cmd.exe"
    assert amb["HOMEDRIVE"] == "C:" and amb["HOMEPATH"] == "\\Users\\Fulano"


@pytest.mark.skipif(sys.platform == "win32", reason="usa o módulo pwd, que só existe fora do Windows")
def test_ambiente_mac_sem_token(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    amb = executor.ambiente(None)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in amb
    assert amb["SHELL"] == "/bin/zsh"


@pytest.mark.skipif(sys.platform == "win32", reason="executável sem extensão: só fora do Windows")
def test_localizar_claude_no_path_do_ambiente(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert executor.localizar_claude({"PATH": str(tmp_path)}) == str(exe)
    assert executor.localizar_claude({"PATH": str(tmp_path / "vazio")}) is None


def test_executavel_fora_do_windows_e_o_nome(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(executor, "localizar_claude", lambda env: pytest.fail("não procura"))
    assert executor._executavel({"PATH": ""}) == "claude"


# --- execução no Windows ---------------------------------------------------------------

class Entrada:
    def __init__(self):
        self.texto, self.fechada = "", False

    def write(self, texto):
        self.texto += texto

    def close(self):
        self.fechada = True


class Processo:
    def __init__(self, linhas):
        self.stdout = iter(linhas)
        self.stdin = Entrada()
        self.pid, self.returncode = 4242, None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    kill = terminate


class Popen:
    def __init__(self, linhas=()):
        self.linhas = list(linhas)
        self.chamadas = []
        self.processos = []

    def __call__(self, args, **kw):
        self.chamadas.append((args, kw))
        proc = Processo(self.linhas)
        self.processos.append(proc)
        return proc


def _executar(tmp_path, popen):
    return executor.executar(tmp_path, PACOTE, None, "tok", lambda *a: None, popen=popen)


def test_executar_windows_sem_claude_falha_com_frase_clara(windows, tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "localizar_claude", lambda env: None)
    popen = Popen()
    res = _executar(tmp_path, popen)
    assert res["status"] == "falhou" and res["detalhe"] == executor.DETALHE_SEM_CLAUDE
    assert popen.chamadas == []
    assert laco.frase_da_falha("erro", res["detalhe"]) == executor.DETALHE_SEM_CLAUDE


def test_executar_claude_ausente_no_mac_tambem_tem_frase_clara(tmp_path):
    def popen(args, **kw):
        raise FileNotFoundError(2, "No such file", "claude")
    res = _executar(tmp_path, popen)
    assert res["status"] == "falhou" and res["detalhe"] == executor.DETALHE_SEM_CLAUDE


def test_executar_windows_exe_sem_janela_e_em_grupo_proprio(windows, tmp_path, monkeypatch):
    exe = PERFIL + "\\.local\\bin\\claude.exe"
    monkeypatch.setattr(executor, "localizar_claude", lambda env: exe)
    popen = Popen()
    res = _executar(tmp_path, popen)
    assert res["status"] == "falhou"  # sem a linha final; o que importa é o processo
    args, kw = popen.chamadas[0]
    assert args[0] == exe and args[1] == "-p" and "MODO PONTE" in args[2]
    assert kw["creationflags"] == (executor.CREATE_NEW_PROCESS_GROUP
                                   | executor.CREATE_NO_WINDOW)
    assert "start_new_session" not in kw
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["env"]["USERPROFILE"] == PERFIL
    assert len(popen.chamadas) == 1  # nada de caffeinate no Windows


def test_executar_windows_cmd_manda_a_instrucao_pela_entrada(windows, tmp_path, monkeypatch):
    exe = PERFIL + "\\AppData\\Roaming\\npm\\claude.cmd"
    monkeypatch.setattr(executor, "localizar_claude", lambda env: exe)
    popen = Popen()
    _executar(tmp_path, popen)
    args, kw = popen.chamadas[0]
    assert args[:3] == [exe, "-p", "--output-format"]
    assert not any("MODO PONTE" in a for a in args)
    assert kw["stdin"] is subprocess.PIPE
    entrada = popen.processos[0].stdin
    assert "MODO PONTE" in entrada.texto and entrada.fechada


def test_validar_acesso_windows_usa_o_executavel_e_sem_janela(windows, tmp_path, monkeypatch):
    exe = PERFIL + "\\.local\\bin\\claude.exe"
    monkeypatch.setattr(executor, "localizar_claude", lambda env: exe)
    chamadas = []

    def rodar(args, **kw):
        chamadas.append((args, kw))
        return subprocess.CompletedProcess(args, 0, json.dumps({"is_error": False}), "")

    assert executor.validar_acesso(None, tmp_path, rodar=rodar, tempo=90) is True
    args, kw = chamadas[0]
    assert args[0] == exe and kw["timeout"] == 90
    assert kw["creationflags"] == executor.CREATE_NO_WINDOW
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in kw["env"]


def test_validar_acesso_windows_sem_claude_nao_sabe(windows, tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "localizar_claude", lambda env: None)
    assert executor.validar_acesso("t", tmp_path, rodar=lambda *a, **k: pytest.fail()) is None


def test_arvore_windows_taskkill_so_com_o_processo_vivo():
    chamadas = []

    def rodar(args, **kw):
        chamadas.append((args, kw))
        return subprocess.CompletedProcess(args, 0)

    vivo = Processo([])
    assert executor._arvore_windows(vivo, rodar=rodar)
    assert chamadas[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert chamadas[0][1]["creationflags"] == executor.CREATE_NO_WINDOW
    vivo.returncode = 0  # já terminou: o PID pode ser de outro processo
    assert not executor._arvore_windows(vivo, rodar=rodar)
    assert len(chamadas) == 1


# --- parada pedida pelo lançador -------------------------------------------------------

def test_parada_antes_de_comecar_nao_roda_o_lex(tmp_path, monkeypatch):
    evento = threading.Event()
    evento.set()
    monkeypatch.setattr(executor, "_parada", threading.Event())
    executor.usar_parada(evento)
    popen = Popen()
    res = _executar(tmp_path, popen)
    assert res["status"] == "cancelado" and popen.chamadas == []


def test_parada_no_meio_encerra_e_devolve_cancelado(tmp_path, monkeypatch):
    evento = threading.Event()
    monkeypatch.setattr(executor, "_parada", evento)
    monkeypatch.setattr(executor, "PASSO_S", 0)
    # O executável é resolvido de verdade no Windows; aqui o teste mede só a parada.
    monkeypatch.setattr(executor, "_executavel", lambda env: "claude")

    class Lento(Processo):
        def __init__(self):
            super().__init__([])
            self.morto = False

        def wait(self, timeout=None):
            if self.morto:
                self.returncode = -15
                return self.returncode
            evento.set()  # o lançador pediu a parada enquanto o Lex trabalhava
            raise subprocess.TimeoutExpired("claude", timeout)

        def terminate(self):
            self.morto = True

        kill = terminate

    proc = Lento()
    res = executor.executar(tmp_path, PACOTE, None, "tok", lambda *a: None,
                            popen=lambda args, **kw: proc if args[0] == "claude" else Processo([]))
    assert res["status"] == "cancelado" and proc.morto


def test_laco_cancelado_pela_parada_nao_avisa_falha(tmp_path):
    class PainelFalso:
        def __init__(self):
            self.chamadas = []

        def proximo(self):
            return dict(PACOTE)

        def autos(self, demanda):
            return None

        def batida(self, *a, **k):
            self.chamadas.append("batida")

        def contato(self, ok):
            self.chamadas.append(("contato", ok))

        def falha(self, *a):
            self.chamadas.append("falha")

    painel = PainelFalso()
    laco.uma_volta(painel, tmp_path, lambda: "t", tmp_path / "e.json",
                   executar=lambda *a, **k: executor._resultado("cancelado"),
                   validar=lambda t, c: True)
    assert "falha" not in painel.chamadas


# --- modo local: token opcional --------------------------------------------------------

def test_laco_sem_token_no_modo_local_reserva_com_o_login_do_usuario(tmp_path):
    vistos = []

    class PainelFalso:
        def proximo(self):
            vistos.append("proximo")
            return None

        def contato(self, ok):
            vistos.append(("contato", ok))

    laco.uma_volta(PainelFalso(), tmp_path, lambda: None, tmp_path / "e.json",
                   exigir_token=False, validar=lambda t, c: True)
    assert vistos == ["proximo", ("contato", True)]
    vistos.clear()
    laco.uma_volta(PainelFalso(), tmp_path, lambda: None, tmp_path / "e.json")
    assert vistos == [("contato", False)]  # o Mac do escritório continua exigindo


# --- higienização com a casa do mentorado ----------------------------------------------

def test_explicacao_sem_a_casa_do_mentorado_no_windows(monkeypatch):
    casa = "C:\\Users\\Fulano\\Meus Documentos\\Casa da Banca"
    monkeypatch.setenv("CONTROLADORIA_CASA_BANCA", casa)
    texto = (f"Li {casa}\\1000001-cartao7\\publicacao.md e não achei a decisão; "
             "o squad em squads\\peticao fica parado.")
    limpo = executor.explicacao_do_lex(texto)
    for resto in ("Fulano", "Meus Documentos", "Casa da Banca", "publicacao", "squads",
                  "C:", "\\"):
        assert resto not in limpo, resto
    assert "não achei a decisão" in limpo


def test_explicacao_sem_a_casa_do_mentorado_no_mac(monkeypatch):
    casa = "/Users/fulano/Documents/Minha Banca/Casos em andamento"
    monkeypatch.setenv("CONTROLADORIA_CASA_BANCA", casa)
    limpo = executor.explicacao_do_lex(f"Conferi {casa}/x e falta a sentença.")
    assert "Minha Banca" not in limpo and "Casos em andamento" not in limpo
    assert "falta a sentença" in limpo


def test_limpar_detalhe_esconde_caminho_do_windows():
    assert executor.limpar_detalhe("erro em C:\\Users\\Fulano\\x.txt agora") == \
        "erro em [caminho] agora"
