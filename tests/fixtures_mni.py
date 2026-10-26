"""Respostas falsas do MNI (nomes e números fictícios) para os testes."""

from types import SimpleNamespace as NS

OAB_DANIEL = "MT0054321A"


def advogado(nome, inscricao):
    return NS(nome=nome, inscricao=inscricao)


def parte(nome, *advogados, intimacao=False):
    return NS(pessoa=NS(nome=nome), advogado=list(advogados), intimacaoPendente=intimacao)


def polo(codigo, *partes):
    return NS(polo=codigo, parte=list(partes))


def movimento(data_hora, codigo, texto):
    return NS(dataHora=data_hora,
              movimentoNacional=NS(codigoNacional=codigo, complemento=[texto]),
              movimentoLocal=None, complemento=None)


def processo(polos, movimentos=(), *, classe="7", valor="1500.5",
             orgao="3ª VARA CÍVEL DE CUIABÁ", ajuizamento="20230105000000"):
    cab = NS(classeProcessual=classe, valorCausa=valor, orgaoJulgador=NS(nomeOrgao=orgao),
             polo=list(polos), dataAjuizamento=ajuizamento)
    return NS(dadosBasicos=cab, movimento=list(movimentos), documento=[])


def processo_do_cliente(movimentos=()):
    """Daniel advoga para MARIA CLIENTE (polo passivo) contra BANCO ALFA S.A.
    No mesmo polo há um homônimo de sobrenome (OUTRO SAGIN, OAB diferente)."""
    return processo([
        polo("AT", parte("BANCO ALFA S.A.", advogado("FULANA DE TAL", "MT0099999A"))),
        polo("PA", parte("MARIA CLIENTE",
                         advogado("MARIA EXEMPLO DA SILVA", OAB_DANIEL),
                         advogado("OUTRO SAGIN", "MT0010999A"))),
    ], movimentos)
