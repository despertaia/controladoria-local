import io
import json
import os
import zipfile

import pytest

import painel
from nucleo import banco, eventos, ponte

NUMERO = "00000010220248110041"
GATE = b'{"gate_status":"aprovado","citations":[],"pendencias_do_profissional":[]}'


# Rota sob /ponte SEM o decorador da chave (registrada na coleta, antes de qualquer
# pedido): não pode herdar a isenção de login/CSRF só pelo caminho.
@painel.app.route("/ponte/teste-sem-decorador", methods=["GET", "POST"])
def _ponte_teste_sem_decorador():
    return "aberta"


class _Cliente:
    """Envolve o cliente de teste com a chave da ponte e um helper de cartão."""

    def __init__(self, c, caminho, chave):
        self._c = c
        self.caminho = caminho
        self.auth = {"Authorization": f"Bearer {chave}"}

    def __getattr__(self, nome):
        return getattr(self._c, nome)

    def cartao_no_lex(self):
        conn = banco.conectar(self.caminho)
        try:
            conn.execute("INSERT OR IGNORE INTO processo (numero, tribunal, orgao_julgador, "
                         "cliente, parte_contraria, advogado_atua) VALUES (?, 'TJMT', 'Vara X', "
                         "'MARIA FICTÍCIA', 'BANCO Y', 1)", (NUMERO,))
            agora = eventos.agora()
            d = conn.execute(
                "INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em, "
                "lex_estado) VALUES (?, 'lex', '2026-10-01', ?, ?, 'na_fila')",
                (NUMERO, agora, agora)).lastrowid
            conn.commit()
            return d
        finally:
            conn.close()

    def estado(self, d):
        conn = banco.conectar(self.caminho)
        try:
            return tuple(conn.execute("SELECT coluna, lex_estado FROM demanda WHERE id = ?",
                                      (d,)).fetchone())
        finally:
            conn.close()


@pytest.fixture
def cliente_ponte(tmp_path, monkeypatch):
    caminho = str(tmp_path / "p.db")
    conn = banco.conectar(caminho)
    with conn:
        chave = ponte.gerar_chave(conn)
    conn.close()
    monkeypatch.setenv("CONTROLADORIA_BANCO", caminho)
    monkeypatch.setenv("CONTROLADORIA_PECAS", str(tmp_path / "pecas"))
    monkeypatch.setattr(painel, "PAINEL_SENHA", "")
    return _Cliente(painel.app.test_client(), caminho, chave)


def _dados(**extra):
    base = {"n": "1", "squad": "s", "run_id": "r", "gate_status": "aprovado",
            "citacoes_total": "2", "citacoes_falhas": "0",
            "peca_docx": (io.BytesIO(b"PK docx"), "x.docx"),
            "peca_pdf": (io.BytesIO(b"%PDF-1.4"), "x.pdf"),
            "citation_gate": (io.BytesIO(GATE), "g.json")}
    base.update(extra)
    return {k: v for k, v in base.items() if v is not None}


def _resultado(c, d, **extra):
    return c.post(f"/ponte/demanda/{d}/resultado", data=_dados(**extra), headers=c.auth,
                  content_type="multipart/form-data")


def test_sem_chave_ou_errada_401(cliente_ponte):
    r = cliente_ponte.post("/ponte/proximo")
    assert r.status_code == 401 and r.get_json() == {"ok": False, "erro": "chave inválida"}
    assert cliente_ponte.post("/ponte/proximo",
                              headers={"Authorization": "Bearer x"}).status_code == 401
    assert cliente_ponte.post("/ponte/proximo",
                              headers={"Authorization": "Basic abc"}).status_code == 401


def test_ponte_nao_exige_login_nem_csrf(cliente_ponte, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    anterior = painel.app.config.get("CSRF_EXIGIDO")
    painel.app.config["CSRF_EXIGIDO"] = True
    try:
        r = cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
        assert r.status_code in (200, 204)
        # as telas continuam protegidas
        assert cliente_ponte.get("/carteira").status_code == 302
    finally:
        painel.app.config["CSRF_EXIGIDO"] = anterior


def test_chamada_valida_registra_contato(cliente_ponte):
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    conn = banco.conectar(cliente_ponte.caminho)
    try:
        assert ponte.ultimo_contato(conn)
    finally:
        conn.close()


def test_proximo_sem_nada_204(cliente_ponte):
    assert cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth).status_code == 204


def test_fluxo_completo_proximo_batida_resultado(cliente_ponte, tmp_path):
    d = cliente_ponte.cartao_no_lex()
    pac = cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth).get_json()
    assert pac["demanda"] == d
    assert cliente_ponte.post(f"/ponte/demanda/{d}/batida", json={"n": 1, "etapa": "redação (9/13)"},
                              headers=cliente_ponte.auth).status_code == 200
    r = _resultado(cliente_ponte, d, nota=(io.BytesIO(b"# nota"), "../../evil.md"),
                   manifesto=(io.BytesIO(b"{}"), "m.json"))
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    pasta = tmp_path / "pecas" / str(d) / "1"
    assert (pasta / "peca.pdf").read_bytes() == b"%PDF-1.4"
    assert (pasta / "peca.docx").exists() and (pasta / "citation-gate.json").exists()
    assert (pasta / "nota-ao-revisor.md").read_text() == "# nota"
    assert (pasta / "manifesto.json").exists() and not (pasta / "termo-de-conferencia.docx").exists()
    assert sorted(os.listdir(pasta)) == ["citation-gate.json", "manifesto.json",
                                         "nota-ao-revisor.md", "peca.docx", "peca.pdf"]
    assert cliente_ponte.estado(d) == ("revisao", "pronto")


def test_resultado_sem_pdf_ou_gate_invalido_400(cliente_ponte, tmp_path):
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    r = _resultado(cliente_ponte, d, peca_pdf=None)
    assert r.status_code == 400 and r.get_json()["ok"] is False
    r = _resultado(cliente_ponte, d, citation_gate=(io.BytesIO(b"isto nao e json"), "g.json"))
    assert r.status_code == 400
    r = _resultado(cliente_ponte, d, peca_docx=None)
    assert r.status_code == 400
    r = _resultado(cliente_ponte, d, n="x")
    assert r.status_code == 400
    assert cliente_ponte.estado(d) == ("lex", "reservado")
    assert not (tmp_path / "pecas" / str(d)).exists()


def test_estado_errado_409(cliente_ponte):
    d = cliente_ponte.cartao_no_lex()  # na fila, sem reserva
    r = cliente_ponte.post(f"/ponte/demanda/{d}/batida", json={"n": 1, "etapa": "x"},
                           headers=cliente_ponte.auth)
    assert r.status_code == 409 and r.get_json()["ok"] is False
    r = _resultado(cliente_ponte, d)
    assert r.status_code == 409
    r = cliente_ponte.post(f"/ponte/demanda/{d}/falha", json={"n": 1, "motivo": "erro"},
                           headers=cliente_ponte.auth)
    assert r.status_code == 409


def test_falha_limite(cliente_ponte):
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    r = cliente_ponte.post(f"/ponte/demanda/{d}/falha",
                           json={"n": 1, "motivo": "limite", "detalhe": "plano esgotado"},
                           headers=cliente_ponte.auth)
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert cliente_ponte.estado(d) == ("lex", "pausado_limite")


def test_falha_motivo_desconhecido_409(cliente_ponte):
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    r = cliente_ponte.post(f"/ponte/demanda/{d}/falha", json={"n": 1, "motivo": "xis"},
                           headers=cliente_ponte.auth)
    assert r.status_code == 409


def test_autos_zip_ou_204(cliente_ponte, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = cliente_ponte.cartao_no_lex()
    assert cliente_ponte.get(f"/ponte/demanda/{d}/autos").status_code == 401
    # Na fila (ainda não reservado pelo Mac): os autos não saem.
    r = cliente_ponte.get(f"/ponte/demanda/{d}/autos", headers=cliente_ponte.auth)
    assert r.status_code == 409
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    r = cliente_ponte.get(f"/ponte/demanda/{d}/autos", headers=cliente_ponte.auth)
    assert r.status_code == 204
    pasta = tmp_path / "peticoes" / NUMERO / "1grau"
    pasta.mkdir(parents=True)
    (pasta / "peca.pdf").write_bytes(b"%PDF autos")
    r = cliente_ponte.get(f"/ponte/demanda/{d}/autos", headers=cliente_ponte.auth)
    assert r.status_code == 200 and r.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        assert z.namelist() == [f"{NUMERO}/1grau/peca.pdf"]
    assert cliente_ponte.get("/ponte/demanda/9999/autos",
                             headers=cliente_ponte.auth).status_code == 404


def test_limite_de_upload_configurado():
    assert painel.app.config["MAX_CONTENT_LENGTH"] == 60 * 1024 * 1024


def test_upload_grande_demais_413_em_json(cliente_ponte, monkeypatch):
    monkeypatch.setitem(painel.app.config, "MAX_CONTENT_LENGTH", 100)
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    r = _resultado(cliente_ponte, d, peca_pdf=(io.BytesIO(b"%PDF" + b"0" * 500), "x.pdf"))
    assert r.status_code == 413 and r.get_json()["ok"] is False


# --- onda final -------------------------------------------------------------------

def _config(c, chave):
    conn = banco.conectar(c.caminho)
    try:
        r = conn.execute("SELECT valor FROM config WHERE chave = ?", (chave,)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def _uma(c, sql, *args):
    conn = banco.conectar(c.caminho)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def test_rota_sob_ponte_sem_decorador_exige_login_e_csrf(cliente_ponte, monkeypatch):
    monkeypatch.setattr(painel, "PAINEL_SENHA", "segredo")
    r = cliente_ponte.get("/ponte/teste-sem-decorador")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    # caminho /ponte/ inexistente também não fica isento (vai ao login, não 404)
    assert cliente_ponte.get("/ponte/nao-existe").status_code == 302
    assert "_ponte_teste_sem_decorador" not in painel._ENDPOINTS_PONTE
    assert {"ponte_proximo", "ponte_autos", "ponte_batida", "ponte_resultado", "ponte_falha",
            "ponte_contato"} <= painel._ENDPOINTS_PONTE
    monkeypatch.setitem(painel.app.config, "CSRF_EXIGIDO", True)
    with cliente_ponte.session_transaction() as s:
        s["logado"] = True
    r = cliente_ponte.post("/ponte/teste-sem-decorador", headers={"Accept": "application/json"})
    assert r.status_code == 400 and r.get_json()["erro"] == painel.MENSAGEM_400
    assert cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth).status_code == 204


def test_400_da_ponte_tem_mensagem_propria(cliente_ponte, monkeypatch):
    from flask import abort

    def quebra(valor, nome):
        abort(400)

    monkeypatch.setattr(painel, "_inteiro", quebra)
    r = cliente_ponte.post("/ponte/demanda/1/falha", json={"n": 1}, headers=cliente_ponte.auth)
    assert r.status_code == 400
    assert r.get_json() == {"ok": False, "erro": painel.MENSAGEM_400_PONTE}
    assert painel.MENSAGEM_400_PONTE != painel.MENSAGEM_400


def test_chave_invalida_conta_tentativas_sem_evento(cliente_ponte):
    for _ in range(3):
        assert cliente_ponte.post("/ponte/proximo",
                                  headers={"Authorization": "Bearer errada"}).status_code == 401
    assert _config(cliente_ponte, ponte.CHAVE_INVALIDA_N) == "3"
    assert _config(cliente_ponte, ponte.CHAVE_INVALIDA_EM)
    assert _config(cliente_ponte, ponte.ULTIMO_CONTATO) is None
    assert _uma(cliente_ponte, "SELECT COUNT(*) FROM evento WHERE tipo NOT IN "
                "('ponte_chave_gerada')")[0] == 0


def test_contato_com_claude_ok(cliente_ponte):
    assert cliente_ponte.post("/ponte/contato", json={"claude_ok": 0}).status_code == 401
    r = cliente_ponte.post("/ponte/contato", json={"claude_ok": 0}, headers=cliente_ponte.auth)
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert _config(cliente_ponte, ponte.CLAUDE_OK) == "0"
    assert _config(cliente_ponte, ponte.ULTIMO_CONTATO)
    cliente_ponte.post("/ponte/contato", json={"claude_ok": 1}, headers=cliente_ponte.auth)
    assert _config(cliente_ponte, ponte.CLAUDE_OK) == "1"
    for ruim in ({}, {"claude_ok": "talvez"}, {"claude_ok": 2}, None):
        r = cliente_ponte.post("/ponte/contato", json=ruim, headers=cliente_ponte.auth)
        assert r.status_code == 400, ruim
    assert _uma(cliente_ponte, "SELECT COUNT(*) FROM evento WHERE tipo LIKE 'ponte_contato%'")[0] == 0


def test_batida_e_resultado_com_nome_do_squad(cliente_ponte):
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    r = cliente_ponte.post(f"/ponte/demanda/{d}/batida", headers=cliente_ponte.auth,
                           json={"n": 1, "squad": "s", "squad_nome": "Réplica à contestação",
                                 "etapa": "x" * 900})
    assert r.status_code == 200
    linha = _uma(cliente_ponte, "SELECT lex_squad_nome, lex_etapa FROM demanda WHERE id = ?", d)
    assert linha[0] == "Réplica à contestação" and len(linha[1]) == 200
    assert _resultado(cliente_ponte, d, squad_nome="Réplica\nà\tcontestação").status_code == 200
    assert _uma(cliente_ponte, "SELECT squad_nome FROM execucao_lex WHERE demanda_id = ?",
                d)[0] == "Réplica à contestação"


def test_resultado_usa_o_citation_gate_do_arquivo(cliente_ponte):
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    gate = json.dumps({"gate_status": "reprovado", "citations": [
        {"status": "verificada"}, {"status": "não encontrada"}, {"status": "divergente"}]})
    r = _resultado(cliente_ponte, d, gate_status="aprovado", citacoes_total="2",
                   citacoes_falhas="0", citation_gate=(io.BytesIO(gate.encode()), "g.json"))
    assert r.status_code == 200
    ex = _uma(cliente_ponte, "SELECT gate_status, citacoes_total, citacoes_falhas, detalhe "
              "FROM execucao_lex WHERE demanda_id = ?", d)
    assert tuple(ex[:3]) == ("reprovado", 3, 2)
    assert "divergiu" in ex[3] and "valeu o arquivo" in ex[3]


def test_resultado_coerente_sem_detalhe(cliente_ponte):
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    r = _resultado(cliente_ponte, d, citacoes_total="0")
    assert r.status_code == 200
    assert _uma(cliente_ponte, "SELECT detalhe FROM execucao_lex WHERE demanda_id = ?", d)[0] == ""


def test_pasta_gravada_e_absoluta(cliente_ponte, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTROLADORIA_PECAS", "pecas-relativas")
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)
    assert _resultado(cliente_ponte, d).status_code == 200
    pasta = _uma(cliente_ponte, "SELECT pasta FROM execucao_lex WHERE demanda_id = ?", d)[0]
    assert os.path.isabs(pasta)
    assert os.path.realpath(pasta) == os.path.realpath(tmp_path / "pecas-relativas" / str(d) / "1")


def test_movimento_recusado_na_ponte_vira_409(cliente_ponte, monkeypatch):
    from nucleo import quadro
    d = cliente_ponte.cartao_no_lex()
    cliente_ponte.post("/ponte/proximo", headers=cliente_ponte.auth)

    def recusa(*a, **k):
        raise quadro.MovimentoInvalido("O cartão mudou enquanto isso.")

    monkeypatch.setattr(quadro, "mover", recusa)
    assert _resultado(cliente_ponte, d).status_code == 409
    r = cliente_ponte.post(f"/ponte/demanda/{d}/falha", headers=cliente_ponte.auth,
                           json={"n": 1, "motivo": "sem_tipo"})
    assert r.status_code == 409
    assert cliente_ponte.estado(d) == ("lex", "reservado")
