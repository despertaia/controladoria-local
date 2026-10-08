"""Ajustes da instalação local: json não secreto + segredos no cofre."""

import json
import os

import pytest

from nucleo import ajustes_locais, cofre
from tests.cofre_falso import cofre_falso  # noqa: F401

VARIAVEIS = ("CONTROLADORIA_TRIBUNAL", "PJE_CPF", "PJE_SENHA", "PJE_SENHA_2GRAU",
             "CARTEIRA_ADVOGADO_NOME", "CARTEIRA_OAB_NUMERO", "CARTEIRA_OAB_UF", "SECRET_KEY")


@pytest.fixture
def ambiente(tmp_path, monkeypatch, cofre_falso):  # noqa: F811
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "dados" / "ajustes.json"))
    # salvar() escreve direto em os.environ: o monkeypatch desfaz no fim do teste.
    for var in VARIAVEIS:
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    return tmp_path / "dados" / "ajustes.json"


def _salvar(**extra):
    dados = dict(tribunal="tjmg", advogado_nome="Maria da Silva", oab_numero="12345",
                 oab_uf="mg", pje_cpf="12345678901", pje_senha="senha-1")
    dados.update(extra)
    ajustes_locais.salvar(**dados)


def test_caminho_padrao_e_relativo_a_raiz_do_app(monkeypatch):
    monkeypatch.delenv("CONTROLADORIA_AJUSTES", raising=False)
    assert ajustes_locais.caminho() == ajustes_locais.RAIZ / "dados" / "ajustes.json"


def test_sem_nada_nao_esta_configurado(ambiente):
    assert ajustes_locais.ler() == {}
    assert ajustes_locais.configurado() is False


def test_salvar_grava_json_sem_segredo_e_cofre(ambiente, cofre_falso):  # noqa: F811
    _salvar(pje_senha_2grau="")
    texto = ambiente.read_text(encoding="utf-8")
    assert json.loads(texto) == {"tribunal": "TJMG", "tribunais": ["TJMG"],
                                 "advogado_nome": "Maria da Silva",
                                 "oab_numero": "12345", "oab_uf": "MG"}
    assert "senha" not in texto and "12345678901" not in texto
    assert cofre.ler("pje_cpf") == "12345678901" and cofre.ler("pje_senha") == "senha-1"
    assert ajustes_locais.configurado() is True
    assert not list(ambiente.parent.glob(".ajustes-*"))  # nada de temporário sobrando


def test_salvar_atualiza_o_ambiente_sobrescrevendo(ambiente, monkeypatch):
    monkeypatch.setenv("PJE_SENHA", "antiga")
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "TJMT")
    _salvar(pje_senha_2grau="")
    assert os.environ["PJE_SENHA"] == "senha-1"
    assert os.environ["CONTROLADORIA_TRIBUNAL"] == "TJMG"
    assert os.environ["CARTEIRA_OAB_UF"] == "MG"
    assert "PJE_SENHA_2GRAU" not in os.environ


def test_senha_none_mantem_a_guardada_e_vazio_apaga_a_do_2grau(ambiente):
    _salvar(tribunal="TJMT", pje_senha_2grau="senha-2")
    _salvar(tribunal="TJMT", pje_senha=None, pje_senha_2grau=None, advogado_nome="Maria S.")
    assert cofre.ler("pje_senha") == "senha-1" and cofre.ler("pje_senha_2grau") == "senha-2"
    assert ajustes_locais.ler()["advogado_nome"] == "Maria S."
    _salvar(pje_senha=None, pje_senha_2grau="")
    assert cofre.ler("pje_senha_2grau") is None


def test_salvar_preserva_casa_banca_e_porta(ambiente):
    _salvar(casa_banca="C:/Banca", porta=5070)
    _salvar(advogado_nome="Outra")
    a = ajustes_locais.ler()
    assert a["casa_banca"] == "C:/Banca" and a["porta"] == 5070
    assert ajustes_locais.porta_do_painel() == 5070


def test_porta_padrao(ambiente):
    assert ajustes_locais.porta_do_painel() == ajustes_locais.PORTA_PADRAO


def test_tribunal_desconhecido_nao_grava(ambiente):
    with pytest.raises(ValueError):
        _salvar(tribunal="TJXX")
    assert not ambiente.exists() and cofre.ler("pje_senha") is None


def test_json_ilegivel_vira_vazio(ambiente):
    ambiente.parent.mkdir(parents=True)
    ambiente.write_text("{quebrado", encoding="utf-8")
    assert ajustes_locais.ler() == {}


def test_carregar_no_ambiente_nao_sobrescreve_o_que_ja_existe(ambiente, monkeypatch):
    _salvar()
    for var in VARIAVEIS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PJE_SENHA", "do-env")
    monkeypatch.setenv("CARTEIRA_ADVOGADO_NOME", "Nome do .env")
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "")  # vazio conta como não definido
    definidos = ajustes_locais.carregar_no_ambiente()
    assert os.environ["PJE_SENHA"] == "do-env"
    assert os.environ["CARTEIRA_ADVOGADO_NOME"] == "Nome do .env"
    assert os.environ["CONTROLADORIA_TRIBUNAL"] == "TJMG"
    assert os.environ["PJE_CPF"] == "12345678901"
    assert os.environ["CARTEIRA_OAB_NUMERO"] == "12345"
    assert "PJE_SENHA" not in definidos and "PJE_CPF" in definidos
    assert "PJE_SENHA_2GRAU" not in os.environ


def test_secret_key_gerada_uma_vez_e_guardada(ambiente):
    ajustes_locais.carregar_no_ambiente()
    chave = os.environ["SECRET_KEY"]
    assert len(chave) >= 48 and cofre.ler("secret_key") == chave
    os.environ.pop("SECRET_KEY")
    ajustes_locais.carregar_no_ambiente()
    assert os.environ["SECRET_KEY"] == chave


def test_secret_key_sem_cofre_vale_so_nesta_execucao(ambiente, monkeypatch):
    def recusa(*_a):
        raise cofre.CofreError("sem cofre")
    monkeypatch.setattr(cofre, "gravar", recusa)
    assert len(ajustes_locais.secret_key()) >= 48


def test_pje_ligado_exige_senha_no_cofre(ambiente):
    _salvar()
    cofre.apagar("pje_senha")
    assert ajustes_locais.pje_ligado() is True  # a do tribunal (pje_senha_TJMG) basta
    cofre.apagar("pje_senha_TJMG")
    assert ajustes_locais.pje_ligado() is False
    assert ajustes_locais.configurado() is True  # nome e OAB bastam para o DJEN


def test_salvar_sem_tribunal_tira_o_pje(ambiente):
    _salvar()
    ajustes_locais.salvar(tribunais=[], advogado_nome="Ana", oab_numero="1", oab_uf="GO",
                          pje_cpf="12345678901")
    assert cofre.ler("pje_cpf") is None and cofre.ler("pje_senha_TJMG") is None
    assert ajustes_locais.ler()["tribunais"] == [] and "tribunal" not in ajustes_locais.ler()
    assert ajustes_locais.configurado() is True and ajustes_locais.pje_ligado() is False
