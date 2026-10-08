import sincronizar


def test_criar_cliente_usa_a_fabrica(monkeypatch):
    chamadas = []

    def falsa(instancia, **kwargs):
        chamadas.append((instancia, kwargs))
        return "cliente"

    monkeypatch.setattr(sincronizar, "criar_cliente_mni", falsa)
    assert sincronizar.criar_cliente() == "cliente"
    assert chamadas == [("1grau", {"intervalo": 1.0})]
