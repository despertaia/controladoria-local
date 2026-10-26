"""python -m ponte_mac rodar | uma | guardar-chave | estado"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ponte_mac import chaves, laco
from ponte_mac.cliente import Painel, PainelErro

CONFIG = Path("~/.config/controladoria-ponte.json").expanduser()
ESTADO = Path("~/.config/controladoria-ponte-estado.json").expanduser()
PADRAO = {"painel": "https://processos.despertaia.com.br",
          "casa": "~/Desktop/Claude Cowork/Processos pendentes"}
_CHAVE_VALIDA = re.compile(r"[A-Za-z0-9_-]{40,}")
ESPERA_CONFIG_S = 300  # configuração ou chave com problema: tenta de novo em 5 min


def ler_configuracao(caminho: Path = CONFIG) -> dict:
    cfg = dict(PADRAO)
    try:
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        dados = {}
    if isinstance(dados, dict):
        cfg.update({k: str(v) for k, v in dados.items() if k in PADRAO and v})
    cfg["painel"] = cfg["painel"].rstrip("/")
    cfg["casa"] = str(Path(cfg["casa"]).expanduser())
    return cfg


def _colar() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10).stdout


def _copiar(texto: str) -> None:
    subprocess.run(["pbcopy"], input=texto, text=True, timeout=10)


ROTULO_SERVICO = "br.com.despertaia.controladoria-ponte"


def _iniciar_servico(rodar=subprocess.run) -> bool:
    """Sobe o serviço (se instalado e parado); falha em silêncio. Sem `-k`: um serviço já
    rodando não é morto no meio de uma execução; ele relê a chave sozinho no próximo 401."""
    try:
        r = rodar(["launchctl", "kickstart", f"gui/{os.getuid()}/{ROTULO_SERVICO}"],
                  capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def guardar_chave(colar=_colar, copiar=_copiar, reiniciar=_iniciar_servico) -> int:
    """Lê a chave da ponte da área de transferência, guarda nas Chaves e limpa a área;
    depois sobe o serviço, se ele estiver instalado e parado."""
    valor = (colar() or "").strip()
    if not _CHAVE_VALIDA.fullmatch(valor):
        print("A área de transferência não tem uma chave da ponte válida. Copie a chave "
              "mostrada nas Configurações do Lex e rode de novo.")
        return 1
    try:
        chaves.guardar(chaves.CHAVE_PONTE, valor)
    finally:
        copiar("")
        valor = ""
    print(f"Pronto: chave da ponte guardada nas Chaves do macOS ({chaves.CHAVE_PONTE}) "
          "e área de transferência limpa.")
    if reiniciar():
        print("Serviço da ponte ativo; ele passa a usar a chave nova.")
    return 0


def _painel(cfg: dict) -> Painel | None:
    chave = chaves.ler(chaves.CHAVE_PONTE)
    if not chave:
        logging.getLogger("ponte_mac").error(
            "a chave da ponte não está nas Chaves do macOS; rode: python -m ponte_mac guardar-chave")
        return None
    try:
        return Painel(cfg["painel"], chave)
    except ValueError as exc:
        logging.getLogger("ponte_mac").error("configuração do painel inválida: %s", exc)
        return None


def esperar_painel(ler_cfg=None, criar=None, dormir=time.sleep,
                   parar=lambda: False) -> tuple[dict, Painel] | None:
    """No modo contínuo (launchd), configuração inválida ou chave ausente não encerram o
    serviço: registra no log e tenta de novo a cada 5 min, relendo a configuração."""
    ler_cfg, criar = ler_cfg or ler_configuracao, criar or _painel
    while not parar():
        cfg = ler_cfg()
        painel = criar(cfg)
        if painel is not None:
            return cfg, painel
        logging.getLogger("ponte_mac").warning(
            "nova tentativa em %d min", ESPERA_CONFIG_S // 60)
        dormir(ESPERA_CONFIG_S)
    return None


def _estado(cfg: dict) -> int:
    print(f"Painel: {cfg['painel']}")
    print(f"Casa da Banca: {cfg['casa']} ({'existe' if Path(cfg['casa']).is_dir() else 'NÃO existe'})")
    for rotulo, servico in (("Chave da ponte", chaves.CHAVE_PONTE),
                            ("Acesso do Claude", chaves.ACESSO_CLAUDE)):
        print(f"{rotulo}: {'guardado' if chaves.ler(servico) else 'ausente'} ({servico})")
    pendentes = laco.ler_estado(ESTADO)
    print("Execuções a retomar: " + (", ".join(
        f"cartão {d} ({v.get('squad')}{' · ' + v['run_id'] if v.get('run_id') else ''})"
        for d, v in pendentes.items() if isinstance(v, dict)) or "nenhuma"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ponte_mac",
                                     description="Serviço do Mac da ponte com o Lex.")
    parser.add_argument("acao", choices=["rodar", "uma", "guardar-chave", "estado"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.acao == "guardar-chave":
        return guardar_chave()
    cfg = ler_configuracao()
    if args.acao == "estado":
        return _estado(cfg)
    token_fn = lambda: chaves.ler(chaves.ACESSO_CLAUDE)  # noqa: E731 (lido a cada volta)
    if args.acao == "rodar":
        try:
            pronto = esperar_painel()
            if pronto is None:
                return 0
            cfg, painel = pronto
            laco.rodar(painel, Path(cfg["casa"]), token_fn, ESTADO,
                       recriar=lambda: _painel(cfg),
                       reler_chave=lambda: chaves.ler(chaves.CHAVE_PONTE))
        except KeyboardInterrupt:
            pass
        return 0
    painel = _painel(cfg)
    if painel is None:
        return 2
    try:  # "uma": uma volta só
        laco.uma_volta(painel, Path(cfg["casa"]), token_fn, ESTADO)
    except (PainelErro, OSError, ValueError) as exc:
        logging.getLogger("ponte_mac").error("painel indisponível: %s",
                                             laco.executor_mod.limpar_detalhe(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
