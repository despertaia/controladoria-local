"""Pastas do caso na casa da Banca: o Mac cria `<CNJ> - <cliente>/autos/` para os cartões
que esperam os autos e avisa o painel quando o PDF termina de copiar para lá.
Só biblioteca padrão. O log traz o nome da pasta e contagens, nunca o conteúdo."""

import logging
import os
import re
from pathlib import Path

from ponte_mac.cliente import PainelErro

log = logging.getLogger("ponte_mac.pastas")

CLIENTE_MAX = 40
MAX_ARQUIVOS = 50  # o painel guarda no máximo 50; corpo acima de 64 KB dá 413
SUFIXOS = {".pdf", ".zip", ".p7s"}
PARCIAIS = (".crdownload", ".part", ".download")
_PROIBIDOS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_CHAVE_CRIADAS = "_criadas_novas"
_CHAVE_NOMES = "_nomes"

LEIA_ME = (
    'Coloque dentro da pasta "autos" o PDF (ou o .zip) do processo baixado do PJe.\n'
    "Assim que o arquivo terminar de copiar, a Controladoria avisa o painel e o Lex "
    "pode começar.\n"
    "Nada do que você puser aqui é apagado ou substituído.\n")


def _digitos(numero) -> str:
    return re.sub(r"\D", "", str(numero or ""))


def _cnj(numero) -> str:
    """CNJ formatado (0000000-00.0000.0.00.0000); com outra quantidade, os dígitos crus."""
    d = _digitos(numero)
    if len(d) != 20:
        return d
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def _limpar(texto: str) -> str:
    texto = " ".join(_PROIBIDOS.sub("", texto).split())
    texto = re.sub(r"\.{2,}", ".", texto)  # o servidor recusa ".." no nome da pasta
    return texto.strip(" .")


def _cliente(cliente) -> str:
    primeiro = _limpar(str(cliente or "").split(";")[0])
    if len(primeiro) > CLIENTE_MAX:
        corte = primeiro[:CLIENTE_MAX]
        if primeiro[CLIENTE_MAX] != " " and " " in corte:  # não parte palavra ao meio
            corte = corte.rsplit(" ", 1)[0]
        primeiro = corte.strip(" .")
    return primeiro


def nome_da_pasta(numero: str, cliente: str) -> str:
    """`<CNJ formatado> - <cliente>`; sem cliente, só o número."""
    base = _cnj(numero) or "processo"
    nome = _cliente(cliente)
    return f"{base} - {nome}" if nome else base


def _comeca_com_numero(nome: str, prefixos: tuple) -> bool:
    """O nome começa com o número e este termina ali: depois dele, o fim ou um não dígito
    (a pasta "12345 - Ana" não é do processo "123")."""
    return any(nome.startswith(p) and not nome[len(p):len(p) + 1].isdigit() for p in prefixos)


def nome_aceito_pelo_painel(nome: str) -> bool:
    """A regra de `/ponte/pastas`: não vazio, até 200 caracteres, sem `/`, `\\` nem `..`."""
    nome = nome.strip()
    return bool(nome) and len(nome) <= 200 and not ("/" in nome or "\\" in nome or ".." in nome)


def achar(casa: Path, numero: str) -> Path | None:
    """A pasta da casa que começa com o CNJ (formatado ou só os dígitos; as antigas
    `<dígitos>-cartaoN` não contam), ou None. Não cria nada."""
    casa = Path(casa)
    if not casa.is_dir():
        return None
    digitos = _digitos(numero)
    prefixos = tuple(p for p in dict.fromkeys((_cnj(numero), digitos)) if p)
    antiga = re.compile(rf"{re.escape(digitos)}-cartao\d+") if digitos else None
    for filho in sorted(casa.iterdir(), key=lambda f: f.name):
        if (filho.is_dir() and _comeca_com_numero(filho.name, prefixos)
                and not (antiga and antiga.match(filho.name))):
            return filho
    return None


def achar_ou_criar(casa: Path, numero: str, cliente: str) -> Path:
    """Reaproveita a pasta da casa que começa com o CNJ (formatado ou só os dígitos; as
    antigas `<dígitos>-cartaoN` não contam) ou cria a nova; garante `autos/` e o LEIA-ME."""
    casa = Path(casa)
    casa.mkdir(parents=True, exist_ok=True)
    pasta = achar(casa, numero)
    if pasta is None:
        pasta = casa / nome_da_pasta(numero, cliente)
        log.info("pasta do caso criada: %s", pasta.name)
    pasta.mkdir(exist_ok=True)
    (pasta / "autos").mkdir(exist_ok=True)
    try:
        with open(pasta / "LEIA-ME.txt", "x", encoding="utf-8") as f:
            f.write(LEIA_ME)
    except FileExistsError:
        pass
    return pasta


def _ignorado(nome: str) -> bool:
    minusculo = nome.lower()
    return (nome.startswith(".") or minusculo.startswith("leia-me") or nome.startswith("~$")
            or minusculo.endswith(PARCIAIS))


def arquivos_prontos(autos: Path, memoria: dict) -> list[dict]:
    """PDF, zip e p7s de `autos/` (e subpastas) cujo tamanho não mudou desde a volta anterior.
    `memoria` guarda o tamanho visto de cada arquivo e é atualizada a cada chamada."""
    autos = Path(autos)
    prontos, vistos = [], set()
    for raiz, subpastas, nomes in os.walk(autos):
        subpastas[:] = sorted(d for d in subpastas if not _ignorado(d))
        for nome in sorted(nomes):
            caminho = Path(raiz) / nome
            if _ignorado(nome) or caminho.suffix.lower() not in SUFIXOS:
                continue
            try:
                tamanho = caminho.stat().st_size
            except OSError:
                continue
            chave = str(caminho)
            vistos.add(chave)
            if tamanho > 0 and memoria.get(chave) == tamanho:
                prontos.append({"nome": nome if raiz == str(autos)
                                else str(caminho.relative_to(autos)), "tamanho": tamanho})
            memoria[chave] = tamanho
    base = str(autos) + os.sep
    for chave in [k for k in memoria if k.startswith(base) and k not in vistos]:
        del memoria[chave]
    return prontos


def sincronizar(painel, casa: Path, memoria: dict) -> int:
    """Uma volta: pergunta os casos que esperam pasta (avisando as criadas na volta anterior),
    cria/acha cada pasta e, com arquivo pronto, avisa o painel. Devolve quantos cartões
    foram avisados. A memória vive só no processo."""
    criadas = memoria.get(_CHAVE_CRIADAS, [])
    nomes = memoria.setdefault(_CHAVE_NOMES, {})
    try:
        pendentes = painel.pastas(criadas=list(criadas))
    except PainelErro as exc:
        if exc.status != 400:
            raise
        # o painel recusa o lote inteiro: não insiste nele para sempre
        log.warning("o painel recusou os nomes de pasta; %d descartado(s)", len(criadas))
        for c in criadas:
            nomes.pop(c.get("demanda"), None)
        memoria[_CHAVE_CRIADAS] = []
        return 0
    memoria[_CHAVE_CRIADAS] = []  # entregues; só chegam aqui se a chamada deu certo
    avisados = 0
    por_autos: dict[str, list[dict]] = {}  # uma leitura por pasta `autos/` em cada volta
    for item in pendentes:
        try:
            demanda = int(item["demanda"])
            numero = str(item.get("numero") or "")
            cliente = str(item.get("cliente") or "")
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        try:
            pasta = achar_ou_criar(casa, numero, cliente)
        except OSError as exc:
            log.warning("pasta do cartão %s: %s", demanda, exc)
            continue
        if nomes.get(demanda) != pasta.name:
            if nome_aceito_pelo_painel(pasta.name):
                nomes[demanda] = pasta.name
                memoria[_CHAVE_CRIADAS].append({"demanda": demanda, "pasta_nome": pasta.name})
            else:  # pasta do advogado com nome que o painel recusaria: só vigia os autos
                log.warning("pasta do cartão %s tem nome que o painel não aceita; não anunciada",
                            demanda)
                nomes[demanda] = pasta.name
        try:
            autos = pasta / "autos"
            if str(autos) not in por_autos:  # dois cartões do mesmo processo dividem a pasta
                por_autos[str(autos)] = arquivos_prontos(autos, memoria)
            arquivos = por_autos[str(autos)][:MAX_ARQUIVOS]
        except OSError as exc:
            log.warning("autos do cartão %s: %s", demanda, exc)
            continue
        if not arquivos:
            continue
        try:
            painel.autos_na_pasta(demanda, arquivos)
        except PainelErro as exc:
            if exc.status != 409:
                raise
            log.info("cartão %s não espera mais os autos (409)", demanda)
            continue
        avisados += 1
        log.info("autos do cartão %s na pasta %s: %d arquivo(s) avisados", demanda,
                 pasta.name, len(arquivos))
    return avisados
