"""Lançador local (python -m local.iniciar): agenda, instância única e fumaça do servidor."""

import io
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from local import iniciar
from nucleo import ajustes_locais, banco
from tests.cofre_falso import cofre_falso  # noqa: F401

CUIABA = ZoneInfo("America/Cuiaba")
SP = ZoneInfo("America/Sao_Paulo")


def _c(*args):
    return datetime(*args, tzinfo=CUIABA)


# --- proximo_disparo ------------------------------------------------------------------

@pytest.mark.parametrize("agora, esperado", [
    (_c(2026, 10, 7, 5, 0), _c(2026, 10, 7, 6, 0)),        # quarta, antes das 6h
    (_c(2026, 10, 7, 6, 0), _c(2026, 10, 7, 12, 0)),       # exatamente 6h: o próximo
    (_c(2026, 10, 7, 12, 30), _c(2026, 10, 7, 18, 0)),
    (_c(2026, 10, 7, 18, 0, 1), _c(2026, 10, 8, 6, 0)),    # depois das 18h: amanhã
    (_c(2026, 10, 9, 18, 0, 1), _c(2026, 10, 12, 6, 0)),   # sexta à noite: segunda
    (_c(2026, 10, 10, 9, 0), _c(2026, 10, 12, 6, 0)),      # sábado: segunda
    (_c(2026, 10, 11, 23, 59), _c(2026, 10, 12, 6, 0)),    # domingo: segunda
    (_c(2026, 12, 31, 19, 0), _c(2027, 1, 1, 6, 0)),       # virada do ano (quinta → sexta)
    (_c(2026, 10, 31, 20, 0), _c(2026, 11, 2, 6, 0)),      # virada do mês num sábado
])
def test_proximo_disparo_em_dias_uteis(agora, esperado):
    assert iniciar.proximo_disparo(agora, "America/Cuiaba") == esperado


def test_proximo_disparo_converte_o_fuso():
    agora = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)  # 05h30 em Cuiabá, 06h30 em SP
    cuiaba = iniciar.proximo_disparo(agora, "America/Cuiaba")
    sp = iniciar.proximo_disparo(agora, "America/Sao_Paulo")
    assert cuiaba == _c(2026, 10, 7, 6, 0) and cuiaba.tzinfo == CUIABA
    assert sp == datetime(2026, 10, 7, 12, 0, tzinfo=SP)


def test_proximo_disparo_sem_fuso_e_hora_local_do_tribunal():
    assert iniciar.proximo_disparo(datetime(2026, 10, 7, 7, 0), "America/Sao_Paulo") == \
        datetime(2026, 10, 7, 12, 0, tzinfo=SP)


def test_proximo_disparo_usa_o_fuso_do_tribunal(monkeypatch):
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "TJMG")
    # 15h30 UTC = 12h30 em SP (próximo: 18h) e 11h30 em Cuiabá (próximo: 12h)
    alvo = iniciar.proximo_disparo(datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc))
    assert alvo.utcoffset() == SP.utcoffset(datetime(2026, 10, 7)) and alvo.hour == 18
    monkeypatch.setenv("CONTROLADORIA_TRIBUNAL", "TJMT")
    assert iniciar.proximo_disparo(datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)).hour == 12


# --- agendador ------------------------------------------------------------------------

def _relogio(instantes, parado):
    fila = list(instantes)

    def agora():
        if len(fila) > 1:
            return fila.pop(0)
        parado.set()
        return fila[0]
    return agora


def _tarefas(caminho):
    conn = banco.conectar(caminho)
    try:
        return [tuple(r) for r in conn.execute("SELECT tipo, pedida_por FROM tarefa")]
    finally:
        conn.close()


def test_agendador_pede_varredura_no_horario(tmp_path, monkeypatch):
    caminho = str(tmp_path / "t.db")
    monkeypatch.setattr(iniciar, "_configurado", lambda: True)
    parado = threading.Event()
    agora = _relogio([_c(2026, 10, 7, 5, 59), _c(2026, 10, 7, 5, 59, 30),
                      _c(2026, 10, 7, 6, 0, 2), _c(2026, 10, 7, 6, 1)], parado)
    iniciar.rodar_agendador(lambda: banco.conectar(caminho), parado, agora_fn=agora,
                            espera_maxima=0.01)
    assert _tarefas(caminho) == [("varredura", "agendador")]


def test_agendador_nao_pede_antes_da_configuracao(tmp_path, monkeypatch):
    caminho = str(tmp_path / "t.db")
    monkeypatch.setattr(iniciar, "_configurado", lambda: False)
    parado = threading.Event()
    agora = _relogio([_c(2026, 10, 7, 5, 59), _c(2026, 10, 7, 6, 0, 2),
                      _c(2026, 10, 7, 6, 1)], parado)
    iniciar.rodar_agendador(lambda: banco.conectar(caminho), parado, agora_fn=agora,
                            espera_maxima=0.01)
    assert _tarefas(caminho) == []


# --- instância única ------------------------------------------------------------------

class _Resposta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_que_devolve(corpo=None, erro=None):
    def urlopen(url, timeout=None):
        assert url.endswith("/saude")
        if erro is not None:
            raise erro
        return _Resposta(corpo)
    return urlopen


@pytest.mark.parametrize("corpo, esperado", [
    (json.dumps({"ok": True, "trabalhador": "ativo", "horas_desde_varredura": None}).encode(),
     True),
    (b"<html>outro programa</html>", False),
    (json.dumps({"ok": True}).encode(), False),
])
def test_ja_rodando_reconhece_a_controladoria(monkeypatch, corpo, esperado):
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_que_devolve(corpo))
    assert iniciar.ja_rodando(5056) is esperado


def test_ja_rodando_aceita_503_da_controladoria(monkeypatch):
    erro = urllib.error.HTTPError("http://127.0.0.1:5056/saude", 503, "x", {},
                                  io.BytesIO(b'{"ok": false, "erro": "OperationalError"}'))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_que_devolve(erro=erro))
    assert iniciar.ja_rodando(5056) is True


def test_ja_rodando_porta_livre(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        _urlopen_que_devolve(erro=urllib.error.URLError("recusada")))
    assert iniciar.ja_rodando(5056) is False


@pytest.fixture
def ambiente_main(tmp_path, monkeypatch):
    """main() sem sujar o repositório nem o ambiente dos outros testes."""
    for nome in ("CONTROLADORIA_LOCAL", "PAINEL_SENHA", "SECRET_KEY"):
        monkeypatch.setenv(nome, "")
    monkeypatch.setattr(iniciar, "RAIZ", str(tmp_path))
    monkeypatch.setattr(iniciar, "configurar_log", lambda *a, **k: None)
    monkeypatch.setattr(iniciar, "_ajustes_locais", lambda: None)
    monkeypatch.chdir(tmp_path)
    abertos = []
    monkeypatch.setattr(iniciar.webbrowser, "open", abertos.append)
    return abertos


def test_segunda_instancia_so_abre_o_navegador(ambiente_main, monkeypatch):
    monkeypatch.setattr(iniciar, "ja_rodando", lambda porta: True)
    monkeypatch.setattr(iniciar, "criar_servidor",
                        lambda porta: pytest.fail("não deveria subir outro servidor"))
    assert iniciar.main(["--porta", "5099"]) == 0
    assert ambiente_main == ["http://127.0.0.1:5099"]


def test_segunda_instancia_sem_navegador_nao_abre(ambiente_main, monkeypatch):
    monkeypatch.setattr(iniciar, "ja_rodando", lambda porta: True)
    assert iniciar.main(["--sem-navegador"]) == 0
    assert ambiente_main == []


def test_porta_ocupada_por_outro_programa(ambiente_main, monkeypatch):
    monkeypatch.setattr(iniciar, "ja_rodando", lambda porta: False)

    def ocupada(porta):
        raise OSError("Address already in use")
    monkeypatch.setattr(iniciar, "criar_servidor", ocupada)
    assert iniciar.main(["--porta", "5099"]) == 2


def test_porta_vem_do_ajustes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(iniciar, "_ajustes_locais", lambda: None)
    assert iniciar.ler_porta(None) == 5056
    (tmp_path / "dados").mkdir()
    (tmp_path / "dados" / "ajustes.json").write_text('{"porta": 5070}', encoding="utf-8")
    assert iniciar.ler_porta(None) == 5070
    assert iniciar.ler_porta(5080) == 5080
    assert not iniciar.primeira_execucao()


def test_preparar_ambiente_modo_local(ambiente_main, monkeypatch):
    monkeypatch.setenv("PAINEL_SENHA", "da-vps")
    iniciar.preparar_ambiente()
    import os
    assert os.environ["CONTROLADORIA_LOCAL"] == "1"
    assert os.environ["PAINEL_SENHA"] == ""
    assert len(os.environ["SECRET_KEY"]) >= 32


def _servir(servidor):
    """servidor.run() numa thread; o close() vindo de fora derruba o select: tudo bem."""
    try:
        servidor.run()
    except (OSError, ValueError):
        pass


VARIAVEIS = ("CONTROLADORIA_LOCAL", "PAINEL_SENHA", "SECRET_KEY", "CONTROLADORIA_TRIBUNAL",
             "PJE_CPF", "PJE_SENHA", "PJE_SENHA_2GRAU", "CARTEIRA_ADVOGADO_NOME",
             "CARTEIRA_OAB_NUMERO", "CARTEIRA_OAB_UF")


@pytest.fixture
def ajustes_reais(tmp_path, monkeypatch, cofre_falso):  # noqa: F811
    """Os ajustes locais de verdade (frente B), com cofre em memória e json no tmp."""
    for var in VARIAVEIS:
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("CONTROLADORIA_AJUSTES", str(tmp_path / "ajustes" / "ajustes.json"))
    monkeypatch.chdir(tmp_path)  # main() faz chdir para a raiz: o monkeypatch devolve
    return cofre_falso


def test_porta_e_primeira_execucao_pelos_ajustes_locais(ajustes_reais, tmp_path):
    assert iniciar.primeira_execucao() is True
    assert iniciar.ler_porta(None) == 5056
    ajustes_locais.salvar(tribunal="TJMG", advogado_nome="FULANA DE TAL", oab_numero="123",
                          oab_uf="MG", pje_cpf="00000000000", pje_senha="x", porta=5071)
    assert iniciar.primeira_execucao() is False
    assert iniciar.ler_porta(None) == 5071
    assert iniciar._configurado() is True


def test_preparar_ambiente_carrega_ajustes_e_chave_do_cofre(ajustes_reais, tmp_path,
                                                            monkeypatch):
    import os
    monkeypatch.setattr(iniciar, "RAIZ", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    ajustes_locais.salvar(tribunal="TJMG", advogado_nome="FULANA DE TAL", oab_numero="123",
                          oab_uf="MG", pje_cpf="00000000000", pje_senha="x")
    for var in VARIAVEIS:
        os.environ[var] = ""
    iniciar.preparar_ambiente()
    assert os.environ["CONTROLADORIA_TRIBUNAL"] == "TJMG"
    assert os.environ["CARTEIRA_OAB_UF"] == "MG"
    assert os.environ["SECRET_KEY"] == ajustes_reais.itens[
        ("br.com.despertaia.controladoria", "secret_key")]


def test_raiz_sem_configuracao_leva_ao_configurar(ajustes_reais, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTROLADORIA_LOCAL", "1")
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "dados" / "controladoria.db"))
    servidor = iniciar.criar_servidor(0)
    fio = threading.Thread(target=_servir, args=(servidor,), daemon=True)
    fio.start()

    class SemSeguir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        abridor = urllib.request.build_opener(SemSeguir)
        with pytest.raises(urllib.error.HTTPError) as exc:
            abridor.open(f"http://127.0.0.1:{servidor.effective_port}/", timeout=5)
        assert exc.value.code == 302
        assert exc.value.headers["Location"].endswith("/configurar")
    finally:
        servidor.close()
        fio.join(timeout=5)


# --- fumaça: servidor de verdade numa porta livre --------------------------------------

def test_fumaca_servidor_trabalhador_e_agendador(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTROLADORIA_BANCO", str(tmp_path / "dados" / "controladoria.db"))
    servidor = iniciar.criar_servidor(0)
    porta = servidor.effective_port
    parado = threading.Event()
    fio = threading.Thread(target=_servir, args=(servidor,), daemon=True)
    fio.start()
    trabalhador = iniciar.iniciar_trabalhador(parado, pausa=0.1)
    agendador = iniciar.iniciar_agendador(parado)
    try:
        corpo = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{porta}/saude", timeout=2) as r:
                    corpo = json.loads(r.read())
                if corpo.get("trabalhador") == "ativo":
                    break
            except OSError:
                pass
            time.sleep(0.1)
        assert corpo and corpo["ok"] is True and corpo["trabalhador"] == "ativo"
        assert iniciar.ja_rodando(porta) is True
        assert trabalhador.is_alive() and agendador.is_alive()
    finally:
        parado.set()
        servidor.close()
        fio.join(timeout=5)
        trabalhador.join(timeout=15)
        agendador.join(timeout=5)


# --- casa da Banca e ponte do Lex -------------------------------------------------------

def test_definir_casa_grava_so_o_campo_e_preserva_o_resto(ajustes_reais, tmp_path):
    ajustes_locais.salvar(tribunal="TJMT", advogado_nome="FULANA DE TAL", oab_numero="123",
                          oab_uf="MT", pje_cpf="00000000000", pje_senha="x", porta=5072)
    itens_antes = dict(ajustes_reais.itens)
    casa = tmp_path / "Lex Lab" / "Escritório"
    casa.mkdir(parents=True)
    assert iniciar.main(["--definir-casa", str(casa)]) == 0
    dados = ajustes_locais.ler()
    assert dados["casa_banca"] == str(casa)
    assert dados["porta"] == 5072 and dados["advogado_nome"] == "FULANA DE TAL"
    assert ajustes_reais.itens == itens_antes  # cofre intocado


def test_definir_casa_sem_ajustes_ainda(ajustes_reais, tmp_path):
    casa = tmp_path / "casa"
    casa.mkdir()
    assert iniciar.definir_casa(str(casa)) == 0
    assert ajustes_locais.ler() == {"casa_banca": str(casa)}


def test_definir_casa_pasta_inexistente(ajustes_reais, tmp_path, capsys):
    assert iniciar.definir_casa(str(tmp_path / "nao-existe")) == 1
    assert "não existe" in capsys.readouterr().out
    assert ajustes_locais.ler() == {}


def test_ponte_sobe_e_para_com_o_lancador(monkeypatch):
    from ponte_mac import local as ponte_local
    chamadas = []

    def rodar_em_thread(url, casa, parar):
        chamadas.append(("rodar", url, casa))
        fio = threading.Thread(target=parar.wait, daemon=True)
        fio.start()
        return fio

    monkeypatch.setattr(ponte_local, "rodar_em_thread", rodar_em_thread)
    monkeypatch.setattr(ponte_local, "parar",
                        lambda parar, fio: (chamadas.append(("parar",)), parar.set(),
                                            fio.join(1)))
    ponte = iniciar.iniciar_ponte(5056)
    assert chamadas == [("rodar", "http://127.0.0.1:5056", None)]
    iniciar.parar_ponte(ponte)
    assert chamadas[-1] == ("parar",) and not ponte[2].is_alive()


def _varredura_antiga(caminho, quando):
    conn = banco.conectar(caminho)
    with conn:
        conn.execute("INSERT INTO tarefa (tipo, ref, estado, pedida_em, pedida_por) "
                     "VALUES ('varredura', '', 'ok', ?, 'agendador')", (quando,))
    conn.close()


def test_disparo_anterior():
    fuso = "America/Cuiaba"
    assert iniciar.disparo_anterior(_c(2026, 10, 7, 9, 30), fuso).hour == 6
    assert iniciar.disparo_anterior(_c(2026, 10, 7, 12, 0), fuso).hour == 12
    seg = iniciar.disparo_anterior(_c(2026, 10, 12, 5, 0), fuso)  # segunda cedo → sexta 18h
    assert (seg.day, seg.hour) == (9, 18)


def test_ligou_depois_do_horario_perdido_varre_ao_iniciar(tmp_path, monkeypatch):
    """O computador estava desligado às 06h; ligou às 09h30: varre na hora."""
    caminho = str(tmp_path / "t.db")
    _varredura_antiga(caminho, "2026-10-06T18:00:05-04:00")
    monkeypatch.setattr(iniciar, "_configurado", lambda: True)
    parado = threading.Event()
    agora = _relogio([_c(2026, 10, 7, 9, 30), _c(2026, 10, 7, 9, 31)], parado)
    iniciar.rodar_agendador(lambda: banco.conectar(caminho), parado, agora_fn=agora,
                            espera_maxima=0.01)
    assert _tarefas(caminho) == [("varredura", "agendador"), ("varredura", "agendador")]


def test_sem_horario_perdido_nao_varre_ao_iniciar(tmp_path, monkeypatch):
    caminho = str(tmp_path / "t.db")
    _varredura_antiga(caminho, "2026-10-07T06:00:03-04:00")
    monkeypatch.setattr(iniciar, "_configurado", lambda: True)
    parado = threading.Event()
    agora = _relogio([_c(2026, 10, 7, 9, 30), _c(2026, 10, 7, 9, 31)], parado)
    iniciar.rodar_agendador(lambda: banco.conectar(caminho), parado, agora_fn=agora,
                            espera_maxima=0.01)
    assert _tarefas(caminho) == [("varredura", "agendador")]  # só a antiga
