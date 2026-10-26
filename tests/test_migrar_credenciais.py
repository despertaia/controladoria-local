"""deploy/migrar_credenciais.py: passa TJMT_* do .env para o cofre, com ensaio,
backup e sem nunca imprimir senha ou CPF."""

import importlib.util
import os
import pathlib
import stat

import pytest

from nucleo import banco, cofre_servidor, credenciais, conexao

CAMINHO = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "migrar_credenciais.py"
CPF = "12345678901"
SENHA1 = "SenhaUm#771"
SENHA2 = "SenhaDois#992"

ENV = (f"SECRET_KEY=abc\nTJMT_CPF={CPF}\nTJMT_SENHA={SENHA1}\nTJMT_SENHA_2GRAU={SENHA2}\n"
       "PAINEL_USUARIO=daniel\n")


@pytest.fixture
def migrar():
    spec = importlib.util.spec_from_file_location("migrar_credenciais", CAMINHO)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "cofre.key"))
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "b.db"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setattr(credenciais, "_definidas", set())
    cofre_servidor.criar_chave()
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    return env, tmp_path / "backups"


def _testar_tudo(estado="ok", mensagem=""):
    chamadas = []

    def falso(sigla, instancia, cpf, senha):
        chamadas.append((sigla, instancia))
        return estado, mensagem
    falso.chamadas = chamadas
    return falso


def _rodar(migrar, env, backups, *extra):
    return migrar.main(["--env", str(env), "--backup-dir", str(backups), *extra])


def _cofre():
    conn = banco.conectar()
    try:
        return credenciais.cpf_guardado(conn), credenciais.cadastradas(conn)
    finally:
        conn.close()


def test_ensaio_testa_mas_nao_grava_nada(migrar, ambiente, monkeypatch, capsys):
    env, backups = ambiente
    falso = _testar_tudo()
    monkeypatch.setattr(migrar.conexao, "testar", falso)
    assert _rodar(migrar, env, backups) == 0
    assert falso.chamadas == [("TJMT", "1grau"), ("TJMT", "2grau")]
    assert env.read_text(encoding="utf-8") == ENV
    assert not backups.exists()
    assert _cofre() == ("", set())
    saida = capsys.readouterr().out
    assert "✓ TJMT 1ª instância: login aceito" in saida
    assert "✓ TJMT 2ª instância: login aceito" in saida


def test_aplicar_grava_faz_backup_e_comenta_o_env(migrar, ambiente, monkeypatch, capsys):
    env, backups = ambiente
    monkeypatch.setattr(migrar.conexao, "testar", _testar_tudo())
    assert _rodar(migrar, env, backups, "--aplicar") == 0
    cpf, cadastradas = _cofre()
    assert cpf == CPF and cadastradas == {("TJMT", "1grau"), ("TJMT", "2grau")}
    conn = banco.conectar()
    try:
        assert credenciais.ler(conn, "TJMT", "1grau")["senha"] == SENHA1
        assert credenciais.ler(conn, "TJMT", "2grau")["senha"] == SENHA2
        estados = {(r["sigla"], r["instancia"]): r["estado"] for r in
                   conn.execute("SELECT sigla, instancia, estado FROM conexao_tribunal")}
    finally:
        conn.close()
    assert estados == {("TJMT", "1grau"): "ok", ("TJMT", "2grau"): "ok"}
    copias = list(backups.glob(".env.*"))
    assert len(copias) == 1
    assert copias[0].read_text(encoding="utf-8") == ENV
    if os.name != "nt":  # permissão POSIX: no Windows o chmod não restringe assim
        assert stat.S_IMODE(copias[0].stat().st_mode) == 0o600
    texto = env.read_text(encoding="utf-8")
    linhas = texto.splitlines()
    for segredo in (SENHA1, SENHA2, CPF):
        assert segredo not in texto  # nem em comentário
    for chave in ("TJMT_SENHA", "TJMT_SENHA_2GRAU", "TJMT_CPF"):
        assert not any(l.startswith(chave + "=") for l in linhas)
        assert sum(l.startswith(f"# {chave} movida para o cofre em ")
                   and l[len(f"# {chave} movida para o cofre em "):].count("-") == 2
                   for l in linhas) == 1
    assert "SECRET_KEY=abc" in linhas and "PAINEL_USUARIO=daniel" in linhas
    saida = capsys.readouterr().out
    assert "✓ TJMT 1ª instância: login aceito" in saida
    chave = migrar.cofre_servidor.caminho_da_chave()
    assert (f"Guarde uma cópia de {chave} fora deste servidor: sem ela, o backup do banco "
            "não recupera as senhas.") in saida


def test_aplicar_com_recusa_sai_1_e_nao_muda_nada(migrar, ambiente, monkeypatch, capsys):
    env, backups = ambiente

    def recusa_a_segunda(sigla, instancia, cpf, senha):
        return ("ok", "") if instancia == "1grau" else ("recusada", "senha recusada")
    monkeypatch.setattr(migrar.conexao, "testar", recusa_a_segunda)
    assert _rodar(migrar, env, backups, "--aplicar") == 1
    assert env.read_text(encoding="utf-8") == ENV
    assert not backups.exists()
    assert _cofre() == ("", set())
    saida = capsys.readouterr().out
    assert "✓ TJMT 1ª instância: login aceito" in saida
    assert "✗ TJMT 2ª instância" in saida and "senha recusada" in saida


def test_ensaio_com_recusa_tambem_sai_1(migrar, ambiente, monkeypatch):
    env, backups = ambiente
    monkeypatch.setattr(migrar.conexao, "testar", _testar_tudo("fora", "tribunal fora do ar"))
    assert _rodar(migrar, env, backups) == 1


def test_saida_nunca_traz_senha_nem_cpf(migrar, ambiente, monkeypatch, capsys):
    env, backups = ambiente
    # mesmo que a mensagem da sonda trouxesse o segredo, ele é escondido
    monkeypatch.setattr(migrar.conexao, "testar",
                        _testar_tudo("recusada", f"erro com {SENHA1} e {CPF} e {SENHA2}"))
    _rodar(migrar, env, backups, "--aplicar")
    monkeypatch.setattr(migrar.conexao, "testar", _testar_tudo())
    _rodar(migrar, env, backups, "--aplicar")
    captura = capsys.readouterr()
    for segredo in (SENHA1, SENHA2, CPF):
        assert segredo not in captura.out and segredo not in captura.err


def test_so_a_primeira_instancia(migrar, tmp_path, monkeypatch, ambiente, capsys):
    env, backups = ambiente
    env.write_text(f"TJMT_CPF={CPF}\nTJMT_SENHA={SENHA1}\n", encoding="utf-8")
    falso = _testar_tudo()
    monkeypatch.setattr(migrar.conexao, "testar", falso)
    assert _rodar(migrar, env, backups, "--aplicar") == 0
    assert falso.chamadas == [("TJMT", "1grau")]
    assert _cofre() == (CPF, {("TJMT", "1grau")})
    texto = env.read_text(encoding="utf-8")
    assert texto.count("movida para o cofre em ") == 2 and SENHA1 not in texto and CPF not in texto


def test_senha_sem_cpf_sai_1_e_sem_senha_nao_ha_o_que_migrar(migrar, ambiente, monkeypatch):
    env, backups = ambiente
    monkeypatch.setattr(migrar.conexao, "testar", _testar_tudo())
    env.write_text(f"TJMT_SENHA={SENHA1}\n", encoding="utf-8")
    assert _rodar(migrar, env, backups, "--aplicar") == 1
    env.write_text(f"TJMT_CPF={CPF}\n", encoding="utf-8")  # já migrado: idempotente
    assert _rodar(migrar, env, backups, "--aplicar") == 0
    assert _cofre() == ("", set()) and not backups.exists()


def test_aplicar_sem_chave_do_cofre_sai_1_sem_mexer(migrar, ambiente, monkeypatch, tmp_path):
    env, backups = ambiente
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    monkeypatch.setattr(migrar.conexao, "testar", _testar_tudo())
    assert _rodar(migrar, env, backups, "--aplicar") == 1
    assert env.read_text(encoding="utf-8") == ENV and not backups.exists()


@pytest.mark.skipif(os.name == "nt", reason="permissão POSIX (o .env do servidor é Linux)")
def test_env_mantem_o_modo_do_arquivo(migrar, ambiente, monkeypatch):
    env, backups = ambiente
    os.chmod(env, 0o600)
    monkeypatch.setattr(migrar.conexao, "testar", _testar_tudo())
    assert _rodar(migrar, env, backups, "--aplicar") == 0
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
