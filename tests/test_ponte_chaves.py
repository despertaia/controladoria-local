"""Cofre da ponte: `nucleo.cofre` no modo local (nomes ponte_chave/lex_claude),
`security` com os nomes de sempre no Mac do escritório."""

import pytest

from nucleo import cofre
from ponte_mac import chaves
from tests.cofre_falso import cofre_falso  # noqa: F401


@pytest.fixture
def local(cofre_falso, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    return cofre_falso


def _nao_roda(*a, **kw):
    pytest.fail("no modo local nada de processo (nem `security`)")


def test_modo_local_usa_o_cofre_da_instalacao_com_os_nomes_locais(local):
    chaves.guardar(chaves.CHAVE_PONTE, "A" * 43, rodar=_nao_roda)
    chaves.guardar(chaves.ACESSO_CLAUDE, "sk-ant-oat01-x", rodar=_nao_roda)
    assert local.itens == {(cofre.SERVICO, "ponte_chave"): "A" * 43,
                           (cofre.SERVICO, "lex_claude"): "sk-ant-oat01-x"}
    assert chaves.ler(chaves.CHAVE_PONTE, rodar=_nao_roda) == "A" * 43
    assert chaves.ler(chaves.ACESSO_CLAUDE, rodar=_nao_roda) == "sk-ant-oat01-x"


def test_modo_local_troca_e_apaga(local):
    chaves.guardar(chaves.ACESSO_CLAUDE, "sk-ant-oat01-um")
    chaves.guardar(chaves.ACESSO_CLAUDE, "sk-ant-oat01-dois")
    assert chaves.ler(chaves.ACESSO_CLAUDE) == "sk-ant-oat01-dois"
    chaves.apagar(chaves.ACESSO_CLAUDE)
    assert chaves.ler(chaves.ACESSO_CLAUDE) is None
    chaves.apagar(chaves.ACESSO_CLAUDE)  # já não havia: tudo bem


def test_modo_local_recusa_valor_fora_do_padrao_e_nome_desconhecido(local):
    with pytest.raises(ValueError):
        chaves.guardar(chaves.ACESSO_CLAUDE, "tem espaço")
    with pytest.raises(ValueError):
        chaves.guardar("outra-coisa", "x")
    assert local.itens == {}


def test_modo_local_cofre_que_nao_guarda_vira_runtimeerror_sem_o_valor(local, monkeypatch):
    monkeypatch.setattr(local, "set_password", lambda *a: None)  # aceita e descarta
    with pytest.raises(RuntimeError) as exc:
        chaves.guardar(chaves.ACESSO_CLAUDE, "sk-ant-oat01-SEGREDO")
    assert "SEGREDO" not in str(exc.value)


def test_mac_do_escritorio_continua_no_security(monkeypatch):
    """Fora do modo local: o item que o `security add-generic-password` criou."""
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    chamadas = []

    def rodar(args, **kw):
        chamadas.append(args)

        class R:
            returncode, stdout = 0, "guardado\n"
        return R()

    assert chaves.ler(chaves.CHAVE_PONTE, rodar=rodar) == "guardado"
    chaves.apagar(chaves.CHAVE_PONTE, rodar=rodar)
    assert chamadas == [["security", "find-generic-password", "-s", chaves.CHAVE_PONTE, "-w"],
                        ["security", "delete-generic-password", "-s", chaves.CHAVE_PONTE]]
