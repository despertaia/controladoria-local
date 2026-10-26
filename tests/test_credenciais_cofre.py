import os
import pytest
from nucleo import banco, cofre_servidor, credenciais


@pytest.fixture
def chave(tmp_path, monkeypatch):
    caminho = tmp_path / "cofre.key"
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(caminho))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    cofre_servidor.criar_chave()
    return caminho


def test_cifra_e_decifra_sem_guardar_em_claro(chave):
    token = cofre_servidor.cifrar("s3nh@ secreta")
    assert "s3nh@" not in token
    assert cofre_servidor.decifrar(token) == "s3nh@ secreta"
    if os.name != "nt":  # permissão POSIX: no Windows o chmod não restringe assim
        assert oct(chave.stat().st_mode & 0o777) == "0o600"


def test_criar_chave_nao_sobrescreve(chave):
    antes = chave.read_bytes()
    cofre_servidor.criar_chave()
    assert chave.read_bytes() == antes


def test_chave_errada_da_erro_sem_vazar(chave, tmp_path, monkeypatch):
    token = cofre_servidor.cifrar("s3nh@")
    outra = tmp_path / "outra.key"
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(outra))
    cofre_servidor.criar_chave()
    with pytest.raises(cofre_servidor.CofreServidorError) as exc:
        cofre_servidor.decifrar(token)
    assert "s3nh@" not in str(exc.value)


def test_gravar_ler_apagar_na_vps(chave, tmp_path):
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "12345678901")
        credenciais.gravar(conn, "TJMT", "1grau", "abc")
    linha = conn.execute("SELECT * FROM credencial").fetchone()
    assert "abc" not in linha["senha_cifrada"]
    assert "12345678901" not in conn.execute("SELECT valor FROM config WHERE chave = 'pje_cpf_cifrado'").fetchone()[0]
    assert credenciais.ler(conn, "TJMT", "1grau") == {"cpf": "12345678901", "senha": "abc"}
    assert credenciais.cadastradas(conn) == {("TJMT", "1grau")}
    with conn:
        credenciais.apagar(conn, "TJMT", "1grau")
    assert credenciais.ler(conn, "TJMT", "1grau") is None


def test_carregar_no_ambiente_vps(chave, tmp_path, monkeypatch):
    for v in ("PJE_CPF", "PJE_SENHA_TJMT", "PJE_SENHA_2GRAU_TJMT", "PJE_SENHA_TJMG",
              "CONTROLADORIA_TRIBUNAIS"):
        monkeypatch.delenv(v, raising=False)
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg")
        credenciais.gravar(conn, "TJMT", "2grau", "mt2")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["PJE_CPF"] == "11111111111"
    assert os.environ["PJE_SENHA_2GRAU_TJMT"] == "mt2"
    assert os.environ["PJE_SENHA_TJMG"] == "mg"
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"
    with conn:
        credenciais.apagar(conn, "TJMG", "1grau")
    credenciais.carregar_no_ambiente(conn)
    assert "PJE_SENHA_TJMG" not in os.environ


def test_sem_chave_vps_nao_carrega_e_avisa(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    conn = banco.conectar(str(tmp_path / "b.db"))
    assert cofre_servidor.pronto() is False
    credenciais.carregar_no_ambiente(conn)  # não levanta: cai no .env de sempre
    with pytest.raises(cofre_servidor.CofreServidorError):
        credenciais.gravar_cpf(conn, "11111111111")


def test_modo_local_usa_o_cofre_do_sistema(tmp_path, monkeypatch):
    guardado = {}
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setattr("nucleo.cofre.gravar", lambda n, v: guardado.__setitem__(n, v))
    monkeypatch.setattr("nucleo.cofre.ler", lambda n: guardado.get(n))
    monkeypatch.setattr("nucleo.cofre.apagar", lambda n: guardado.pop(n, None))
    conn = banco.conectar(str(tmp_path / "b.db"))
    credenciais.gravar_cpf(conn, "22222222222")
    credenciais.gravar(conn, "TJMG", "1grau", "x")
    assert guardado["pje_senha_TJMG"] == "x" and guardado["pje_cpf"] == "22222222222"
    assert conn.execute("SELECT COUNT(*) FROM credencial").fetchone()[0] == 0
    assert credenciais.ler(conn, "TJMG", "1grau") == {"cpf": "22222222222", "senha": "x"}


def test_gravar_exige_cpf_e_cpf_so_digitos(chave, tmp_path):
    conn = banco.conectar(str(tmp_path / "b.db"))
    with pytest.raises(ValueError, match="Guarde primeiro o seu CPF."):
        credenciais.gravar(conn, "TJMT", "1grau", "abc")
    with conn:
        credenciais.gravar_cpf(conn, "123.456.789-01")
    assert credenciais.cpf_guardado(conn) == "12345678901"
    with pytest.raises(ValueError):
        credenciais.gravar_cpf(conn, "123")


def test_instancias_homologadas():
    inst = credenciais.INSTANCIAS_HOMOLOGADAS
    assert ("TJMT", "1grau", "1ª instância") in inst
    assert ("TJMT", "2grau", "2ª instância") in inst
    assert ("TJMG", "1grau", "1ª instância") in inst
    assert len(inst) == 3


def test_local_guarda_a_lista_de_tribunais_e_ambiente(tmp_path, monkeypatch):
    guardado = {}
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setattr("nucleo.cofre.gravar", lambda n, v: guardado.__setitem__(n, v))
    monkeypatch.setattr("nucleo.cofre.ler", lambda n: guardado.get(n))
    monkeypatch.setattr("nucleo.cofre.apagar", lambda n: guardado.pop(n, None))
    monkeypatch.setenv("SECRET_KEY", "k")
    for v in ("PJE_CPF", "PJE_SENHA_TJMT", "PJE_SENHA_2GRAU_TJMT", "PJE_SENHA_TJMG",
              "PJE_SENHA", "PJE_SENHA_2GRAU", "CONTROLADORIA_TRIBUNAIS", "CONTROLADORIA_TRIBUNAL"):
        monkeypatch.delenv(v, raising=False)
    from nucleo import ajustes_locais
    conn = banco.conectar(str(tmp_path / "b.db"))
    credenciais.gravar_cpf(conn, "22222222222")
    credenciais.gravar(conn, "TJMT", "1grau", "a")
    credenciais.gravar(conn, "TJMT", "2grau", "b")
    credenciais.gravar(conn, "TJMG", "1grau", "c")
    assert ajustes_locais.tribunais_guardados() == ["TJMT", "TJMG"]
    assert credenciais.cadastradas(conn) == {("TJMT", "1grau"), ("TJMT", "2grau"), ("TJMG", "1grau")}
    credenciais.apagar(conn, "TJMT", "1grau")
    assert "TJMT" in ajustes_locais.tribunais_guardados()
    credenciais.apagar(conn, "TJMT", "2grau")
    assert ajustes_locais.tribunais_guardados() == ["TJMG"]
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["PJE_SENHA_TJMG"] == "c"
    monkeypatch.delenv("PJE_CPF"); monkeypatch.delenv("PJE_SENHA_TJMG")
    monkeypatch.delenv("CONTROLADORIA_TRIBUNAIS"); monkeypatch.delenv("CONTROLADORIA_TRIBUNAL")


# --- rodada 1 -------------------------------------------------------------------

@pytest.fixture
def local(tmp_path, monkeypatch):
    """Modo local com cofre em memória e ajustes.json numa pasta temporária."""
    guardado = {}
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    monkeypatch.setenv("SECRET_KEY", "k")
    monkeypatch.setattr("nucleo.cofre.gravar",
                        lambda n, v: guardado.__setitem__(n, v) if v else guardado.pop(n, None))
    monkeypatch.setattr("nucleo.cofre.ler", lambda n: guardado.get(n))
    monkeypatch.setattr("nucleo.cofre.apagar", lambda n: guardado.pop(n, None))
    monkeypatch.setattr(credenciais, "_definidas", set())
    return guardado


def test_local_apagar_principal_com_chaves_antigas(local, tmp_path):
    from nucleo import ajustes_locais
    conn = banco.conectar(str(tmp_path / "b.db"))
    ajustes_locais.guardar_tribunais(["TJMT"])
    local.update({"pje_cpf": "22222222222", "pje_senha": "velha1", "pje_senha_2grau": "velha2"})
    assert credenciais.ler(conn, "TJMT", "1grau")["senha"] == "velha1"
    credenciais.apagar(conn, "TJMT", "1grau")
    assert credenciais.ler(conn, "TJMT", "1grau") is None
    assert ("TJMT", "1grau") not in credenciais.cadastradas(conn)
    assert credenciais.ler(conn, "TJMT", "2grau")["senha"] == "velha2"  # migrada, segue valendo
    assert "pje_senha" not in local and local["pje_senha_2grau"] == "velha2"
    assert ajustes_locais.tribunais_guardados() == ["TJMT"]


def test_local_gravar_no_principal_atualiza_o_espelho(local, tmp_path):
    from nucleo import ajustes_locais
    conn = banco.conectar(str(tmp_path / "b.db"))
    ajustes_locais.guardar_tribunais(["TJMT"])
    local.update({"pje_cpf": "22222222222", "pje_senha": "velha1"})
    credenciais.gravar(conn, "TJMT", "1grau", "nova1")
    assert local["pje_senha"] == "nova1" and local["pje_senha_TJMT"] == "nova1"
    credenciais.gravar(conn, "TJMT", "2grau", "nova2")
    assert local["pje_senha_2grau"] == "nova2"
    assert credenciais.ler(conn, "TJMT", "1grau")["senha"] == "nova1"


def test_local_carregar_poe_troca_e_tira(local, tmp_path, monkeypatch):
    conn = banco.conectar(str(tmp_path / "b.db"))
    credenciais.gravar_cpf(conn, "22222222222")
    credenciais.gravar(conn, "TJMG", "1grau", "um")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["PJE_SENHA_TJMG"] == "um" and os.environ["PJE_CPF"] == "22222222222"
    credenciais.gravar(conn, "TJMG", "1grau", "dois")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["PJE_SENHA_TJMG"] == "dois"
    credenciais.apagar(conn, "TJMG", "1grau")
    credenciais.carregar_no_ambiente(conn)
    assert "PJE_SENHA_TJMG" not in os.environ and "PJE_SENHA" not in os.environ
    assert "CONTROLADORIA_TRIBUNAIS" not in os.environ


def test_local_variavel_do_env_continua_mandando(local, tmp_path, monkeypatch):
    conn = banco.conectar(str(tmp_path / "b.db"))
    monkeypatch.setenv("PJE_SENHA_TJMG", "do-env")
    credenciais.gravar_cpf(conn, "22222222222")
    credenciais.gravar(conn, "TJMG", "1grau", "cofre")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["PJE_SENHA_TJMG"] == "do-env"


def test_vps_une_cofre_e_env(chave, tmp_path, monkeypatch):
    monkeypatch.setenv("TJMT_SENHA", "do-env")  # produção: TJMT só no .env
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"
    credenciais.carregar_no_ambiente(conn)  # idempotente
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"


def test_vps_sem_senha_no_cofre_devolve_ao_padrao(chave, tmp_path):
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMG"
    with conn:
        credenciais.apagar(conn, "TJMG", "1grau")
    credenciais.carregar_no_ambiente(conn)
    assert "CONTROLADORIA_TRIBUNAIS" not in os.environ
    assert "PJE_SENHA_TJMG" not in os.environ


def test_vps_sem_senha_e_sem_nada_nao_mexe(chave, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAIS", "TJMT,TJMG")  # veio do .env
    conn = banco.conectar(str(tmp_path / "b.db"))
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"


def test_vps_chave_trocada_nao_deixa_ambiente_pela_metade(chave, tmp_path, monkeypatch, caplog):
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "segredo-mg")
    outra = tmp_path / "outra.key"
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(outra))
    cofre_servidor.criar_chave()
    monkeypatch.setenv("PJE_CPF", "do-env")
    credenciais.carregar_no_ambiente(conn)  # não levanta
    assert os.environ["PJE_CPF"] == "do-env"
    assert "PJE_SENHA_TJMG" not in os.environ and "CONTROLADORIA_TRIBUNAIS" not in os.environ
    assert "segredo-mg" not in caplog.text and "11111111111" not in caplog.text
    assert "chave" in caplog.text


def test_guardar_tribunais_atualiza_o_principal(tmp_path, monkeypatch):
    from nucleo import ajustes_locais
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes.json"))
    ajustes_locais.guardar_tribunais(["TJMG", "TJMT"])
    assert ajustes_locais.ler()["tribunal"] == "TJMG"
    assert ajustes_locais.tribunais_guardados() == ["TJMG", "TJMT"]
    ajustes_locais.guardar_tribunais([])
    assert "tribunal" not in ajustes_locais.ler()
    assert ajustes_locais.tribunais_guardados() == []


# --- leitura somente leitura (verificador de publicação) ---------------------------

def _limpar_ambiente(monkeypatch):
    for v in ("PJE_CPF", "PJE_SENHA_TJMT", "PJE_SENHA_2GRAU_TJMT", "PJE_SENHA_TJMG",
              "CONTROLADORIA_TRIBUNAIS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(credenciais, "_definidas", set())


def test_somente_leitura_banco_antigo_sem_tabela_devolve_false_e_nao_toca(chave, tmp_path,
                                                                          monkeypatch):
    import sqlite3
    _limpar_ambiente(monkeypatch)
    caminho = tmp_path / "antigo.db"
    c = sqlite3.connect(caminho)
    for i in range(6):  # banco v6: antes da tabela `credencial`
        c.executescript(f"BEGIN;\n{banco.MIGRACOES[i]}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
    c.close()
    antes = (caminho.stat().st_mtime_ns, caminho.read_bytes())
    assert credenciais.carregar_do_banco_somente_leitura(str(caminho)) is False
    assert (caminho.stat().st_mtime_ns, caminho.read_bytes()) == antes
    c = sqlite3.connect(caminho)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    c.close()
    assert "PJE_CPF" not in os.environ


def test_somente_leitura_carrega_no_ambiente_sem_migrar(chave, tmp_path, monkeypatch):
    import sqlite3
    _limpar_ambiente(monkeypatch)
    caminho = tmp_path / "b.db"
    conn = banco.conectar(str(caminho))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg")
        credenciais.gravar(conn, "TJMT", "2grau", "mt2")
    conn.close()
    assert credenciais.carregar_do_banco_somente_leitura(str(caminho)) is True
    assert os.environ["PJE_CPF"] == "11111111111"
    assert os.environ["PJE_SENHA_TJMG"] == "mg"
    assert os.environ["PJE_SENHA_2GRAU_TJMT"] == "mt2"
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"
    c = sqlite3.connect(caminho)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES)
    c.close()


def test_somente_leitura_sem_chave_ou_sem_banco_devolve_false(tmp_path, monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    caminho = tmp_path / "b.db"
    banco.conectar(str(caminho)).close()
    assert credenciais.carregar_do_banco_somente_leitura(str(caminho)) is False
    assert credenciais.carregar_do_banco_somente_leitura(str(tmp_path / "fantasma.db")) is False
    assert credenciais.carregar_do_banco_somente_leitura("") is False
    assert not (tmp_path / "fantasma.db").exists()


# --- chave perdida ou trocada: leitura linha a linha e estado do cofre -------------

def _linha_com_outra_chave(conn, tmp_path, monkeypatch, sigla, instancia, senha):
    """Grava uma linha cifrada com uma chave diferente da atual (e volta à atual)."""
    atual = os.environ["CONTROLADORIA_CHAVE_COFRE"]
    outra = tmp_path / f"outra-{sigla}-{instancia}.key"
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(outra))
    cofre_servidor.criar_chave()
    token = cofre_servidor.cifrar(senha)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", atual)
    with conn:
        conn.execute("INSERT INTO credencial (sigla, instancia, senha_cifrada, atualizado_em) "
                     "VALUES (?, ?, ?, '2026-10-21T10:00:00') ON CONFLICT (sigla, instancia) "
                     "DO UPDATE SET senha_cifrada = excluded.senha_cifrada",
                     (sigla, instancia, token))


def test_estado_do_cofre(chave, tmp_path, monkeypatch):
    conn = banco.conectar(str(tmp_path / "b.db"))
    assert credenciais.estado_do_cofre(conn) == "ok"  # nada guardado: a chave serve
    assert credenciais.tem_credenciais_cifradas(conn) is False
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMT", "1grau", "mt")
    assert credenciais.estado_do_cofre(conn) == "ok"
    assert credenciais.tem_credenciais_cifradas(conn) is True
    assert credenciais.ilegiveis(conn) == set()
    _linha_com_outra_chave(conn, tmp_path, monkeypatch, "TJMG", "1grau", "segredo-mg")
    assert credenciais.estado_do_cofre(conn) == "nao_abre"
    assert credenciais.ilegiveis(conn) == {("TJMG", "1grau")}
    # regravar com a chave atual sobrescreve a linha ilegível
    with conn:
        credenciais.gravar(conn, "TJMG", "1grau", "nova")
    assert credenciais.estado_do_cofre(conn) == "ok"


def test_estado_do_cofre_sem_chave_e_chave_ilegivel(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    conn = banco.conectar(str(tmp_path / "b.db"))
    assert credenciais.estado_do_cofre(conn) == "sem_chave"
    ruim = tmp_path / "ruim.key"
    ruim.write_text("isto nao e uma chave")
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(ruim))
    assert credenciais.estado_do_cofre(conn) == "sem_chave"
    assert credenciais.ilegiveis(conn) == set()


def test_estado_do_cofre_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    conn = banco.conectar(str(tmp_path / "b.db"))
    assert credenciais.estado_do_cofre(conn) == "local"
    assert credenciais.ilegiveis(conn) == set()


def test_cpf_com_outra_chave_nao_abre(chave, tmp_path, monkeypatch):
    conn = banco.conectar(str(tmp_path / "b.db"))
    atual = os.environ["CONTROLADORIA_CHAVE_COFRE"]
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "velha.key"))
    cofre_servidor.criar_chave()
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", atual)
    assert credenciais.estado_do_cofre(conn) == "nao_abre"


def test_carga_linha_a_linha_pula_a_ilegivel(chave, tmp_path, monkeypatch, caplog):
    _limpar_ambiente(monkeypatch)
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg-legivel")
    _linha_com_outra_chave(conn, tmp_path, monkeypatch, "TJMT", "1grau", "segredo-mt")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["PJE_CPF"] == "11111111111"
    assert os.environ["PJE_SENHA_TJMG"] == "mg-legivel"
    assert "PJE_SENHA_TJMT" not in os.environ
    assert "TJMG" in os.environ["CONTROLADORIA_TRIBUNAIS"]
    assert "segredo-mt" not in caplog.text and "11111111111" not in caplog.text
    assert "TJMT" in caplog.text  # o log diz qual linha ficou de fora (sem o segredo)


def test_cpf_ilegivel_nao_aplica_senha_nenhuma(chave, tmp_path, monkeypatch, caplog):
    _limpar_ambiente(monkeypatch)
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg-legivel")
    atual = os.environ["CONTROLADORIA_CHAVE_COFRE"]
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "outra-cpf.key"))
    cofre_servidor.criar_chave()
    token_cpf = cofre_servidor.cifrar("22222222222")
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", atual)
    with conn:
        conn.execute("UPDATE config SET valor = ? WHERE chave = 'pje_cpf_cifrado'", (token_cpf,))
    credenciais.carregar_no_ambiente(conn)
    assert "PJE_CPF" not in os.environ
    assert "PJE_SENHA_TJMG" not in os.environ
    assert "22222222222" not in caplog.text and "mg-legivel" not in caplog.text


def test_problema_do_cofre_no_banco(chave, tmp_path, monkeypatch):
    import sqlite3
    caminho = tmp_path / "prod.db"
    conn = banco.conectar(str(caminho))
    assert credenciais.problema_do_cofre_no_banco(str(caminho)) == ""  # sem credenciais
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMT", "1grau", "segredo-mt")
    assert credenciais.problema_do_cofre_no_banco(str(caminho)) == ""
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "sumiu.key"))
    motivo = credenciais.problema_do_cofre_no_banco(str(caminho))
    assert "chave do cofre" in motivo and "sumiu.key" in motivo
    assert "segredo-mt" not in motivo and "11111111111" not in motivo
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nova.key"))
    cofre_servidor.criar_chave()
    motivo = credenciais.problema_do_cofre_no_banco(str(caminho))
    assert "não abre" in motivo
    conn.close()
    c = sqlite3.connect(caminho)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(banco.MIGRACOES)
    c.close()
    assert credenciais.problema_do_cofre_no_banco(str(tmp_path / "fantasma.db")) == ""
    assert not (tmp_path / "fantasma.db").exists()


def test_problema_do_cofre_banco_antigo_sem_tabela(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "nao-existe.key"))
    caminho = tmp_path / "antigo.db"
    c = sqlite3.connect(caminho)
    for i in range(6):
        c.executescript(f"BEGIN;\n{banco.MIGRACOES[i]}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
    c.close()
    assert credenciais.problema_do_cofre_no_banco(str(caminho)) == ""


# --- ambiente: sem mock em produção, valor do .env restaurado ----------------------

def test_codigo_de_producao_nao_usa_mock():
    import inspect
    assert "unittest" not in inspect.getsource(credenciais)
    assert "mock" not in inspect.getsource(credenciais)


def test_cofre_vazio_restaura_os_tribunais_do_env(chave, tmp_path, monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAIS", "TJMT,TJMG")  # veio do .env
    monkeypatch.setenv("TJMT_SENHA", "do-env")
    conn = banco.conectar(str(tmp_path / "b.db"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"
    credenciais.carregar_no_ambiente(conn)  # 2ª carga: o .env original continua guardado
    with conn:
        credenciais.apagar(conn, "TJMG", "1grau")
    credenciais.carregar_no_ambiente(conn)
    assert os.environ["CONTROLADORIA_TRIBUNAIS"] == "TJMT,TJMG"
    assert "CONTROLADORIA_TRIBUNAIS" not in credenciais._definidas


def test_siglas_do_ambiente_nao_mexe_no_os_environ(chave, tmp_path, monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.setenv("TJMT_SENHA", "do-env")
    os.environ["PJE_SENHA_TJMG"] = "do-modulo"
    credenciais._definidas.add("PJE_SENHA_TJMG")
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAIS", "TJMT,TJMG")
    antes = dict(os.environ)
    assert credenciais._siglas_do_ambiente() == ["TJMT"]  # o TJMG é do módulo, não do .env
    assert dict(os.environ) == antes
    os.environ.pop("PJE_SENHA_TJMG", None)
