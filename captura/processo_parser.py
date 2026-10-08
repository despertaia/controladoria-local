"""
Converte a resposta zeep do consultarProcesso num dicionário simples e
serializável (para virar JSON no cache e alimentar o painel web).

Nada aqui faz rede; só transforma o objeto já recebido.
"""

from __future__ import annotations

from typing import Any

from nucleo import tribunal

_POLOS = {
    "AT": "Polo Ativo", "PA": "Polo Passivo", "TC": "Terceiro",
    "TJ": "Terceiro Juízo", "VI": "Vítima", "FL": "Fiscal da Lei",
}


def _attr(obj, *nomes, padrao=None):
    for nome in nomes:
        v = getattr(obj, nome, None)
        if v not in (None, ""):
            return v
    return padrao


def _verdade(v) -> bool:
    """Coage o valor de um booleano do MNI (pode vir bool ou string)."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "sim", "s")
    return bool(v)


def _advogados(parte) -> list[dict]:
    """Extrai os advogados de uma parte: nome + inscrição (OAB)."""
    advs = []
    for a in (getattr(parte, "advogado", None) or []):
        nome = _attr(a, "nome", padrao="")
        oab = _attr(a, "inscricao", "numeroOAB", padrao="")
        if nome:
            advs.append({"nome": str(nome), "oab": str(oab or "")})
    return advs


def formatar_numero_cnj(numero) -> str:
    n = "".join(c for c in str(numero) if c.isdigit())
    if len(n) != 20:
        return str(numero)
    return f"{n[0:7]}-{n[7:9]}.{n[9:13]}.{n[13:14]}.{n[14:16]}.{n[16:20]}"


_RAMOS = {
    "1": "STF", "2": "CNJ", "3": "STJ", "4": "Justiça Federal",
    "5": "Justiça do Trabalho", "6": "Justiça Eleitoral",
    "7": "Justiça Militar da União", "8": "Justiça Estadual", "9": "Justiça Militar Estadual",
}


def _dv_esperado(digitos: str) -> int:
    """Dígito verificador esperado (módulo 97, base 10000) para um CNJ de 20 dígitos."""
    seq, ano, j, tr, origem = digitos[:7], digitos[9:13], digitos[13], digitos[14:16], digitos[16:20]
    return 98 - (int(f"{seq}{ano}{j}{tr}{origem}00") % 97)


def validar_cnj(numero: str) -> dict:
    """Classifica um número CNJ: válido do tribunal do escritório (TJMT, TJMG…),
    de outro tribunal, ou com dígito verificador inválido (provável erro de
    digitação). "do_tribunal" diz se é do tribunal do escritório; "tjmt" é o
    nome antigo da mesma chave (mantido para quem ainda lê)."""
    siglas = "/".join(t.sigla for t in tribunal.configurados())
    d = "".join(c for c in str(numero) if c.isdigit())
    if len(d) != 20:
        return {"digitos": d, "formatado": str(numero), "ok": False,
                "dv_ok": False, "do_tribunal": False, "tjmt": False, "ramo": "",
                "motivo": "não tem 20 dígitos"}
    j, tr = d[13], d[14:16]
    dv_ok = int(d[7:9]) == _dv_esperado(d)
    do_tribunal = tribunal.e_do_tribunal(j, tr)
    if not dv_ok:
        motivo = f"dígito verificador inválido (deveria ser {_dv_esperado(d):02d} — provável digitação)"
    elif not do_tribunal:
        ramo = _RAMOS.get(j, "ramo desconhecido")
        motivo = f"não é do {siglas} ({ramo}{', outro estado' if j == '8' else ''})"
    else:
        motivo = ""
    return {"digitos": d, "formatado": formatar_numero_cnj(d), "ok": dv_ok and do_tribunal,
            "dv_ok": dv_ok, "do_tribunal": do_tribunal, "tjmt": do_tribunal,
            "ramo": _RAMOS.get(j, ""), "motivo": motivo}


def classificar_lista(numeros: list[str]) -> dict:
    """Separa uma lista de números em válidos (do tribunal do escritório) e com problema."""
    validos, problemas = [], []
    for n in numeros:
        v = validar_cnj(n)
        (validos if v["ok"] else problemas).append(v)
    return {"validos": [v["digitos"] for v in validos],
            "n_validos": len(validos), "problemas": problemas}


def formatar_data(valor) -> str:
    s = "".join(c for c in str(valor or "") if c.isdigit())
    if len(s) >= 8:
        base = f"{s[6:8]}/{s[4:6]}/{s[0:4]}"
        return f"{base} {s[8:10]}:{s[10:12]}" if len(s) >= 12 else base
    return ""


def _data_ordenavel(valor) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def formatar_valor_causa(valor) -> str:
    if valor in (None, "", "—"):
        return ""
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _codigo_nacional(m) -> int | None:
    """Código do movimento na Tabela Processual Unificada do CNJ (ex.: 246 =
    Arquivado Definitivamente). None se o tribunal não informou."""
    mn = getattr(m, "movimentoNacional", None)
    codigo = getattr(mn, "codigoNacional", None) if mn is not None else None
    try:
        return int(codigo)
    except (TypeError, ValueError):
        return None


def _texto_movimento(m) -> str:
    partes: list[str] = []
    mn = getattr(m, "movimentoNacional", None)
    if mn is not None:
        for x in (getattr(mn, "complemento", None) or []):
            if str(x).strip():
                partes.append(str(x).strip())
        if not partes:
            cod = getattr(mn, "codigoNacional", None)
            if cod is not None:
                partes.append(f"[movimento nacional {cod}]")
    ml = getattr(m, "movimentoLocal", None)
    if ml is not None:
        desc = getattr(ml, "descricao", None)
        if desc:
            partes.append(str(desc))
    for x in (getattr(m, "complemento", None) or []):
        if str(x).strip():
            partes.append(str(x).strip())
    return " — ".join(partes) if partes else "(sem descrição)"


def processo_para_dict(numero: str, processo: Any) -> dict:
    digitos = "".join(c for c in str(numero) if c.isdigit())
    resultado: dict = {
        "numero": digitos,
        "numero_formatado": formatar_numero_cnj(numero),
        "classe": "",
        "valor_causa": "",
        "orgao_julgador": "",
        "data_ajuizamento": "",
        "partes": [],
        "andamentos": [],
        "documentos": [],
    }
    if processo is None:
        return resultado

    cab = getattr(processo, "dadosBasicos", None)
    if cab is not None:
        resultado["classe"] = str(_attr(cab, "classeProcessual", padrao="") or "")
        resultado["valor_causa"] = formatar_valor_causa(_attr(cab, "valorCausa"))
        orgao = getattr(cab, "orgaoJulgador", None)
        resultado["orgao_julgador"] = str(_attr(orgao, "nomeOrgao", padrao="") or "") if orgao else ""
        resultado["data_ajuizamento"] = _data_ordenavel(_attr(cab, "dataAjuizamento"))[:8]

        for polo in (getattr(cab, "polo", None) or []):
            nomes = []
            integrantes = []
            for parte in (getattr(polo, "parte", None) or []):
                pessoa = getattr(parte, "pessoa", None)
                nome = _attr(pessoa, "nome", padrao="") if pessoa else ""
                if not nome:
                    continue
                nomes.append(str(nome))
                integrantes.append({
                    "nome": str(nome),
                    "advogados": _advogados(parte),
                    "intimacao_pendente": _verdade(_attr(parte, "intimacaoPendente")),
                })
            resultado["partes"].append({
                "polo": _POLOS.get(str(getattr(polo, "polo", "")).upper(), f"Polo {getattr(polo, 'polo', '')}"),
                "nomes": nomes,              # compatibilidade (lista simples de nomes)
                "integrantes": integrantes,  # detalhe: nome + advogados + intimação pendente
            })

    movs = list(getattr(processo, "movimento", None) or [])
    movs.sort(key=lambda m: _data_ordenavel(getattr(m, "dataHora", "")), reverse=True)
    for m in movs:
        resultado["andamentos"].append({
            "data": formatar_data(getattr(m, "dataHora", "")),
            "data_ordenavel": _data_ordenavel(getattr(m, "dataHora", "")),
            "codigo": _codigo_nacional(m),
            "texto": _texto_movimento(m),
        })

    for d in (getattr(processo, "documento", None) or []):
        resultado["documentos"].append({
            "id": str(_attr(d, "idDocumento", padrao="") or ""),
            "descricao": str(_attr(d, "descricao", padrao="(sem descrição)")),
            "tipo": str(_attr(d, "tipoDocumento", "tipoDocumentoLocal", padrao="") or ""),
            "mimetype": str(_attr(d, "mimetype", padrao="") or ""),
            "data": formatar_data(getattr(d, "dataHora", "")),
        })

    return resultado
