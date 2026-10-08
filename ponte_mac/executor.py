"""Executa o Lex num cartão: prepara a pasta do caso na casa da Banca, roda o Claude Code
sem janela (`claude -p`) com ambiente limpo e lê o resultado (linha JSON final + pacote)."""

import collections
import hashlib
import io
import json
import logging
import ntpath
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

log = logging.getLogger("ponte_mac.executor")

CAMPOS_FIM = ("status", "squad", "peca", "citacoes")
TEMPO_LIMITE_S = 3 * 3600
INTERVALO_BATIDA_S = 60
PASSO_S = 5  # de quanto em quanto tempo o laço olha o processo
LIMITE_DETALHE = 300
LIMITE_AUTOS_BYTES = 500 * 1024 * 1024  # descompactado

ALLOWED_TOOLS = [
    "Bash(npx banca:*)", "Bash(node:*)", "Bash(npm run:*)", "Bash(ls:*)", "Bash(cat:*)",
    "Bash(mkdir:*)", "Bash(cp:*)", "Bash(cd:*)", "Bash(test:*)", "Bash(find:*)",
    "Bash(grep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(sed -n:*)",
    "Bash(echo:*)", "Read", "Write", "Edit", "Glob", "Grep", "Task", "WebSearch",
    "WebFetch", "TodoWrite",
]

_SINAIS_DE_LIMITE = ("usage limit", "limit reached", "rate limit", "limite de uso",
                     "hit your limit")
# o Claude recusou o acesso (token vencido, revogado ou inválido): só o padrão de erro de
# autenticação da CLI, nunca um "oauth"/"authentication" solto no texto do caso
_SINAIS_DE_ACESSO = re.compile(
    r"(?i)\bauthentication_error\b|\binvalid[ _]api[ _]key\b|\boauth token\b"
    r"|please run /login\b"
    r"|\b(?:api error|status|http|erro|error)\W{0,3}401\b|\b401\W{0,3}unauthori")
DETALHE_LIMITE = "pausado: limite do plano"
DETALHE_ACESSO = "o acesso ao plano do Lex no Mac foi recusado"  # vai à tela (histórico)
DETALHE_SEM_CLAUDE = "o Claude Code não foi encontrado neste computador"

# Windows: o `claude` sem janela de console (o lançador roda com `pythonw`) e num grupo de
# processos próprio, para o encerramento alcançar a árvore inteira.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# Parada pedida por quem roda a ponte numa thread (`ponte_mac.local`): a execução em
# andamento encerra o Lex e devolve `cancelado`, sem avisar falha ao painel.
_parada = threading.Event()


def usar_parada(evento: threading.Event) -> None:
    """Passa a observar `evento` (o do lançador) como pedido de parada."""
    global _parada
    _parada = evento


_casa_local = ""  # a casa da Banca do modo local (ponte_mac.local), para a higienização


def usar_casa(casa: str) -> None:
    global _casa_local
    _casa_local = str(casa or "")


def no_windows() -> bool:
    return sys.platform.startswith("win")


# --- detalhe que vai para o painel -----------------------------------------------------

_SEGREDOS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "[oculto]"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [oculto]"),
    (re.compile(r"(?:/Users|/home|/private/var|/var/folders)/[^\s'\"`]*"), "[caminho]"),
    (re.compile(r"[A-Za-z0-9_-]{32,}"), "[oculto]"),
    (re.compile(r"(?<![A-Za-z])[A-Za-z]:\\[^\s'\"`]*"), "[caminho]"),  # C:\Users\...
)


def limpar_detalhe(texto) -> str:
    """Uma linha, até 300 caracteres, sem caminho da pasta pessoal, token ou chave."""
    texto = str(texto or "")
    for padrao, troca in _SEGREDOS:
        texto = padrao.sub(troca, texto)
    texto = " ".join(texto.split())
    if len(texto) > LIMITE_DETALHE:
        texto = texto[:LIMITE_DETALHE - 1].rstrip() + "…"
    return texto


# o que vai ao painel da explicação do Lex no sem_tipo (ruling): só o texto, sem caminho,
# nome de arquivo nem pasta de squad
_EXPLICACAO_TIRA = (
    re.compile(r"\S*squads[/\\]\S*", re.I),
    re.compile(r"[^\s'\"`()\[\]“”]*\.(?:json|md|docx|pdf)\b", re.I),
    re.compile(r"(?<![\w.])~?/[^\s'\"`()\[\]“”]*"),
    re.compile(r"(?<![A-Za-z])[A-Za-z]:\\[^\s'\"`()\[\]“”]*"),  # C:\Users\...
    re.compile(r"\[(?:caminho|oculto)\]"),
    # sobras de caminho com espaço (a casa fica em "~/Desktop/Claude Cowork/Processos pendentes")
    re.compile(r"\b(?:Claude Cowork|Processos pendentes)\b", re.I),
    re.compile(r"(?<!\S)(?!e/ou(?!\S))\S*[/\\]\S*", re.I),
)


def _sobras_da_casa() -> re.Pattern | None:
    """No modo local a casa da Banca é a do mentorado (`usar_casa` ou
    `CONTROLADORIA_CASA_BANCA`): os pedaços do caminho dela que têm espaço sobram das
    regras acima e também saem."""
    casas = (_casa_local, os.getenv("CONTROLADORIA_CASA_BANCA") or "")
    partes = sorted({p.strip() for casa in casas for p in re.split(r"[/\\]", casa)
                     if " " in p.strip()},
                    key=len, reverse=True)
    if not partes:
        return None
    return re.compile("|".join(re.escape(p) for p in partes), re.I)


def explicacao_do_lex(texto) -> str:
    """A explicação do Lex para não escolher o tipo de peça, higienizada para o painel:
    uma linha, sem caminhos `/…`, sem nomes de arquivo (.json/.md/.docx/.pdf), sem
    `squads/`, sem token, até 300 caracteres."""
    texto = str(texto or "")
    sobras = _sobras_da_casa()
    if sobras is not None:  # antes das regras de caminho, que cortam o nome no espaço
        texto = sobras.sub(" ", texto)
    for padrao, troca in _SEGREDOS[:2] + _SEGREDOS[3:]:  # tokens, chaves e C:\...
        texto = padrao.sub(troca, texto)
    for padrao in _EXPLICACAO_TIRA:
        texto = padrao.sub(" ", texto)
    texto = texto.replace("[oculto]", " ")
    texto = re.sub(r"[\"'`“”‘’]\s*[\"'`“”‘’]|\(\s*\)", " ", texto)  # aspas que ficaram vazias
    texto = " ".join(texto.split())
    texto = re.sub(r"\s+([,.;:!?])", r"\1", texto).strip(" ,;:-")
    if len(texto) > LIMITE_DETALHE:
        texto = texto[:LIMITE_DETALHE - 1].rstrip() + "…"
    return texto


# --- pasta do caso ---------------------------------------------------------------------

def _publicacao_md(pacote: dict) -> str:
    linhas = [f"# Processo {pacote.get('numero_formatado') or pacote.get('numero') or ''}", ""]
    for rotulo, chave in (("Tribunal", "tribunal"), ("Órgão julgador", "orgao"),
                          ("Cliente", "cliente"), ("Parte contrária", "parte_contraria")):
        if pacote.get(chave):
            linhas.append(f"- {rotulo}: {pacote[chave]}")
    linhas.append(f"- Cartão {pacote.get('demanda')} · execução {pacote.get('n')} · "
                  f"modo {pacote.get('modo') or 'novo'}")
    if pacote.get("orientacao"):
        linhas += ["", "## Orientação do advogado", "", str(pacote["orientacao"])]
    if pacote.get("modo") == "ajuste" and pacote.get("ajuste"):
        linhas += ["", "## Ajuste pedido pelo advogado", "", str(pacote["ajuste"])]
    linhas += ["", "## Publicações", ""]
    publicacoes = pacote.get("publicacoes") or []
    if not publicacoes:
        linhas.append("Nenhuma publicação ligada a este cartão.")
    for p in publicacoes:
        titulo = " · ".join(str(x) for x in (p.get("data"), p.get("tipo"), p.get("orgao")) if x)
        linhas += [f"### {titulo or 'Publicação'}", "", str(p.get("texto") or "(sem texto)")]
        if p.get("link"):
            linhas += ["", f"Link: {p['link']}"]
        linhas.append("")
    linhas += ["", "## Intimações", ""]
    intimacoes = pacote.get("intimacoes") or []
    if not intimacoes:
        linhas.append("Nenhuma intimação ligada a este cartão.")
    for i in intimacoes:
        linhas.append("- " + (" · ".join(str(x) for x in (i.get("data"), i.get("tipo")) if x)
                              or "Intimação"))
    return "\n".join(linhas).rstrip() + "\n"


def _itens_seguros(z: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    """Os arquivos do zip e seus caminhos; recusa o zip inteiro se algum nome tem `..`,
    é absoluto ou se o total declarado passa do limite (nada é gravado antes disto)."""
    itens = []
    total = 0
    for info in z.infolist():
        nome = info.filename.replace("\\", "/")
        partes = PurePosixPath(nome).parts
        if nome.startswith("/") or re.match(r"^[A-Za-z]:", nome) or ".." in partes:
            raise ValueError("autos com caminho inválido no zip")
        if not info.is_dir() and partes:
            itens.append((info, partes))
            total += info.file_size
    if total > LIMITE_AUTOS_BYTES:
        raise ValueError("autos maiores que o limite de 500 MB descompactados")
    return itens


def _extrair(z: zipfile.ZipFile, itens, destino: Path) -> None:
    base = destino.resolve()
    gravados = 0
    for info, partes in itens:
        alvo = destino.joinpath(*partes)
        if base not in alvo.resolve().parents:
            raise ValueError("autos com caminho inválido no zip")
        alvo.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as origem, open(alvo, "wb") as saida:
            while bloco := origem.read(1 << 20):
                gravados += len(bloco)
                if gravados > LIMITE_AUTOS_BYTES:  # o tamanho declarado pode mentir
                    raise ValueError("autos maiores que o limite de 500 MB descompactados")
                saida.write(bloco)


def _apagar(caminho: Path) -> None:
    if caminho.is_symlink() or caminho.is_file():
        caminho.unlink()
    elif caminho.exists():
        shutil.rmtree(caminho)


def pasta_do_caso(casa: Path, pacote: dict) -> Path:
    numero = re.sub(r"[^0-9A-Za-z.-]", "", str(pacote.get("numero") or "")) or "processo"
    return Path(casa) / f"{numero}-cartao{int(pacote['demanda'])}"


CONTROLE_AUTOS = ".controle-autos.json"  # hash e lista do último zip extraído em autos/


def _ler_controle(pasta: Path) -> dict:
    dados = _ler_json(pasta / CONTROLE_AUTOS)
    if not isinstance(dados, dict) or not isinstance(dados.get("arquivos"), list):
        return {}
    arquivos = []
    for item in dados["arquivos"]:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
                and isinstance(item[1], int)):
            return {}
        partes = PurePosixPath(item[0]).parts
        if not partes or ".." in partes or item[0].startswith("/"):
            return {}
        arquivos.append((item[0], item[1]))
    return {"sha256": str(dados.get("sha256") or ""), "arquivos": arquivos}


def _autos_iguais(pasta: Path, controle: dict, resumo: str, lista) -> bool:
    """O zip é o mesmo da última extração (hash, nomes e tamanhos) e os arquivos dele
    continuam em autos/ com os mesmos tamanhos."""
    if not controle or controle["sha256"] != resumo or controle["arquivos"] != lista:
        return False
    for nome, tamanho in lista:
        alvo = (pasta / "autos").joinpath(*PurePosixPath(nome).parts)
        try:
            if alvo.is_symlink() or not alvo.is_file() or alvo.stat().st_size != tamanho:
                return False
        except OSError:
            return False
    return True


def _dentro(base: Path, alvo: Path) -> bool:
    try:
        destino = alvo.resolve()
    except OSError:
        return False
    return destino == base or base in destino.parents


def _trocar_autos(autos: Path, tmp: Path, anteriores: set[str], novos: list[str]) -> None:
    """Põe em autos/ os arquivos novos (de `tmp`) e tira só os que vieram do zip anterior e
    não estão no novo; o que o motor criou ali (ex.: `_index.yaml`) fica."""
    if autos.is_symlink() or (autos.exists() and not autos.is_dir()):
        _apagar(autos)
    autos.mkdir(exist_ok=True)
    base = autos.resolve()
    for nome in sorted(anteriores - set(novos)):
        alvo = autos.joinpath(*PurePosixPath(nome).parts)
        if not _dentro(base, alvo.parent) or not (alvo.is_file() or alvo.is_symlink()):
            continue
        alvo.unlink()
        pai = alvo.parent
        while pai != autos and _dentro(base, pai):
            try:
                pai.rmdir()  # só some se ficou vazia
            except OSError:
                break
            pai = pai.parent
    for nome in novos:
        partes = PurePosixPath(nome).parts
        alvo = autos.joinpath(*partes)
        pai = autos
        for parte in partes[:-1]:
            pai = pai / parte
            if pai.is_symlink() or (pai.exists() and not pai.is_dir()):
                _apagar(pai)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        if not _dentro(base, alvo.parent):
            raise ValueError("autos com caminho inválido no zip")
        if alvo.is_dir() and not alvo.is_symlink():
            shutil.rmtree(alvo)
        os.replace(tmp.joinpath(*partes), alvo)


def _gravar_controle(pasta: Path, dados: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".controle-", dir=pasta)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False)
        os.replace(tmp, pasta / CONTROLE_AUTOS)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def preparar_pasta(casa: Path, pacote: dict, autos_zip: bytes | None) -> Path:
    """`casa/<numero>-cartao<demanda>/` com `publicacao.md` e, se houver, `autos/`.
    Os autos são extraídos numa pasta temporária e só então entram em `autos/`; um zip
    recusado não deixa gravação parcial. O mesmo zip de antes (hash, nomes e tamanhos no
    arquivo de controle) não mexe em `autos/`; um zip novo troca só os arquivos que vieram
    do zip anterior, nunca o que o motor criou ali (ex.: `_index.yaml`)."""
    pasta = pasta_do_caso(casa, pacote)
    criada = not pasta.exists()
    if autos_zip:
        resumo = hashlib.sha256(autos_zip).hexdigest()
        with zipfile.ZipFile(io.BytesIO(autos_zip)) as z:
            itens = _itens_seguros(z)
            lista = [("/".join(partes), info.file_size) for info, partes in itens]
            controle = _ler_controle(pasta)
            if _autos_iguais(pasta, controle, resumo, lista):
                log.info("autos sem mudança desde a última execução; mantidos")
            else:
                pasta.mkdir(parents=True, exist_ok=True)
                tmp = Path(tempfile.mkdtemp(prefix=".autos-", dir=pasta))
                try:
                    _extrair(z, itens, tmp)
                    _trocar_autos(pasta / "autos", tmp,
                                  {n for n, _ in controle.get("arquivos", [])},
                                  [n for n, _ in lista])
                    _gravar_controle(pasta, {"sha256": resumo,
                                             "arquivos": [list(i) for i in lista]})
                except BaseException:
                    if criada:
                        shutil.rmtree(pasta, ignore_errors=True)
                    raise
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "publicacao.md").write_text(_publicacao_md(pacote), encoding="utf-8")
    return pasta


# --- instrução, ambiente e comando -----------------------------------------------------

def _uma_linha(texto) -> str:
    return " ".join(str(texto or "").split())


def uma_linha(texto, limite: int) -> str:
    """Idêntica a `nucleo.ponte.uma_linha` (o servidor normaliza igual): sem caracteres
    de controle, espaços juntos e cortado em `limite`. Mesma função dos dois lados para
    o resumo do gate não divergir à toa."""
    limpo = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                    for c in str(texto or ""))
    return " ".join(limpo.split())[:limite].rstrip()


def montar_prompt(pacote: dict, pasta: Path, retomar: dict | None) -> str:
    orientacao = _uma_linha(pacote.get("orientacao")) or "nenhuma orientação específica."
    processo = " · ".join(str(x) for x in (pacote.get("numero_formatado") or pacote.get("numero"),
                                           pacote.get("tribunal"), pacote.get("orgao")) if x)
    partes = [
        "MODO PONTE: execução automática do Lex, sem ninguém no teclado. Tudo o que esta "
        "instrução pede está pré-autorizado pelo advogado responsável; nunca pergunte nada "
        "e nunca espere resposta.",
        "",
        f"Caso: a pasta `{Path(pasta).name}` (dentro desta casa da Banca) tem `publicacao.md` com o "
        "processo, as publicações e as intimações e, quando houver, `autos/` com as peças "
        f"já baixadas. Processo: {processo}.",
        "",
        f"Orientação do advogado (prevalece sobre qualquer escolha sua): {orientacao}",
        "",
    ]
    if retomar and retomar.get("squad") and retomar.get("run_id"):
        squad, run = retomar["squad"], retomar["run_id"]
        partes.append(
            f"Tarefa: esta execução foi interrompida antes; retome o run {run} do squad {squad} "
            "pelo ledger (run-status → resume), reaproveitando o que já está no disco, e "
            "leve o squad até o fim.")
    elif pacote.get("modo") == "ajuste":
        squad = str(pacote.get("squad_anterior") or "")
        run = str(pacote.get("run_anterior") or "")
        pedido = _uma_linha(pacote.get("ajuste"))
        partes += [
            f"Tarefa (ajuste de peça já entregue): o advogado pediu este ajuste: {pedido}",
            f"Antes de reabrir, confira pelo ledger (run-status) que o run entregue do squad "
            f'{squad} é {run}; se não for, não reabra nada: encerre com status "falhou" e '
            'explique em "detalhe".',
            "Depois reabra o run com o comando abaixo, exatamente como está, numa linha só, e "
            'siga o runner na seção "Alteração depois da entrega":',
            f"    node scripts/squad-state.mjs reabrir squads/{shlex.quote(squad)} --modo ajustes "
            f"--pedido {shlex.quote(pedido)}",
        ]
    else:
        partes += [
            "Tarefa: crie um squad novo para este caso com o comando abaixo (troque só "
            "<a peça>, sem dado do caso) e execute-o do começo ao fim com este caso:",
            f'    npx banca squad-modelo --para "<a peça>" --criar --caso '
            f"{shlex.quote(Path(pasta).name)} --json",
            "Use sempre um squad novo, ligado a este caso; nunca use `--reusar` nem um squad "
            "de outro caso.",
        ]
    partes += [
        "",
        "Checkpoints: escolha sempre a opção recomendada e registre a decisão como "
        '"pré-autorizado: Lex automático (ponte)".',
        "Citation Gate: obrigatório; nenhuma súmula, tese ou precedente citado de memória; "
        "confira as citações antes de encerrar.",
        "Pare antes de qualquer envio, e-mail ou protocolo: a peça vai para revisão humana.",
        "Nunca leia, liste nem mostre variáveis de ambiente, chaves ou tokens.",
        "Regras de execução: não use `cd … &&` (rode cada comando a partir da casa da "
        "Banca); crie e altere arquivos só com as ferramentas Write/Edit; rode os "
        "subagentes sempre em primeiro plano, nunca em segundo plano.",
        "Se não tiver segurança sobre qual peça cabe, não redija: encerre com status "
        '"sem_tipo" e explique o motivo em "detalhe".',
        "",
        "A última linha da sua resposta final deve ser só este JSON, numa linha, sem nada "
        "depois:",
        '{"status": "pronto|sem_tipo|falhou", "squad": "<nome do squad>", '
        '"run_id": "<run>", "peca": "<caminho do .docx da peça>", '
        '"citacoes": {"total": 0, "falhas": 0, "relatorio": "<caminho do relatório>"}, '
        '"detalhe": "<uma frase>"}',
    ]
    return "\n".join(partes)


def ambiente(token: str | None = None) -> dict:
    """Ambiente limpo: nada herdado do processo pai além do mínimo que o sistema exige; o
    acesso ao plano entra só pelo token. Sem token (modo local, sem acesso guardado no
    cofre), o `claude` usa o login normal do usuário."""
    amb = _ambiente_windows() if no_windows() else _ambiente_unix()
    if token:
        amb["CLAUDE_CODE_OAUTH_TOKEN"] = token
    amb["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    return amb


def _ambiente_unix() -> dict:
    import pwd  # só existe fora do Windows

    conta = pwd.getpwuid(os.getuid())
    casa = conta.pw_dir
    return {
        "HOME": casa,
        "USER": conta.pw_name,
        "LOGNAME": conta.pw_name,
        "SHELL": "/bin/zsh",
        "LANG": "pt_BR.UTF-8",
        "PATH": f"{casa}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
                "/usr/sbin:/sbin",
    }


# Do ambiente do Windows só passa o que o sistema, o Node e o Git Bash (que o Claude Code
# usa para o Bash) precisam para funcionar. Nada de proxy (HTTP(S)_PROXY, NO_PROXY), de
# ANTHROPIC_* ou de CLAUDE_CODE_*: quando o mentorado instala pelo Claude Desktop, o
# lançador nasce de um processo do app e herdaria as variáveis que o app põe para o
# Claude Code embutido dele (proxy local do app, endpoint, token da sessão dele). Com
# elas, o `claude` do Lex falaria com o proxy do app (que some quando o app fecha) e não
# com o plano do mentorado. A exceção é o caminho do Git Bash, que é do usuário.
_DO_WINDOWS = (
    "SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT", "OS", "USERNAME",
    "USERDOMAIN", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
    "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "PUBLIC",
    "HOMEDRIVE", "HOMEPATH", "CLAUDE_CODE_GIT_BASH_PATH",
)


def _var(nome: str) -> str:
    """Variável do ambiente sem diferenciar maiúsculas (como o Windows faz)."""
    valor = os.environ.get(nome)
    if valor is None:
        alvo = nome.upper()
        valor = next((v for k, v in os.environ.items() if k.upper() == alvo), None)
    return valor or ""


def _path_do_registro() -> list[str]:
    """O PATH do sistema e o do usuário como estão no registro (o herdado pode ter vindo
    de um app); [] se não der para ler."""
    try:
        import winreg
    except ImportError:
        return []
    caminhos = []
    for raiz, chave in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(raiz, chave) as k:
                valor, _ = winreg.QueryValueEx(k, "Path")
        except OSError:
            continue
        caminhos += [os.path.expandvars(p) for p in str(valor or "").split(";")]
    return caminhos


def _ambiente_windows() -> dict:
    amb = {nome: _var(nome) for nome in _DO_WINDOWS if _var(nome)}
    perfil = _var("USERPROFILE") or os.path.expanduser("~")
    appdata = _var("APPDATA") or ntpath.join(perfil, "AppData", "Roaming")
    local = _var("LOCALAPPDATA") or ntpath.join(perfil, "AppData", "Local")
    temp = _var("TEMP") or _var("TMP") or ntpath.join(local, "Temp")
    raiz = amb.setdefault("SystemRoot", "C:\\Windows")
    amb.setdefault("COMSPEC", ntpath.join(raiz, "System32", "cmd.exe"))
    amb.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    if perfil[1:3] == ":\\":
        amb.setdefault("HOMEDRIVE", perfil[:2])
        amb.setdefault("HOMEPATH", perfil[2:])
    amb.update({
        "USERPROFILE": perfil, "HOME": perfil, "APPDATA": appdata, "LOCALAPPDATA": local,
        "TEMP": temp, "TMP": temp, "LANG": "pt_BR.UTF-8",
    })
    # o `claude` (instalador nativo em .local\bin; npm em %APPDATA%\npm) e o node antes
    # do PATH do sistema
    primeiros = [ntpath.join(perfil, ".local", "bin"), ntpath.join(appdata, "npm")]
    if amb.get("ProgramFiles"):
        primeiros.append(ntpath.join(amb["ProgramFiles"], "nodejs"))
    sistema = _path_do_registro() or _var("PATH").split(";")
    sistema += [ntpath.join(raiz, "System32"), raiz,
                ntpath.join(raiz, "System32", "WindowsPowerShell", "v1.0")]
    vistos, path = set(), []
    for p in primeiros + sistema:
        p = p.strip()
        if p and p.lower() not in vistos:
            vistos.add(p.lower())
            path.append(p)
    amb["PATH"] = ";".join(path)
    return amb


def localizar_claude(env: dict) -> str | None:
    """O executável do `claude` no PATH do ambiente do Lex (no Windows, `claude.exe` ou
    `claude.cmd`), ou None."""
    return shutil.which("claude", path=env.get("PATH") or None)


def _executavel(env: dict) -> str:
    """Fora do Windows, o nome basta (o Popen procura no PATH de `env`, como sempre). No
    Windows, o caminho completo: o CreateProcess procura no PATH do processo pai e não
    completa `.cmd`."""
    if not no_windows():
        return "claude"
    achado = localizar_claude(env)
    if not achado:
        raise FileNotFoundError(DETALHE_SEM_CLAUDE)
    return achado


def _por_lote(exe: str) -> bool:
    """`claude.cmd` (instalação pelo npm) passa pelo cmd.exe, que corta o argumento na
    quebra de linha e expande `%...%`: a instrução vai pela entrada padrão."""
    return exe.lower().endswith((".cmd", ".bat"))


def _opcoes_do_processo() -> dict:
    if no_windows():
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def comando(prompt: str | None, exe: str = "claude") -> list[str]:
    """Sem `prompt` (None), a instrução vai pela entrada padrão."""
    return [exe, "-p", *([prompt] if prompt is not None else []), "--output-format",
            "stream-json", "--verbose", "--permission-mode", "acceptEdits",
            "--disallowedTools", "AskUserQuestion", "--allowedTools", *ALLOWED_TOOLS]


# --- leitura do andamento e do fim -----------------------------------------------------

def progresso(casa: Path, desde: float, pasta: Path) -> tuple[str, str] | None:
    """O `squads/*/state.json` modificado depois de `desde` mais recente, só de squad cujo
    `caso.json` aponta para a pasta deste caso (outro cartão rodando na mesma casa não
    conta)."""
    candidatos = []
    for st in Path(casa).glob("squads/*/state.json"):
        if not squad_do_caso(casa, st.parent.name, pasta):
            continue
        try:
            mtime = st.stat().st_mtime
        except OSError:
            continue
        if mtime > desde:
            candidatos.append((mtime, st))
    for _, st in sorted(candidatos, reverse=True):
        try:
            dados = json.loads(st.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        passo = dados.get("step") if isinstance(dados, dict) else None
        if isinstance(passo, dict):
            rotulo = _uma_linha(passo.get("label")) or "Trabalhando"
            return st.parent.name, f"{rotulo} ({passo.get('current')}/{passo.get('total')})"
        if isinstance(dados, dict):
            return st.parent.name, _uma_linha(dados.get("status")) or "Trabalhando"
    return None


def _instante(texto) -> float | None:
    """ISO 8601 (com `Z` ou fuso) em segundos desde a época; None se ilegível."""
    try:
        return datetime.fromisoformat(str(texto).strip().replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def run_atual(casa: Path, squad: str, desde: float | None = None) -> str:
    """O run aberto no ledger do squad (`run-state.json`), ou "". Com `desde`, só vale o
    run iniciado (`startedAt`) a partir dali: run antigo é de outro caso ou outra entrega."""
    dados = _ler_json(Path(casa) / "squads" / squad / "run-state.json")
    if not isinstance(dados, dict):
        return ""
    if desde is not None:
        inicio = _instante(dados.get("startedAt"))
        if inicio is None or inicio < desde:
            return ""
    return str(dados.get("runId") or dados.get("run_id") or "")


def _eventos_result(linhas: list[str]) -> list[dict]:
    eventos = []
    for linha in linhas:
        try:
            ev = json.loads(linha)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            eventos.append(ev)
    return eventos


def _texto_final(linhas: list[str]) -> str:
    eventos = _eventos_result(linhas)
    return str(eventos[-1].get("result") or "") if eventos else ""


def ler_fim(linhas_stream: list[str]) -> dict | None:
    """Do último evento `result`, a última linha do texto que for JSON com CAMPOS_FIM."""
    for linha in reversed(_texto_final(linhas_stream).splitlines()):
        linha = linha.strip().strip("`").strip()
        if not linha.startswith("{"):
            continue
        try:
            dados = json.loads(linha)
        except ValueError:
            continue
        if isinstance(dados, dict) and all(c in dados for c in CAMPOS_FIM):
            return dados
    return None


def parece_limite(texto: str) -> bool:
    baixo = (texto or "").lower()
    return any(s in baixo for s in _SINAIS_DE_LIMITE)


def parece_erro_de_acesso(texto: str) -> bool:
    return bool(_SINAIS_DE_ACESSO.search(texto or ""))


def hora_do_limite(texto: str, agora: float | None = None) -> float | None:
    """Quando o limite do plano volta, se a mensagem disser: `...|<época>` ou
    `resets 3pm` / `reset at 3:30pm` (hora local; se já passou hoje, amanhã)."""
    texto = texto or ""
    agora = time.time() if agora is None else agora
    m = re.search(r"\|(\d{10})\b", texto)
    if m:
        return float(m.group(1))
    m = re.search(r"(?i)\bresets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", texto)
    if not m:
        return None
    hora, minuto = int(m.group(1)), int(m.group(2) or 0)
    if not 1 <= hora <= 12 or minuto > 59:
        return None
    hora = hora % 12 + (12 if m.group(3).lower() == "p" else 0)
    alvo = datetime.fromtimestamp(agora).replace(hour=hora, minute=minuto, second=0,
                                                 microsecond=0)
    if alvo.timestamp() <= agora:
        alvo += timedelta(days=1)
    return alvo.timestamp()


def validar_acesso(token: str | None, casa: Path, rodar=subprocess.run,
                   tempo: int = 300) -> bool | None:
    """Confere o acesso do Claude com um pedido mínimo: True (aceito), False (recusado) ou
    None (não deu para saber: sem rede, sem `claude`, tempo esgotado...). Sem token, vale
    o login normal do usuário."""
    env = ambiente(token)
    opcoes = ({"creationflags": CREATE_NO_WINDOW} if no_windows() else {})
    try:
        r = rodar([_executavel(env), "-p", "Responda apenas: ok", "--output-format", "json"],
                  cwd=str(casa), env=env, stdin=subprocess.DEVNULL,
                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=tempo, **opcoes)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        dados = json.loads(r.stdout or "")
    except ValueError:
        dados = None
    if r.returncode == 0 and isinstance(dados, dict) and dados.get("is_error") is False:
        return True
    if parece_erro_de_acesso(f"{r.stdout or ''}\n{r.stderr or ''}"):
        return False
    return None


# --- pacote da peça --------------------------------------------------------------------

def _tem_peca(pasta: Path) -> bool:
    return (any(not p.name.upper().startswith("TERMO") for p in pasta.glob("*.docx"))
            and any(pasta.glob("*.pdf")))


def localizar_pacote(casa: Path, squad: str, run_id: str) -> Path | None:
    """`squads/<squad>/output/pacote/<run_id>/` (sem run_id, o mais recente) com a peça
    em .docx e .pdf."""
    if not squad or "/" in squad or "\\" in squad or squad.startswith("."):
        return None
    base = Path(casa) / "squads" / squad / "output" / "pacote"
    if run_id:
        if "/" in run_id or "\\" in run_id or run_id.startswith("."):
            return None
        pasta = base / run_id
        return pasta if pasta.is_dir() and _tem_peca(pasta) else None
    try:
        pastas = [p for p in base.iterdir() if p.is_dir() and _tem_peca(p)]
    except OSError:
        return None
    return max(pastas, key=lambda p: (p.stat().st_mtime, p.name)) if pastas else None


def _ler_json(caminho: Path):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _versao(pasta: Path) -> int:
    m = re.fullmatch(r"v(\d+)", pasta.name)
    return int(m.group(1)) if m else -1


def arquivos_do_pacote(casa: Path, squad: str, pacote: Path) -> dict[str, Path]:
    """Os arquivos a enviar: peca_docx, peca_pdf, citation_gate (obrigatórios) e nota,
    termo, manifesto (quando existem)."""
    arquivos: dict[str, Path] = {}
    manifesto = pacote / "MANIFESTO.json"
    dados = _ler_json(manifesto) if manifesto.is_file() else None
    dados = dados if isinstance(dados, dict) else {}
    pdf_nome = ((dados.get("pdf") or {}).get("arquivo") if isinstance(dados.get("pdf"), dict)
                else None)
    pdfs = sorted(pacote.glob("*.pdf"))
    pdf = next((p for p in pdfs if p.name == pdf_nome), pdfs[0] if pdfs else None)
    docxs = sorted(p for p in pacote.glob("*.docx") if not p.name.upper().startswith("TERMO"))
    docx = next((p for p in docxs if pdf and p.stem == pdf.stem), docxs[0] if docxs else None)
    if docx:
        arquivos["peca_docx"] = docx
    if pdf:
        arquivos["peca_pdf"] = pdf
    run = str(dados.get("run_id") or pacote.name)
    gate_nome = (dados.get("citation_gate") or {}).get("manifesto") \
        if isinstance(dados.get("citation_gate"), dict) else None
    gates = [g for g in (Path(casa) / "squads" / squad / "output" / run).glob(
        "v*/*.citation-gate.json") if _versao(g.parent) >= 0]
    preferidos = [g for g in gates if g.name == gate_nome] or gates
    if preferidos:
        arquivos["citation_gate"] = max(preferidos, key=lambda g: (_versao(g.parent), g.name))
    for campo, nome in (("nota", "NOTA-AO-REVISOR.md"), ("termo", "TERMO-DE-CONFERENCIA.docx"),
                        ("manifesto", "MANIFESTO.json")):
        if (pacote / nome).is_file():
            arquivos[campo] = pacote / nome
    return arquivos


# --- execução --------------------------------------------------------------------------

class Cancelado(Exception):
    """Levantada por `ao_progresso` quando o cartão deixou de ser deste Mac: a execução
    para e devolve o status interno `cancelado`."""


class _Leitura(threading.Thread):
    """Lê o stream-json em segundo plano: guarda os eventos `result` e as linhas soltas."""

    def __init__(self, saida):
        super().__init__(daemon=True)
        self.saida = saida
        self.resultados: list[str] = []
        self.soltas: collections.deque[str] = collections.deque(maxlen=40)  # não-JSON (stderr)

    def run(self):
        try:
            for linha in self.saida:
                linha = linha.rstrip("\n")
                if not linha.strip():
                    continue
                try:
                    ev = json.loads(linha)
                except ValueError:
                    self.soltas.append(linha[:4000])
                    continue
                if isinstance(ev, dict) and ev.get("type") == "result":
                    self.resultados.append(linha)
        except (OSError, ValueError):
            pass


def _sinal_no_grupo(proc, sinal) -> None:
    try:
        os.killpg(proc.pid, sinal)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _arvore_windows(proc, rodar=subprocess.run) -> bool:
    """Windows não tem SIGTERM nem grupo de sinais: `taskkill /T /F` derruba o `claude` e
    a árvore dele (Git Bash, node dos subagentes). Só com o processo ainda vivo: o PID de
    um processo que já terminou pode ter sido reusado por outro."""
    if getattr(proc, "returncode", None) is not None or proc.poll() is not None:
        return False
    try:
        r = rodar(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                  timeout=30, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _encerrar(proc) -> None:
    """Termina o processo (e, num Popen de verdade, o grupo dele: Bash e subagentes)."""
    real = isinstance(proc, subprocess.Popen)
    if real and no_windows():
        if not _arvore_windows(proc):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.error("o processo do Lex não terminou depois do taskkill")
        return
    if real:
        _sinal_no_grupo(proc, signal.SIGTERM)
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    if real:
        _sinal_no_grupo(proc, signal.SIGKILL)
    else:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        log.error("o processo do Lex não terminou depois de SIGKILL")


# o processo do Lex em andamento (no máximo um): o laço o encerra se o serviço for parado
_EM_ANDAMENTO: list = []


def encerrar_em_andamento() -> int:
    """Encerra o grupo do processo do Lex em andamento, se houver; devolve quantos."""
    total = 0
    while _EM_ANDAMENTO:
        proc = _EM_ANDAMENTO.pop()
        if getattr(proc, "returncode", None) is None:
            _encerrar(proc)
            total += 1
    return total


def _resultado(status: str, *, squad: str = "", run_id: str = "", arquivos=None, campos=None,
               detalhe: str = "", ate: float | None = None) -> dict:
    return {"status": status, "squad": squad, "run_id": run_id, "arquivos": arquivos or {},
            "campos": campos or {}, "detalhe": limpar_detalhe(detalhe), "ate": ate}


def _verificada(status) -> bool:
    return str(status or "").strip().lower().startswith(("verificada", "verified"))


def _gerado_em(pacote: Path, manifesto) -> float:
    """Quando o pacote foi gerado: `gerado_em` do MANIFESTO.json; na falta, o mtime do
    MANIFESTO; sem ele, o arquivo mais novo do pacote."""
    if isinstance(manifesto, dict):
        instante = _instante(manifesto.get("gerado_em"))
        if instante is not None:
            return instante
    try:
        return (pacote / "MANIFESTO.json").stat().st_mtime
    except OSError:
        pass
    try:
        return max((p.stat().st_mtime for p in pacote.iterdir() if p.is_file()), default=0.0)
    except OSError:
        return 0.0


def squad_do_caso(casa: Path, squad: str, pasta: Path) -> bool:
    """O squad pertence a este caso: o `caso.json` dele (gravado pelo `--caso`) existe e
    aponta para a pasta deste caso. Sem `caso.json` (squads antigos da casa) nunca vale."""
    if not squad or "/" in squad or "\\" in squad or squad.startswith("."):
        return False
    dados = _ler_json(Path(casa) / "squads" / squad / "caso.json")
    alvo = dados.get("pasta") if isinstance(dados, dict) else None
    if not alvo or not isinstance(alvo, str):
        return False
    alvo = Path(alvo).expanduser()
    alvo = alvo if alvo.is_absolute() else Path(casa) / alvo
    try:
        return alvo.resolve() == Path(pasta).resolve()
    except OSError:
        return False


LIMITE_NOME_SQUAD = 120
_LINHA_NAME = re.compile(r"name:[ \t]*(.*)$")  # só no topo do squad.yaml (sem recuo)


def nome_do_squad(casa: Path, squad: str) -> str:
    """O `name` de `squads/<squad>/squad.yaml` (aspas opcionais), numa linha, até 120
    caracteres; "" se não houver. Leitura simples da linha, sem biblioteca de YAML."""
    if not squad or "/" in squad or "\\" in squad or squad.startswith("."):
        return ""
    try:
        texto = (Path(casa) / "squads" / squad / "squad.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for linha in texto.splitlines():
        m = _LINHA_NAME.match(linha)
        if not m:
            continue
        valor = m.group(1).strip()
        if valor[:1] in ("'", '"'):
            fim = valor.find(valor[0], 1)
            valor = valor[1:fim] if fim > 0 else valor[1:]
        else:
            valor = re.split(r"\s#", valor, maxsplit=1)[0]
        valor = _uma_linha("".join(c if c.isprintable() else " " for c in valor))
        return valor[:LIMITE_NOME_SQUAD].rstrip()
    return ""


def limpar_sobras(casa: Path) -> int:
    """Apaga extrações de autos interrompidas (`<caso>/.autos-*`); devolve quantas."""
    total = 0
    for sobra in Path(casa).glob("*-cartao*/.autos-*"):
        if sobra.is_dir() and not sobra.is_symlink():
            shutil.rmtree(sobra, ignore_errors=True)
            total += 1
    return total


def _interpretar(casa: Path, leitura: _Leitura, rc, squad_visto: str, *, desde: float,
                 pasta: Path, retomar: dict | None, cartao: dict) -> dict:
    fim = ler_fim(leitura.resultados)
    texto = _texto_final(leitura.resultados)
    saida = texto + "\n" + "\n".join(leitura.soltas)
    if fim is None:
        if parece_erro_de_acesso(saida):
            log.error("o Claude recusou o acesso: %s", limpar_detalhe(saida[-400:]))
            return _resultado("acesso", squad=squad_visto, detalhe=DETALHE_ACESSO)
        if parece_limite(saida):
            return _resultado("limite", squad=squad_visto, detalhe=DETALHE_LIMITE,
                              ate=hora_do_limite(saida))
        ultimo = texto.strip().splitlines()[-1] if texto.strip() else ""
        ultimo = ultimo or (leitura.soltas[-1] if leitura.soltas else "")
        return _resultado("falhou", squad=squad_visto,
                          detalhe=ultimo or f"o Lex terminou sem a linha final (código {rc})")
    squad = str(fim.get("squad") or squad_visto or "")
    detalhe = str(fim.get("detalhe") or "")
    status = str(fim.get("status") or "")
    if status == "sem_tipo":
        return _resultado("sem_tipo", squad=squad,
                          detalhe=detalhe or "o Lex não teve segurança sobre o tipo de peça")
    if status != "pronto":
        if parece_limite(detalhe + "\n" + texto):
            return _resultado("limite", squad=squad, detalhe=DETALHE_LIMITE,
                              ate=hora_do_limite(detalhe + "\n" + texto))
        return _resultado("falhou", squad=squad, detalhe=detalhe or f"o Lex terminou: {status}")
    run_id = str(fim.get("run_id") or "")
    # Regra única, em todos os caminhos: só vale pacote de squad cujo caso.json aponta
    # para a pasta deste caso.
    if not squad_do_caso(casa, squad, pasta):
        return _resultado("falhou", squad=squad, run_id=run_id,
                          detalhe="o squad não é deste caso; pacote recusado")
    ajuste = cartao.get("modo") == "ajuste"
    if ajuste:
        run_anterior = str(cartao.get("run_anterior") or "")
        if squad != cartao.get("squad_anterior") or (run_id and run_id != run_anterior):
            return _resultado("falhou", squad=squad, run_id=run_id,
                              detalhe="ajuste fora da execução anterior")
        run_id = run_anterior
    pacote = localizar_pacote(casa, squad, run_id)
    if pacote is None:
        return _resultado("falhou", squad=squad, run_id=run_id,
                          detalhe=f"pacote da peça não encontrado em squads/{squad}/output/pacote")
    arquivos = arquivos_do_pacote(casa, squad, pacote)
    manifesto = _ler_json(arquivos["manifesto"]) if "manifesto" in arquivos else None
    do_manifesto = str(manifesto.get("run_id") or "") if isinstance(manifesto, dict) else ""
    if do_manifesto and do_manifesto != pacote.name:
        return _resultado("falhou", squad=squad, run_id=run_id,
                          detalhe=("ajuste fora da execução anterior" if ajuste else
                                   "o MANIFESTO do pacote é de outro run; pacote recusado"))
    run_id = run_id or do_manifesto or pacote.name
    # o atalho vale só para o run validado que o laço gravou para este cartão
    conhecido = bool(retomar and retomar.get("run_id") and retomar["run_id"] == run_id
                     and retomar.get("squad") == squad)
    if not conhecido and _gerado_em(pacote, manifesto) < desde:
        return _resultado("falhou", squad=squad, run_id=run_id,
                          detalhe="o Lex não gerou pacote novo para este caso")
    gate = _ler_json(arquivos["citation_gate"]) if "citation_gate" in arquivos else None
    if not isinstance(gate, dict):
        return _resultado("falhou", squad=squad, run_id=run_id,
                          detalhe="manifesto do Citation Gate não encontrado ou ilegível")
    lista = [c for c in gate.get("citations") or [] if isinstance(c, dict)] \
        if isinstance(gate.get("citations"), list) else []
    campos = {
        "squad": squad, "run_id": run_id,
        "gate_status": uma_linha(gate.get("gate_status"), 40) or "desconhecido",
        "citacoes_total": len(lista),
        "citacoes_falhas": sum(1 for c in lista if not _verificada(c.get("status"))),
    }
    return _resultado("pronto", squad=squad, run_id=run_id, arquivos=arquivos, campos=campos,
                      detalhe=detalhe)


_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _manter_acordado(proc, popen):
    """Mac: `caffeinate` enquanto o Lex roda. Windows: pede ao sistema para não dormir
    enquanto esta thread estiver na execução (desfeito em `_deixar_dormir`)."""
    if no_windows():
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
            return "windows"
        except (ImportError, AttributeError, OSError):
            log.warning("não consegui manter o computador acordado")
            return None
    try:
        return popen(["caffeinate", "-i", "-w", str(proc.pid)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        log.warning("não consegui manter o Mac acordado (caffeinate)")
        return None


def _deixar_dormir(cafe) -> None:
    if cafe is None:
        return
    if cafe == "windows":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except (ImportError, AttributeError, OSError):
            pass
        return
    try:
        cafe.terminate()
        cafe.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def executar(casa: Path, pacote: dict, autos_zip: bytes | None, token: str, ao_progresso,
             retomar: dict | None = None, popen=subprocess.Popen, relogio=time.time,
             desde: float | None = None) -> dict:
    """Roda o Lex no cartão e devolve o resultado normalizado:
    {"status": pronto|sem_tipo|falhou|limite|acesso|tempo|cancelado, "squad", "run_id",
     "arquivos", "campos", "detalhe", "ate"} (`ate`: quando o limite do plano volta, se
    o Claude disse). `desde` é o início da 1ª execução desta demanda
    (no ajuste, o desta execução): pacote ou run mais antigo não é deste caso."""
    casa = Path(casa)
    if pacote.get("modo") == "ajuste" and not (pacote.get("squad_anterior")
                                                and pacote.get("run_anterior")):
        return _resultado("falhou", detalhe="ajuste sem a execução anterior")
    pasta = preparar_pasta(casa, pacote, autos_zip)
    try:  # o painel sabe que o caso está pronto antes de o Lex começar
        ao_progresso("", "autos baixados; iniciando o Lex" if autos_zip
                     else "caso preparado; iniciando o Lex")
    except Cancelado as exc:
        log.warning("execução cancelada antes de começar: %s", limpar_detalhe(exc))
        return _resultado("cancelado",
                          detalhe="o cartão deixou de ser deste Mac; execução interrompida")
    except Exception as exc:  # o aviso nunca derruba a execução
        log.warning("aviso de progresso falhou: %s", limpar_detalhe(exc))
    if retomar and not (retomar.get("squad") and retomar.get("run_id")
                        and squad_do_caso(casa, retomar["squad"], pasta)):
        log.warning("retomada sem run validado deste caso; execução nova")
        retomar = None
    prompt = montar_prompt(pacote, pasta, retomar)
    inicio = relogio()
    desde = inicio if desde is None else desde
    if _parada.is_set():
        return _resultado("cancelado", detalhe="a ponte foi parada; execução não iniciada")
    env = ambiente(token)
    try:
        exe = _executavel(env)
    except FileNotFoundError:
        return _resultado("falhou", detalhe=DETALHE_SEM_CLAUDE)
    pela_entrada = _por_lote(exe)
    # SIGTERM entre criar o processo e registrá-lo deixaria o Lex órfão: sinais em espera
    # (no Windows não há sinais nem pthread_sigmask)
    _sinais = {signal.SIGTERM, signal.SIGINT}
    bloquear = hasattr(signal, "pthread_sigmask")
    if bloquear:
        signal.pthread_sigmask(signal.SIG_BLOCK, _sinais)
    try:
        try:
            proc = popen(comando(None if pela_entrada else prompt, exe), cwd=str(casa), env=env,
                         stdin=subprocess.PIPE if pela_entrada else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", **_opcoes_do_processo())
        except FileNotFoundError:
            return _resultado("falhou", detalhe=DETALHE_SEM_CLAUDE)
        _EM_ANDAMENTO.append(proc)
    finally:
        if bloquear:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, _sinais)
    if pela_entrada:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            log.warning("não consegui entregar a instrução ao Lex pela entrada padrão")
    cafe = _manter_acordado(proc, popen)
    leitura = _Leitura(proc.stdout)
    leitura.start()
    parada = ""  # "tempo" ou "cancelado"
    ultimo: tuple[str, str] | None = None
    proximo_aviso = inicio + INTERVALO_BATIDA_S
    try:
        while True:
            try:
                proc.wait(timeout=PASSO_S)
                break
            except subprocess.TimeoutExpired:
                pass
            agora = relogio()
            if _parada.is_set():
                parada = "cancelado"
                log.warning("a ponte foi parada; encerrando o Lex em andamento")
                _encerrar(proc)
                break
            if agora - inicio >= TEMPO_LIMITE_S:
                parada = "tempo"
                log.warning("o Lex passou de %d h; encerrando", TEMPO_LIMITE_S // 3600)
                _encerrar(proc)
                break
            if agora >= proximo_aviso:
                proximo_aviso = agora + INTERVALO_BATIDA_S
                ultimo = progresso(casa, inicio, pasta) or ultimo
                try:
                    ao_progresso(*(ultimo or ("", "Lex trabalhando")))
                except Cancelado as exc:
                    parada = "cancelado"
                    log.warning("execução cancelada: %s", limpar_detalhe(exc))
                    _encerrar(proc)
                    break
                except Exception as exc:  # o aviso nunca derruba a execução
                    log.warning("aviso de progresso falhou: %s", limpar_detalhe(exc))
    finally:
        if getattr(proc, "returncode", None) is None:
            _encerrar(proc)
        if isinstance(proc, subprocess.Popen) and not no_windows():
            _sinal_no_grupo(proc, signal.SIGTERM)  # sobras do grupo (Bash, subagentes)
        if proc in _EM_ANDAMENTO:  # só sai da lista depois de encerrado
            _EM_ANDAMENTO.remove(proc)
        _deixar_dormir(cafe)
        leitura.join(timeout=10)
        try:
            proc.stdout.close()
        except (OSError, AttributeError):
            pass
    squad_visto = ultimo[0] if ultimo else ""
    if parada == "tempo":
        return _resultado("tempo", squad=squad_visto,
                          detalhe=f"o Lex passou de {TEMPO_LIMITE_S // 3600} h e foi interrompido")
    if parada == "cancelado":
        return _resultado("cancelado", squad=squad_visto,
                          detalhe="o cartão deixou de ser deste Mac; execução interrompida")
    if _parada.is_set():  # parado de fora (encerrar_em_andamento) no meio da execução
        return _resultado("cancelado", squad=squad_visto,
                          detalhe="a ponte foi parada; execução interrompida")
    return _interpretar(casa, leitura, proc.returncode, squad_visto, desde=desde, pasta=pasta,
                        retomar=retomar, cartao=pacote)
