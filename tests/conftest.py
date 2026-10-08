"""Configuração comum dos testes."""

import pytest


@pytest.fixture(autouse=True)
def _csrf_desligado_por_padrao():
    """Os testes de tela antigos postam sem token; tests/test_csrf.py religa e
    confere a proteção de verdade (e que todo formulário POST tem o campo)."""
    import painel
    anterior = painel.app.config.get("CSRF_EXIGIDO", True)
    painel.app.config["CSRF_EXIGIDO"] = False
    yield
    painel.app.config["CSRF_EXIGIDO"] = anterior


@pytest.fixture(autouse=True)
def _pecas_em_pasta_temporaria(tmp_path, monkeypatch):
    """O banco de demonstração grava as peças fictícias em CONTROLADORIA_PECAS: os
    testes nunca escrevem em dados/ do repositório (quem precisa de outra pasta
    redefine a variável)."""
    monkeypatch.setenv("CONTROLADORIA_PECAS", str(tmp_path / "pecas-do-teste"))


@pytest.fixture(autouse=True)
def _tribunal_padrao(monkeypatch):
    """Os testes partem do tribunal da produção (TJMT) e sem as PJE_* da máquina;
    quem testa o TJMG define CONTROLADORIA_TRIBUNAL."""
    from nucleo.tribunal import TRIBUNAIS
    por_tribunal = [f"{nome}_{sigla}" for sigla in TRIBUNAIS
                    for nome in ("PJE_SENHA", "PJE_SENHA_2GRAU")]
    for variavel in ("CONTROLADORIA_TRIBUNAL", "CONTROLADORIA_TRIBUNAIS", "PJE_CPF",
                     "PJE_SENHA", "PJE_SENHA_2GRAU", "PJE_TIMEOUT", "PJE_INTERVALO_SEGUNDOS",
                     *por_tribunal):
        # setenv antes do delenv: o monkeypatch passa a desfazer também o que o código
        # testado gravar direto em os.environ (ajustes_locais.salvar).
        monkeypatch.setenv(variavel, "")
        monkeypatch.delenv(variavel)


@pytest.fixture(autouse=True)
def _versao_sem_rede(monkeypatch):
    """Nenhum teste consulta o GitHub: a versão publicada começa sem cache e a busca
    padrão falha (quem testa a consulta passa um `obter` falso)."""
    from nucleo import versao

    def _sem_rede(url, timeout):
        raise OSError("sem rede nos testes")
    monkeypatch.setattr(versao, "_obter_padrao", _sem_rede)
    versao.limpar_cache()
    yield
    versao.limpar_cache()
