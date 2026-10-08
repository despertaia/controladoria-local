from datetime import date

import carteira as cli


def test_exportar_com_banco_vazio(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    saida = tmp_path / "Carteira.xlsx"
    assert cli.main(["exportar", "--saida", str(saida)]) == 0
    assert saida.exists()


def test_atualizar_sem_configuracao(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    for variavel in ("CARTEIRA_ADVOGADO_NOME", "CARTEIRA_OAB_NUMERO", "CARTEIRA_OAB_UF"):
        monkeypatch.delenv(variavel, raising=False)
    assert cli.main(["atualizar"]) == 1
    assert "CARTEIRA_OAB_NUMERO" in capsys.readouterr().err


def test_atualizar_imprime_resumo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.setenv("CARTEIRA_ADVOGADO_NOME", "MARIA EXEMPLO DA SILVA")
    monkeypatch.setenv("CARTEIRA_OAB_NUMERO", "54321")
    monkeypatch.setenv("CARTEIRA_OAB_UF", "MT")
    recebido = {}

    def falso(conn, adv, *, desde):
        recebido.update(adv=adv, desde=desde)
        return {"processos": 3, "carteira_ativa": 2}

    monkeypatch.setattr(cli.nucleo_carteira, "atualizar", falso)
    assert cli.main(["atualizar", "--desde", "2024-01-01"]) == 0
    assert recebido["desde"] == date(2024, 1, 1) and recebido["adv"].numero_oab == "54321"
    assert "processos=3" in capsys.readouterr().out


def test_main_trabalha_na_pasta_do_projeto(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["exportar", "--saida", str(tmp_path / "C.xlsx")]) == 0
    assert os.path.realpath(os.getcwd()) == os.path.realpath(os.path.dirname(cli.__file__))
