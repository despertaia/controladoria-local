"""Identifica o advogado pela inscrição na OAB, nos formatos que aparecem
nos tribunais: PJe/TJMT "MT0054321A", DJEN número "54321" + UF "MT",
e variações como "54321/MT". Nunca pelo nome: há homônimos de sobrenome."""

from __future__ import annotations

import re


UFS = frozenset("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split())


def normalizar_inscricao(inscricao: str | None) -> tuple[str, str] | None:
    """(UF ou "", número sem zeros à esquerda); None se não houver número.

    Ignora o prefixo "OAB", junta o número separado por ponto ou espaço
    ("54.321") e só aceita como UF uma sigla de estado válida."""
    texto = str(inscricao or "").upper()
    texto = re.sub(r"\bOAB\b", " ", texto)
    texto = re.sub(r"(?<=\d)[.\s](?=\d)", "", texto)
    numero = re.search(r"\d+", texto)
    if not numero:
        return None
    uf = next((t for t in re.findall(r"(?<![A-Z])[A-Z]{2}(?![A-Z])", texto) if t in UFS), "")
    return (uf, numero.group(0).lstrip("0") or "0")


def e_o_advogado(inscricao: str, numero_oab: str, uf_oab: str) -> bool:
    """True se a inscrição é da OAB informada. Sem UF na inscrição, vale o número."""
    achada = normalizar_inscricao(inscricao)
    alvo = normalizar_inscricao(f"{uf_oab}{numero_oab}")
    if achada is None or alvo is None:
        return False
    return achada[1] == alvo[1] and (not achada[0] or achada[0] == alvo[0])
