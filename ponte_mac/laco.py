"""O laço do serviço: consulta o painel a cada minuto e, com cartão, executa o Lex e
devolve o resultado (ou a falha com o motivo)."""

import hashlib
import json
import logging
import os
import signal
import tempfile
import time
from pathlib import Path

from ponte_mac import executor as executor_mod
from ponte_mac.cliente import PainelErro

log = logging.getLogger("ponte_mac.laco")

INTERVALO_S = 60
TENTATIVAS_ENVIO = 3
ESPERA_ENVIO_S = 30

PAUSA_LIMITE_S = 30 * 60  # depois do limite do plano, sem hora informada
PAUSA_MAXIMA_S = 6 * 3600  # hora informada além disto (ou já passada) é ignorada
REVALIDAR_ACESSO_S = 10 * 60  # acesso do Claude recusado: confere de novo a cada 10 min

MOTIVOS = {"falhou": "erro", "tempo": "tempo", "limite": "limite", "sem_tipo": "sem_tipo",
           "acesso": "erro"}
# O painel só recebe frases fixas e curtas; o detalhe cru (do Lex, do Claude, do sistema)
# fica só no registro local. Exceção (ruling): no sem_tipo vai a explicação do Lex,
# higienizada (`executor.explicacao_do_lex`).
_DETALHE_PADRAO = {"erro": "a execução do Lex falhou", "tempo": "passou de 3 h",
                   "limite": executor_mod.DETALHE_LIMITE,
                   "sem_tipo": "o Lex não teve segurança sobre o tipo de peça"}
_FRASES_DE_ERRO = (
    "pacote inválido", "autos indisponíveis", "o squad não é deste caso; pacote recusado",
    "pacote da peça não encontrado", "o Lex não gerou pacote novo para este caso",
    "ajuste fora da execução anterior", "ajuste sem a execução anterior",
    "o MANIFESTO do pacote é de outro run; pacote recusado",
    "manifesto do Citation Gate não encontrado ou ilegível", "o painel recusou o pacote",
    executor_mod.DETALHE_ACESSO, executor_mod.DETALHE_SEM_CLAUDE,
    "o Lex terminou sem a linha final", "erro no Mac",
)
# depois destes, a demanda não tem mais nada a retomar no disco
_ENCERRAM = {"pronto", "sem_tipo"}


# --- estado local (demanda -> squad/run para retomar) ----------------------------------

def ler_estado(caminho: Path) -> dict:
    try:
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return dados if isinstance(dados, dict) else {}


def _gravar_estado(caminho: Path, dados: dict) -> None:
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".estado-", dir=caminho.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(tmp, caminho)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _anotar(caminho: Path, demanda: int, valor: dict | None) -> None:
    dados = ler_estado(caminho)
    if valor is None:
        if dados.pop(str(demanda), None) is None:
            return
    else:
        if dados.get(str(demanda)) == valor:
            return
        dados[str(demanda)] = valor
    try:
        _gravar_estado(caminho, dados)
    except OSError as exc:
        log.warning("não consegui gravar o estado local: %s", executor_mod.limpar_detalhe(exc))


# --- uma volta -------------------------------------------------------------------------

def _enviar_resultado(painel, demanda: int, n: int, res: dict, dormir) -> str:
    """Envia o pacote: "enviado", "fora" (409: o cartão não é mais deste Mac), "recusado"
    (outra recusa do painel) ou "rede" (não chegou depois das tentativas)."""
    for tentativa in range(1, TENTATIVAS_ENVIO + 1):
        try:
            painel.resultado(demanda, n, res["campos"], res["arquivos"])
            return "enviado"
        except PainelErro as exc:
            if exc.status < 500:
                log.error("o painel recusou o resultado do cartão %s: %s", demanda,
                          executor_mod.limpar_detalhe(exc))
                if exc.status == 409:
                    return "fora"
                if exc.status == 400:  # pacote inválido: avisa como falha, não fica preso
                    _falhar(painel, demanda, n, "erro",
                            f"o painel recusou o pacote: {exc.mensagem or exc.status}")
                return "recusado"
            erro = exc
        except OSError as exc:
            erro = exc
        log.warning("envio do resultado do cartão %s falhou (tentativa %d): %s", demanda,
                    tentativa, executor_mod.limpar_detalhe(erro))
        if tentativa < TENTATIVAS_ENVIO:
            dormir(ESPERA_ENVIO_S)
    return "rede"


def frase_da_falha(motivo: str, detalhe: str) -> str:
    """A frase fixa que vai ao painel: a conhecida com que o detalhe começa (só em `erro`)
    ou a padrão do motivo. No sem_tipo, a explicação do Lex higienizada."""
    if motivo == "sem_tipo":
        return executor_mod.explicacao_do_lex(detalhe) or _DETALHE_PADRAO[motivo]
    limpo = executor_mod.limpar_detalhe(detalhe)
    if motivo == "erro":
        for frase in _FRASES_DE_ERRO:
            if limpo.startswith(frase):
                return frase
    return _DETALHE_PADRAO[motivo]


def _falhar(painel, demanda: int, n: int, motivo: str, detalhe: str) -> None:
    if detalhe:
        log.info("cartão %s: falha %s: %s", demanda, motivo,
                 executor_mod.limpar_detalhe(detalhe))
    detalhe = frase_da_falha(motivo, detalhe)
    try:
        painel.falha(demanda, n, motivo, detalhe)
    except (PainelErro, OSError) as exc:
        log.warning("aviso de falha do cartão %s não chegou ao painel: %s", demanda,
                    executor_mod.limpar_detalhe(exc))


_CANCELA = {401, 404, 409}  # o cartão não é (ou não é mais) deste Mac
_CANCELA_NA_EXECUCAO = {404, 409}  # 401 com o Lex rodando: chave trocada, relê e segue


def _trocar_chave(painel, reler_chave) -> bool:
    """Depois de um 401, relê a chave das Chaves e passa a usá-la no mesmo painel."""
    if reler_chave is None or not hasattr(painel, "trocar_chave"):
        return False
    try:
        nova = reler_chave()
    except Exception as exc:  # o Chaves pode falhar; a execução segue
        log.warning("não consegui reler a chave da ponte: %s", executor_mod.limpar_detalhe(exc))
        return False
    if not nova:
        return False
    painel.trocar_chave(nova)
    log.info("chave da ponte relida das Chaves do macOS")
    return True


def _episodio(pacote: dict) -> str:
    """O envio ao Lex que gerou esta reserva (servidor antigo não manda: "")."""
    valor = pacote.get("episodio")
    if isinstance(valor, bool) or not isinstance(valor, (int, str)):
        return ""
    return str(valor).strip()[:64]


def _pacote_valido(pacote: dict) -> bool:
    if pacote.get("modo") not in ("novo", "ajuste") or not str(pacote.get("numero") or ""):
        return False
    for chave in ("publicacoes", "intimacoes"):
        lista = pacote.get(chave) or []
        if not isinstance(lista, list) or not all(isinstance(i, dict) for i in lista):
            return False
    return True


def _contato(painel, claude_ok: bool) -> None:
    """Avisa o painel que o Mac está vivo (e se o acesso do Claude está bom)."""
    try:
        painel.contato(claude_ok)
    except (PainelErro, OSError) as exc:
        log.warning("contato com o painel falhou: %s", executor_mod.limpar_detalhe(exc))


def _digital(token: str) -> str:
    """Impressão curta do token, só em memória, para notar que ele foi trocado."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _acesso_ok(situacao: dict, token: str, casa: Path, agora: float, validar) -> bool:
    """Depois de um erro de autenticação do Claude, nada é reservado até o acesso voltar:
    confere de novo a cada 10 min, ou logo, se o token nas Chaves mudou."""
    recusa = situacao.get("acesso_recusado")
    if not recusa:
        return True
    digital = _digital(token)
    if digital == recusa.get("token") and agora < recusa.get("revalidar_em", 0):
        return False
    if validar(token, casa) is True:
        situacao.pop("acesso_recusado", None)
        log.info("o acesso do Claude voltou; reservas retomadas")
        return True
    recusa.update(token=digital, revalidar_em=agora + REVALIDAR_ACESSO_S)
    log.warning("o acesso do Claude continua recusado; nova conferência em %d min",
                REVALIDAR_ACESSO_S // 60)
    return False


def uma_volta(painel, casa: Path, token_fn, estado_path: Path, *, dormir=time.sleep,
              executar=executor_mod.executar, relogio=time.time, situacao: dict | None = None,
              validar=executor_mod.validar_acesso, reler_chave=None,
              exigir_token: bool = True) -> bool:
    """Consulta o painel uma vez e trata o cartão reservado, se houver. Devolve True se
    tratou um cartão. Erros de rede ao consultar sobem para quem chamou.
    `reler_chave()` devolve a chave da ponte guardada nas Chaves (ou None): um 401 numa
    batida durante a execução não a cancela; a chave é relida e a próxima chamada a usa.
    `situacao` (do laço, só em memória) guarda a pausa do limite do plano (`pausa_ate`) e
    a recusa do acesso do Claude (`acesso_recusado`) entre as voltas.
    Sem `exigir_token` (modo local), sem acesso guardado no cofre o Lex roda com o login
    normal do `claude` do usuário."""
    situacao = {} if situacao is None else situacao
    agora = relogio()
    token = token_fn() or ""
    if not token and exigir_token:
        log.warning("o acesso do Claude não está nas Chaves do macOS; nada reservado")
        _contato(painel, False)
        return False
    recusado_antes = bool(situacao.get("acesso_recusado"))
    if not _acesso_ok(situacao, token, casa, agora, validar):
        _contato(painel, False)
        return False
    if recusado_antes:  # o acesso voltou: o painel sabe já, sem esperar a volta seguinte
        _contato(painel, True)
    if situacao.get("pausa_ate"):
        if agora < situacao["pausa_ate"]:
            _contato(painel, True)
            return False
        situacao.pop("pausa_ate", None)
        log.info("fim da pausa pelo limite do plano; reservas retomadas")
    pacote = painel.proximo()
    if not pacote:
        _contato(painel, True)
        return False
    try:
        demanda, n = int(pacote["demanda"]), int(pacote["n"])
    except (KeyError, TypeError, ValueError):
        log.error("o painel mandou um pacote inválido (sem cartão ou execução)")
        return False
    if not _pacote_valido(pacote):
        log.error("o painel mandou um pacote inválido para o cartão %s", demanda)
        _falhar(painel, demanda, n, "erro", "pacote inválido")
        return True
    log.info("cartão %s reservado (execução %s, modo %s)", demanda, n, pacote.get("modo"))
    try:
        painel.batida(demanda, n, "", "preparando o caso", squad_nome="")
    except PainelErro as exc:
        if exc.status in _CANCELA:
            log.warning("cartão %s não é deste Mac (painel %s); nada executado", demanda,
                        exc.status)
            if exc.status == 404:
                _anotar(estado_path, demanda, None)
            return True
        log.warning("batida do cartão %s falhou: %s", demanda, executor_mod.limpar_detalhe(exc))
    except OSError as exc:
        log.warning("batida do cartão %s falhou: %s", demanda, executor_mod.limpar_detalhe(exc))
    try:
        autos_zip = painel.autos(demanda)
    except PainelErro as exc:
        if exc.status in (401, 409):  # o cartão saiu deste Mac: nada a avisar
            log.warning("autos do cartão %s: painel %s; nada executado", demanda, exc.status)
            return True
        if exc.status == 404:
            _falhar(painel, demanda, n, "erro", "autos indisponíveis")
            return True
        raise

    # `desde`: início da 1ª execução deste episódio da demanda (no ajuste, desta execução);
    # pacote ou run mais antigo não é deste caso. Episódio = um envio ao Lex: só se retoma
    # dentro do mesmo; episódio novo ou desconhecido (servidor antigo) começa do zero.
    episodio = _episodio(pacote)
    base = {"desde": relogio(), "pasta": str(executor_mod.pasta_do_caso(casa, pacote)),
            "episodio": episodio}
    entrada = ler_estado(estado_path).get(str(demanda))
    mesmo_episodio = (isinstance(entrada, dict) and bool(episodio)
                      and str(entrada.get("episodio") or "") == episodio)
    if pacote.get("modo") == "ajuste" or not mesmo_episodio \
            or not isinstance(entrada.get("desde"), (int, float)):
        if isinstance(entrada, dict) and entrada.get("squad") and pacote.get("modo") != "ajuste":
            log.info("cartão %s: envio novo ao Lex; a execução anterior não é retomada", demanda)
        entrada = dict(base)
    pasta = Path(base["pasta"])  # a mesma que o executor usa
    # só se retoma um run validado (squad deste caso + run iniciado nesta demanda)
    retomar = ({"squad": entrada["squad"], "run_id": entrada["run_id"]}
               if pacote.get("modo") != "ajuste" and entrada.get("squad")
               and entrada.get("run_id") else None)
    _anotar(estado_path, demanda, entrada)
    desde = float(entrada["desde"])
    cancelado: dict = {}

    def anotar_squad(squad: str) -> None:
        """Grava o squad só se o caso.json dele aponta para este caso, e o run só se o
        ledger confirma que ele começou nesta demanda; o que o Lex diz não basta."""
        if not executor_mod.squad_do_caso(casa, squad, pasta):
            log.warning("cartão %s: o squad informado não é deste caso; não guardado", demanda)
            return
        atual = ler_estado(estado_path).get(str(demanda)) or entrada
        anterior = atual.get("run_id", "") if atual.get("squad") == squad else ""
        run = executor_mod.run_atual(casa, squad, desde) or anterior
        _anotar(estado_path, demanda, {**entrada, "squad": squad, "run_id": run})

    def ao_progresso(squad: str, etapa: str) -> None:
        if squad:
            anotar_squad(squad)
        try:
            painel.batida(demanda, n, squad, etapa,
                          squad_nome=executor_mod.nome_do_squad(casa, squad) if squad else "")
        except PainelErro as exc:
            if exc.status in _CANCELA_NA_EXECUCAO:
                cancelado["status"] = exc.status
                raise executor_mod.Cancelado(f"o painel respondeu {exc.status}") from None
            if exc.status == 401:
                log.warning("batida do cartão %s: o painel recusou a chave; relendo a chave "
                            "e seguindo com a execução", demanda)
                _trocar_chave(painel, reler_chave)
                return
            log.warning("batida do cartão %s falhou: %s", demanda,
                        executor_mod.limpar_detalhe(exc))
        except OSError as exc:
            log.warning("batida do cartão %s falhou: %s", demanda,
                        executor_mod.limpar_detalhe(exc))

    try:
        res = executar(Path(casa), pacote, autos_zip, token, ao_progresso, retomar=retomar,
                       desde=desde)
    except Exception as exc:  # qualquer quebra vira falha visível no painel
        log.error("a execução do cartão %s quebrou: %s", demanda,
                  executor_mod.limpar_detalhe(exc))
        _falhar(painel, demanda, n, "erro", f"erro no Mac: {exc}")
        return True

    status = res.get("status")
    if status == "cancelado":
        log.warning("cartão %s: execução cancelada (painel %s); nada enviado", demanda,
                    cancelado.get("status"))
        if cancelado.get("status") == 404:  # o cartão sumiu: nada a retomar
            _anotar(estado_path, demanda, None)
        return True
    if res.get("squad"):
        anotar_squad(res["squad"])
    if getattr(painel, "chave_recusada", False):  # 401 sem chave nova ainda: tenta de novo
        _trocar_chave(painel, reler_chave)
    if status == "pronto":
        nome = executor_mod.nome_do_squad(casa, res.get("squad") or "")
        if nome:
            res = {**res, "campos": {**res.get("campos", {}), "squad_nome": nome}}
        envio = _enviar_resultado(painel, demanda, n, res, dormir)
        if envio == "enviado":
            log.info("cartão %s: peça enviada ao painel", demanda)
            _anotar(estado_path, demanda, None)
        # 409 ("fora") mantém a entrada: se o cartão voltar neste episódio, a retomada
        # aceita o pacote do run conhecido em vez de refazer a peça.
        return True
    motivo = MOTIVOS.get(status, "erro")
    log.info("cartão %s: %s", demanda, motivo)
    _falhar(painel, demanda, n, motivo, res.get("detalhe") or "")
    if status in _ENCERRAM:
        _anotar(estado_path, demanda, None)
    agora = relogio()
    if status == "acesso":  # um cartão volta com erro; os seguintes nem são reservados
        log.error("o Claude recusou o acesso; nada será reservado até ele voltar "
                  "(rode ponte_mac/guardar_acesso_claude.sh)")
        situacao["acesso_recusado"] = {"token": _digital(token),
                                       "revalidar_em": agora + REVALIDAR_ACESSO_S}
        _contato(painel, False)
    elif status == "limite":
        ate = res.get("ate")
        if not (isinstance(ate, (int, float)) and agora < ate <= agora + PAUSA_MAXIMA_S):
            ate = agora + PAUSA_LIMITE_S
        situacao["pausa_ate"] = ate
        log.warning("limite do plano: nada será reservado nos próximos %d min",
                    max(1, round((ate - agora) / 60)))
    return True


def _ao_parar(sinal, _quadro) -> None:
    """SIGTERM/SIGINT (launchctl, Ctrl-C): encerra o grupo do `claude` em andamento antes
    de sair, para não deixar o Lex trabalhando sozinho."""
    if executor_mod.encerrar_em_andamento():
        log.warning("serviço parado (sinal %s): a execução do Lex em andamento foi encerrada",
                    sinal)
    raise SystemExit(0)


def _instalar_tratadores() -> dict:
    anteriores = {}
    for sinal in (signal.SIGTERM, signal.SIGINT):
        try:
            anteriores[sinal] = signal.signal(sinal, _ao_parar)
        except (ValueError, OSError):  # fora da linha principal: segue sem tratador
            pass
    return anteriores


def _restaurar_tratadores(anteriores: dict) -> None:
    for sinal, anterior in anteriores.items():
        try:
            signal.signal(sinal, anterior)
        except (ValueError, OSError, TypeError):
            pass


def rodar(painel, casa: Path, token_fn, estado_path: Path, dormir=time.sleep,
          parar=lambda: False, executar=executor_mod.executar, relogio=time.time,
          recriar=None, validar=executor_mod.validar_acesso, reler_chave=None,
          exigir_token: bool = True) -> None:
    """Laço contínuo: uma volta a cada 60 s; erro de rede vira log e nova tentativa.
    Se o painel recusou a chave (401), `recriar()` relê a chave das Chaves e devolve um
    Painel novo (ou None) antes da volta seguinte: uma chave nova guardada com o serviço
    rodando passa a valer sem reiniciar. SIGTERM/SIGINT encerram o Lex em andamento e
    saem (SystemExit)."""
    anteriores = _instalar_tratadores()
    try:
        _rodar(painel, casa, token_fn, estado_path, dormir, parar, executar, relogio,
               recriar, validar, reler_chave, exigir_token)
    finally:
        _restaurar_tratadores(anteriores)


def _rodar(painel, casa, token_fn, estado_path, dormir, parar, executar, relogio, recriar,
           validar, reler_chave, exigir_token=True) -> None:
    try:
        if sobras := executor_mod.limpar_sobras(casa):
            log.info("apagadas %d extrações de autos interrompidas", sobras)
    except OSError as exc:
        log.warning("não consegui limpar as sobras de autos: %s", executor_mod.limpar_detalhe(exc))
    situacao: dict = {}  # pausa do limite e recusa do acesso, só em memória
    while not parar():
        if recriar is not None and getattr(painel, "chave_recusada", False):
            novo = recriar()
            if novo is not None:
                painel = novo
                log.info("chave da ponte relida das Chaves do macOS")
        try:
            uma_volta(painel, casa, token_fn, estado_path, dormir=dormir, executar=executar,
                      relogio=relogio, situacao=situacao, validar=validar,
                      reler_chave=reler_chave, exigir_token=exigir_token)
        except (PainelErro, OSError, ValueError) as exc:
            log.warning("painel indisponível: %s", executor_mod.limpar_detalhe(exc))
        dormir(INTERVALO_S)
