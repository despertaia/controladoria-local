"""A leitura de prazo no Mac (e no modo local): um fio próprio, ao lado do laço do squad, que
não espera a peça terminar. Pega o pedido do painel, roda um `claude -p` curto, sem nenhuma
ferramenta, e devolve a resposta crua: quem confere é o servidor. O texto da publicação nunca
vai para o registro."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from ponte_mac import executor
from ponte_mac.cliente import PainelErro

log = logging.getLogger("ponte_mac.leitor")

MODELO = "sonnet"
TEMPO_S = 300
INTERVALO_S = 60
PAUSA_LIMITE_S = 30 * 60
PAUSA_ACESSO_S = 10 * 60
PAUSA_ENTRE_LEITURAS = 20  # segundos depois de cada leitura: a primeira fila não drena o plano do squad
ESPERA_BANCO_OCUPADO_S = 30  # o painel respondeu 503 à resposta do Lex: espera e manda de novo
TENTATIVAS_BANCO_OCUPADO = 3  # cabem na reserva: a resposta já custou uma chamada, não se joga fora
_AVISADO: set = set()
GRACA_S = 3.0  # depois que o filho sai, quanto esperar a saída fechar antes de derrubar o grupo
_LIMITE_CURTO = 200
# a mensagem de limite do plano é uma frase curta e começa por ela; prosa que só cita «usage
# limit» ou «rate limit» não é o limite do plano (e o texto da publicação não pode induzi-lo)
_MENSAGEM_DE_LIMITE = re.compile(
    r"(?i)^\W*(?:claude(?:\s+ai)?\s+)?(?:usage limit reached|limit reached|"
    r"you(?:'ve|\u2019ve| have) hit your (?:usage |session |weekly )?limit|"
    r"(?:5-hour|weekly|opus|session) limit reached|limite de uso(?: atingido)?)\b")


def comando_leitura(exe: str = "claude") -> list[str]:
    """Sem ferramentas, sem sessão guardada, sem MCP, sem skills e sem configuração da casa da
    Banca (hooks e instruções do escritório não entram). O pedido vai pela entrada padrão."""
    return [exe, "-p", "--output-format", "json", "--model", MODELO, "--tools", "",
            "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
            "--setting-sources", ""]


def _e_mensagem_de_limite(texto: str) -> bool:
    """O limite do plano veio como a resposta inteira: curta, sem JSON, começando pela frase."""
    texto = texto.strip()
    return (len(texto) < _LIMITE_CURTO and "{" not in texto and "[" not in texto
            and bool(_MENSAGEM_DE_LIMITE.match(texto)))


def _classificar(texto: str) -> str:
    if executor.parece_limite(texto):
        return "limite"
    if executor.parece_erro_de_acesso(texto):
        return "acesso"
    return "erro"


# --- o processo: tempo e parada chegam à árvore inteira --------------------------------

SONDA_S = 1.0  # de quanto em quanto tempo o laço olha o relógio e o sinal de parada

# o processo da leitura em andamento (no máximo um por fio): serve ao desligamento do serviço
_EM_ANDAMENTO: list = []


# o início de um processo e o encerramento se excluem: ou o encerramento vê o processo na
# lista, ou o início vê o pedido de parada (quem encerra pede a parada antes)
_TRAVA = threading.Lock()


def encerrar_em_andamento() -> int:
    """Encerra a árvore do `claude` da leitura em andamento, se houver; devolve quantas."""
    total = 0
    with _TRAVA:
        while _EM_ANDAMENTO:
            proc = _EM_ANDAMENTO.pop()
            if proc.poll() is None:
                executor._encerrar(proc)
                total += 1
    return total


def _executar(cmd, *, input, cwd, env, timeout, parar=None, **opcoes):
    """Como `subprocess.run`, mas o tempo e a parada derrubam a árvore do processo (o `node`
    sob o `claude.cmd`, no Windows, não fica pendurado). Levanta `subprocess.TimeoutExpired`
    ao estourar o tempo e `Parado` se `parar()` ficar verdadeiro."""
    with _TRAVA:
        if parar is not None and parar():
            raise Parado()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", cwd=cwd, env=env, **opcoes)
        _EM_ANDAMENTO.append(proc)
    try:
        fim = time.monotonic() + timeout
        saiu_em = None
        entrada = input
        while True:
            try:
                saida, erro = proc.communicate(entrada, timeout=SONDA_S)
                if proc.returncode != 0 and parar is not None and parar():
                    raise Parado()  # derrubado pelo desligamento: não é falha da leitura
                return subprocess.CompletedProcess(cmd, proc.returncode, saida, erro)
            except subprocess.TimeoutExpired:
                entrada = None  # a entrada já foi entregue ao processo
            if parar is not None and parar():
                raise Parado()
            if proc.poll() is not None:  # o filho saiu, mas um neto ainda segura a saída
                saiu_em = saiu_em or time.monotonic()
                if time.monotonic() - saiu_em >= GRACA_S:
                    return _recolher(proc, cmd)
            if time.monotonic() >= fim:
                raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        if proc in _EM_ANDAMENTO:
            _EM_ANDAMENTO.remove(proc)
        if proc.poll() is None:
            executor._encerrar(proc)
        for fluxo in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if fluxo is not None:
                    fluxo.close()
            except OSError:
                pass


def _recolher(proc, cmd) -> subprocess.CompletedProcess:
    """O filho já saiu e a saída não fechou: derruba o grupo e fica com o que foi lido. Uma
    leitura que terminou não gasta tentativa por causa de um neto."""
    executor._encerrar(proc)
    try:
        saida, erro = proc.communicate(timeout=3)
    except subprocess.TimeoutExpired as exc:
        saida, erro = exc.stdout, exc.stderr
    saida, erro = (x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")
                   for x in (saida, erro))
    return subprocess.CompletedProcess(cmd, proc.returncode, saida, erro)


class Parado(Exception):
    """O serviço foi parado no meio da leitura."""


def ler(pedido: str, token: str | None, pasta: Path, rodar=None, tempo: int = TEMPO_S,
        parar=None) -> tuple[str, str]:
    """Roda o pedido e devolve (`ok`, resposta) ou (o motivo da falha, ""). Motivos: `limite`,
    `acesso` (inclui a CLI ausente: nada disso gasta tentativa), `tempo`, `erro` e `parado`."""
    rodar = rodar or _executar
    env = executor.ambiente(token)
    try:
        r = rodar(comando_leitura(executor._executavel(env)), cwd=str(pasta), env=env,
                  input=pedido, timeout=tempo, parar=parar, **executor._opcoes_do_processo())
    except Parado:
        return "parado", ""
    except subprocess.TimeoutExpired:
        return "tempo", ""
    except FileNotFoundError:  # a CLI sumiu: a leitura espera o Lex voltar
        _avisar_uma_vez("sem_claude", "o programa do Lex (claude) não foi encontrado; a leitura "
                        "de prazos espera")
        return "acesso", ""
    except (OSError, subprocess.SubprocessError) as exc:
        # só o tipo: a mensagem de um erro pode trazer o pedido, que tem o texto da publicação
        log.warning("leitura de prazo: %s", type(exc).__name__)
        return "erro", ""
    try:
        dados = json.loads(r.stdout or "")
    except ValueError:
        dados = None
    resultado = dados.get("result") if isinstance(dados, dict) else None
    do_cli = isinstance(dados, dict) and dados.get("is_error") is True  # erro dito pela CLI
    if (r.returncode == 0 and isinstance(dados, dict) and dados.get("is_error") is False
            and isinstance(resultado, str)):
        voltas = dados.get("num_turns")
        if isinstance(voltas, int) and voltas > 1:
            return "erro", ""  # mais de uma volta: não foi a chamada sem ferramentas pedida
        if _e_mensagem_de_limite(resultado):
            return "limite", ""  # o limite do plano veio como texto, com código de saída 0
        return "ok", resultado
    # a resposta do modelo (is_error falso) nunca decide limite nem acesso: só a CLI e o stderr
    envelope = resultado if do_cli and isinstance(resultado, str) else ""
    return _classificar(f"{envelope}\n{r.stderr or ''}"), ""


def _avisar_uma_vez(chave: str, mensagem: str) -> None:
    if chave not in _AVISADO:
        _AVISADO.add(chave)
        log.warning(mensagem)


def _avisar(painel, prazo_id: int, motivo: str, resposta: str, reserva: str | None) -> str:
    """Devolve o resultado ao painel, sempre com a reserva que recebeu. Um 409 (outro leitor
    terminou, ou o advogado preencheu à mão) é só registrado: a resposta atrasada não faz falta.
    Resposta que o servidor recusa por tamanho (400) vira falha de leitura, que conta tentativa:
    sem isso o painel a entregaria de novo, sem fim. Devolve o motivo que valeu."""
    try:
        if motivo == "ok":
            try:
                painel.responder_leitura(prazo_id, resposta, reserva)
                return motivo
            except PainelErro as exc:
                if exc.status != 400:
                    raise
                log.warning("leitura de prazo: o painel recusou a resposta (400)")
                motivo = "erro"
        painel.falhar_leitura(prazo_id, motivo, reserva)
    except PainelErro as exc:
        if exc.status != 409:
            raise
        log.info("leitura de prazo: o prazo %s já não espera esta leitura", prazo_id)
    return motivo


def _avisar_esperando_o_banco(painel, prazo_id: int, motivo: str, resposta: str,
                              reserva: str | None, dormir, parar) -> str | None:
    """`_avisar`, com o banco do painel ocupado (503) esperando e tentando de novo. Parado no meio,
    devolve None: a reserva vence sozinha. Se o banco não soltar, o erro sobe e o laço espera."""
    tentativa = 1
    while True:
        try:
            return _avisar(painel, prazo_id, motivo, resposta, reserva)
        except PainelErro as exc:
            if exc.status != 503:
                raise
            if parar is not None and parar():
                return None
            if tentativa >= TENTATIVAS_BANCO_OCUPADO:
                raise
        log.info("leitura de prazo: banco do painel ocupado; tento de novo")
        dormir(ESPERA_BANCO_OCUPADO_S)
        tentativa += 1


def uma_leitura(painel, token_fn, pasta: Path, *, rodar=None, exigir_token: bool = True,
                parar=None, dormir=time.sleep) -> str:
    """Trata uma publicação da fila. Sem acesso guardado (no Mac, ou o de «Conectar o Lex» no modo
    local), ou sem o programa do Lex, nem consulta a fila: nada é reservado, nada é gasto, as
    leituras esperam e o prazo segue «a definir»."""
    token = token_fn() or ""
    if not token and exigir_token:
        return "sem_acesso"
    if not executor.localizar_claude(executor.ambiente(token)):
        _avisar_uma_vez("sem_claude", "o programa do Lex (claude) não foi encontrado; a leitura "
                        "de prazos espera")
        return "sem_claude"
    _AVISADO.discard("sem_claude")
    pacote = painel.proxima_leitura()
    if not pacote:
        return "vazia"
    prazo_id, reserva = int(pacote["id"]), pacote.get("reserva")
    motivo, resposta = ler(str(pacote["pedido"]), token, pasta, rodar=rodar, parar=parar)
    if motivo == "parado":  # o serviço está parando: a reserva vence sozinha
        return "parado"
    motivo = _avisar_esperando_o_banco(painel, prazo_id, motivo, resposta, reserva, dormir, parar)
    if motivo is None:
        return "parado"
    if motivo == "limite":  # só a linha e o estado: nada do texto da resposta
        log.warning("leitura de prazo: limite do plano na linha %s", prazo_id)
    return {"limite": "limite", "acesso": "acesso", "ok": "lida"}.get(motivo, "falha")


_PAUSAS = {"limite": PAUSA_LIMITE_S, "acesso": PAUSA_ACESSO_S, "sem_claude": PAUSA_ACESSO_S,
           "lida": PAUSA_ENTRE_LEITURAS, "parado": 0}


def rodar_leituras(obter_painel, token_fn, pasta: Path, *, parar, dormir=time.sleep,
                   rodar=None, exigir_token: bool = True) -> None:
    """O laço: uma leitura de cada vez, com `PAUSA_ENTRE_LEITURAS` entre elas; sem fila, a cada
    minuto. `dormir` tem de acabar com o sinal de `parar`. Nada aqui derruba o serviço nem mexe no laço do squad. Chave recusada (401) espera dez minutos,
    para não contar uma chave inválida por minuto."""
    while not parar():
        espera = INTERVALO_S
        try:
            painel = obter_painel()
            if painel is not None:
                Path(pasta).mkdir(parents=True, exist_ok=True)
                resultado = uma_leitura(painel, token_fn, pasta, rodar=rodar,
                                        exigir_token=exigir_token, parar=parar, dormir=dormir)
                espera = _PAUSAS.get(resultado, INTERVALO_S)
        except PainelErro as exc:
            if exc.status == 401:
                espera = PAUSA_ACESSO_S
            log.warning("leitura de prazo: o painel respondeu %s", exc.status)
        except Exception as exc:  # painel fora do ar: tenta de novo depois. Só o tipo vai ao
            log.warning("leitura de prazo: %s", type(exc).__name__)  # registro, nunca o texto
        if espera:
            dormir(espera)


def em_thread(obter_painel, token_fn, pasta: Path, *, parar, dormir=time.sleep,
              exigir_token: bool = True) -> threading.Thread:
    thread = threading.Thread(target=rodar_leituras, args=(obter_painel, token_fn, pasta),
                              kwargs={"parar": parar, "dormir": dormir,
                                      "exigir_token": exigir_token},
                              name="leitura-de-prazos", daemon=True)
    thread.start()
    return thread
