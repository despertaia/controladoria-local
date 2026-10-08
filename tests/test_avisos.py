from types import SimpleNamespace as NS

import pytest

from nucleo import avisos, banco, eventos

A = "00000010220248110041"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    yield c
    c.close()


def _mni(id_aviso="123", numero="0000001-02.2024.8.11.0041", data="20261005143000",
         tipo="INT"):
    return NS(idAviso=id_aviso, tipoComunicacao=tipo, dataDisponibilizacao=data,
              processo=NS(numero=numero, orgaoJulgador=NS(nomeOrgao="1ª VARA CÍVEL")),
              destinatario=NS(pessoa=NS(nome="FICTÍCIO")))


def test_ler_aviso():
    assert avisos.ler_aviso(_mni(), "1grau") == {
        "id": "123", "instancia": "1grau", "numero": A, "tipo_comunicacao": "INT",
        "data_disponibilizacao": "2026-10-05", "orgao": "1ª VARA CÍVEL"}


def test_ler_aviso_incompleto():
    assert avisos.ler_aviso(_mni(id_aviso=""), "1grau") is None
    assert avisos.ler_aviso(_mni(numero="123"), "1grau") is None
    assert avisos.ler_aviso(NS(), "1grau") is None
    assert avisos.ler_aviso(_mni(data="lixo"), "1grau")["data_disponibilizacao"] == ""


def test_sincronizar_novos_e_encerrados(conn):
    a1 = avisos.ler_aviso(_mni("1"), "1grau")
    a2 = avisos.ler_aviso(_mni("2"), "1grau")
    assert avisos.sincronizar(conn, "1grau", [a1, a2]) == [a1, a2]
    assert avisos.sincronizar(conn, "1grau", [a1, a2]) == []
    assert avisos.sincronizar(conn, "1grau", [a2]) == []          # 1 sumiu: ciência
    pendentes = dict(conn.execute("SELECT id, pendente FROM aviso").fetchall())
    assert pendentes == {"1": 0, "2": 1}
    tipos = [e["tipo"] for e in eventos.listar(conn, A)]
    assert tipos == ["aviso_detectado", "aviso_detectado", "aviso_encerrado"]


def test_instancias_nao_se_misturam(conn):
    avisos.sincronizar(conn, "1grau", [avisos.ler_aviso(_mni("1"), "1grau")])
    avisos.sincronizar(conn, "2grau", [])  # lista vazia do 2º grau não encerra o 1º
    assert conn.execute("SELECT pendente FROM aviso").fetchone()[0] == 1


def test_rotulo_do_tipo():
    assert avisos.rotulo_do_tipo("INT") == "Intimação"
    assert avisos.rotulo_do_tipo("") == "Comunicação"
    assert avisos.rotulo_do_tipo("XYZ") == "XYZ"


def test_sincronizar_sem_encerrar(conn):
    a = {"id": "1", "instancia": "1grau", "numero": "0" * 20, "tipo_comunicacao": "INT",
         "data_disponibilizacao": "", "orgao": ""}
    avisos.sincronizar(conn, "1grau", [a])
    assert avisos.sincronizar(conn, "1grau", [], encerrar=False) == []
    assert conn.execute("SELECT pendente FROM aviso").fetchone()[0] == 1
