"""Fábrica única de clientes MNI: um lugar só decide WSDL, senha da instância,
timeout e intervalo entre chamadas (antes cada script criava o seu)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from captura import credenciais
from captura.instancias import instancias
from captura.mni_client import CredencialInvalidaError, MNIClient, MNIError
from nucleo import tribunal
from nucleo import tribunal as _tribunais  # `tribunal` também é parâmetro de criar_cliente


class CredencialAusenteError(MNIError):
    """Não há credencial do PJe configurada (nem no .env, nem na sessão)."""


def wsdl_da_instancia(instancia: str, trib=None) -> str:
    """WSDL da instância no tribunal `trib` (objeto ou sigla; padrão, o principal)."""
    trib = tribunal.obter(trib)
    for chave, _rotulo, env, padrao in instancias(trib):
        if chave == instancia:
            return os.getenv(env, "").strip() or padrao
    raise MNIError(f"Instância desconhecida no {trib.sigla}: {instancia}")


def criar_cliente(instancia: str, cred: dict | None = None, *,
                  intervalo: float | None = None, tribunal=None) -> MNIClient:
    """Cliente MNI da instância no tribunal pedido (objeto ou sigla; padrão, o
    principal): WSDL, endereço forçado e, sem `cred`, a senha guardada DELE."""
    trib = _tribunais.obter(tribunal)
    cred = cred if cred is not None else credenciais.credencial_do_env(trib.sigla)
    if not cred or not cred.get("cpf") or not cred.get("senha"):
        raise CredencialAusenteError(
            "Credencial do PJe ausente: preencha o CPF e a senha do PJe na Configuração "
            "(PJE_CPF e PJE_SENHA; TJMT_CPF e TJMT_SENHA também valem) "
            "ou conecte-se ao PJe.")
    if intervalo is None:
        intervalo = float(_tribunais.pje_env("INTERVALO_SEGUNDOS", "3"))
    wsdl_url = wsdl_da_instancia(instancia, trib)
    extras = {}
    if trib.forcar_endereco:
        # O WSDL declara um host que não responde: as chamadas vão para o do WSDL.
        extras["endereco"] = wsdl_url.split("?")[0]
    return MNIClient(
        wsdl_url=wsdl_url,
        cpf=cred["cpf"],
        senha=credenciais.senha_da_instancia(cred, instancia),
        timeout=int(_tribunais.pje_env("TIMEOUT", "120")),
        intervalo_minimo_segundos=intervalo,
        **extras,
    )


# Sonda de login: a mesma da varredura (criar o cliente e listar os avisos pendentes,
# que não abre teor nem registra ciência), separada para a tela de configuração.
OK, RECUSADA, INDISPONIVEL = "ok", "recusada", "indisponivel"
TIMEOUT_SONDA = 25


def _frase(exc: Exception, limite: int = 200) -> str:
    texto = " ".join(str(exc).split())
    frase = texto.split(". ", 1)[0]
    return (frase[:limite].rstrip() + "…" if len(frase) > limite else frase) or type(exc).__name__


def _sondar_uma(cred: dict, instancia, timeout: int, cliente_mni,
                forcar_endereco: bool) -> tuple[str, str]:
    if isinstance(instancia, str):
        chave, wsdl = instancia, wsdl_da_instancia(instancia)
    else:  # tupla do tribunal: (chave, rótulo, variável do WSDL, WSDL padrão)
        chave, _rotulo, env, padrao = instancia
        wsdl = os.getenv(env, "").strip() or padrao
    extras = {"endereco": wsdl.split("?")[0]} if forcar_endereco else {}
    try:
        cliente = cliente_mni(wsdl_url=wsdl, cpf=cred["cpf"],
                              senha=credenciais.senha_da_instancia(cred, chave),
                              timeout=timeout, intervalo_minimo_segundos=0, **extras)
        cliente.consultar_avisos_pendentes()
    except CredencialInvalidaError:
        return RECUSADA, "O PJe recusou o CPF ou a senha."
    except Exception as exc:  # noqa: BLE001 — WSDL inválido, fora do ar, tempo esgotado
        return INDISPONIVEL, _frase(exc)
    return OK, "Login aceito pelo PJe."


def sondar_login(cred: dict, instancias, *, timeout: int = TIMEOUT_SONDA,
                 cliente_mni=MNIClient,
                 forcar_endereco: bool | None = None) -> dict[str, tuple[str, str]]:
    """Testa o login em cada instância, em paralelo. `instancias`: chaves ("1grau") ou
    tuplas do tribunal (`Tribunal.instancias`, para sondar um tribunal que ainda não é
    o atual). `forcar_endereco`: o do tribunal sondado (padrão: o do tribunal atual).
    Devolve {instância: (ok | recusada | indisponivel, mensagem)}; a mensagem
    nunca traz a senha."""
    lista = list(instancias)
    if not lista:
        return {}
    chaves = [i if isinstance(i, str) else i[0] for i in lista]
    if forcar_endereco is None:
        forcar_endereco = tribunal.atual().forcar_endereco
    with ThreadPoolExecutor(max_workers=len(lista)) as pool:
        resultados = pool.map(lambda i: _sondar_uma(cred, i, timeout, cliente_mni, forcar_endereco), lista)
        return dict(zip(chaves, resultados))
