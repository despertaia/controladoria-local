from datetime import date
from types import SimpleNamespace

import pytest
import requests

from nucleo import djen
from tests.fixtures_djen import item_djen

TJMT = "00000010220248110041"
TRF1 = "00000030220244013600"


def test_janelas_mensais():
    assert djen.janelas_mensais(date(2026, 1, 15), date(2026, 3, 10)) == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 10)),
    ]


def test_buscar_pagina_ate_acabar():
    paginas = []

    def obter(params):
        paginas.append(params["pagina"])
        quantos = 100 if params["pagina"] == 1 else 3
        return {"status": "success", "items": [item_djen(i, TJMT) for i in range(quantos)]}

    assert len(djen.buscar({"nomeAdvogado": "X"}, obter=obter, pausa=0)) == 103
    assert paginas == [1, 2]


def test_buscar_com_erro_do_djen():
    with pytest.raises(djen.DJENError):
        djen.buscar({}, obter=lambda p: {"status": "error", "message": "falhou"}, pausa=0)


def test_normalizar_com_oab_confirmada():
    pub = djen.normalizar(item_djen(7, "1008888-34.2023.4.01.3600", "TRF1", oab=("54321", "MT")),
                          "54321", "MT")
    assert pub["numero"] == "10088883420234013600"
    assert pub["oab_confirmada"] is True
    assert pub["id"] == 7 and pub["data_disponibilizacao"] == "2026-07-10"


def test_normalizar_sem_oab_vinculada():
    pub = djen.normalizar(item_djen(8, TJMT), "54321", "MT")
    assert pub["oab_confirmada"] is False and pub["tribunal"] == "TJMT"


def test_buscar_por_advogado_une_nome_e_oab_sem_repetir():
    chamadas = []

    def obter(params):
        chamadas.append(params)
        if "nomeAdvogado" in params:
            return {"status": "success", "items": [item_djen(1, TJMT), item_djen(2, TRF1, "TRF1")]}
        return {"status": "success", "items": [item_djen(2, TRF1, "TRF1", oab=("54321", "MT"))]}

    pubs = djen.buscar_por_advogado("MARIA EXEMPLO DA SILVA", "54321", "MT",
                                    date(2026, 7, 1), date(2026, 7, 31), obter=obter, pausa=0)
    assert [p["id"] for p in pubs] == [1, 2]
    assert pubs[1]["oab_confirmada"] is True
    assert {c.get("nomeAdvogado") for c in chamadas} == {"MARIA EXEMPLO DA SILVA", None}
    assert all(c["dataDisponibilizacaoInicio"] == "2026-07-01" for c in chamadas)


def test_buscar_detecta_paginacao_incompleta():
    def obter(params):
        if params["pagina"] == 1:
            return {"status": "success", "count": 150, "items": [item_djen(i, TJMT) for i in range(100)]}
        return {"status": "success", "items": [item_djen(100 + i, TJMT) for i in range(20)]}

    with pytest.raises(djen.DJENError, match="paginação incompleta"):
        djen.buscar({}, obter=obter, pausa=0)


def test_buscar_limita_paginas(monkeypatch):
    monkeypatch.setattr(djen, "MAX_PAGINAS", 3)

    def obter(params):
        return {"status": "success", "items": [item_djen(i, TJMT) for i in range(100)]}

    with pytest.raises(djen.DJENError, match="mais de"):
        djen.buscar({}, obter=obter, pausa=0)


def test_obter_padrao_nao_repete_erro_4xx(monkeypatch):
    chamadas = [0]

    def mock_get(*args, **kwargs):
        chamadas[0] += 1
        r = requests.models.Response()
        r.status_code = 400
        r.url = djen.URL
        r.raise_for_status()  # This will raise HTTPError with response=r
        return r

    sleeps = []

    def mock_sleep(t):
        sleeps.append(t)

    monkeypatch.setattr(djen.requests, "get", mock_get)
    monkeypatch.setattr(djen.time, "sleep", mock_sleep)

    with pytest.raises(djen.DJENError):
        djen._obter_padrao({})

    assert chamadas[0] == 1
    assert sleeps == []


def test_obter_padrao_tenta_6_vezes_com_429(monkeypatch):
    """429 (Too Many Requests) should be retried, not fail immediately."""
    chamadas = [0]

    def mock_get(*args, **kwargs):
        chamadas[0] += 1
        r = requests.models.Response()
        r.status_code = 429
        r.url = djen.URL
        r.raise_for_status()  # This will raise HTTPError with response=r
        return r

    sleeps = []

    def mock_sleep(t):
        sleeps.append(t)

    monkeypatch.setattr(djen.requests, "get", mock_get)
    monkeypatch.setattr(djen.time, "sleep", mock_sleep)

    with pytest.raises(djen.DJENError):
        djen._obter_padrao({})

    assert chamadas[0] == 6
    assert sleeps == [3, 6, 9, 12, 15]


def test_obter_padrao_tenta_6_vezes_sem_dormir_no_fim(monkeypatch):
    chamadas = [0]

    def mock_get(*args, **kwargs):
        chamadas[0] += 1
        raise requests.ConnectionError("mock")

    sleeps = []

    def mock_sleep(t):
        sleeps.append(t)

    monkeypatch.setattr(djen.requests, "get", mock_get)
    monkeypatch.setattr(djen.time, "sleep", mock_sleep)

    with pytest.raises(djen.DJENError):
        djen._obter_padrao({})

    assert chamadas[0] == 6
    assert sleeps == [3, 6, 9, 12, 15]


def _mock_429(monkeypatch, cabecalhos):
    chamadas = [0]

    def mock_get(*args, **kwargs):
        chamadas[0] += 1
        r = requests.models.Response()
        r.status_code = 429
        r.headers.update(cabecalhos)
        r.url = djen.URL
        r.raise_for_status()

    sleeps = []
    monkeypatch.setattr(djen.requests, "get", mock_get)
    monkeypatch.setattr(djen.time, "sleep", sleeps.append)
    return chamadas, sleeps


def test_obter_padrao_respeita_retry_after(monkeypatch):
    chamadas, sleeps = _mock_429(monkeypatch, {"Retry-After": "7"})
    with pytest.raises(djen.DJENError):
        djen._obter_padrao({})
    assert chamadas[0] == 6
    assert sleeps == [7, 7, 7, 7, 7]


def test_obter_padrao_limita_retry_after_a_120s(monkeypatch):
    _, sleeps = _mock_429(monkeypatch, {"Retry-After": "3600"})
    with pytest.raises(djen.DJENError):
        djen._obter_padrao({})
    assert sleeps == [120] * 5


def test_obter_padrao_ignora_retry_after_nao_numerico(monkeypatch):
    _, sleeps = _mock_429(monkeypatch, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with pytest.raises(djen.DJENError):
        djen._obter_padrao({})
    assert sleeps == [3, 6, 9, 12, 15]


def test_buscar_por_advogado_preserva_oab_confirmada():
    def obter(params):
        if "nomeAdvogado" in params:
            return {"status": "success", "items": [item_djen(2, TRF1, "TRF1", oab=("54321", "MT"))]}
        return {"status": "success", "items": [item_djen(2, TRF1, "TRF1")]}

    pubs = djen.buscar_por_advogado("MARIA EXEMPLO DA SILVA", "54321", "MT",
                                    date(2026, 7, 1), date(2026, 7, 31), obter=obter, pausa=0)
    assert len(pubs) == 1
    assert pubs[0]["oab_confirmada"] is True


# ---------------------------------------------------------------------------
# Busca por nome da parte
# ---------------------------------------------------------------------------

def _item_com_partes(id_, numero, data="2026-07-10", destinatarios=None, link="https://exemplo.invalido/x",
                     tribunal="TJMT"):
    item = item_djen(id_, numero, tribunal, data)
    item["link"] = link
    item["numeroprocessocommascara"] = ""
    item["destinatarios"] = destinatarios if destinatarios is not None else [
        {"nome": "BANCO DO BRASIL S.A.", "comunicacao_id": id_, "polo": "A"},
        {"nome": "JOÃO DA SILVA", "comunicacao_id": id_, "polo": "P"},
        {"nome": "SEM POLO LTDA", "comunicacao_id": id_}]
    return item


def test_buscar_por_parte_pagina_ate_o_limite_e_marca_truncado():
    paginas = []

    def obter(params):
        paginas.append(params["pagina"])
        base = (params["pagina"] - 1) * 100
        return {"status": "success", "items": [_item_com_partes(base + i, TJMT) for i in range(100)]}

    r = djen.buscar_por_parte("banco", "TJMT", date(2026, 1, 1), date(2026, 7, 1),
                              limite=250, obter=obter, pausa=0)
    assert paginas == [1, 2, 3]
    assert len(r["publicacoes"]) == 250 and r["truncado"] is True


def test_buscar_por_parte_para_em_pagina_curta():
    paginas = []

    def obter(params):
        paginas.append(params["pagina"])
        quantos = 100 if params["pagina"] == 1 else 7
        return {"status": "success", "items": [_item_com_partes(params["pagina"] * 1000 + i, TJMT)
                                               for i in range(quantos)]}

    r = djen.buscar_por_parte("banco", "TJMT", date(2026, 1, 1), date(2026, 7, 1), obter=obter, pausa=0)
    assert paginas == [1, 2]
    assert len(r["publicacoes"]) == 107 and r["truncado"] is False


def test_buscar_por_parte_exatamente_no_limite_com_pagina_cheia_e_truncado():
    def obter(params):
        return {"status": "success", "items": [_item_com_partes(i, TJMT) for i in range(100)]}

    r = djen.buscar_por_parte("x", "", date(2026, 1, 1), date(2026, 7, 1), limite=100, obter=obter, pausa=0)
    assert len(r["publicacoes"]) == 100 and r["truncado"] is True


def test_buscar_por_parte_envia_os_parametros():
    recebidos = []

    def obter(params):
        recebidos.append(dict(params))
        return {"status": "success", "items": []}

    djen.buscar_por_parte("Maria Souza", "TRF1", date(2026, 1, 2), date(2026, 7, 3), obter=obter, pausa=0)
    assert recebidos == [{"nomeParte": "Maria Souza", "siglaTribunal": "TRF1",
                          "dataDisponibilizacaoInicio": "2026-01-02",
                          "dataDisponibilizacaoFim": "2026-07-03",
                          "itensPorPagina": 100, "pagina": 1}]


def test_buscar_por_parte_omite_o_tribunal_vazio():
    recebidos = []

    def obter(params):
        recebidos.append(dict(params))
        return {"status": "success", "items": []}

    r = djen.buscar_por_parte("Maria Souza", "", date(2026, 1, 2), date(2026, 7, 3), obter=obter, pausa=0)
    assert "siglaTribunal" not in recebidos[0]
    assert r == {"publicacoes": [], "truncado": False}


def test_buscar_por_parte_erro_de_status():
    with pytest.raises(djen.DJENError):
        djen.buscar_por_parte("x", "", date(2026, 1, 1), date(2026, 7, 1),
                              obter=lambda p: {"status": "error", "message": "falhou"}, pausa=0)


def test_publicacao_com_partes_mapeia_polo_e_campos():
    p = djen._publicacao_com_partes(_item_com_partes(5, "0000001-02.2024.8.11.0041"))
    assert p["id"] == 5 and p["numero"] == TJMT and p["numero_formatado"] == "0000001-02.2024.8.11.0041"
    assert p["tribunal"] == "TJMT" and p["orgao"] == "1ª VARA CÍVEL"
    assert p["classe"] == "PROCEDIMENTO COMUM CÍVEL" and p["data"] == "2026-07-10"
    assert p["link"] == "https://exemplo.invalido/x"
    assert p["partes"] == [{"nome": "BANCO DO BRASIL S.A.", "polo": "Polo Ativo"},
                           {"nome": "JOÃO DA SILVA", "polo": "Polo Passivo"},
                           {"nome": "SEM POLO LTDA", "polo": ""}]


def test_publicacao_com_partes_usa_mascara_e_descarta_link_nao_http():
    item = _item_com_partes(6, TJMT, link="javascript:alert(1)")
    item["numeroprocessocommascara"] = "0000001-02.2024.8.11.0041"
    p = djen._publicacao_com_partes(item)
    assert p["link"] == "" and p["numero_formatado"] == "0000001-02.2024.8.11.0041"
    item["numeroprocessocommascara"] = ""
    item["destinatarios"] = None
    p = djen._publicacao_com_partes(item)
    assert p["numero_formatado"] == "0000001-02.2024.8.11.0041" and p["partes"] == []


def test_agrupar_por_processo():
    novo = {"id": 2, "numero": TJMT, "numero_formatado": "N1", "tribunal": "TJMT", "orgao": "2ª VARA",
            "classe": "CLASSE NOVA", "data": "2026-07-10", "link": "https://a/novo",
            "partes": [{"nome": "BANCO", "polo": "Polo Ativo"}, {"nome": "ZÉ", "polo": "Polo Passivo"}]}
    velho = {"id": 1, "numero": TJMT, "numero_formatado": "N1", "tribunal": "TJMT", "orgao": "1ª VARA",
             "classe": "CLASSE VELHA", "data": "2026-03-01", "link": "https://a/velho",
             "partes": [{"nome": "ZÉ", "polo": "Polo Passivo"}, {"nome": "OUTRO", "polo": ""}]}
    outro = {"id": 3, "numero": TRF1, "numero_formatado": "N2", "tribunal": "TRF1", "orgao": "VARA FEDERAL",
             "classe": "C", "data": "2026-07-10", "link": "", "partes": []}
    # fora de ordem de propósito: o mais velho primeiro
    grupos = djen.agrupar_por_processo([velho, novo, outro])
    assert [g["numero"] for g in grupos] == [TJMT, TRF1]  # mesma data: desempata pelo número
    g = grupos[0]
    assert g["orgao"] == "2ª VARA" and g["classe"] == "CLASSE NOVA" and g["link"] == "https://a/novo"
    assert g["ultima_publicacao"] == "2026-07-10" and g["n_publicacoes"] == 2
    # únicas por nome, na ordem em que aparecem (da publicação mais recente para a mais antiga)
    assert g["partes"] == [{"nome": "BANCO", "polo": "Polo Ativo"}, {"nome": "ZÉ", "polo": "Polo Passivo"},
                           {"nome": "OUTRO", "polo": ""}]


def test_agrupar_ordena_pela_ultima_publicacao_desc():
    def pub(id_, numero, data):
        return {"id": id_, "numero": numero, "numero_formatado": numero, "tribunal": "TJMT", "orgao": "",
                "classe": "", "data": data, "link": "", "partes": []}

    grupos = djen.agrupar_por_processo([pub(1, "B", "2026-01-01"), pub(2, "A", "2026-05-01"),
                                        pub(3, "C", "2026-05-01")])
    assert [g["numero"] for g in grupos] == ["A", "C", "B"]


def test_publicacao_com_partes_aceita_item_sem_id_ou_numero_como_invalido():
    assert djen._publicacao_com_partes({"numero_processo": TJMT}) is None
    assert djen._publicacao_com_partes({"id": "abc", "numero_processo": TJMT}) is None
    assert djen._publicacao_com_partes({"id": 1}) is None
    assert djen._publicacao_com_partes({"id": 1, "numero_processo": ""}) is None
    assert djen._publicacao_com_partes("lixo") is None


def test_buscar_por_parte_ignora_itens_invalidos_e_repetidos_entre_paginas():
    itens = [_item_com_partes(1, TJMT), {"numero_processo": TJMT}, {"id": 2}, "lixo"]
    pagina2 = [_item_com_partes(1, TJMT), _item_com_partes(3, TJMT)]

    def obter(params):
        return {"status": "success", "items": (itens + [_item_com_partes(100 + i, TJMT) for i in range(96)]
                                               if params["pagina"] == 1 else pagina2)}

    r = djen.buscar_por_parte("x", "", date(2026, 1, 1), date(2026, 7, 1), obter=obter, pausa=0)
    ids = [p["id"] for p in r["publicacoes"]]
    assert ids.count(1) == 1 and 3 in ids and len(ids) == len(set(ids)) == 98


def test_buscar_por_parte_resposta_que_nao_e_dict():
    with pytest.raises(djen.DJENError):
        djen.buscar_por_parte("x", "", date(2026, 1, 1), date(2026, 7, 1), obter=lambda p: ["lixo"], pausa=0)


def test_obter_interativo_tenta_no_maximo_duas_vezes(monkeypatch):
    chamadas, pausas = [], []

    def get(url, **kw):
        chamadas.append(kw)
        raise requests.ConnectionError("fora")

    monkeypatch.setattr(djen.requests, "get", get)
    monkeypatch.setattr(djen.time, "sleep", pausas.append)
    with pytest.raises(djen.DJENError):
        djen.obter_interativo({"nomeParte": "x"})
    assert len(chamadas) == 2 and pausas == [1]
    assert all(c["timeout"] == 12 for c in chamadas)


def test_obter_interativo_nao_respeita_retry_after_e_falha_em_4xx_e_5xx(monkeypatch):
    pausas = []

    class Resp:
        def __init__(self, codigo):
            self.status_code = codigo
            self.headers = {"Retry-After": "120"}

        def raise_for_status(self):
            raise requests.HTTPError(response=self)

    for codigo in (429, 500, 404):
        pausas.clear()
        monkeypatch.setattr(djen.requests, "get", lambda url, _c=codigo, **kw: Resp(_c))
        monkeypatch.setattr(djen.time, "sleep", pausas.append)
        with pytest.raises(djen.DJENError):
            djen.obter_interativo({})
        assert all(p <= 1 for p in pausas), codigo


def test_obter_interativo_json_invalido_e_sucesso(monkeypatch):
    class Resp:
        status_code = 200
        headers = {}

        def __init__(self, dados):
            self._dados = dados

        def raise_for_status(self):
            pass

        def json(self):
            if isinstance(self._dados, Exception):
                raise self._dados
            return self._dados

    monkeypatch.setattr(djen.time, "sleep", lambda s: None)
    monkeypatch.setattr(djen.requests, "get", lambda url, **kw: Resp(ValueError("json")))
    with pytest.raises(djen.DJENError):
        djen.obter_interativo({})
    monkeypatch.setattr(djen.requests, "get", lambda url, **kw: Resp({"status": "success", "items": []}))
    assert djen.obter_interativo({}) == {"status": "success", "items": []}


def test_normalizar_guarda_as_partes_com_o_polo_normalizado():
    item = item_djen(11, "1008888-34.2023.4.01.3600", "TRF1")
    item["destinatarios"] = [
        {"nome": "  JOANA   FICTÍCIA ", "polo": "A"},
        {"nome": "UNIÃO FEDERAL", "polo": "passivo"},
        {"nome": "TERCEIRO INTERESSADO", "polo": "T"},
        {"nome": "PEDRO FICTÍCIO", "polo": "ATIVO"},
        {"nome": "AUTARQUIA X", "polo": "p"},
        {"nome": "JOANA FICTÍCIA", "polo": "A"},  # repetida
        {"nome": "", "polo": "A"}, "lixo", {"polo": "P"},
    ]
    assert djen.normalizar(item, "54321", "MT")["partes"] == [
        {"nome": "JOANA FICTÍCIA", "polo": "ativo"},
        {"nome": "UNIÃO FEDERAL", "polo": "passivo"},
        {"nome": "TERCEIRO INTERESSADO", "polo": ""},
        {"nome": "PEDRO FICTÍCIO", "polo": "ativo"},
        {"nome": "AUTARQUIA X", "polo": "passivo"},
    ]


def test_normalizar_sem_destinatarios_tem_partes_vazias():
    item = item_djen(12, TJMT)
    assert djen.normalizar(item, "54321", "MT")["partes"] == []
    item["destinatarios"] = None
    assert djen.normalizar(item, "54321", "MT")["partes"] == []


def test_buscar_por_processo_filtra_pelo_numero_e_pagina():
    chamadas = []

    def obter(params):
        chamadas.append(params)
        if params["pagina"] == 1:
            return {"status": "success", "count": 102,
                    "items": [item_djen(i, TRF1, "TRF1") for i in range(100)]}
        return {"status": "success", "items": [item_djen(100, TRF1, "TRF1"),
                                               item_djen(101, TRF1, "TRF1", oab=("54321", "MT"))]}

    pubs = djen.buscar_por_processo("0000003-02.2024.4.01.3600", date(2026, 9, 1),
                                    date(2026, 10, 6), numero_oab="54321", uf_oab="MT",
                                    obter=obter, pausa=0)
    assert len(pubs) == 102 and {p["numero"] for p in pubs} == {TRF1}
    assert [c["pagina"] for c in chamadas] == [1, 2]
    assert all(c["numeroProcesso"] == TRF1 for c in chamadas)
    assert all("nomeAdvogado" not in c and "numeroOab" not in c for c in chamadas)
    assert chamadas[0]["dataDisponibilizacaoInicio"] == "2026-09-01"
    assert chamadas[0]["dataDisponibilizacaoFim"] == "2026-10-06"
    assert [p["oab_confirmada"] for p in pubs if p["id"] == 101] == [True]
    assert not any(p["oab_confirmada"] for p in pubs if p["id"] != 101)


def test_buscar_por_processo_descarta_outro_numero_e_repetido():
    def obter(params):
        return {"status": "success", "items": [item_djen(1, TJMT), item_djen(1, TJMT),
                                               item_djen(2, TRF1, "TRF1")]}

    pubs = djen.buscar_por_processo(TJMT, date(2026, 9, 1), date(2026, 10, 6), obter=obter, pausa=0)
    assert [p["id"] for p in pubs] == [1]


def test_buscar_por_processo_recusa_numero_invalido_sem_chamar():
    with pytest.raises(ValueError):
        djen.buscar_por_processo("123", date(2026, 9, 1), date(2026, 10, 6),
                                 obter=lambda p: pytest.fail("não deve chamar"), pausa=0)


def test_buscar_por_processo_com_erro_do_djen():
    with pytest.raises(djen.DJENError):
        djen.buscar_por_processo(TJMT, date(2026, 9, 1), date(2026, 10, 6),
                                 obter=lambda p: {"status": "error", "message": "falhou"}, pausa=0)
