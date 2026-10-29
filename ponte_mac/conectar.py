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
TEMPO_CONFERIR_S = 60      # depois de colar, a Anthropic responde em segundos
MAX_RECUSAS = 3

INSTALADOR_WINDOWS = "irm https://claude.ai/install.ps1 | iex"
INSTALADOR_MAC = "curl -fsSL https://claude.ai/install.sh | bash"

_ACESSO = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{20,500}")
_LINK = re.compile(r"https://\S+/oauth/authorize\?\S+")
_CODIGO_COLADO = re.compile(r"[A-Za-z0-9_#.-]{10,600}")
_RECUSA = re.compile(r"(?i)OAuth error|wasn't accepted|was not accepted")
# Para o diagnóstico: a parte do link depois do "?", o acesso e qualquer sequência longa
# (o código colado, o eco mascarado dele) nunca saem daqui.
_QUERY = re.compile(r"(https?://[^\s?]+)\?\S*")
_LONGO = re.compile(r"[A-Za-z0-9_#.*-]{20,}")
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
               "Autorizar. Use só a aba mais recente (feche as antigas) e depois volte "
               "para esta página.",
    TESTANDO: "Autorizado. Guardando o acesso e testando (até 2 minutos)…",
    PRONTO: "Pronto: o Lex está ligado ao seu plano do Claude.",
}

# O que o colar_codigo responde à tela.
COLADO, FORMATO, FORA, JA_CONFERINDO = "colado", "formato", "fora", "conferindo"

MENSAGEM_CONFERINDO = "Código enviado. Conferindo com a Anthropic…"
MENSAGEM_RECUSADO = ("A Anthropic não aceitou esse código. Isso acontece quando ele é de "
                     "uma aba antiga ou ficou incompleto. Feche as abas de autorização "
                     "abertas e use só esta, que acabamos de abrir.")
MENSAGEM_REABRINDO = "A Anthropic não aceitou esse código. Abrindo uma autorização nova…"
MENSAGEM_SEM_RESPOSTA = ("A Anthropic não respondeu ao código em 1 minuto. Confira se "
                         "copiou o código inteiro e envie de novo; se repetir, clique em "
                         "Cancelar e em Conectar o Lex de novo.")
MENSAGEM_TRES_RECUSAS = ("A Anthropic recusou o código 3 vezes seguidas. Feche todas as "
                         "abas de autorização, clique em Conectar o Lex de novo e use só a "
                         "aba nova. Se repetir, clique em Copiar diagnóstico e cole no "
                         "grupo da mentoria.")


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


def linha_de_recusa(texto: str) -> str | None:
    """A linha em que o `claude setup-token` diz que a Anthropic não aceitou o código."""
    for linha in texto.splitlines():
        if _RECUSA.search(linha):
            return sem_segredos(linha)
    return None


def sem_segredos(linha: str) -> str:
    """Uma linha para o diagnóstico: sem acesso, sem código e sem a parte do link
    depois do "?"."""
    linha = _QUERY.sub(r"\1?…", str(linha or ""))
    linha = _ACESSO.sub("[oculto]", linha)
    linha = _LONGO.sub("[oculto]", linha)
    return " ".join(linha.split())[:300]


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
        self._conferindo_desde: float | None = None
        self._marca = 0              # tamanho da saída quando o último código foi enviado
        self._recusas = 0
        self._eventos: list[str] = []  # para o "Copiar diagnóstico"
        self._linhas: list[str] = []   # mensagens de erro do claude, sem segredos
        self._teste = ""

    # -- leitura pela tela --
    def estado(self) -> dict:
        with self._trava:
            etapa = self._etapa
            conferindo = etapa == AUTORIZAR and self._conferindo_desde is not None
            mensagem = (MENSAGEM_CONFERINDO if conferindo
                        else self._mensagem or MENSAGENS.get(etapa, ""))
            return {"etapa": etapa, "mensagem": mensagem,
                    "link": self._link if etapa == AUTORIZAR else None,
                    "conferindo": conferindo, "recusas": self._recusas,
                    "andamento": etapa in (INSTALANDO, ABRINDO, AUTORIZAR, TESTANDO)}

    def em_andamento(self) -> bool:
        return self.estado()["andamento"]

    def _mudar(self, etapa: str, mensagem: str = "", link: str | None = None) -> None:
        with self._trava:
            self._etapa, self._mensagem, self._link = etapa, mensagem, link
            self._conferindo_desde = None
        self._registrar(f"{etapa}: {mensagem or MENSAGENS.get(etapa, '')}")

    def _registrar(self, texto: str) -> None:
        linha = f"{time.strftime('%H:%M:%S')} {sem_segredos(texto)}"
        with self._trava:
            self._eventos = (self._eventos + [linha])[-40:]
        log.info("conectar o Lex: %s", linha)

    def diagnostico(self) -> list[str]:
        with self._trava:
            return [*self._eventos, *(f"claude: {l}" for l in self._linhas)]

    def registrar_teste(self, resultado: str) -> None:
        with self._trava:
            self._teste = f"{time.strftime('%d/%m %H:%M')} {resultado}"
        log.info("testar o acesso do Lex: %s", resultado)

    def ultimo_teste(self) -> str:
        with self._trava:
            return self._teste

    # -- ações da tela --
    def iniciar(self, *, em_thread: bool = True) -> bool:
        """Começa uma conexão; False se já há uma em andamento."""
        with self._trava:
            if self._etapa in (INSTALANDO, ABRINDO, AUTORIZAR, TESTANDO):
                return False
            self._etapa, self._mensagem, self._link = ABRINDO, "", None
            self._recusas, self._eventos, self._linhas = 0, [], []
            self._conferindo_desde = None
        self._cancelar.clear()
        if em_thread:
            self._thread = threading.Thread(target=self._rodar, name="conectar-lex",
                                            daemon=True)
            self._thread.start()
        else:
            self._rodar()
        return True

    def colar_codigo(self, bruto: str) -> str:
        """Manda ao `claude setup-token` o código que a página da Anthropic mostrou.
        Responde COLADO, FORMATO, FORA (nenhuma autorização esperando código) ou
        JA_CONFERINDO (outro código ainda está sendo conferido)."""
        codigo = codigo_colado_valido(bruto)
        with self._trava:
            terminal = self._terminal if self._etapa == AUTORIZAR else None
            if terminal is None:
                return FORA
            if not codigo:
                return FORMATO
            if self._conferindo_desde is not None:
                return JA_CONFERINDO
            self._marca = len(limpar_tela(terminal.texto()))
            self._conferindo_desde = self._relogio()
        terminal.escrever(codigo + "\r")
        self._registrar("código enviado; conferindo")
        return COLADO

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

    def _abrir_autorizacao(self, exe: str) -> _Terminal:
        env = executor.ambiente(None)  # sem acesso antigo: o setup-token gera um novo
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        terminal = self._abrir([exe, "setup-token"], env, str(Path.home()))
        with self._trava:
            antigo, self._terminal = self._terminal, terminal
            self._conferindo_desde, self._marca = None, 0
        if antigo is not None:
            antigo.encerrar()
        return terminal

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
        terminal = self._abrir_autorizacao(exe)

        inicio = self._relogio()
        link, mensagem_link = None, ""
        while True:
            if self._cancelar.is_set():
                self._mudar(ERRO, "Conexão cancelada.")
                return
            texto = limpar_tela(terminal.texto())
            acesso = achar_acesso(texto)
            if acesso:
                break
            if link is None and (link := achar_link(texto)):
                self._mudar(AUTORIZAR, mensagem_link, link=link)
            with self._trava:
                desde, marca = self._conferindo_desde, self._marca
            if desde is not None:
                recusa = linha_de_recusa(texto[marca:])
                if recusa:
                    with self._trava:
                        self._recusas += 1
                        recusas = self._recusas
                        self._linhas = (self._linhas + [recusa])[-10:]
                    if recusas >= MAX_RECUSAS:
                        self._mudar(ERRO, MENSAGEM_TRES_RECUSAS)
                        return
                    self._mudar(ABRINDO, MENSAGEM_REABRINDO)
                    terminal = self._abrir_autorizacao(exe)
                    inicio, link, mensagem_link = self._relogio(), None, MENSAGEM_RECUSADO
                    continue
                if self._relogio() - desde > TEMPO_CONFERIR_S:
                    with self._trava:
                        self._conferindo_desde = None
                        self._mensagem = MENSAGEM_SEM_RESPOSTA
                    self._registrar("a Anthropic não respondeu ao código em 60 s")
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


def versao_claude(rodar=subprocess.run) -> str:
    """A versão do Claude Code deste computador ("2.1.312 (Claude Code)"), ou ""."""
    env = executor.ambiente(None)
    opcoes = {"creationflags": executor.CREATE_NO_WINDOW} if executor.no_windows() else {}
    try:
        r = rodar([executor._executavel(env), "--version"], env=env,
                  stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=20, **opcoes)
    except (OSError, subprocess.SubprocessError):
        return ""
    linhas = (r.stdout or "").strip().splitlines()
    return linhas[0][:60] if r.returncode == 0 and linhas else ""


def texto_diagnostico(cx: "Conexao", *, versao: str, claude: str, sistema: str) -> str:
    """O texto do "Copiar diagnóstico": versões, sistema, último teste e a última conexão.
    Nunca o acesso, o código colado ou a parte do link depois do "?"."""
    eventos = cx.diagnostico()
    linhas = ["Diagnóstico da conexão do Lex (Controladoria do Lex Lab)",
              f"Controladoria: {versao or 'desconhecida'}",
              f"Claude Code: {claude or 'não encontrado'}",
              f"Sistema: {sistema}",
              f"Último teste de acesso: {cx.ultimo_teste() or 'nenhum'}",
              "Última conexão:"]
    linhas += [f"  {sem_segredos(e)}" for e in eventos] or ["  (nenhuma desde que o painel abriu)"]
    return "\n".join(linhas)


CONEXAO = Conexao()
