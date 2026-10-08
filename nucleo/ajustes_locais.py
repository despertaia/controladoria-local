"""Ajustes da instalação local (Controladoria no computador do advogado).

Dois lugares, de propósito:
- `dados/ajustes.json`: o que não é segredo (tribunais, nome como sai no DJEN, OAB,
  pasta da Banca, porta). A atualização do código nunca toca em `dados/`.
- cofre do sistema (`nucleo.cofre`): CPF e senhas do PJe e a chave de sessão do painel.

Vários tribunais (o advogado com processos no TJMT e no TJMG): `tribunais` no json, o
principal primeiro (`tribunal` continua sendo o principal, para quem lê o formato
antigo); no cofre, um CPF só (`pje_cpf`) e as senhas de cada um
(`pje_senha_<SIGLA>`, `pje_senha_2grau_<SIGLA>`). As chaves antigas `pje_senha` e
`pje_senha_2grau` valem para o principal enquanto ele não tiver as suas e seguem
espelhando as dele (uma versão anterior do programa continua lendo).

Ordem de uso pelo lançador: `carregar_no_ambiente()` ANTES de importar o painel,
porque o painel lê `SECRET_KEY` (e o resto do ambiente) no import."""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

from nucleo import cofre
from nucleo import tribunal as nucleo_tribunal

RAIZ = Path(__file__).resolve().parent.parent
CAMINHO = RAIZ / "dados" / "ajustes.json"
PORTA_PADRAO = 5056

CAMPOS = ("tribunal", "tribunais", "advogado_nome", "oab_numero", "oab_uf", "casa_banca",
          "porta")
SEGREDOS = ("pje_cpf", "pje_senha", "pje_senha_2grau", "secret_key")

# Variável de ambiente ← campo do json (os tribunais vão à parte: ver _ambiente()).
_AMBIENTE_DO_JSON = {
    "CARTEIRA_ADVOGADO_NOME": "advogado_nome",
    "CARTEIRA_OAB_NUMERO": "oab_numero",
    "CARTEIRA_OAB_UF": "oab_uf",
}

log = logging.getLogger(__name__)


def caminho() -> Path:
    """`CONTROLADORIA_AJUSTES` troca o arquivo (testes); padrão `dados/ajustes.json`."""
    return Path(os.getenv("CONTROLADORIA_AJUSTES", "").strip() or CAMINHO)


def ler() -> dict:
    """Campos não secretos gravados; {} se o arquivo não existe ou está ilegível."""
    try:
        dados = json.loads(caminho().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Ajustes ilegíveis em %s (%s); seguindo sem eles.", caminho(), exc)
        return {}
    if not isinstance(dados, dict):
        return {}
    return {c: dados[c] for c in CAMPOS if dados.get(c) not in (None, "")}


def _gravar_json(dados: dict) -> None:
    """Escrita atômica: arquivo temporário na mesma pasta e `os.replace`."""
    destino = caminho()
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ajustes-", suffix=".tmp", dir=destino.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, destino)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def tribunais_guardados(a: dict | None = None) -> list[str]:
    """Siglas guardadas, o principal primeiro. Json antigo (só `tribunal`) = [tribunal]."""
    a = ler() if a is None else a
    lista = a.get("tribunais")
    if not isinstance(lista, list):
        lista = [a["tribunal"]] if a.get("tribunal") else []
    siglas: list[str] = []
    for sigla in lista:
        sigla = str(sigla).strip().upper()
        if sigla in nucleo_tribunal.TRIBUNAIS and sigla not in siglas:
            siglas.append(sigla)
    return siglas


def _chave(sigla: str, segundo_grau: bool = False) -> str:
    return f"pje_senha_2grau_{sigla}" if segundo_grau else f"pje_senha_{sigla}"


def senha_guardada(sigla: str, segundo_grau: bool = False,
                   principal: str | None = None) -> str | None:
    """Senha do tribunal no cofre. O principal (padrão: o 1º guardado) cai nas chaves
    antigas `pje_senha`/`pje_senha_2grau` quando ainda não tem as suas."""
    valor = cofre.ler(_chave(sigla, segundo_grau))
    if valor:
        return valor
    if principal is None:
        principal = (tribunais_guardados() or [None])[0]
    if sigla == principal:
        return cofre.ler("pje_senha_2grau" if segundo_grau else "pje_senha")
    return None


def porta_do_painel() -> int:
    try:
        return int(ler().get("porta") or PORTA_PADRAO)
    except (TypeError, ValueError):
        return PORTA_PADRAO


def secret_key() -> str:
    """Chave de sessão do painel: guardada no cofre, gerada na primeira vez. Se o
    cofre não aceitar, vale só até reiniciar (as sessões caem; nada se perde)."""
    atual = cofre.ler("secret_key")
    if atual:
        return atual
    nova = secrets.token_urlsafe(48)
    try:
        cofre.gravar("secret_key", nova)
    except cofre.CofreError as exc:
        log.warning("Chave de sessão não guardada no cofre: %s", exc)
    return nova


def configurado() -> bool:
    """Pronto para usar: nome e OAB no json. É o que a busca de publicações no DJEN
    precisa; o PJe (tribunais, CPF e senha) é opcional e só serve para baixar os autos."""
    a = ler()
    return all(a.get(c) for c in ("advogado_nome", "oab_numero", "oab_uf"))


def pje_ligado(a: dict | None = None) -> bool:
    """Há PJe configurado: um tribunal marcado, o CPF e a senha de pelo menos um deles."""
    a = ler() if a is None else a
    siglas = tribunais_guardados(a)
    return bool(siglas) and bool(cofre.ler("pje_cpf")) and any(
        senha_guardada(s, principal=siglas[0]) for s in siglas)


def _ambiente(a: dict) -> dict[str, str]:
    """Variáveis de ambiente que os ajustes (json + cofre) definem; vazio = não definir."""
    siglas = tribunais_guardados(a)
    principal = siglas[0] if siglas else ""
    valores = {"CONTROLADORIA_TRIBUNAIS": ",".join(siglas),
               "CONTROLADORIA_TRIBUNAL": principal}
    valores.update({var: str(a.get(campo, "")) for var, campo in _AMBIENTE_DO_JSON.items()})
    valores["PJE_CPF"] = cofre.ler("pje_cpf") or ""
    for sigla in siglas:
        valores[f"PJE_SENHA_{sigla}"] = senha_guardada(sigla, principal=principal) or ""
        valores[f"PJE_SENHA_2GRAU_{sigla}"] = senha_guardada(sigla, True, principal) or ""
    valores["PJE_SENHA"] = valores.get(f"PJE_SENHA_{principal}", "")
    valores["PJE_SENHA_2GRAU"] = valores.get(f"PJE_SENHA_2GRAU_{principal}", "")
    return valores


def carregar_no_ambiente() -> list[str]:
    """Preenche `os.environ` a partir do json e do cofre, sem sobrescrever variável já
    definida e não vazia (o `.env` ou o sistema mandam). Devolve os NOMES definidos."""
    valores = _ambiente(ler())
    principal = valores["CONTROLADORIA_TRIBUNAL"]
    # Senha do principal já posta no .env (PJE_SENHA) manda sobre a do cofre.
    for var in ("PJE_SENHA", "PJE_SENHA_2GRAU"):
        if principal and os.environ.get(var, "").strip():
            valores.pop(f"{var}_{principal}", None)
    definidos = []
    for var, valor in valores.items():
        if valor and not os.environ.get(var, "").strip():
            os.environ[var] = valor
            definidos.append(var)
    if not os.environ.get("SECRET_KEY", "").strip():
        os.environ["SECRET_KEY"] = secret_key()
        definidos.append("SECRET_KEY")
    return definidos


def salvar(*, advogado_nome: str, oab_numero: str, oab_uf: str, pje_cpf: str,
           tribunal: str | None = None, pje_senha: str | None = None,
           pje_senha_2grau: str | None = None, tribunais: list[str] | None = None,
           senhas: dict | None = None, casa_banca: str | None = None,
           porta: int | None = None) -> None:
    """Grava os ajustes (json + cofre) e já os põe em `os.environ`, sobrescrevendo:
    é uma troca deliberada.

    `tribunais`: siglas, o principal primeiro (padrão: [tribunal]); lista vazia = sem
    PJe (só o DJEN): o CPF e as senhas saem do cofre. `senhas`:
    {sigla: (senha, senha_2grau)}; `pje_senha`/`pje_senha_2grau` valem para o principal
    quando ele não está em `senhas`. Senha `None` mantém a guardada; senha do 2º grau
    "" apaga a dele (tribunal sem 2º grau). Tribunal que sai da lista tem as senhas
    apagadas do cofre. Erro do cofre sobe como `CofreError` antes de mexer no json."""
    lista = list(tribunais) if tribunais is not None else ([tribunal] if tribunal else [])
    siglas: list[str] = []
    for bruto in lista:
        sigla = str(bruto).strip().upper()
        if sigla not in nucleo_tribunal.TRIBUNAIS:
            raise ValueError(f"Tribunal desconhecido: {bruto!r}")
        if sigla not in siglas:
            siglas.append(sigla)
    principal = siglas[0] if siglas else None
    senhas = dict(senhas or {})
    if principal:
        senhas.setdefault(principal, (pje_senha, pje_senha_2grau))

    # Instalação de antes (só as chaves antigas): passa a senha do principal de então
    # para as chaves dele, antes que o espelho das antigas mude de dono.
    antes = tribunais_guardados()
    if antes:
        for segundo in (False, True):
            if not cofre.ler(_chave(antes[0], segundo)):
                antiga = cofre.ler("pje_senha_2grau" if segundo else "pje_senha")
                if antiga:
                    cofre.gravar(_chave(antes[0], segundo), antiga)

    cofre.gravar("pje_cpf", pje_cpf if siglas else "")  # sem tribunal, sem PJe: sai o CPF
    for sigla in siglas:
        senha, senha_2grau = senhas.get(sigla, (None, None))
        if senha is not None:
            cofre.gravar(_chave(sigla), senha)
        if senha_2grau is not None:
            cofre.gravar(_chave(sigla, True), senha_2grau)
    for sigla in nucleo_tribunal.TRIBUNAIS:
        if sigla not in siglas:  # desmarcado: as senhas dele saem do cofre
            cofre.apagar(_chave(sigla))
            cofre.apagar(_chave(sigla, True))
    # Espelho nas chaves antigas: as do principal (sem tribunal, ficam vazias).
    cofre.gravar("pje_senha", (cofre.ler(_chave(principal)) if principal else "") or "")
    cofre.gravar("pje_senha_2grau",
                 (cofre.ler(_chave(principal, True)) if principal else "") or "")

    dados = ler()
    dados.update(tribunal=principal, tribunais=siglas, advogado_nome=advogado_nome.strip(),
                 oab_numero=oab_numero.strip(), oab_uf=oab_uf.strip().upper())
    if casa_banca is not None:
        dados["casa_banca"] = casa_banca
    if porta is not None:
        dados["porta"] = int(porta)
    _gravar_json({c: dados[c] for c in CAMPOS if dados.get(c) not in (None, "")})

    for sigla in nucleo_tribunal.TRIBUNAIS:  # o que sobrou de uma lista anterior sai
        os.environ.pop(f"PJE_SENHA_{sigla}", None)
        os.environ.pop(f"PJE_SENHA_2GRAU_{sigla}", None)
    for var, valor in _ambiente(dados).items():
        if valor:
            os.environ[var] = valor
        else:
            os.environ.pop(var, None)
