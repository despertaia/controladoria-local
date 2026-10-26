"""deploy/verificar_tribunal.py: lê o cofre do banco de produção (só leitura) antes de
procurar a credencial."""

import importlib.util
import pathlib

import pytest

CAMINHO = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "verificar_tribunal.py"


@pytest.fixture
def verificar(monkeypatch):
    spec = importlib.util.spec_from_file_location("verificar_tribunal", CAMINHO)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    ordem = []
    monkeypatch.setattr(modulo.cofre, "carregar_do_banco_somente_leitura",
                        lambda caminho: ordem.append(("cofre", caminho)) or True)
    monkeypatch.setattr(modulo.credenciais, "credencial_do_env",
                        lambda *a: ordem.append(("env",)))
    monkeypatch.setattr(modulo.cofre, "problema_do_cofre_no_banco",
                        lambda caminho: ordem.append(("problema", caminho)) or "")
    modulo.ordem = ordem
    return modulo


def test_le_o_cofre_do_banco_de_producao_antes_do_env(verificar, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO_PRODUCAO", "/opt/app/dados/controladoria.db")
    assert verificar.main() == 0
    assert verificar.ordem == [("problema", "/opt/app/dados/controladoria.db"),
                               ("cofre", "/opt/app/dados/controladoria.db"), ("env",)]


def test_sem_a_variavel_nao_toca_no_cofre(verificar, monkeypatch):
    monkeypatch.delenv("CONTROLADORIA_BANCO_PRODUCAO", raising=False)
    assert verificar.main() == 0
    assert verificar.ordem == [("env",)]


def test_banco_com_senhas_e_cofre_que_nao_abre_barra_a_publicacao(verificar, monkeypatch,
                                                                   capsys):
    monkeypatch.setenv("CONTROLADORIA_BANCO_PRODUCAO", "/opt/app/dados/controladoria.db")
    monkeypatch.setattr(verificar.cofre, "problema_do_cofre_no_banco",
                        lambda caminho: "a chave do cofre sumiu")
    assert verificar.main() == 1
    assert "a chave do cofre sumiu" in capsys.readouterr().out
    assert verificar.ordem == []  # nem carrega o cofre nem cai no .env


def test_de_ponta_a_ponta_sem_chave_barra_e_sem_credenciais_segue(tmp_path, monkeypatch,
                                                                   capsys):
    """Sem stub do cofre: banco real com senha cifrada e a chave sumida."""
    from nucleo import banco, cofre_servidor, credenciais as nc
    spec = importlib.util.spec_from_file_location("verificar_tribunal_real", CAMINHO)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    monkeypatch.setattr(modulo, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setattr(nc, "_definidas", set())
    caminho = tmp_path / "prod.db"
    monkeypatch.setenv("CONTROLADORIA_BANCO_PRODUCAO", str(caminho))
    banco.conectar(str(caminho)).close()
    # banco sem credenciais e sem senha no .env: nada a verificar
    assert modulo.main() == 0
    assert "nada a verificar" in capsys.readouterr().out
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "cofre.key"))
    cofre_servidor.criar_chave()
    conn = banco.conectar(str(caminho))
    with conn:
        nc.gravar_cpf(conn, "11111111111")
        nc.gravar(conn, "TJMT", "1grau", "segredo-mt")
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "sumiu.key"))
    assert modulo.main() == 1
    saida = capsys.readouterr().out
    assert "chave do cofre" in saida
    assert "segredo-mt" not in saida and "11111111111" not in saida
