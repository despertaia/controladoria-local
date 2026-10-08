"""Cofre do sistema (keyring) com backend em memória."""

import logging

import pytest

from nucleo import cofre
from tests.cofre_falso import cofre_falso, keyring  # noqa: F401


def test_grava_le_e_apaga(cofre_falso):
    assert cofre.ler("pje_senha") is None
    cofre.gravar("pje_senha", "s3cr3ta")
    assert cofre_falso.itens == {(cofre.SERVICO, "pje_senha"): "s3cr3ta"}
    assert cofre.ler("pje_senha") == "s3cr3ta"
    cofre.apagar("pje_senha")
    assert cofre.ler("pje_senha") is None
    cofre.apagar("pje_senha")  # apagar o que não existe não é erro


def test_gravar_vazio_apaga(cofre_falso):
    cofre.gravar("pje_senha_2grau", "x")
    cofre.gravar("pje_senha_2grau", "")
    assert cofre.ler("pje_senha_2grau") is None


def test_backend_nulo_le_none_e_recusa_gravar():
    from keyring.backends import null
    anterior = keyring.get_keyring()
    keyring.set_keyring(null.Keyring())
    try:
        assert cofre.ler("pje_senha") is None
        with pytest.raises(cofre.CofreError, match="nenhum cofre"):
            cofre.gravar("pje_senha", "s3cr3ta")
    finally:
        keyring.set_keyring(anterior)


def test_erro_do_backend_vira_none_na_leitura_e_cofreerror_na_gravacao(caplog):
    from keyring.backends import fail
    anterior = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        with caplog.at_level(logging.WARNING):
            assert cofre.ler("pje_senha") is None
        with pytest.raises(cofre.CofreError) as exc:
            cofre.gravar("pje_senha", "s3cr3ta")
        assert "s3cr3ta" not in str(exc.value)
        with pytest.raises(cofre.CofreError):
            cofre.apagar("pje_senha")
    finally:
        keyring.set_keyring(anterior)
    assert "pje_senha" in caplog.text


def test_nunca_loga_o_valor(cofre_falso, caplog, monkeypatch):
    def quebra(*_a):
        raise keyring.errors.KeyringError("trancado")
    with caplog.at_level(logging.DEBUG):
        cofre.gravar("pje_senha", "valor-que-nao-pode-vazar")
        monkeypatch.setattr(cofre_falso, "get_password", quebra)
        cofre.ler("pje_senha")
    assert "valor-que-nao-pode-vazar" not in caplog.text


def test_sem_keyring_instalado_le_none_e_recusa_gravar(monkeypatch):
    def sem_keyring():
        raise cofre.CofreError("O cofre do sistema não está disponível neste computador.")
    monkeypatch.setattr(cofre, "_keyring", sem_keyring)
    assert cofre.ler("pje_senha") is None
    with pytest.raises(cofre.CofreError, match="não está disponível"):
        cofre.gravar("pje_senha", "x")
