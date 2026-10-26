"""Instâncias do PJe que a controladoria consulta: as do tribunal do escritório
(`CONTROLADORIA_TRIBUNAL`, ver nucleo/tribunal.py).

Cada item: chave (vira subpasta e sufixo da senha: senha_<chave>), rótulo,
variável de ambiente com a URL do WSDL e a URL padrão.

O tribunal é lido a cada uso, não no import: o lançador local carrega os ajustes
no ambiente depois de alguns imports.
"""

from __future__ import annotations

from collections.abc import Sequence

from nucleo import tribunal


def instancias(trib=None) -> list[tuple[str, str, str, str]]:
    """Instâncias do tribunal pedido (objeto ou sigla); padrão, o principal."""
    return list(tribunal.obter(trib).instancias)


class _InstanciasDoTribunal(Sequence):
    """`INSTANCIAS` antigo (era uma lista fixa do TJMT): agora acompanha o tribunal
    atual em cada iteração, mesmo para quem fez `from captura.instancias import
    INSTANCIAS` antes de o ambiente ser ajustado."""

    def __getitem__(self, indice):
        return instancias()[indice]

    def __len__(self) -> int:
        return len(instancias())

    def __repr__(self) -> str:
        return repr(instancias())


INSTANCIAS = _InstanciasDoTribunal()
