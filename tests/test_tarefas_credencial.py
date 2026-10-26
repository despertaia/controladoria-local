from captura import tarefas


def _preparar(monkeypatch, tmp_path, usadas):
    def falsa_fabrica(instancia, cpf, senha):
        usadas.append((instancia, senha))
        return object()

    monkeypatch.setattr(tarefas, "_fabrica_cliente", falsa_fabrica)
    monkeypatch.setattr(tarefas, "_instancias", [("1grau", "1ª"), ("2grau", "2ª")])
    monkeypatch.setattr(tarefas.grupos, "ler",
                        lambda slug: {"numeros": ["10088882920238110041"]})
    monkeypatch.setattr(tarefas.grupos, "PASTA_GRUPOS", str(tmp_path))


def test_baixar_usa_a_senha_de_cada_instancia(monkeypatch, tmp_path):
    usadas = []
    _preparar(monkeypatch, tmp_path, usadas)
    monkeypatch.setattr(tarefas, "baixar_processo_completo", lambda *a, **k: iter(()))
    tarefas._executar("g", "baixar", {"cpf": "1", "senha": "s1", "senha_2grau": "s2"})
    assert usadas == [("1grau", "s1"), ("2grau", "s2")]


def test_sincronizar_usa_a_senha_do_1grau(monkeypatch, tmp_path):
    usadas = []
    _preparar(monkeypatch, tmp_path, usadas)
    monkeypatch.setattr(tarefas, "sincronizar_um", lambda cli, n: {"andamentos": []})
    tarefas._executar("g", "sincronizar", {"cpf": "1", "senha": "s1", "senha_2grau": "s2"})
    assert usadas == [("1grau", "s1")]
