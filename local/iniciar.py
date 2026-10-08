"""Lançador da Controladoria no computador do advogado (Windows e Mac).

Uso:
    python -m local.iniciar [--abrir] [--porta N] [--sem-navegador]

Sobe o painel (waitress) em 127.0.0.1, o trabalhador da fila e o agendador das
varreduras (06h, 12h e 18h em dias úteis, no fuso do tribunal), tudo num processo só.
Se a Controladoria já estiver rodando nesta porta, só abre o navegador e sai.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTA_PADRAO = 5056
AJUSTES_PADRAO = os.path.join("dados", "ajustes.json")
LOG = os.path.join("dados", "controladoria.log")
HORARIOS = (6, 12, 18)  # horas cheias das varreduras automáticas
DIAS_UTEIS = range(0, 5)  # segunda (0) a sexta (4)

log = logging.getLogger("controladoria.local")


def _ajustes_locais():
    """O módulo de ajustes locais (cofre + dados/ajustes.json), se presente."""
    try:
        from nucleo import ajustes_locais
    except ImportError:
        return None
    return ajustes_locais


# --- agendador ---------------------------------------------------------------------


def proximo_disparo(agora: datetime, fuso: tzinfo | str | None = None) -> datetime:
    """O próximo horário de varredura estritamente depois de `agora`: 06h, 12h ou 18h
    de segunda a sexta, no fuso indicado (padrão: o do tribunal do escritório).
    `agora` sem fuso é lido como hora local desse fuso; o resultado vem nesse fuso."""
    if fuso is None:
        from nucleo import tribunal
        zona = tribunal.fuso()
    elif isinstance(fuso, str):
        zona = ZoneInfo(fuso)
    else:
        zona = fuso
    agora = agora.replace(tzinfo=zona) if agora.tzinfo is None else agora.astimezone(zona)
    dia = agora.date()
    for _ in range(8):
        if dia.weekday() in DIAS_UTEIS:
            for hora in HORARIOS:
                alvo = datetime(dia.year, dia.month, dia.day, hora, tzinfo=zona)
                if alvo > agora:
                    return alvo
        dia += timedelta(days=1)
    raise AssertionError("sem disparo em 8 dias")  # impossível: toda semana tem dia útil


def disparo_anterior(agora: datetime, fuso: tzinfo | str | None = None) -> datetime:
    """O horário de varredura mais recente até `agora` (inclusive), no mesmo critério de
    `proximo_disparo`."""
    if fuso is None:
        from nucleo import tribunal
        zona = tribunal.fuso()
    elif isinstance(fuso, str):
        zona = ZoneInfo(fuso)
    else:
        zona = fuso
    agora = agora.replace(tzinfo=zona) if agora.tzinfo is None else agora.astimezone(zona)
    dia = agora.date()
    for _ in range(8):
        if dia.weekday() in DIAS_UTEIS:
            for hora in reversed(HORARIOS):
                alvo = datetime(dia.year, dia.month, dia.day, hora, tzinfo=zona)
                if alvo <= agora:
                    return alvo
        dia -= timedelta(days=1)
    raise AssertionError("sem disparo em 8 dias")  # impossível: toda semana tem dia útil


def varredura_atrasada(conn, agora: datetime) -> bool:
    """Passou um horário de varredura sem nenhuma pedida desde então (o computador estava
    desligado). Instalação sem nenhuma varredura ainda não conta: a primeira sai da
    Configuração."""
    linha = conn.execute(
        "SELECT MAX(pedida_em) FROM tarefa WHERE tipo = 'varredura' "
        "AND estado != 'cancelada'").fetchone()
    if not linha or not linha[0]:
        return False
    try:
        ultima = datetime.fromisoformat(linha[0])
    except ValueError:
        return False
    anterior = disparo_anterior(agora)
    if ultima.tzinfo is None:
        ultima = ultima.replace(tzinfo=anterior.tzinfo)
    return ultima < anterior


def _configurado() -> bool:
    ajustes = _ajustes_locais()
    if ajustes is None:
        return True  # sem a tela de configuração: quem decide é o trabalhador
    try:
        return bool(ajustes.configurado())
    except Exception:  # noqa: BLE001 — cofre indisponível conta como não configurado
        log.exception("não consegui conferir a configuração")
        return False


def pedir_varredura(conectar) -> bool:
    from nucleo import tarefas_fila
    conn = conectar()
    try:
        with conn:
            return tarefas_fila.pedir(conn, "varredura", por="agendador") is not None
    finally:
        conn.close()


def rodar_agendador(conectar, parado: threading.Event, *, agora_fn=None,
                    espera_maxima: float = 60.0) -> None:
    """Põe uma varredura na fila em cada horário. Reavalia a cada minuto (o computador
    pode ter dormido; o fuso pode mudar ao configurar). Horário perdido com o computador
    dormindo dispara uma vez ao acordar; com ele desligado, dispara uma vez ao iniciar."""
    def agora():
        from nucleo import tribunal
        return agora_fn() if agora_fn else datetime.now(tribunal.fuso())

    inicio = agora()
    alvo = proximo_disparo(inicio)
    try:  # ligou depois de um horário perdido (computador desligado): varre agora
        conn = conectar()
        try:
            atrasada = varredura_atrasada(conn, inicio)
        finally:
            conn.close()
        if atrasada and _configurado() and pedir_varredura(conectar):
            log.info("agendador: o computador estava desligado no último horário; "
                     "varredura posta na fila agora")
    except Exception:  # noqa: BLE001 — o agendador não pode morrer
        log.exception("agendador: não consegui conferir a varredura atrasada")
    log.info("agendador: próxima varredura em %s", alvo.isoformat(timespec="minutes"))
    while not parado.is_set():
        instante = agora()
        if instante >= alvo:
            try:
                if not _configurado():
                    log.info("agendador: varredura pulada, a Configuração ainda não foi feita")
                elif pedir_varredura(conectar):
                    log.info("agendador: varredura posta na fila")
                else:
                    log.info("agendador: já havia uma varredura na fila")
            except Exception:  # noqa: BLE001 — o agendador não pode morrer
                log.exception("agendador: não consegui pedir a varredura")
            alvo = proximo_disparo(instante)
            log.info("agendador: próxima varredura em %s", alvo.isoformat(timespec="minutes"))
            continue
        parado.wait(max(0.05, min(espera_maxima, (alvo - instante).total_seconds())))


# --- instância única ---------------------------------------------------------------


def url_do_painel(porta: int) -> str:
    return f"http://127.0.0.1:{porta}"


def ja_rodando(porta: int, timeout: float = 2.0) -> bool:
    """True se a Controladoria já responde nesta porta (o /saude devolve o JSON dela,
    mesmo com 503)."""
    try:
        with urllib.request.urlopen(url_do_painel(porta) + "/saude", timeout=timeout) as r:
            corpo = r.read()
    except urllib.error.HTTPError as exc:
        corpo = exc.read() if exc.code == 503 else b""
    except (OSError, ValueError):
        return False
    try:
        dados = json.loads(corpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(dados, dict) and "ok" in dados and (
        "trabalhador" in dados or "erro" in dados)


# --- preparação do ambiente ----------------------------------------------------------


def _caminho_ajustes() -> str:
    ajustes = _ajustes_locais()
    if ajustes is not None and hasattr(ajustes, "caminho"):
        return str(ajustes.caminho())
    return AJUSTES_PADRAO


def ler_porta(argumento: int | None) -> int:
    """--porta, senão a porta dos ajustes locais, senão 5056."""
    if argumento:
        return argumento
    ajustes = _ajustes_locais()
    if ajustes is not None and hasattr(ajustes, "porta_do_painel"):
        porta = ajustes.porta_do_painel()
    else:
        try:
            with open(AJUSTES_PADRAO, encoding="utf-8") as f:
                porta = int(json.load(f).get("porta") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            porta = 0
    return porta if 0 < porta < 65536 else PORTA_PADRAO


def primeira_execucao() -> bool:
    return not os.path.exists(_caminho_ajustes())


def configurar_log(caminho: str = LOG) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    formato = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    arquivo = logging.handlers.RotatingFileHandler(
        caminho, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    arquivo.setFormatter(formato)
    raiz.addHandler(arquivo)
    if sys.stderr is not None:
        tela = logging.StreamHandler()
        tela.setFormatter(formato)
        raiz.addHandler(tela)

    def _excecao_em_thread(args):
        log.error("erro na thread %s", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _excecao_em_thread


def preparar_ambiente() -> None:
    """Diretório = raiz do app; modo local sem login; .env e ajustes no ambiente. Precisa
    rodar ANTES de importar o painel (ele lê SECRET_KEY e PAINEL_SENHA no import)."""
    os.chdir(RAIZ)
    if RAIZ not in sys.path:
        sys.path.insert(0, RAIZ)
    os.environ["CONTROLADORIA_LOCAL"] = "1"
    os.environ["PAINEL_SENHA"] = ""
    if os.path.exists(".env"):
        from dotenv import load_dotenv
        load_dotenv(".env")  # não sobrescreve o que já está no ambiente
    ajustes = _ajustes_locais()
    if ajustes is not None:
        try:
            ajustes.carregar_no_ambiente()
        except Exception:  # noqa: BLE001 — cofre indisponível: o painel pede a Configuração
            log.exception("não consegui carregar os ajustes locais")
    if not os.environ.get("SECRET_KEY"):
        # Sem chave guardada: uma por processo (só as sessões do navegador recomeçam).
        os.environ["SECRET_KEY"] = secrets.token_hex(32)


def _silenciar_saidas_ausentes() -> None:
    """Com pythonw (Windows, sem janela) stdout/stderr são None: algumas bibliotecas
    escrevem neles e quebrariam."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


# --- servidor, trabalhador e agendador -------------------------------------------------


def criar_servidor(porta: int, host: str = "127.0.0.1"):
    import waitress
    import painel
    return waitress.create_server(painel.app, host=host, port=porta, threads=6,
                                  ident="Controladoria")


def iniciar_trabalhador(parado: threading.Event, pausa: float = 10.0) -> threading.Thread:
    from nucleo import banco, carteira, painel_dados
    from nucleo import trabalhador as nucleo_trabalhador

    def alvo():
        while not parado.is_set():
            try:
                nucleo_trabalhador.rodar(banco.conectar, adv_fn=carteira.advogado_do_env,
                                         hoje_fn=painel_dados.hoje_cuiaba,
                                         pausa=pausa, parar=parado.is_set)
            except Exception:  # noqa: BLE001 — banco travado etc.: recomeça daqui a pouco
                log.exception("trabalhador caiu; recomeçando em 30 s")
                parado.wait(30)

    fio = threading.Thread(target=alvo, name="trabalhador", daemon=True)
    fio.start()
    return fio


def iniciar_agendador(parado: threading.Event) -> threading.Thread:
    from nucleo import banco

    def alvo():
        while not parado.is_set():
            try:
                rodar_agendador(banco.conectar, parado)
            except Exception:  # noqa: BLE001
                log.exception("agendador caiu; recomeçando em 60 s")
                parado.wait(60)

    fio = threading.Thread(target=alvo, name="agendador", daemon=True)
    fio.start()
    return fio


def abrir_navegador(porta: int, atraso: float = 1.0) -> None:
    def abrir():
        try:
            webbrowser.open(url_do_painel(porta))
        except Exception:  # noqa: BLE001
            log.exception("não consegui abrir o navegador")
    threading.Timer(atraso, abrir).start()


def iniciar_ponte(porta: int):
    """Ponte do Lex no mesmo processo: só trabalha quando há cartão na coluna Lex e espera
    a casa da Banca aparecer nos ajustes. Devolve (evento, thread) ou None se faltar."""
    try:
        from ponte_mac import local as ponte_local
    except ImportError:
        log.warning("ponte do Lex indisponível nesta instalação")
        return None
    parar = threading.Event()
    try:
        thread = ponte_local.rodar_em_thread(url_do_painel(porta), None, parar)
    except Exception:  # noqa: BLE001 — sem a ponte, o resto da Controladoria segue
        log.exception("não consegui iniciar a ponte do Lex")
        return None
    return ponte_local, parar, thread


def verificar_versao() -> None:
    """Consulta, numa thread, se há versão nova publicada (o painel mostra a faixa).
    Sem rede, nada acontece."""
    try:
        from nucleo import versao
        versao.verificar_em_segundo_plano()
    except Exception:  # noqa: BLE001 — o aviso de versão nunca impede a Controladoria
        log.exception("não consegui verificar a versão publicada")


def parar_ponte(ponte) -> None:
    if ponte is None:
        return
    ponte_local, parar, thread = ponte
    try:
        ponte_local.parar(parar, thread)
    except Exception:  # noqa: BLE001
        log.exception("erro ao parar a ponte do Lex")


def definir_casa(caminho: str) -> int:
    """Grava `casa_banca` em dados/ajustes.json (sem tocar no cofre nem nas senhas)."""
    pasta = os.path.abspath(os.path.expanduser(caminho.strip().strip('"')))
    if not os.path.isdir(pasta):
        print(f"ERRO: a pasta {pasta} não existe. Crie a casa da Banca primeiro.")
        return 1
    ajustes = _ajustes_locais()
    destino = _caminho_ajustes()
    dados = {}
    try:
        with open(destino, encoding="utf-8") as f:
            dados = json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f"ERRO: não consegui ler {destino} ({exc}).")
        return 1
    if not isinstance(dados, dict):
        dados = {}
    dados["casa_banca"] = pasta
    gravar = getattr(ajustes, "_gravar_json", None)  # escrita atômica dos ajustes
    if gravar is not None:
        gravar(dados)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(destino)), exist_ok=True)
        with open(destino, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
    print(f"Casa da Banca definida: {pasta}")
    return 0


def _sinal_para_sair(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m local.iniciar",
                                     description="Inicia a Controladoria neste computador.")
    parser.add_argument("--abrir", action="store_true", help="abre o painel no navegador")
    parser.add_argument("--porta", type=int, default=None, help="porta local (padrão 5056)")
    parser.add_argument("--sem-navegador", action="store_true",
                        help="nunca abre o navegador (início automático)")
    parser.add_argument("--definir-casa", metavar="PASTA", default=None,
                        help="grava a pasta da casa da Banca nos ajustes e sai")
    args = parser.parse_args(argv)

    _silenciar_saidas_ausentes()
    os.chdir(RAIZ)
    if args.definir_casa is not None:
        return definir_casa(args.definir_casa)
    configurar_log()
    preparar_ambiente()
    porta = ler_porta(args.porta)
    abrir = not args.sem_navegador and (args.abrir or primeira_execucao())

    if ja_rodando(porta):
        log.info("a Controladoria já está rodando em %s", url_do_painel(porta))
        print(f"A Controladoria já está aberta em {url_do_painel(porta)}")
        if not args.sem_navegador:
            webbrowser.open(url_do_painel(porta))
        return 0

    try:
        servidor = criar_servidor(porta)
    except OSError as exc:
        log.error("porta %s ocupada: %s", porta, exc)
        print(f"A porta {porta} está ocupada por outro programa. Feche-o ou escolha outra "
              f"porta (python -m local.iniciar --porta 5057).")
        return 2

    parado = threading.Event()
    iniciar_trabalhador(parado)
    iniciar_agendador(parado)
    try:
        signal.signal(signal.SIGTERM, _sinal_para_sair)
    except (ValueError, OSError):
        pass
    log.info("Controladoria no ar em %s", url_do_painel(porta))
    print(f"Controladoria no ar em {url_do_painel(porta)} (Ctrl+C para encerrar)")
    if abrir:
        abrir_navegador(porta)
    verificar_versao()
    ponte = iniciar_ponte(porta)  # a porta já escuta: a ponte conecta assim que o run() girar
    try:
        servidor.run()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("encerrando a Controladoria")
        parado.set()
        parar_ponte(ponte)
        servidor.close()
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
