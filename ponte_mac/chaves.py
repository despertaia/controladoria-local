"""Credenciais da ponte no cofre do sistema. Nunca em arquivo, log ou linha de comando.

Dois lugares, conforme quem roda a ponte:

- Modo local (`CONTROLADORIA_LOCAL=1`, a Controladoria no computador do mentorado, Windows
  ou Mac): o cofre da instalação local (`nucleo.cofre`, via `keyring`, serviço
  `br.com.despertaia.controladoria`), com os nomes `ponte_chave` e `lex_claude`, ao lado
  das senhas do PJe.
- Serviço do Mac do escritório (produção): as Chaves do macOS pelo `security`, com os
  nomes de sempre (`controladoria-ponte-chave`, `controladoria-lex-claude`), que o
  `guardar_acesso_claude.sh` e o `instalar.sh` também usam. Não passa pelo `keyring` de
  propósito: o item criado pelo `security add-generic-password` só confia no próprio
  `/usr/bin/security`; lido pelo Python do `.venv` (que muda de caminho a cada
  atualização), o macOS abriria a janela das Chaves e o serviço, sem ninguém na frente,
  ficaria parado nela.

Quem chama usa sempre os nomes de produção (`CHAVE_PONTE`, `ACESSO_CLAUDE`); a troca para
os nomes do modo local é feita aqui."""

import getpass
import os
import re
import subprocess

CHAVE_PONTE = "controladoria-ponte-chave"
ACESSO_CLAUDE = "controladoria-lex-claude"

# nome no cofre da instalação local (nucleo.cofre) ← nome de produção
NOMES_LOCAIS = {CHAVE_PONTE: "ponte_chave", ACESSO_CLAUDE: "lex_claude"}

_VALOR_SEGURO = re.compile(r"[A-Za-z0-9_.-]+")


def modo_local() -> bool:
    return os.getenv("CONTROLADORIA_LOCAL", "").strip() == "1"


def _nome_local(servico: str) -> str:
    try:
        return NOMES_LOCAIS[servico]
    except KeyError:
        raise ValueError("credencial desconhecida da ponte") from None


def _conferir(servico: str, valor: str | None = None) -> None:
    if not _VALOR_SEGURO.fullmatch(servico or "") or (
            valor is not None and not _VALOR_SEGURO.fullmatch(valor or "")):
        raise ValueError("valor ou serviço com caracteres não permitidos")


def ler(servico: str, rodar=subprocess.run) -> str | None:
    """O segredo guardado em `servico`, ou None se não houver (ou o cofre recusar)."""
    if modo_local():
        from nucleo import cofre
        return (cofre.ler(_nome_local(servico)) or "").strip() or None
    try:
        r = rodar(["security", "find-generic-password", "-s", servico, "-w"],
                  capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    valor = (r.stdout or "").strip()
    return valor if r.returncode == 0 and valor else None


def guardar(servico: str, valor: str, rodar=subprocess.run) -> None:
    """Guarda (ou troca) o segredo. No Mac do escritório, pela entrada padrão do
    `security -i`, para que ele não apareça na lista de processos. Recusa: RuntimeError
    (o `nucleo.cofre.CofreError` é um RuntimeError), sem o valor na mensagem."""
    _conferir(servico, valor)
    if modo_local():
        from nucleo import cofre
        cofre.gravar(_nome_local(servico), valor)
        return
    conta = getpass.getuser()
    if not _VALOR_SEGURO.fullmatch(conta):
        raise ValueError("nome de usuário com caracteres não permitidos")
    r = rodar(["security", "-i"],
              input=f"add-generic-password -U -a {conta} -s {servico} -w {valor}\n",
              capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("o Chaves do macOS recusou guardar a credencial")


def apagar(servico: str, rodar=subprocess.run) -> None:
    """Tira o segredo do cofre; se não havia, tudo bem."""
    _conferir(servico)
    if modo_local():
        from nucleo import cofre
        cofre.apagar(_nome_local(servico))
        return
    try:
        rodar(["security", "delete-generic-password", "-s", servico],
              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("o Chaves do macOS não respondeu") from None
