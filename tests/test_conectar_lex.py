"""Botão "Conectar o Lex" (ponte_mac.conectar): o `claude setup-token` roda num terminal
invisível; aqui o terminal é um dublê que devolve a saída que o claude real imprime."""

import pathlib
import threading

import pytest

import painel
import painel_local
from nucleo import banco, cofre
from ponte_mac import conectar, executor
from tests.cofre_falso import cofre_falso  # noqa: F401

ACESSO = "sk-ant-oat01-" + "Zx9_-q" * 15
LINK = ("https://claude.com/cai/oauth/authorize?code=true&client_id=abc&response_type=code"
        "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=xyz")
# Como o claude 2.1 imprime: espaços como "avança o cursor", cores, spinner.
TELA_INICIAL = ("\x1b[1mWelcome\x1b[1Cto\x1b[1CClaude\x1b[1CCode\x1b[0m\r\n·\x1b[1COpening"
                "\x1b[1Cbrowser\r\nBrowser didn't open? Use the url below to sign in\r\n\r\n"
                f"\x1b[2m{LINK}\x1b[0m\r\n\r\nPaste\x1b[1Ccode\x1b[1Chere\x1b[1Cif\x1b[1C"
                "prompted\x1b[1C>")
TELA_FINAL = (f"\r\n✓ Long-lived authentication token created successfully!\r\n\r\n"
              f"Your OAuth token (valid for 1 year):\r\n\r\n\x1b[1m{ACESSO}\x1b[0m\r\n")


class TerminalFalso(conectar._Terminal):
    def __init__(self, partes, vivo=True):
        super().__init__()
        self.partes = list(partes)  # cada leitura do texto libera mais um pedaço
        self.escrito = []
        self._vivo = vivo
        self.encerrado = False

    def texto(self):
        if self.partes:
            self._juntar(self.partes.pop(0))
        return super().texto()

    def escrever(self, texto):
        self.escrito.append(texto)

    def vivo(self):
        return self._vivo and not self.encerrado

    def encerrar(self):
        self.encerrado = True


class Relogio:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def dormir(self, s):
        self.t += s


def _conexao(terminal, *, claude="/bin/claude", instalar=lambda: True, validar=True,
             guardados=None, abertos=None):
    relogio = Relogio()
    guardados = guardados if guardados is not None else []

    def abrir(argv, env, cwd):
        if abertos is not None:
            abertos.append((argv, env))
        return terminal

    return conectar.Conexao(
        abrir=abrir, instalar=instalar, validar=lambda acesso, casa, tempo: validar,
        guardar=guardados.append, localizar=(claude if callable(claude) else lambda: claude),
        relogio=relogio, dormir=relogio.dormir)


def test_limpa_a_tela_e_acha_link_e_acesso():
    texto = conectar.limpar_tela(TELA_INICIAL + TELA_FINAL)
    assert "Welcome to Claude Code" in texto
    assert conectar.achar_link(texto) == LINK
    assert conectar.achar_acesso(texto) == ACESSO


def test_caminho_feliz_guarda_testa_e_nao_expoe_o_acesso():
    terminal = TerminalFalso([TELA_INICIAL, "", TELA_FINAL])
    guardados, abertos = [], []
    cx = _conexao(terminal, guardados=guardados, abertos=abertos)
    assert cx.iniciar(em_thread=False)
    assert guardados == [ACESSO]
    estado = cx.estado()
    assert estado["etapa"] == conectar.PRONTO and not estado["andamento"]
    assert ACESSO not in repr(estado) and terminal.encerrado
    argv, env = abertos[0]
    assert argv == ["/bin/claude", "setup-token"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # o acesso antigo não entra


def test_mostra_o_link_enquanto_espera_autorizar():
    vistos = []
    terminal = TerminalFalso([TELA_INICIAL, "", "", TELA_FINAL])
    cx = _conexao(terminal)
    dormir_original = cx._dormir

    def dormir(s):
        vistos.append(cx.estado())
        dormir_original(s)
    cx._dormir = dormir
    cx.iniciar(em_thread=False)
    autorizar = [e for e in vistos if e["etapa"] == conectar.AUTORIZAR]
    assert autorizar and autorizar[0]["link"] == LINK and autorizar[0]["andamento"]


def test_codigo_colado_vai_para_o_terminal():
    terminal = TerminalFalso([TELA_INICIAL])
    cx = _conexao(terminal)
    cx._etapa, cx._terminal = conectar.AUTORIZAR, terminal
    assert cx.colar_codigo("  abcDEF123_-#ghiJKL456  ") == conectar.COLADO
    assert terminal.escrito == ["abcDEF123_-#ghiJKL456\r"]
    cx._conferindo_desde = None
    assert cx.colar_codigo("curto") != conectar.COLADO
    cx._conferindo_desde = None
    assert cx.colar_codigo("tem espaço; e ponto-e-vírgula") != conectar.COLADO
    cx._conferindo_desde = None
    cx._etapa = conectar.TESTANDO
    assert cx.colar_codigo("abcDEF123_-#ghiJKL456") == conectar.FORA


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "claude"
LINK2 = LINK.replace("state=xyz", "state=novo")
TELA_LINK2 = f"Browser didn't open? Use the url below to sign in\r\n\r\n{LINK2}\r\n"
CODIGO = "abcdefghijklmnopqrstuvwxyz0123456789#estadoinventado123"


@pytest.mark.parametrize("sistema", ["mac", "windows"])
def test_acha_a_recusa_na_saida_real_do_claude(sistema):
    bruto = (FIXTURES / f"recusa_{sistema}.txt").read_text(encoding="utf-8")
    linha = conectar.linha_de_recusa(conectar.limpar_tela(bruto))
    assert linha.startswith("OAuth error: That code wasn't accepted.")
    assert "ado123" not in linha  # o eco mascarado do código não entra
    assert conectar.linha_de_recusa("Paste code here if prompted >") is None


class TerminaisEmSequencia:
    """Cada `abrir` devolve o próximo terminal falso (a reabertura depois da recusa)."""

    def __init__(self, *terminais):
        self.terminais = list(terminais)
        self.abertos = []

    def __call__(self, argv, env, cwd):
        t = self.terminais.pop(0)
        self.abertos.append(t)
        return t


def _conexao_seq(seq, validar=True):
    relogio = Relogio()
    return conectar.Conexao(
        abrir=seq, instalar=lambda: True, validar=lambda acesso, casa, tempo: validar,
        guardar=lambda acesso: None, localizar=lambda: "/bin/claude",
        relogio=relogio, dormir=relogio.dormir)


def _recusa():
    return (FIXTURES / "recusa_mac.txt").read_text(encoding="utf-8")


def test_codigo_recusado_abre_autorizacao_nova_e_avisa():
    primeiro = TerminalFalso([TELA_INICIAL] + [""] * 3 + [_recusa()] + [""] * 50)
    segundo = TerminalFalso([TELA_LINK2] + [""] * 3 + [TELA_FINAL])
    seq = TerminaisEmSequencia(primeiro, segundo)
    cx = _conexao_seq(seq)
    vistos = []
    dormir = cx._dormir

    def dormir_e_colar(s):
        e = cx.estado()
        vistos.append(e)
        if e["etapa"] == conectar.AUTORIZAR and e["link"] == LINK and not e["conferindo"]:
            assert cx.colar_codigo(CODIGO) == conectar.COLADO
        dormir(s)
    cx._dormir = dormir_e_colar
    cx.iniciar(em_thread=False)

    assert primeiro.encerrado and len(seq.abertos) == 2
    assert any(e["conferindo"] and e["mensagem"] == conectar.MENSAGEM_CONFERINDO for e in vistos)
    novos = [e for e in vistos if e["link"] == LINK2]
    assert novos and novos[0]["mensagem"] == conectar.MENSAGEM_RECUSADO
    assert novos[0]["recusas"] == 1 and not novos[0]["conferindo"]
    assert cx.estado()["etapa"] == conectar.PRONTO
    texto = "\n".join(cx.diagnostico())
    assert "OAuth error" in texto and CODIGO not in texto and "ado123" not in texto
    assert "state=" not in texto


def test_tres_recusas_param_com_erro():
    terminais = [TerminalFalso([TELA_INICIAL] + [""] * 3 + [_recusa()] + [""] * 50)
                 for _ in range(3)]
    seq = TerminaisEmSequencia(*terminais)
    cx = _conexao_seq(seq)
    dormir = cx._dormir

    def dormir_e_colar(s):
        e = cx.estado()
        if e["etapa"] == conectar.AUTORIZAR and e["link"] and not e["conferindo"]:
            cx.colar_codigo(CODIGO)
        dormir(s)
    cx._dormir = dormir_e_colar
    cx.iniciar(em_thread=False)
    estado = cx.estado()
    assert estado["etapa"] == conectar.ERRO and estado["recusas"] == 3
    assert estado["mensagem"] == conectar.MENSAGEM_TRES_RECUSAS
    assert len(seq.abertos) == 3 and all(t.encerrado for t in terminais)


def test_sem_resposta_em_60s_volta_a_pedir_o_codigo():
    terminal = TerminalFalso([TELA_INICIAL] + [""] * 400)
    cx = _conexao(terminal)
    vistos = []
    dormir = cx._dormir

    def dormir_e_colar(s):
        e = cx.estado()
        vistos.append(e)
        if e["etapa"] == conectar.AUTORIZAR and not any(v["conferindo"] for v in vistos):
            cx.colar_codigo(CODIGO)
        if cx._relogio() > conectar.TEMPO_CONFERIR_S + 5:
            cx.cancelar()
        dormir(s)
    cx._dormir = dormir_e_colar
    cx.iniciar(em_thread=False)
    sem_resposta = [e for e in vistos if e["mensagem"] == conectar.MENSAGEM_SEM_RESPOSTA]
    assert sem_resposta and not sem_resposta[0]["conferindo"]
    assert sem_resposta[0]["etapa"] == conectar.AUTORIZAR


def test_colar_codigo_diz_o_que_houve():
    terminal = TerminalFalso([TELA_INICIAL])
    cx = _conexao(terminal)
    assert cx.colar_codigo(CODIGO) == conectar.FORA  # nenhuma autorização aberta
    cx._etapa, cx._terminal = conectar.AUTORIZAR, terminal
    assert cx.colar_codigo("curto") == conectar.FORMATO
    assert cx.colar_codigo(CODIGO) == conectar.COLADO
    assert terminal.escrito == [CODIGO + "\r"]
    assert cx.colar_codigo(CODIGO) == conectar.JA_CONFERINDO
    assert cx.estado()["conferindo"] is True


def test_sem_segredos():
    linha = (f"x sk-ant-oat01-{'A' * 60} {LINK} {CODIGO} "
             "*************************************************ado123 OAuth error")
    limpo = conectar.sem_segredos(linha)
    assert "sk-ant" not in limpo and "state=" not in limpo and "client_id" not in limpo
    assert CODIGO not in limpo and "ado123" not in limpo
    assert "https://claude.com/cai/oauth/authorize?…" in limpo and "OAuth error" in limpo


def test_sem_claude_instala_antes():
    achados = iter([None, "/home/x/.local/bin/claude"])
    instalou = []
    terminal = TerminalFalso([TELA_INICIAL, TELA_FINAL])
    cx = _conexao(terminal, claude=lambda: next(achados),
                  instalar=lambda: instalou.append(1) or True)
    cx.iniciar(em_thread=False)
    assert instalou == [1] and cx.estado()["etapa"] == conectar.PRONTO


def test_instalacao_falhou_explica():
    cx = _conexao(TerminalFalso([]), claude=None, instalar=lambda: False)
    cx.iniciar(em_thread=False)
    e = cx.estado()
    assert e["etapa"] == conectar.ERRO and "Não consegui instalar o Claude Code" in e["mensagem"]


def test_sem_link_em_tempo_desiste():
    terminal = TerminalFalso(["carregando…"])
    cx = _conexao(terminal)
    cx.iniciar(em_thread=False)
    e = cx.estado()
    assert e["etapa"] == conectar.ERRO and "não abriu a autorização" in e["mensagem"]
    assert terminal.encerrado


def test_terminal_fechado_antes_de_autorizar():
    terminal = TerminalFalso([TELA_INICIAL], vivo=False)
    cx = _conexao(terminal)
    cx.iniciar(em_thread=False)
    assert "fechada antes de terminar" in cx.estado()["mensagem"]


def test_acesso_recusado_no_teste():
    cx = _conexao(TerminalFalso([TELA_INICIAL, TELA_FINAL]), validar=False)
    cx.iniciar(em_thread=False)
    e = cx.estado()
    assert e["etapa"] == conectar.ERRO and "plano pago" in e["mensagem"]


def test_teste_inconclusivo_ainda_conta_como_pronto():
    cx = _conexao(TerminalFalso([TELA_INICIAL, TELA_FINAL]), validar=None)
    cx.iniciar(em_thread=False)
    e = cx.estado()
    assert e["etapa"] == conectar.PRONTO and "Não deu para testar agora" in e["mensagem"]


def test_uma_conexao_por_vez():
    liberar = threading.Event()
    terminal = TerminalFalso([TELA_INICIAL])
    cx = _conexao(terminal)
    cx._dormir = lambda s: liberar.wait(0.01)
    assert cx.iniciar()
    assert not cx.iniciar()
    cx.cancelar()
    cx._thread.join(timeout=5)
    assert cx.estado()["etapa"] == conectar.ERRO and terminal.encerrado
    assert cx.iniciar()  # terminou: pode começar de novo
    cx.cancelar()
    cx._thread.join(timeout=5)


def test_erro_inesperado_nao_derruba():
    def abrir(*_a):
        raise OSError("pty indisponível")
    cx = conectar.Conexao(abrir=abrir, localizar=lambda: "/bin/claude")
    cx.iniciar(em_thread=False)
    assert cx.estado()["etapa"] == conectar.ERRO


def test_instalador_oficial_por_sistema(monkeypatch):
    chamadas = []

    class R:
        returncode = 0

    def rodar(argv, **k):
        chamadas.append((argv, k))
        return R()
    monkeypatch.setattr(executor, "ambiente", lambda token=None: {"PATH": "/usr/bin"})
    monkeypatch.setattr(executor, "no_windows", lambda: False)
    assert conectar.instalar_claude(rodar)
    assert chamadas[0][0] == ["/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]
    monkeypatch.setattr(executor, "no_windows", lambda: True)
    assert conectar.instalar_claude(rodar)
    assert chamadas[1][0][-1] == "irm https://claude.ai/install.ps1 | iex"
    assert chamadas[1][1]["creationflags"] == executor.CREATE_NO_WINDOW
    R.returncode = 1
    assert not conectar.instalar_claude(rodar)


# --- as rotas ------------------------------------------------------------------------

@pytest.fixture
def c(tmp_path, monkeypatch, cofre_falso):  # noqa: F811
    caminho = str(tmp_path / "c.db")
    banco.conectar(caminho).close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setenv("CONTROLADORIA_CASA_BANCA", str(tmp_path))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    monkeypatch.setattr(painel_local, "_configurado", True)
    monkeypatch.setattr(executor, "localizar_claude", lambda env: "/usr/local/bin/claude")
    cx = _conexao(TerminalFalso([TELA_INICIAL]))
    monkeypatch.setattr(conectar, "CONEXAO", cx)
    cli = painel.app.test_client()
    cli.cx = cx
    return cli


def test_rotas_fora_do_modo_local_dao_404(c, monkeypatch):
    monkeypatch.delenv("CONTROLADORIA_LOCAL")
    assert c.post("/configuracoes/lex/conectar").status_code == 404
    assert c.get("/configuracoes/lex/conectar/estado").status_code == 404


def test_botao_inicia_e_estado_em_json(c, monkeypatch):
    iniciados = []
    monkeypatch.setattr(c.cx, "iniciar", lambda: iniciados.append(1) or True)
    r = c.post("/configuracoes/lex/conectar")
    assert r.status_code == 302 and r.headers["Location"].endswith("#acesso-lex")
    assert iniciados == [1]
    c.cx._etapa, c.cx._link = conectar.AUTORIZAR, LINK
    r = c.get("/configuracoes/lex/conectar/estado")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.get_json() == {"etapa": "autorizar", "mensagem": conectar.MENSAGENS["autorizar"],
                            "link": LINK, "conferindo": False, "recusas": 0,
                            "andamento": True}
    html = c.get("/configuracoes/lex").get_data(as_text=True)
    assert 'data-andamento="1"' in html and LINK.replace("&", "&amp;") in html


def test_rota_do_codigo_explica_cada_caso(c):
    r = c.post("/configuracoes/lex/conectar/codigo", data={"codigo": "abcDEF123_-#ghi"})
    assert r.status_code == 409 and "A autorização já terminou" in r.get_json()["mensagem"]
    terminal = TerminalFalso([])
    c.cx._etapa, c.cx._terminal = conectar.AUTORIZAR, terminal
    r = c.post("/configuracoes/lex/conectar/codigo", data={"codigo": "x"})
    assert r.status_code == 400 and "não parece o código" in r.get_json()["mensagem"]
    r = c.post("/configuracoes/lex/conectar/codigo", data={"codigo": "abcDEF123_-#ghi"})
    assert r.get_json() == {"ok": True}
    r = c.post("/configuracoes/lex/conectar/codigo", data={"codigo": "abcDEF123_-#ghi"})
    assert r.status_code == 409 and "Já estamos conferindo" in r.get_json()["mensagem"]


def test_tela_tem_copiar_link_ajuda_e_diagnostico(c):
    c.cx._etapa, c.cx._link = conectar.AUTORIZAR, LINK
    html = c.get("/configuracoes/lex").get_data(as_text=True)
    assert "Copiar o link" in html and 'class="btn btn-secundario conectar-copiar"' in html
    assert "Não foi possível verificar seu navegador" in html
    assert "desligue VPN e bloqueadores" in html
    assert "Copiar diagnóstico" in html and "/configuracoes/lex/diagnostico" in html
    assert "Use só a aba mais recente" in html
    assert 'id="diagnostico-texto"' in html and "Selecione o texto abaixo e copie" in html
    # link novo (outra autorização): o botão volta a "Copiar o link"
    assert "botaoLink.textContent = 'Copiar o link'" in html


def test_diagnostico_sem_segredos(c, monkeypatch):
    monkeypatch.setattr(conectar, "versao_claude", lambda rodar=None: "2.1.312 (Claude Code)")
    c.cx._registrar(f"autorizar: link {LINK}")
    c.cx._linhas = [conectar.sem_segredos("OAuth error: That code wasn't accepted.")]
    c.cx.registrar_teste("recusado")
    r = c.get("/configuracoes/lex/diagnostico")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    texto = r.get_json()["texto"]
    for trecho in ("Controladoria:", "Claude Code: 2.1.312", "Sistema:", "recusado",
                   "claude: OAuth error"):
        assert trecho in texto
    assert "state=" not in texto and "client_id" not in texto and ACESSO not in texto


def test_versao_claude(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(executor, "_executavel", lambda env: "claude")
    ok = lambda argv, **kw: sp.CompletedProcess(argv, 0, stdout="2.1.312 (Claude Code)\n")
    assert conectar.versao_claude(rodar=ok) == "2.1.312 (Claude Code)"

    def falha(argv, **kw):
        raise FileNotFoundError
    assert conectar.versao_claude(rodar=falha) == ""


def test_tela_sem_pasta_da_banca(c, monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROLADORIA_CASA_BANCA", str(tmp_path / "nao-existe"))
    html = c.get("/configuracoes/lex").get_data(as_text=True)
    assert "Pasta da Banca não encontrada" in html and "Ligue o Lex na Controladoria" in html


def test_textos_do_local_nao_falam_em_mac(monkeypatch):
    from nucleo import painel_dados
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    assert "Mac" not in painel_dados.frase_da_falha("reserva_vencida")
    assert "Mac" not in painel_dados.frase_da_falha("erro", "acesso ao plano do Lex no Mac foi recusado")
    monkeypatch.delenv("CONTROLADORIA_LOCAL")
    assert painel_dados.frase_da_falha("reserva_vencida") == "o Mac parou de responder"
