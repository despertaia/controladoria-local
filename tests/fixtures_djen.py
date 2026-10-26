"""Itens falsos da API do DJEN (dados fictícios) para os testes."""


def item_djen(id_, numero, tribunal="TJMT", data="2026-07-10", oab=None):
    """oab=("54321", "MT") simula publicação com o advogado vinculado pela OAB
    (como fazem TRF1/TRF3); sem oab, como o TJMT publica."""
    advogados = ([{"advogado": {"nome": "MARIA EXEMPLO DA SILVA",
                                "numero_oab": oab[0], "uf_oab": oab[1]}}] if oab else [])
    return {
        "id": id_, "numero_processo": numero, "siglaTribunal": tribunal,
        "data_disponibilizacao": data, "tipoComunicacao": "Intimação",
        "nomeOrgao": "1ª VARA CÍVEL", "nomeClasse": "PROCEDIMENTO COMUM CÍVEL",
        "texto": "Intime-se a parte.", "link": "https://exemplo.invalido/documento",
        "destinatarioadvogados": advogados,
    }
