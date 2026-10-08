"""Tribunais do escritório: qual PJe o sistema consulta (endereços por instância), o número
J.TR que reconhece os processos dele e o fuso do relógio das varreduras.

Escolhidos por `CONTROLADORIA_TRIBUNAIS` ("TJMT,TJMG", o principal primeiro); sem ela,
um só, o de `CONTROLADORIA_TRIBUNAL` (padrão TJMT, o da produção). O principal dá o fuso
e a senha sem sigla; cada processo vai ao PJe do tribunal do seu número (`do_numero`).
Os endereços podem ser trocados pelas variáveis de cada instância, sem mexer no código."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone, tzinfo


@dataclass(frozen=True)
class Tribunal:
    sigla: str
    nome: str
    jtr: tuple[str, str]
    fuso: str
    # (chave, rótulo, variável do WSDL, WSDL padrão)
    instancias: tuple[tuple[str, str, str, str], ...]
    # Há tribunal que publica o WSDL num host e declara no `soap:address` outro, que
    # não responde de fora (TJMG: o WSDL vem da consulta pública, mas aponta para
    # pje.tjmg.jus.br, que redireciona para uma página de erro). Com True, as chamadas
    # vão para o próprio endereço do WSDL (sem o "?wsdl"), não para o declarado nele.
    forcar_endereco: bool = False

    def tem_instancia(self, chave: str) -> bool:
        return any(i[0] == chave for i in self.instancias)


TRIBUNAIS: dict[str, Tribunal] = {
    "TJMT": Tribunal(
        "TJMT", "Tribunal de Justiça de Mato Grosso", ("8", "11"), "America/Cuiaba",
        (("1grau", "1ª instância", "TJMT_WSDL_URL",
          "https://pje.tjmt.jus.br/pje/intercomunicacao?wsdl"),
         ("2grau", "2ª instância", "TJMT_WSDL_URL_2GRAU",
          "https://pje2.tjmt.jus.br/pje2/intercomunicacao?wsdl"))),
    # Endereço do MNI do TJMG ainda não confirmado com login real: fica configurável.
    # pje.tjmg.jus.br bloqueia de fora; a consulta pública serve o mesmo MNI 2.2.x.
    "TJMG": Tribunal(
        "TJMG", "Tribunal de Justiça de Minas Gerais", ("8", "13"), "America/Sao_Paulo",
        (("1grau", "1ª instância", "TJMG_WSDL_URL",
          "https://pje-consulta-publica.tjmg.jus.br/pje/intercomunicacao?wsdl"),),
        forcar_endereco=True),
}

PADRAO = "TJMT"


def _unico() -> Tribunal:
    sigla = os.getenv("CONTROLADORIA_TRIBUNAL", "").strip().upper() or PADRAO
    return TRIBUNAIS.get(sigla, TRIBUNAIS[PADRAO])


def configurados() -> list[Tribunal]:
    """Tribunais do escritório, o principal primeiro: `CONTROLADORIA_TRIBUNAIS`
    ("TJMT,TJMG"); vazia (produção), só o de `CONTROLADORIA_TRIBUNAL`."""
    lista: list[Tribunal] = []
    for sigla in os.getenv("CONTROLADORIA_TRIBUNAIS", "").split(","):
        trib = TRIBUNAIS.get(sigla.strip().upper())
        if trib is not None and trib not in lista:
            lista.append(trib)
    return lista or [_unico()]


def atual() -> Tribunal:
    """O tribunal principal (o primeiro configurado)."""
    return configurados()[0]


def obter(tribunal: "Tribunal | str | None" = None) -> Tribunal:
    """Objeto do tribunal a partir dele mesmo ou da sigla; None = o principal."""
    if tribunal is None:
        return atual()
    if isinstance(tribunal, Tribunal):
        return tribunal
    sigla = str(tribunal).strip().upper()
    if sigla not in TRIBUNAIS:
        raise ValueError(f"Tribunal desconhecido: {tribunal!r}")
    return TRIBUNAIS[sigla]


def do_numero(numero_ou_digitos) -> Tribunal | None:
    """O tribunal configurado cujo J.TR casa com o número CNJ (None se nenhum)."""
    d = "".join(c for c in str(numero_ou_digitos or "") if c.isdigit())
    if len(d) != 20:
        return None
    jtr = (d[13], d[14:16])
    return next((t for t in configurados() if t.jtr == jtr), None)


def disponiveis() -> list[Tribunal]:
    return list(TRIBUNAIS.values())


def _env(*nomes: str) -> str:
    for nome in nomes:
        valor = os.getenv(nome, "").strip()
        if valor:
            return valor
    return ""


def cpf() -> str:
    return _env("PJE_CPF", "TJMT_CPF")


def _sigla(sigla: str | None) -> str:
    return (sigla or atual().sigla).strip().upper()


def senha(sigla: str | None = None) -> str:
    """Senha do PJe do tribunal (`PJE_SENHA_<SIGLA>`); a do principal cai em
    `PJE_SENHA` e depois em `TJMT_SENHA` (produção). Sem sigla: a do principal."""
    s = _sigla(sigla)
    return _env(f"PJE_SENHA_{s}") or (
        _env("PJE_SENHA", "TJMT_SENHA") if s == atual().sigla else "")


def senha_2grau(sigla: str | None = None) -> str:
    s = _sigla(sigla)
    return _env(f"PJE_SENHA_2GRAU_{s}") or (
        _env("PJE_SENHA_2GRAU", "TJMT_SENHA_2GRAU") if s == atual().sigla else "")


def senha_configurada() -> bool:
    """Há senha do PJe no ambiente (modo armazenado)? Sem ela, vem da sessão."""
    return bool(senha())


def pje_env(nome: str, padrao: str) -> str:
    """Ajuste do cliente do PJe: `PJE_<nome>`, com fallback para `TJMT_<nome>`."""
    return _env(f"PJE_{nome}", f"TJMT_{nome}") or padrao


# Deslocamento fixo para quando não há base de fusos (Windows sem o pacote tzdata).
# Nenhum dos dois tem horário de verão desde 2019.
_DESLOCAMENTO_SEM_TZDATA = {"America/Cuiaba": -4, "America/Sao_Paulo": -3}


def fuso() -> tzinfo:
    """Fuso do relógio do escritório (o do tribunal principal)."""
    nome = atual().fuso
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(nome)
    except Exception:  # noqa: BLE001 — sem tzdata
        return timezone(timedelta(hours=_DESLOCAMENTO_SEM_TZDATA.get(nome, -3)))


def e_do_tribunal(j: str, tr: str) -> bool:
    """O J.TR (segmento e tribunal do número CNJ) é de um tribunal do escritório?"""
    return any((j, tr) == t.jtr for t in configurados())
