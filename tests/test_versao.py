"""nucleo/versao.py: versão instalada, novidades, versão publicada e o aviso."""

import pytest

from nucleo import versao


def test_atual_le_o_arquivo_da_raiz():
    assert versao.atual() == (versao.RAIZ / "VERSAO").read_text(encoding="utf-8").strip()
    assert versao.valida(versao.atual())


def test_atual_sem_arquivo_ou_invalido(tmp_path):
    assert versao.atual(tmp_path) == ""
    (tmp_path / "VERSAO").write_text("um ponto dois\n", encoding="utf-8")
    assert versao.atual(tmp_path) == ""
    outra = tmp_path / "outra"
    outra.mkdir()
    (outra / "VERSAO").write_text(" 2.10.3 \n", encoding="utf-8")
    assert versao.atual(outra) == "2.10.3"


@pytest.mark.parametrize("a, b, esperado", [
    ("1.2.0", "1.2.0", 0), ("1.2.0", "1.1.9", 1), ("1.1.9", "1.2.0", -1),
    ("1.10.0", "1.9.0", 1), ("2.0.0", "1.99.99", 1), ("0.0.1", "0.0.2", -1),
    (" 1.2.0\n", "1.2.0", 0),
])
def test_comparar(a, b, esperado):
    assert versao.comparar(a, b) == esperado


@pytest.mark.parametrize("ruim", ["1.2", "1.2.0.1", "v1.2.0", "a.b.c", "", "1.2.0-beta"])
def test_comparar_recusa_formato_invalido(ruim):
    assert not versao.valida(ruim)
    with pytest.raises(ValueError):
        versao.comparar(ruim, "1.0.0")


def test_parser_de_novidades():
    texto = (
        "# Novidades\n\nIntrodução que não é item.\n\n"
        "## 1.2.0 — 07/10/2026\n\n- Primeiro item.\n- Segundo item,\n  que continua.\n\n"
        "## 1.1.0 - 01/09/2026\n* Com asterisco.\n\n"
        "## Título sem versão\n- Fica na seção anterior.\n"
        "## 1.0.0\n- Sem data.\n")
    secoes = versao.ler_novidades(texto)
    assert [s["versao"] for s in secoes] == ["1.2.0", "1.1.0", "1.0.0"]
    assert secoes[0] == {"versao": "1.2.0", "data": "07/10/2026",
                         "itens": ["Primeiro item.", "Segundo item, que continua."]}
    assert secoes[1] == {"versao": "1.1.0", "data": "01/09/2026",
                         "itens": ["Com asterisco.", "Fica na seção anterior."]}
    assert secoes[2] == {"versao": "1.0.0", "data": "", "itens": ["Sem data."]}


def test_novidades_do_repositorio_comecam_pela_versao_atual():
    lista = versao.novidades()
    assert lista[0]["versao"] == versao.atual()
    assert all(s["itens"] and s["data"] for s in lista)
    versoes = [s["versao"] for s in lista]
    assert versoes == sorted(versoes, key=versao._tupla, reverse=True)
    lista[0]["itens"].append("mexido")  # cópia: o cache não muda
    assert "mexido" not in versao.novidades()[0]["itens"]


def test_versao_publicada_ok_e_cache_de_24h():
    chamadas = []

    def obter(url, timeout):
        chamadas.append((url, timeout))
        return "1.3.0\n"
    assert versao.versao_publicada(obter, agora=1000.0) == "1.3.0"
    assert chamadas == [(versao.URL_PUBLICADA, 5)]
    assert versao.versao_publicada(obter, agora=1000.0 + 23 * 3600) == "1.3.0"
    assert len(chamadas) == 1  # do cache
    versao.versao_publicada(obter, agora=1000.0 + 25 * 3600)
    assert len(chamadas) == 2


def test_versao_publicada_erro_devolve_none():
    def obter(url, timeout):
        raise TimeoutError("sem resposta")
    assert versao.versao_publicada(obter, agora=0.0) is None
    # sem resposta: tenta de novo depois de 1 h, não a cada página
    assert versao.versao_publicada(lambda u, t: "9.9.9", agora=60.0) is None
    assert versao.versao_publicada(lambda u, t: "9.9.9", agora=3700.0) == "9.9.9"


@pytest.mark.parametrize("lixo", ["<html>404: Not Found</html>", "", "1.2", None, "1.2.0 extra"])
def test_versao_publicada_lixo_devolve_none(lixo):
    assert versao.versao_publicada(lambda u, t: lixo, agora=0.0) is None


def test_aviso_so_no_modo_local_e_so_quando_maior(monkeypatch):
    atual = versao.atual()
    maior = f"{versao._tupla(atual)[0] + 1}.0.0"
    versao.versao_publicada(lambda u, t: maior, agora=0.0)
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    assert versao.aviso_de_versao() is None  # produção (VPS): nunca
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    assert versao.aviso_de_versao() == {"nova": maior, "atual": atual}
    for igual_ou_menor in (atual, "0.0.1"):
        versao.limpar_cache()
        versao.versao_publicada(lambda u, t, v=igual_ou_menor: v, agora=0.0)
        assert versao.aviso_de_versao() is None
    versao.limpar_cache()
    assert versao.aviso_de_versao() is None  # sem consulta ainda: nada


def test_aviso_nunca_vai_a_rede(monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")

    def proibido(url, timeout):
        raise AssertionError("o aviso não pode consultar a rede")
    monkeypatch.setattr(versao, "_obter_padrao", proibido)
    assert versao.aviso_de_versao() is None


def test_verificacao_em_segundo_plano(monkeypatch):
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)
    assert versao.verificar_em_segundo_plano(lambda u, t: "9.0.0") is None
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    fio = versao.verificar_em_segundo_plano(lambda u, t: "9.0.0")
    fio.join(5)
    assert versao.aviso_de_versao() == {"nova": "9.0.0", "atual": versao.atual()}
    # cache em dia: não dispara outra thread
    assert versao.verificar_em_segundo_plano(lambda u, t: "9.9.9") is None


# --- 1.6.0: a versão e as novidades dizem o que foi feito -----------------------------------


def _novidades_da_160() -> str:
    secao = versao.novidades()[0]
    assert secao["versao"] == "1.6.0"
    return "\n".join(secao["itens"])


def test_versao_e_160_com_a_data_de_hoje():
    assert versao.atual() == "1.6.0"
    assert versao.novidades()[0]["data"] == "04/11/2026"


def test_novidades_160_contam_o_que_foi_construido():
    t = _novidades_da_160()
    for frase in ("próximos e vencidos", "um número por grupo", "mesmo processo",
                  "15 dias antes ou depois", "parte do envio", "mudou depois que você abriu",
                  "«Protocolado»", "continua aberto", "o próximo prazo", "os atrasados",
                  "sem data", "[A CONFERIR]", "mais de um ano", "Google", "suspensão de fim de ano",
                  "exceções", "de tal dia a tal dia"):
        assert frase in t, frase


def test_novidades_160_sem_rastro_interno_nem_numero_de_lei():
    """Texto para advogado: sem nome de motor, de script ou de arquivo, sem o modelo, e sem
    número de prazo de lei (só os que são do próprio sistema: o contador e a janela de comparação)."""
    import re
    t = _novidades_da_160()
    assert not re.search(r"\b\w+\.(py|md|sh|db|csv|ics|json)\b", t)
    assert not re.search(r"\b(nucleo|scripts|templates|dados|tests)/", t)
    for proibido in ("claude", "sonnet", "opus", "haiku", "modelo", "script", "banca", "prompt",
                     "token", "json", "sqlite"):
        assert proibido not in t.lower(), proibido
    numeros = {m.group(0).lower() for m in re.finditer(
        r"\b(\d+|um|uma|dois|duas|quinze|trinta)\s+(dias?|horas?)\b", t, re.IGNORECASE)}
    assert numeros <= {"2 dias", "15 dias"}, numeros
