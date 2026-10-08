from nucleo import banco, eventos


def test_registrar_e_listar_em_ordem():
    conn = banco.conectar(":memory:")
    eventos.registrar(conn, "processo_descoberto", "111", {"fonte": "djen"}, quando="2026-10-05T06:00:00")
    eventos.registrar(conn, "processo_sincronizado", "111", {"instancia": "1grau"})
    lista = eventos.listar(conn)
    assert [e["tipo"] for e in lista] == ["processo_descoberto", "processo_sincronizado"]
    assert lista[0]["quando"] == "2026-10-05T06:00:00"
    assert lista[0]["dados"] == {"fonte": "djen"}


def test_listar_filtra_por_numero():
    conn = banco.conectar(":memory:")
    eventos.registrar(conn, "processo_descoberto", "111")
    eventos.registrar(conn, "processo_descoberto", "222")
    assert [e["numero"] for e in eventos.listar(conn, "222")] == ["222"]


def test_agora_em_horario_de_cuiaba():
    assert eventos.agora().endswith("-04:00")
