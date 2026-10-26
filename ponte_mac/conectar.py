"""Botão "Conectar o Lex" (modo local): liga o Lex ao plano do Claude sem terminal.

Na mentoria de 2026-10-07 o caminho antigo (abrir o PowerShell, rodar `claude
setup-token`, copiar o código da janela preta e colar no painel) travou quase todo mundo.
Agora a própria Controladoria faz, numa thread, o que o advogado fazia à mão:

1. se o Claude Code não está instalado, roda o instalador oficial da Anthropic;
2. roda `claude setup-token` num terminal invisível (pseudoterminal: o `pty` do sistema
   no Mac, o `pywinpty` no Windows), porque ele só funciona com terminal;
3. o próprio `claude` abre o navegador na página de autorização; a tela mostra também o
   link alternativo que ele imprime e, para esse caminho, uma caixa para colar o código
   que a página da Anthropic exibe (vai direto para o terminal invisível);
4. lê da saída o acesso (`sk-ant-oat01-…`), guarda no cofre e testa com a mesma sonda da
   ponte (`executor.validar_acesso`).

O acesso nunca vai para a tela, para o log ou para o estado. Uma conexão por vez."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from ponte_mac import chaves, executor
from ponte_mac import local as ponte_local

log = logging.getLogger("ponte_mac.conectar")

TEMPO_INSTALAR_S = 600
TEMPO_LINK_S = 90          # o `claude setup-token` mostra o link em segundos
TEMPO_AUTORIZAR_S = 900    # o advogado tem 15 minutos para autorizar
TEMPO_TESTE_S = 120
COLUNAS = 1000             # terminal largo: o link e o acesso não quebram de linha

INSTALADOR_WINDOWS = "irm https://claude.ai/install.ps1 | iex"
INSTALADOR_MAC = "curl -fsSL https://claude.ai/install.sh | bash"

_ACESSO = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{20,500}")
_LINK = re.compile(r"https://\S+/oauth/authorize\?\S+")
_CODIGO_COLADO = re.compile(r"[A-Za-z0-9_#.-]{10,600}")
# Sequências do terminal: as de "avançar o cursor" viram espaço (o Ink usa no lugar de
# espaços), as demais somem.
_AVANCO = re.compile(r"\x1b\[\d*C")
_ESCAPES = re.compile(r"\x1b\[[0-9;?<>=]*[A-Za-z~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?")

# Etapas (o que a tela mostra). "erro" e "pronto" encerram.
INSTALANDO, ABRINDO, AUTORIZAR, TESTANDO, PRONTO, ERRO = (
    "instalando", "abrindo", "autorizar", "testando", "pronto", "erro")

MENSAGENS = {
    INSTALANDO: "Instalando o Claude Code oficial da Anthropic neste computador "
                "(leva de 1 a 3 minutos)…",
    ABRINDO: "Preparando a autorização…",
    AUTORIZAR: "Abrimos uma aba no navegador: entre na sua conta Claude e clique em "
               "Autorizar. Depois volte para esta página.",
    TESTANDO: "Autorizado. Guardando o acesso e testando (até 2 minutos)…",
    PRONTO: "Pronto: o Lex está ligado ao seu plano do Claude.",
}


def limpar_tela(bruto: str) -> str:
    """Texto do terminal sem as sequências de controle."""
    return _ESCAPES.sub("", _AVANCO.sub(" ", bruto)).replace("\r", "\n")


def achar_link(texto: str) -> str | None:
    """O link de autorização que o `claude setup-token` imprime (o último, se houver
    mais de um)."""
    achados = _LINK.findall(texto)
    return achados[-1] if achados else None


def achar_acesso(texto: str) -> str | None:
    achados = _ACESSO.findall(texto)
    return achados[-1] if achados else None


def codigo_colado_valido(bruto: str) -> str | None:
    """O código que a página da Anthropic mostra (sem espaços), ou None."""
    codigo = "".join(str(bruto or "").split())
    return codigo if _CODIGO_COLADO.fullmatch(codigo) else None


# --- o terminal invisível ----------------------------------------------------------------

class _Terminal:
    """Um programa rodando num pseudoterminal; a saída vai sendo juntada por uma thread."""

    def __init__(self):
        self._partes: list[str] = []
        self._trava = threading.Lock()

    def _juntar(self, texto: str) -> None:
        with self._trava:
            self._partes.append(texto)

    def texto(self) -> str:
        with self._trava:
            return "".join(self._partes)

    def escrever(self, texto: str) -> None:  # pragma: no cover - por sistema
        raise NotImplementedError

    def vivo(self) -> bool:  # pragma: no cover - por sistema
        raise NotImplementedError

    def encerrar(self) -> None:  # pragma: no cover - por sistema
        raise NotImplementedError


class _TerminalUnix(_Terminal):  # pragma: no cover - exercitado no teste real do Mac
    def __init__(self, argv: list[str], env: dict, cwd: str):
        super().__init__()
        import fcntl
        import os
        import pty
        import struct
        import termios

        self._os = os
        mestre, escravo = pty.openpty()
        fcntl.ioctl(escravo, termios.TIOCSWINSZ, struct.pack("HHHH", 50, COLUNAS, 0, 0))
        self._mestre = mestre
        self._proc = subprocess.Popen(argv, stdin=escravo, stdout=escravo, stderr=escravo,
                                      env={**env, "TERM": "xterm-256color"}, cwd=cwd,
                                      start_new_session=True, close_fds=True)
        os.close(escravo)
        threading.Thread(target=self._ler, name="conectar-lex-saida", daemon=True).start()

    def _ler(self):
        while True:
            try:
                dados = self._os.read(self._mestre, 65536)
            except OSError:
                break
            if not dados:
                break
            self._juntar(dados.decode("utf-8", "replace"))

    def escrever(self, texto: str) -> None:
        self._os.write(self._mestre, texto.encode("utf-8"))

    def vivo(self) -> bool:
        return self._proc.poll() is None

    def encerrar(self) -> None:
        if self.vivo():
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        try:
            self._os.close(self._mestre)
        except OSError:
            pass


class _TerminalWindows(_Terminal):  # pragma: no cover - exercitado no teste real do Windows
    def __init__(self, argv: list[str], env: dict, cwd: str):
        super().__init__()
        from winpty import PtyProcess

        self._proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(50, COLUNAS))
        threading.Thread(target=self._ler, name="conectar-lex-saida", daemon=True).start()

    def _ler(self):
        while True:
            try:
                dados = self._proc.read(65536)
            except EOFError:
                break
            except Exception:  # noqa: BLE001 — terminal fechado
                break
            if dados:
                self._juntar(dados)
            elif not self._proc.isalive():
                break
            else:
                time.sleep(0.1)

    def escrever(self, texto: str) -> None:
        self._proc.write(texto)

    def vivo(self) -> bool:
        return bool(self._proc.isalive())

    def encerrar(self) -> None:
        try:
            if self._proc.isalive():
                self._proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass


def abrir_terminal(argv: list[str], env: dict, cwd: str) -> _Terminal:
    if executor.no_windows():
        if argv[0].lower().endswith((".cmd", ".bat")):  # claude instalado pelo npm
            argv = [env.get("COMSPEC") or "cmd.exe", "/c", *argv]
        return _TerminalWindows(argv, env, cwd)
    return _TerminalUnix(argv, env, cwd)


# --- a conexão ---------------------------------------------------------------------------

class Conexao:
    """Estado de uma conexão em andamento (ou da última). Tudo que a tela lê está em
    `estado()`; o acesso nunca entra nele."""

    def __init__(self, *, abrir=abrir_terminal, instalar=None, validar=None,
                 guardar=None, localizar=None, relogio=time.monotonic, dormir=time.sleep):
        self._abrir = abrir
        self._instalar = instalar or instalar_claude
        self._validar = validar or executor.validar_acesso
        self._guardar = guardar or (lambda acesso: chaves.guardar(chaves.ACESSO_CLAUDE, acesso))
        self._localizar = localizar or (lambda: executor.localizar_claude(executor.ambiente()))
        self._relogio = relogio
        self._dormir = dormir
        self._trava = threading.Lock()
        self._etapa: str | None = None
        self._mensagem = ""
        self._link: str | None = None
        self._terminal: _Terminal | None = None
        self._cancelar = threading.Event()
        self._thread: threading.Thread | None = None

    # -- leitura pela tela --
    def estado(self) -> dict:
        with self._trava:
            etapa = self._etapa
            return {"etapa": etapa, "mensagem": self._mensagem or MENSAGENS.get(etapa, ""),
                    "link": self._link if etapa == AUTORIZAR else None,
                    "andamento": etapa in (INSTALANDO, ABRINDO, AUTORIZAR, TESTANDO)}

    def em_andamento(self) -> bool:
        return self.estado()["andamento"]

    def _mudar(self, etapa: str, mensagem: str = "", link: str | None = None) -> None:
        with self._trava:
            self._etapa, self._mensagem, self._link = etapa, mensagem, link
        log.info("conectar o Lex: %s", etapa)

    # -- ações da tela --
    def iniciar(self, *, em_thread: bool = True) -> bool:
        """Começa uma conexão; False se já há uma em andamento."""
        with self._trava:
            if self._etapa in (INSTALANDO, ABRINDO, AUTORIZAR, TESTANDO):
                return False
            self._etapa, self._mensagem, self._link = ABRINDO, "", None
        self._cancelar.clear()
        if em_thread:
            self._thread = threading.Thread(target=self._rodar, name="conectar-lex",
                                            daemon=True)
            self._thread.start()
        else:
            self._rodar()
        return True

    def colar_codigo(self, bruto: str) -> bool:
        """Manda ao `claude setup-token` o código que a página da Anthropic mostrou."""
        codigo = codigo_colado_valido(bruto)
        with self._trava:
            terminal = self._terminal if self._etapa == AUTORIZAR else None
        if not codigo or terminal is None:
            return False
        terminal.escrever(codigo + "\r")
        return True

    def cancelar(self) -> None:
        self._cancelar.set()
        with self._trava:
            terminal = self._terminal
        if terminal is not None:
            terminal.encerrar()

    # -- o trabalho --
    def _rodar(self) -> None:
        try:
            self._conectar()
        except Exception as exc:  # noqa: BLE001 — nada derruba o painel
            log.error("conectar o Lex falhou: %s", executor.limpar_detalhe(exc))
            self._mudar(ERRO, "Não deu certo desta vez. Tente de novo; se repetir, use a "
                              "outra forma, logo abaixo.")
        finally:
            with self._trava:
                terminal, self._terminal = self._terminal, None
            if terminal is not None:
                terminal.encerrar()

    def _conectar(self) -> None:
        exe = self._localizar()
        if not exe:
            self._mudar(INSTALANDO)
            if not self._instalar() or not (exe := self._localizar()):
                self._mudar(ERRO, "Não consegui instalar o Claude Code. Confira a internet e "
                                  "tente de novo, ou peça ao Claude: «Instale o Claude Code "
                                  "de linha de comando».")
                return
        self._mudar(ABRINDO)
        env = executor.ambiente(None)  # sem acesso antigo: o setup-token gera um novo
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        terminal = self._abrir([exe, "setup-token"], env, str(Path.home()))
        with self._trava:
            self._terminal = terminal

        inicio = self._relogio()
        link = None
        while True:
            if self._cancelar.is_set():
                self._mudar(ERRO, "Conexão cancelada.")
                return
            texto = limpar_tela(terminal.texto())
            acesso = achar_acesso(texto)
            if acesso:
                break
            if link is None and (link := achar_link(texto)):
                self._mudar(AUTORIZAR, link=link)
            passado = self._relogio() - inicio
            if not terminal.vivo():
                self._mudar(ERRO, "A autorização foi fechada antes de terminar. Clique em "
                                  "Conectar o Lex de novo.")
                return
            if link is None and passado > TEMPO_LINK_S:
                self._mudar(ERRO, "O Claude Code não abriu a autorização. Tente de novo; se "
                                  "repetir, use a outra forma, logo abaixo.")
                return
            if passado > TEMPO_AUTORIZAR_S:
                self._mudar(ERRO, "O tempo para autorizar acabou. Clique em Conectar o Lex de "
                                  "novo.")
                return
            self._dormir(0.5)

        self._mudar(TESTANDO)
        terminal.encerrar()
        self._guardar(acesso)
        casa = Path(ponte_local.casa_da_banca() or Path.home()).expanduser()
        if not casa.is_dir():
            casa = Path.home()
        resultado = self._validar(acesso, casa, tempo=TEMPO_TESTE_S)
        del acesso
        if resultado is False:
            self._mudar(ERRO, "O Claude recusou o acesso gerado. Confira se a sua conta tem "
                              "um plano pago (Pro ou Max) e tente de novo.")
        elif resultado is None:
            self._mudar(PRONTO, "Acesso guardado. Não deu para testar agora (internet ou o "
                                "Claude demorou); o Lex tenta sozinho no próximo cartão.")
        else:
            self._mudar(PRONTO)


def instalar_claude(rodar=subprocess.run) -> bool:
    """Roda o instalador oficial do Claude Code (Anthropic). True se terminou bem."""
    env = executor.ambiente(None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if executor.no_windows():
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                INSTALADOR_WINDOWS]
        opcoes = {"creationflags": executor.CREATE_NO_WINDOW}
    else:
        argv = ["/bin/bash", "-c", INSTALADOR_MAC]
        opcoes = {}
    try:
        r = rodar(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=TEMPO_INSTALAR_S, **opcoes)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("instalador do Claude Code falhou: %s", executor.limpar_detalhe(exc))
        return False
    if r.returncode != 0:
        log.error("instalador do Claude Code terminou com código %s", r.returncode)
        return False
    return True


CONEXAO = Conexao()
