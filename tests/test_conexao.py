from nucleo import banco, conexao


def _conn(tmp_path):
    return banco.conectar(str(tmp_path / "b.db"))


def test_regra_das_duas_falhas(tmp_path):
    conn = _conn(tmp_path)
    with conn:
        conexao.registrar(conn, "TJMT", "1grau", "ok", agora="2026-10-21T06:00:00-04:00")
        conexao.registrar(conn, "TJMT", "1grau", "fora", "tempo esgotado",
                          agora="2026-10-21T12:00:00-04:00")
    linha = next(s for s in conexao.situacao(conn) if s["instancia"] == "1grau" and s["sigla"] == "TJMT")
    assert linha["selo"] == "nao_cadastrado"  # sem credencial cadastrada
    assert conexao.avisos(conn) == []  # 1 falha não avisa (e sem credencial nunca avisa)


def test_uma_falha_com_credencial_e_instavel(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr("nucleo.credenciais.cadastradas", lambda c: {("TJMT", "1grau")})
    with conn:
        conexao.registrar(conn, "TJMT", "1grau", "fora", "tempo esgotado",
                          agora="2026-10-21T12:00:00-04:00")
    linha = next(s for s in conexao.situacao(conn) if (s["sigla"], s["instancia"]) == ("TJMT", "1grau"))
    assert linha["selo"] == "instavel" and linha["cadastrada"] is True
    assert linha["falhas_seguidas"] == 1 and linha["detalhe"] == "tempo esgotado"
    assert conexao.avisos(conn) == []


def test_avisos_com_credencial(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr("nucleo.credenciais.cadastradas", lambda c: {("TJMT", "1grau"), ("TJMT", "2grau")})
    with conn:
        conexao.registrar(conn, "TJMT", "1grau", "fora", agora="2026-10-21T12:00:00-04:00")
        conexao.registrar(conn, "TJMT", "1grau", "fora", agora="2026-10-21T18:00:00-04:00")
        conexao.registrar(conn, "TJMT", "2grau", "recusada", agora="2026-10-21T18:00:00-04:00")
    textos = [a["texto"] for a in conexao.avisos(conn)]
    assert any("não responde desde 12:00 de 21/10" in t for t in textos)
    assert any("foi recusada" in t and "2ª instância" in t for t in textos)
    selos = {(s["sigla"], s["instancia"]): s["selo"] for s in conexao.situacao(conn)}
    assert selos[("TJMT", "1grau")] == "fora" and selos[("TJMT", "2grau")] == "recusada"


def test_mudou_em_so_muda_quando_o_estado_muda_e_ok_zera_as_falhas(tmp_path):
    conn = _conn(tmp_path)
    with conn:
        conexao.registrar(conn, "TJMT", "1grau", "fora", agora="2026-10-21T12:00:00-04:00")
        conexao.registrar(conn, "TJMT", "1grau", "fora", agora="2026-10-21T18:00:00-04:00")
    linha = conn.execute("SELECT * FROM conexao_tribunal").fetchone()
    assert linha["falhas_seguidas"] == 2
    assert linha["mudou_em"] == "2026-10-21T12:00:00-04:00"
    assert linha["verificado_em"] == "2026-10-21T18:00:00-04:00"
    with conn:
        conexao.registrar(conn, "TJMT", "1grau", "ok", agora="2026-10-22T06:00:00-04:00")
    linha = conn.execute("SELECT * FROM conexao_tribunal").fetchone()
    assert linha["falhas_seguidas"] == 0 and linha["mudou_em"] == "2026-10-22T06:00:00-04:00"


def test_evento_so_quando_muda(tmp_path):
    conn = _conn(tmp_path)
    with conn:
        assert conexao.registrar(conn, "TJMG", "1grau", "ok") is True
        assert conexao.registrar(conn, "TJMG", "1grau", "ok") is False
        assert conexao.registrar(conn, "TJMG", "1grau", "recusada") is True
    n = conn.execute("SELECT COUNT(*) FROM evento WHERE tipo = 'conexao_mudou'").fetchone()[0]
    assert n == 2


def test_testar_mapeia_indisponivel_para_fora(monkeypatch):
    monkeypatch.setattr("nucleo.mni_fabrica.sondar_login",
                        lambda cred, inst, **kw: {"1grau": ("indisponivel", "tempo esgotado")})
    assert conexao.testar("TJMT", "1grau", "11111111111", "x") == ("fora", "tempo esgotado")
    monkeypatch.setattr("nucleo.mni_fabrica.sondar_login",
                        lambda cred, inst, **kw: {"1grau": ("recusada", "O PJe recusou o CPF ou a senha.")})
    assert conexao.testar("TJMT", "1grau", "1", "x")[0] == "recusada"


# --- cofre que não abre (chave perdida ou trocada) ----------------------------------

def _vps_com_chave(tmp_path, monkeypatch, nome="cofre.key"):
    from nucleo import cofre_servidor
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / nome))
    cofre_servidor.criar_chave()


AVISO_COFRE = ("As senhas dos tribunais guardadas no painel não puderam ser usadas. "
               "Abra Configurações › Tribunais.")


def test_linha_que_nao_decifra_pede_para_digitar_de_novo(tmp_path, monkeypatch):
    from nucleo import credenciais
    _vps_com_chave(tmp_path, monkeypatch, "velha.key")
    conn = _conn(tmp_path)
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMG", "1grau", "mg")
        conexao.registrar(conn, "TJMG", "1grau", "ok")
    _vps_com_chave(tmp_path, monkeypatch, "nova.key")
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")  # CPF regravado com a chave nova
        credenciais.gravar(conn, "TJMT", "1grau", "mt")
    selos = {(s["sigla"], s["instancia"]): s["selo"] for s in conexao.situacao(conn)}
    assert selos[("TJMG", "1grau")] == "redigitar"
    assert selos[("TJMT", "1grau")] == "nao_verificado"
    assert [a["texto"] for a in conexao.avisos(conn)] == [AVISO_COFRE]


def test_sem_chave_com_credenciais_avisa_e_sem_credenciais_nao(tmp_path, monkeypatch):
    from nucleo import credenciais
    _vps_com_chave(tmp_path, monkeypatch)
    conn = _conn(tmp_path)
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "sumiu.key"))
    assert conexao.avisos(conn) == []
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "cofre.key"))
    with conn:
        credenciais.gravar_cpf(conn, "11111111111")
        credenciais.gravar(conn, "TJMT", "1grau", "mt")
    assert conexao.avisos(conn) == []
    monkeypatch.setenv("CONTROLADORIA_CHAVE_COFRE", str(tmp_path / "sumiu.key"))
    assert [a["texto"] for a in conexao.avisos(conn)] == [AVISO_COFRE]
