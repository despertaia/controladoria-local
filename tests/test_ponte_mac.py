"""Serviço do Mac (ponte_mac): sem rede, sem Claude, sem Chaves de verdade."""

import functools
import http.client
import io
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import zipfile
from pathlib import Path

import pytest

from ponte_mac import chaves, cliente, executor, laco, pastas
from ponte_mac import __main__ as principal

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="serviço do Mac: processos POSIX (true, killpg, caffeinate, bash)")

PACOTE = {
    "demanda": 7, "n": 1, "episodio": 501, "modo": "novo", "numero": "10000011120268110001",
    "numero_formatado": "1000001-11.2026.8.11.0001", "tribunal": "TJMT",
    "cliente": "Maria Exemplo", "parte_contraria": "Banco Fictício S.A.",
    "orgao": "1º Juizado Especial Cível", "orientacao": "Pedir danos morais de R$ 10 mil.",
    "ajuste": "", "squad_anterior": "", "run_anterior": "",
    "publicacoes": [{"data": "2026-10-01", "tipo": "Intimação", "orgao": "1º JEC",
                     "texto": "Fica a parte intimada para emendar a inicial.", "link": ""}],
    "intimacoes": [{"data": "2026-10-02", "tipo": "Intimação eletrônica"}],
}
# a pasta do processo na casa da Banca (sem a pasta antiga `<dígitos>-cartao<N>`)
PASTA_CASO = "1000001-11.2026.8.11.0001 - Maria Exemplo"
PASTA_ANTIGA = "10000011120268110001-cartao7"
PUBLICACAO = "publicacao-cartao7.md"


# --- Painel (cliente HTTP) -------------------------------------------------------------

class Resposta:
    def __init__(self, status=200, corpo=b""):
        self.status = status
        self._corpo = corpo

    def read(self):
        return self._corpo

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AbrirFalso:
    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.pedidos = []

    def __call__(self, pedido, timeout=None):
        self.pedidos.append(pedido)
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _erro_http(status, corpo=b'{"ok": false, "erro": "chave inv\\u00e1lida"}'):
    return urllib.error.HTTPError("https://painel/x", status, "erro", {}, io.BytesIO(corpo))


def test_painel_proximo_monta_autorizacao_e_devolve_pacote():
    abrir = AbrirFalso(Resposta(200, json.dumps(PACOTE).encode()))
    p = cliente.Painel("https://painel.exemplo/", "chave-secreta", abrir=abrir)
    assert p.proximo() == PACOTE
    pedido = abrir.pedidos[0]
    assert pedido.full_url == "https://painel.exemplo/ponte/proximo"
    assert pedido.get_method() == "POST"
    assert pedido.get_header("Authorization") == "Bearer chave-secreta"


def test_painel_204_vira_none():
    abrir = AbrirFalso(Resposta(204), Resposta(204))
    p = cliente.Painel("https://painel.exemplo", "k", abrir=abrir)
    assert p.proximo() is None
    assert p.autos(7) is None
    assert abrir.pedidos[1].full_url == "https://painel.exemplo/ponte/demanda/7/autos"
    assert abrir.pedidos[1].get_method() == "GET"


def test_painel_autos_devolve_bytes():
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(Resposta(200, b"PK..")))
    assert p.autos(7) == b"PK.."


def test_painel_401_vira_painel_erro_sem_vazar_a_chave():
    p = cliente.Painel("https://painel.exemplo", "chave-secreta", abrir=AbrirFalso(_erro_http(401)))
    with pytest.raises(cliente.PainelErro) as exc:
        p.proximo()
    assert exc.value.status == 401
    assert "chave-secreta" not in str(exc.value)


def test_painel_batida_e_falha_mandam_json():
    abrir = AbrirFalso(Resposta(200, b'{"ok":true}'), Resposta(200, b'{"ok":true}'))
    p = cliente.Painel("https://painel.exemplo", "k", abrir=abrir)
    p.batida(7, 2, "peticao-inicial-jec", "Pesquisa (3/13)")
    p.falha(7, 2, "limite", "pausado")
    b, f = abrir.pedidos
    assert b.full_url.endswith("/ponte/demanda/7/batida")
    assert json.loads(b.data) == {"n": 2, "squad": "peticao-inicial-jec", "etapa": "Pesquisa (3/13)"}
    assert b.get_header("Content-type") == "application/json"
    assert f.full_url.endswith("/ponte/demanda/7/falha")
    assert json.loads(f.data) == {"n": 2, "motivo": "limite", "detalhe": "pausado"}


def test_painel_resultado_multipart_com_campos_e_arquivos(tmp_path):
    docx = tmp_path / "peticao-final.docx"
    docx.write_bytes(b"DOCX-BYTES")
    gate = tmp_path / "x.citation-gate.json"
    gate.write_bytes(b'{"gate_status": "aprovado"}')
    abrir = AbrirFalso(Resposta(200, b'{"ok":true}'))
    p = cliente.Painel("https://painel.exemplo", "k", abrir=abrir)
    p.resultado(7, 1, {"squad": "s", "run_id": "r1", "gate_status": "aprovado",
                       "citacoes_total": 25, "citacoes_falhas": 0},
                {"peca_docx": docx, "citation_gate": gate})
    pedido = abrir.pedidos[0]
    assert pedido.full_url.endswith("/ponte/demanda/7/resultado")
    tipo = pedido.get_header("Content-type")
    assert tipo.startswith("multipart/form-data; boundary=")
    corpo = pedido.data
    assert b'name="n"\r\n\r\n1\r\n' in corpo
    assert b'name="citacoes_total"\r\n\r\n25\r\n' in corpo
    assert b'name="run_id"\r\n\r\nr1\r\n' in corpo
    assert b'name="peca_docx"; filename="peticao-final.docx"' in corpo
    assert b"DOCX-BYTES" in corpo
    assert b'name="citation_gate"; filename="x.citation-gate.json"' in corpo
    fronteira = tipo.split("boundary=", 1)[1].encode()
    assert corpo.rstrip().endswith(b"--" + fronteira + b"--")


def test_painel_erro_de_rede_passa_adiante():
    p = cliente.Painel("https://painel.exemplo", "k",
                       abrir=AbrirFalso(urllib.error.URLError("sem rede")))
    with pytest.raises(OSError):
        p.proximo()


# --- Chaves do macOS -------------------------------------------------------------------

class RodarFalso:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout
        self.chamadas = []

    def __call__(self, args, **kw):
        self.chamadas.append((args, kw))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


@pytest.fixture
def no_mac(monkeypatch):
    """O serviço do Mac do escritório (fora do modo local): Chaves pelo `security`."""
    monkeypatch.delenv("CONTROLADORIA_LOCAL", raising=False)


def test_chaves_ler(no_mac):
    rodar = RodarFalso(0, "valor-guardado\n")
    assert chaves.ler("controladoria-ponte-chave", rodar=rodar) == "valor-guardado"
    args, _ = rodar.chamadas[0]
    assert args == ["security", "find-generic-password", "-s", "controladoria-ponte-chave", "-w"]
    assert chaves.ler("x", rodar=RodarFalso(44, "")) is None


def test_chaves_guardar_pela_entrada_padrao_nunca_na_linha_de_comando(no_mac):
    rodar = RodarFalso(0, "")
    chaves.guardar("controladoria-ponte-chave", "A" * 43, rodar=rodar)
    args, kw = rodar.chamadas[0]
    assert args == ["security", "-i"]
    assert "A" * 43 not in " ".join(args)
    assert "add-generic-password -U" in kw["input"]
    assert "-s controladoria-ponte-chave" in kw["input"]
    assert "A" * 43 in kw["input"]
    with pytest.raises(ValueError):
        chaves.guardar("x", "tem espaço", rodar=rodar)


# --- Executor: pasta, prompt, ambiente, comando ----------------------------------------

def _zip(entradas: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for nome, dados in entradas.items():
            z.writestr(nome, dados)
    return buf.getvalue()


def test_preparar_pasta_grava_publicacao_e_extrai_autos(tmp_path):
    autos = _zip({"1grau/001-inicial.pdf": b"%PDF-1", "2grau/002-acordao.pdf": b"%PDF-2"})
    pasta = executor.preparar_pasta(tmp_path, PACOTE, autos)
    assert pasta == tmp_path / PASTA_CASO
    md = (pasta / PUBLICACAO).read_text(encoding="utf-8")
    assert "1000001-11.2026.8.11.0001" in md
    assert "Fica a parte intimada para emendar a inicial." in md
    assert "Intimação eletrônica" in md
    assert (pasta / "autos" / "1grau" / "001-inicial.pdf").read_bytes() == b"%PDF-1"
    assert (pasta / "autos" / "2grau" / "002-acordao.pdf").read_bytes() == b"%PDF-2"


def test_preparar_pasta_sem_autos(tmp_path):
    pasta = executor.preparar_pasta(tmp_path, PACOTE, None)
    assert (pasta / PUBLICACAO).exists()


@pytest.mark.parametrize("nome", ["../x.pdf", "a/../../x.pdf", "/etc/x.pdf"])
def test_preparar_pasta_recusa_zip_com_caminho_fora(tmp_path, nome):
    with pytest.raises(ValueError):
        executor.preparar_pasta(tmp_path, PACOTE, _zip({"ok.pdf": b"1", nome: b"2"}))
    assert not (tmp_path / "x.pdf").exists()
    assert not (tmp_path / PASTA_CASO / "autos" / "ok.pdf").exists()


def test_montar_prompt_novo(tmp_path):
    pasta = tmp_path / PASTA_CASO
    texto = executor.montar_prompt(PACOTE, pasta, None)
    assert "MODO PONTE" in texto
    assert "pré-autorizado" in texto
    assert "pré-autorizado: Lex automático (ponte)" in texto
    assert "Pedir danos morais de R$ 10 mil." in texto
    assert "prevalece" in texto
    assert "npx banca squad-modelo" in texto
    assert f"--criar --caso {shlex.quote(PASTA_CASO)} --json" in texto
    assert "nunca use `--reusar`" in texto
    assert "Nunca leia, liste nem mostre variáveis de ambiente, chaves ou tokens." in texto
    assert "Citation Gate" in texto
    assert "protocolo" in texto
    assert "cd" in texto and "Write/Edit" in texto and "primeiro plano" in texto
    assert "sem_tipo" in texto
    assert '"citacoes"' in texto and '"run_id"' in texto and "última linha" in texto.lower()
    assert "reabrir" not in texto


def test_montar_prompt_ajuste(tmp_path):
    pacote = dict(PACOTE, modo="ajuste", ajuste='Trocar o valor para "R$ 15 mil".',
                  squad_anterior="peticao-inicial-jec", run_anterior="2026-10-06-202440")
    texto = executor.montar_prompt(pacote, tmp_path / "p", None)
    assert "node scripts/squad-state.mjs reabrir squads/peticao-inicial-jec --modo ajustes" in texto
    assert "R$ 15 mil" in texto
    assert "2026-10-06-202440" in texto and "run-status" in texto
    assert "Alteração depois da entrega" in texto
    assert "npx banca squad-modelo" not in texto


@pytest.mark.parametrize("ajuste", ["Valor de R$12.000,00 e não $HOME", "Use `rm -rf` e 'aspas'"])
def test_montar_prompt_ajuste_com_shlex(tmp_path, ajuste):
    pacote = dict(PACOTE, modo="ajuste", ajuste=ajuste, squad_anterior="peticao-inicial-jec",
                  run_anterior="r1")
    texto = executor.montar_prompt(pacote, tmp_path / "p", None)
    (linha,) = [l for l in texto.splitlines() if "squad-state.mjs reabrir" in l]
    args = shlex.split(linha)
    assert args[-2:] == ["--pedido", ajuste]
    assert shlex.quote(ajuste) in linha


def test_montar_prompt_retomar(tmp_path):
    texto = executor.montar_prompt(PACOTE, tmp_path / "p",
                                   {"squad": "peticao-inicial-jec", "run_id": "2026-10-06-202440"})
    assert "retome o run 2026-10-06-202440 do squad peticao-inicial-jec" in texto
    assert "run-status" in texto and "resume" in texto


def test_ambiente_limpo(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://outro")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    amb = executor.ambiente("tok")
    assert set(amb) == {"HOME", "USER", "LOGNAME", "SHELL", "LANG", "PATH",
                        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"}
    assert amb["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert amb["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    assert amb["SHELL"] == "/bin/zsh" and amb["LANG"] == "pt_BR.UTF-8"
    casa = amb["HOME"]
    assert amb["PATH"] == (f"{casa}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
                           "/usr/bin:/bin:/usr/sbin:/sbin")
    assert "ANTHROPIC_BASE_URL" not in amb


def test_comando():
    cmd = executor.comando("faça")
    assert cmd[:3] == ["claude", "-p", "faça"]
    assert cmd[3:11] == ["--output-format", "stream-json", "--verbose", "--permission-mode",
                         "acceptEdits", "--disallowedTools", "AskUserQuestion", "--allowedTools"]
    assert cmd[11:] == executor.ALLOWED_TOOLS
    assert executor.ALLOWED_TOOLS == [
        "Bash(npx banca:*)", "Bash(node:*)", "Bash(npm run:*)", "Bash(ls:*)", "Bash(cat:*)",
        "Bash(mkdir:*)", "Bash(cp:*)", "Bash(cd:*)", "Bash(test:*)", "Bash(find:*)",
        "Bash(grep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(sed -n:*)",
        "Bash(echo:*)", "Read", "Write", "Edit", "Glob", "Grep", "Task", "WebSearch",
        "WebFetch", "TodoWrite"]
    assert executor.TEMPO_LIMITE_S == 3 * 3600
    assert executor.CAMPOS_FIM == ("status", "squad", "peca", "citacoes")


# --- Executor: leitura do stream, progresso e pacote -----------------------------------

FIM_PRONTO = {"status": "pronto", "squad": "peticao-inicial-jec",
              "peca": "4 - Peças prontas/x/peticao.docx",
              "citacoes": {"total": 25, "falhas": 0,
                           "relatorio": "squads/peticao-inicial-jec/output/2026-10-06-202440/RELATORIO.md"},
              "detalhe": "ok"}


def _linha(**ev):
    return json.dumps(ev, ensure_ascii=False)


def _stream(texto_final, *, antes=None):
    linhas = [_linha(type="system", subtype="init"),
              _linha(type="assistant", message={"content": [{"type": "text", "text": "Vou começar."}]})]
    if antes is not None:
        linhas.append(_linha(type="result", subtype="success", result=antes))
    linhas.append(_linha(type="result", subtype="success", result=texto_final))
    return linhas


def test_ler_fim_acha_json_na_ultima_linha_do_ultimo_result():
    texto = "Terminei a peça.\n\n" + json.dumps(FIM_PRONTO, ensure_ascii=False)
    assert executor.ler_fim(_stream(texto, antes="Esperando tarefas em segundo plano.")) == FIM_PRONTO


def test_ler_fim_sem_json_ou_sem_campos():
    assert executor.ler_fim(_stream("Não consegui.")) is None
    assert executor.ler_fim(_stream('Feito.\n{"status": "pronto"}')) is None
    assert executor.ler_fim(["lixo", "{nao json"]) is None
    assert executor.ler_fim([]) is None


def test_parece_limite():
    assert executor.parece_limite("Claude AI usage limit reached|1760000000")
    assert executor.parece_limite("Rate limit exceeded")
    assert executor.parece_limite("Atingiu o limite de uso")
    assert not executor.parece_limite("Peça pronta, sem limitações.")


GATE = {"gate_status": "aprovado",
        "citations": [{"status": "verificada"}] * 23 + [{"status": "verificada_no_acervo"},
                                                         {"status": "NAO_ENCONTRADA"}]}


def _squad_do_caso(casa: Path, squad="peticao-inicial-jec", pasta=PASTA_CASO):
    (casa / "squads" / squad).mkdir(parents=True, exist_ok=True)
    (casa / "squads" / squad / "caso.json").write_text(json.dumps({"pasta": pasta}))


def _agora_iso(delta=0.0):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() + delta, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _pacote_falso(casa: Path, squad="peticao-inicial-jec", run="2026-10-06-202440",
                  gerado_em=None, caso=PASTA_CASO):
    if caso:
        _squad_do_caso(casa, squad, caso)
    base = casa / "squads" / squad / "output"
    pac = base / "pacote" / run
    pac.mkdir(parents=True)
    (pac / "peticao-inicial-final.docx").write_bytes(b"DOCX")
    (pac / "peticao-inicial-final.pdf").write_bytes(b"%PDF")
    (pac / "TERMO-DE-CONFERENCIA.docx").write_bytes(b"TERMO")
    (pac / "NOTA-AO-REVISOR.md").write_text("nota", encoding="utf-8")
    manifesto = {"run_id": run, **({"gerado_em": gerado_em} if gerado_em else {})}
    (pac / "MANIFESTO.json").write_text(json.dumps(manifesto), encoding="utf-8")
    for v in ("v1", "v4", "v10"):
        (base / run / v).mkdir(parents=True)
    (base / run / "v1" / "peca.md.citation-gate.json").write_text('{"gate_status": "reprovado"}')
    (base / run / "v4" / "peca.md.citation-gate.json").write_text(json.dumps(GATE))
    return pac


def test_localizar_pacote_com_run(tmp_path):
    pac = _pacote_falso(tmp_path)
    assert executor.localizar_pacote(tmp_path, "peticao-inicial-jec", "2026-10-06-202440") == pac
    assert executor.localizar_pacote(tmp_path, "peticao-inicial-jec", "outro-run") is None
    assert executor.localizar_pacote(tmp_path, "nao-existe", "") is None


def test_localizar_pacote_sem_run_pega_o_mais_recente(tmp_path):
    velho = _pacote_falso(tmp_path, run="2026-10-01-100000")
    novo = _pacote_falso(tmp_path, run="2026-10-06-202440")
    os.utime(velho, (1_000, 1_000))
    os.utime(novo, (2_000, 2_000))
    assert executor.localizar_pacote(tmp_path, "peticao-inicial-jec", "") == novo


def test_localizar_pacote_exige_docx_e_pdf(tmp_path):
    pac = _pacote_falso(tmp_path)
    (pac / "peticao-inicial-final.pdf").unlink()
    assert executor.localizar_pacote(tmp_path, "peticao-inicial-jec", "2026-10-06-202440") is None


def test_arquivos_do_pacote_e_gate_da_maior_versao(tmp_path):
    pac = _pacote_falso(tmp_path)
    arq = executor.arquivos_do_pacote(tmp_path, "peticao-inicial-jec", pac)
    assert arq["peca_docx"].name == "peticao-inicial-final.docx"
    assert arq["peca_pdf"].name == "peticao-inicial-final.pdf"
    assert arq["termo"].name == "TERMO-DE-CONFERENCIA.docx"
    assert arq["nota"].name == "NOTA-AO-REVISOR.md"
    assert arq["manifesto"].name == "MANIFESTO.json"
    assert arq["citation_gate"].parent.name == "v4"  # v10 não tem manifesto do gate


def test_progresso_le_o_state_mais_recente(tmp_path):
    pasta = tmp_path / PASTA_CASO
    assert executor.progresso(tmp_path, 0, pasta) is None
    for nome, mtime, etapa, caso in (("a", 1_000, 2, PASTA_CASO), ("b", 3_000, 5, PASTA_CASO),
                                     ("de-outro", 4_000, 9, "999-cartao1"), ("sem-caso", 4_500, 1, None)):
        if caso:
            _squad_do_caso(tmp_path, nome, caso)
        (tmp_path / "squads" / nome).mkdir(parents=True, exist_ok=True)
        st = tmp_path / "squads" / nome / "state.json"
        st.write_text(json.dumps({"status": "running",
                                  "step": {"current": etapa, "total": 13, "label": "Pesquisa"}}))
        os.utime(st, (mtime, mtime))
    # só squads cujo caso.json aponta para este caso contam
    assert executor.progresso(tmp_path, 0, pasta) == ("b", "Pesquisa (5/13)")
    assert executor.progresso(tmp_path, 3_500, pasta) is None
    assert executor.progresso(tmp_path, 0, tmp_path / "999-cartao1") == ("de-outro", "Pesquisa (9/13)")


def test_limpar_detalhe():
    sujo = ("Erro em /Users/fulano/Desktop/caso/x.md\nsegunda linha "
            "token sk-ant-oat01-abcdefghijklmnopqrstuvwxyz0123 e Bearer abc123 "
            + "K" * 45 + " " + "y" * 400)
    limpo = executor.limpar_detalhe(sujo)
    assert "\n" not in limpo
    assert len(limpo) <= 300
    assert "/Users/fulano" not in limpo
    assert "sk-ant" not in limpo
    assert "abc123" not in limpo
    assert "K" * 45 not in limpo


# --- Executor: execução com processo falso ---------------------------------------------

class SaidaFalsa:
    def __init__(self, linhas):
        self._linhas = [l + "\n" for l in linhas]

    def __iter__(self):
        return iter(self._linhas)

    def close(self):
        pass


class ProcessoFalso:
    def __init__(self, linhas, *, termina=True, rc=0, pid=4242):
        self.stdout = SaidaFalsa(linhas)
        self.termina, self.returncode, self.pid = termina, None, pid
        self._rc = rc
        self.morto = False

    def wait(self, timeout=None):
        if self.termina or self.morto:
            self.returncode = self._rc if not self.morto else -9
            return self.returncode
        raise subprocess.TimeoutExpired("claude", timeout)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.morto = True

    def kill(self):
        self.morto = True


class PopenFalso:
    """O primeiro comando é o `claude`; o `caffeinate` recebe um processo que termina."""

    def __init__(self, linhas, *, termina=True, rc=0, ao_iniciar=None):
        self.linhas, self.termina, self.rc = linhas, termina, rc
        self.ao_iniciar = ao_iniciar
        self.chamadas = []
        self.processos = []

    def __call__(self, args, **kw):
        self.chamadas.append((args, kw))
        if args[0] == "claude":
            if self.ao_iniciar:
                self.ao_iniciar()
            proc = ProcessoFalso(self.linhas, termina=self.termina, rc=self.rc)
        else:
            proc = ProcessoFalso([], termina=True)
        self.processos.append(proc)
        return proc


class Relogio:
    def __init__(self, passo=30.0):
        self.t, self.passo = 0.0, passo

    def __call__(self):
        self.t += self.passo
        return self.t


def _executar(casa, popen, relogio=None, retomar=None, pacote=PACOTE, desde=None,
              ao_progresso=None):
    progresso = []
    res = executor.executar(casa, pacote, None, "tok-secreto",
                            ao_progresso or (lambda squad, etapa: progresso.append((squad, etapa))),
                            retomar=retomar, popen=popen, relogio=relogio or Relogio(),
                            desde=desde)
    return res, progresso


def test_executar_sucesso(tmp_path):
    _pacote_falso(tmp_path)
    texto = "Pronto.\n" + json.dumps(FIM_PRONTO, ensure_ascii=False)
    popen = PopenFalso(_stream(texto))
    res, _ = _executar(tmp_path, popen)
    assert res["status"] == "pronto"
    assert res["squad"] == "peticao-inicial-jec"
    assert res["run_id"] == "2026-10-06-202440"  # do MANIFESTO.json, o JSON final não trouxe
    assert res["campos"] == {"squad": "peticao-inicial-jec", "run_id": "2026-10-06-202440",
                             "gate_status": "aprovado", "citacoes_total": 25,
                             "citacoes_falhas": 1}  # do manifesto do gate, não do Lex
    assert {"peca_docx", "peca_pdf", "citation_gate"} <= set(res["arquivos"])
    (claude_args, kw), (caf_args, _) = popen.chamadas
    assert claude_args[:2] == ["claude", "-p"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-secreto"
    assert "ANTHROPIC_BASE_URL" not in kw["env"]
    assert caf_args == ["caffeinate", "-i", "-w", "4242"]
    assert (tmp_path / PASTA_CASO / PUBLICACAO).exists()


def test_executar_sem_tipo(tmp_path):
    fim = {"status": "sem_tipo", "squad": "", "peca": "", "citacoes": {"total": 0, "falhas": 0},
           "detalhe": "A publicação não deixa claro se cabe recurso ou petição."}
    res, _ = _executar(tmp_path, PopenFalso(_stream("Parei.\n" + json.dumps(fim))))
    assert res["status"] == "sem_tipo"
    assert "não deixa claro" in res["detalhe"]
    assert res["arquivos"] == {}


def test_executar_limite(tmp_path):
    linhas = [_linha(type="system", subtype="init"),
              _linha(type="result", subtype="error", is_error=True,
                     result="Claude AI usage limit reached|1760000000")]
    res, _ = _executar(tmp_path, PopenFalso(linhas, rc=1))
    assert res["status"] == "limite"


def test_executar_sem_json_falha(tmp_path):
    res, _ = _executar(tmp_path, PopenFalso(_stream("Algo deu errado em /Users/fulano/x."), rc=1))
    assert res["status"] == "falhou"
    assert "/Users/fulano" not in res["detalhe"]


def test_executar_pronto_sem_pacote_falha(tmp_path):
    res, _ = _executar(tmp_path, PopenFalso(_stream(json.dumps(FIM_PRONTO))))
    assert res["status"] == "falhou"
    assert "pacote" in res["detalhe"].lower()


def test_executar_tempo_mata_o_processo_e_avisa_o_progresso(tmp_path):
    def cria_state():
        _squad_do_caso(tmp_path)
        (tmp_path / "squads" / "peticao-inicial-jec" / "state.json").write_text(
            json.dumps({"step": {"current": 3, "total": 13, "label": "Pesquisa"}}))
        _state_do_run(tmp_path, "peticao-inicial-jec", _agora_iso())  # run desta execução

    popen = PopenFalso([], termina=False, ao_iniciar=cria_state)
    res, progresso = _executar(tmp_path, popen, relogio=Relogio(passo=600))
    assert res["status"] == "tempo"
    assert popen.processos[0].morto
    assert progresso  # avisou a etapa durante a execução
    assert progresso[-1] == ("peticao-inicial-jec", "Pesquisa (3/13)")


def test_executar_retomar_vai_no_prompt(tmp_path):
    _squad_do_caso(tmp_path)
    executor.marcar_squad(tmp_path, "peticao-inicial-jec", 7)  # squad deste cartão
    popen = PopenFalso(_stream("x"))
    _executar(tmp_path, popen, retomar={"squad": "peticao-inicial-jec", "run_id": "r9"})
    prompt = popen.chamadas[0][0][2]
    assert "retome o run r9 do squad peticao-inicial-jec" in prompt


# --- Laço ------------------------------------------------------------------------------

class PainelFalso:
    def __init__(self, pacotes, *, erro_proximo=None, erro_batida=None, erro_batida_desde=0,
                 erro_resultado=None):
        self.pacotes = list(pacotes)
        self.erro_proximo = erro_proximo
        self.erro_batida, self.erro_batida_desde = erro_batida, erro_batida_desde
        self.erro_resultado = erro_resultado
        self.chamadas = []
        self.nomes = []  # squad_nome de cada batida

    def proximo(self):
        self.chamadas.append(("proximo",))
        if self.erro_proximo:
            raise self.erro_proximo
        return self.pacotes.pop(0) if self.pacotes else None

    def autos(self, demanda):
        self.chamadas.append(("autos", demanda))
        return None

    def batida(self, demanda, n, squad, etapa, squad_nome=""):
        self.chamadas.append(("batida", demanda, n, squad, etapa))
        self.nomes.append(squad_nome)
        if self.erro_batida and len(_so(self, "batida")) > self.erro_batida_desde:
            raise self.erro_batida

    def resultado(self, demanda, n, campos, arquivos):
        self.chamadas.append(("resultado", demanda, n, campos, arquivos))
        if self.erro_resultado:
            raise self.erro_resultado

    def falha(self, demanda, n, motivo, detalhe):
        self.chamadas.append(("falha", demanda, n, motivo, detalhe))

    def contato(self, claude_ok):
        self.chamadas.append(("contato", claude_ok))

    def pastas(self, criadas):
        self.pastas_pedidas = getattr(self, "pastas_pedidas", []) + [criadas]
        return []

    def autos_na_pasta(self, demanda, arquivos):
        raise AssertionError("sem pendentes, não há o que avisar")


def _so(painel, nome):
    return [c for c in painel.chamadas if c[0] == nome]


def test_laco_pronto_envia_resultado_com_os_tres_arquivos(tmp_path):
    pac = _pacote_falso(tmp_path)

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        assert token == "tok"
        ao_progresso("peticao-inicial-jec", "Pesquisa (3/13)")
        arquivos = executor.arquivos_do_pacote(casa, "peticao-inicial-jec", pac)
        return {"status": "pronto", "squad": "peticao-inicial-jec", "run_id": "2026-10-06-202440",
                "arquivos": arquivos, "detalhe": "",
                "campos": {"squad": "peticao-inicial-jec", "run_id": "2026-10-06-202440",
                           "gate_status": "aprovado", "citacoes_total": 25, "citacoes_falhas": 0}}

    painel = PainelFalso([PACOTE])
    estado = tmp_path / "estado.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(1), executar=executar_falso)
    (res,) = _so(painel, "resultado")
    assert res[1:3] == (7, 1)
    assert {"peca_docx", "peca_pdf", "citation_gate"} <= set(res[4])
    assert res[3]["gate_status"] == "aprovado"
    assert ("batida", 7, 1, "peticao-inicial-jec", "Pesquisa (3/13)") in painel.chamadas
    assert not _so(painel, "falha")
    assert json.loads(estado.read_text()).get("7") is None  # pronto: nada a retomar


def test_laco_falha_com_motivo_e_guarda_para_retomar(tmp_path):
    vistos = []

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        vistos.append(retomar)
        _squad_do_caso(casa)
        _state_do_run(casa, "peticao-inicial-jec", _agora_iso(+1))
        ao_progresso("peticao-inicial-jec", "Redação (6/13)")
        return {"status": "limite", "squad": "peticao-inicial-jec", "run_id": "",
                "arquivos": {}, "campos": {}, "detalhe": "pausado: limite do Claude"}

    painel = PainelFalso([PACOTE, dict(PACOTE, n=2)])
    estado = tmp_path / "estado.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(2), executar=executar_falso,
               relogio=Relogio(passo=2000))  # a 2ª volta já passou da pausa do limite
    falhas = _so(painel, "falha")
    assert falhas[0][1:4] == (7, 1, "limite")
    assert vistos[0] is None
    assert vistos[1] == {"squad": "peticao-inicial-jec", "run_id": "r-novo"}


@pytest.mark.parametrize("status,motivo", [("falhou", "erro"), ("tempo", "tempo"),
                                           ("sem_tipo", "sem_tipo")])
def test_laco_motivos(tmp_path, status, motivo):
    def executar_falso(*a, **k):
        return {"status": status, "squad": "", "run_id": "", "arquivos": {}, "campos": {},
                "detalhe": "x"}

    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1), executar=executar_falso)
    assert _so(painel, "falha")[0][3] == motivo


def test_laco_erro_na_execucao_vira_falha_erro(tmp_path):
    def explode(*a, **k):
        raise RuntimeError("quebrou em /Users/fulano/x")

    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1), executar=explode)
    (f,) = _so(painel, "falha")
    assert f[3] == "erro" and "/Users/fulano" not in f[4]


def test_laco_erro_de_rede_nao_derruba(tmp_path, caplog):
    painel = PainelFalso([], erro_proximo=urllib.error.URLError("sem rede"))
    dormidas = []
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=dormidas.append,
               parar=_parar_depois(3), executar=lambda *a, **k: pytest.fail("não devia executar"))
    assert len(_so(painel, "proximo")) == 3
    assert dormidas == [60, 60, 60]


def test_laco_sem_token_nao_reserva(tmp_path):
    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: None, tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(2), executar=lambda *a, **k: pytest.fail("não devia"))
    assert not _so(painel, "proximo")
    assert _so(painel, "contato") == [("contato", False)] * 2  # M5: o Mac aparece, sem Claude


def test_laco_nunca_registra_token(tmp_path, caplog):
    caplog.set_level("DEBUG")

    def executar_falso(*a, **k):
        raise RuntimeError("falhou")

    laco.rodar(PainelFalso([PACOTE]), tmp_path, lambda: "tok-super-secreto", tmp_path / "e.json",
               dormir=lambda s: None, parar=_parar_depois(1), executar=executar_falso)
    assert "tok-super-secreto" not in caplog.text


def _parar_depois(voltas):
    contagem = {"n": 0}

    def parar():
        contagem["n"] += 1
        return contagem["n"] > voltas
    return parar


# --- __main__ --------------------------------------------------------------------------

def test_guardar_chave_valida_e_limpa_a_area_de_transferencia(monkeypatch):
    guardadas, copiados = [], []
    monkeypatch.setattr(principal.chaves, "guardar", lambda s, v: guardadas.append((s, v)))
    boa = "Ab3_-" * 9
    assert principal.guardar_chave(colar=lambda: boa + "\n", copiar=copiados.append,
                                   reiniciar=lambda: False) == 0
    assert guardadas == [("controladoria-ponte-chave", boa)]
    assert copiados == [""]
    guardadas.clear()
    assert principal.guardar_chave(colar=lambda: "curta", copiar=copiados.append,
                                   reiniciar=lambda: False) != 0
    assert guardadas == []


def test_configuracao_padrao(tmp_path):
    cfg = principal.ler_configuracao(tmp_path / "nao-existe.json")
    assert cfg["painel"] == "https://processos.despertaia.com.br"
    assert cfg["casa"].endswith("Desktop/Claude Cowork/Processos pendentes")
    assert not cfg["casa"].startswith("~")


# --- rodada de correção 1 --------------------------------------------------------------

def test_painel_exige_https_salvo_localhost():
    with pytest.raises(ValueError):
        cliente.Painel("http://processos.exemplo", "k", abrir=AbrirFalso())
    with pytest.raises(ValueError):
        cliente.Painel("ftp://processos.exemplo", "k", abrir=AbrirFalso())
    assert cliente.Painel("http://localhost:5000/", "k", abrir=AbrirFalso()).url == \
        "http://localhost:5000"
    assert cliente.Painel("http://127.0.0.1:5000", "k", abrir=AbrirFalso()).url
    assert cliente.Painel("https://processos.exemplo", "k").url == "https://processos.exemplo"


def test_painel_nao_segue_redirecionamento():
    assert cliente._SemRedirecionar().redirect_request(None, None, 302, "Found", {},
                                                       "https://outro") is None
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(_erro_http(302, b"")))
    with pytest.raises(cliente.PainelErro) as exc:
        p.proximo()
    assert exc.value.status == 302
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(Resposta(301)))
    with pytest.raises(cliente.PainelErro):
        p.proximo()


def test_preparar_pasta_troca_os_autos_anteriores(tmp_path):
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"velho.pdf": b"1"}))
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"novo.pdf": b"2"}))
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["novo.pdf"]
    assert [p.name for p in pasta.iterdir() if p.name.startswith(".autos")] == []


def test_preparar_pasta_limite_de_tamanho_sem_gravacao_parcial(tmp_path, monkeypatch):
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"velho.pdf": b"1"}))
    monkeypatch.setattr(executor, "LIMITE_AUTOS_BYTES", 10)
    with pytest.raises(ValueError):
        executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"x" * 6, "b.pdf": b"y" * 6}))
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["velho.pdf"]
    assert [p.name for p in pasta.iterdir() if p.name.startswith(".autos")] == []


def test_preparar_pasta_limite_conta_bytes_reais(tmp_path, monkeypatch):
    # o tamanho declarado passa; o real (contado na extração) estoura
    monkeypatch.setattr(executor, "_itens_seguros", lambda z: [(i, (i.filename,))
                                                              for i in z.infolist()])
    monkeypatch.setattr(executor, "LIMITE_AUTOS_BYTES", 10)
    with pytest.raises(ValueError):
        executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"x" * 20}))
    # a pasta do processo é do advogado: fica, mas sem gravação parcial nem sobra
    pasta = tmp_path / PASTA_CASO
    assert list((pasta / "autos").iterdir()) == []
    assert [p.name for p in pasta.iterdir() if p.name.startswith(".autos")] == []
    assert not (pasta / executor.CONTROLE_AUTOS).exists()


def _state_do_run(casa, squad, iniciado):
    (casa / "squads" / squad).mkdir(parents=True, exist_ok=True)
    (casa / "squads" / squad / "run-state.json").write_text(
        json.dumps({"runId": "r-novo", "startedAt": iniciado}))


def test_run_atual_so_aceita_run_iniciado_depois_de_desde(tmp_path):
    _state_do_run(tmp_path, "s", "2026-10-07T12:00:00Z")
    marco = executor._instante("2026-10-07T12:00:00Z")
    assert executor.run_atual(tmp_path, "s") == "r-novo"
    assert executor.run_atual(tmp_path, "s", marco - 60) == "r-novo"
    assert executor.run_atual(tmp_path, "s", marco + 60) == ""
    (tmp_path / "squads" / "s" / "run-state.json").write_text(json.dumps({"runId": "x"}))
    assert executor.run_atual(tmp_path, "s", 0) == ""  # sem startedAt não vale


def _fim(**extra):
    return "Pronto.\n" + json.dumps({**FIM_PRONTO, **extra}, ensure_ascii=False)


def test_executar_recusa_pacote_antigo(tmp_path):
    _pacote_falso(tmp_path, gerado_em="2020-01-01T00:00:00Z")
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), desde=time.time())
    assert res["status"] == "falhou"
    assert res["detalhe"] == "o Lex não gerou pacote novo para este caso"
    assert res["arquivos"] == {}


def test_executar_recusa_pacote_antigo_pelo_mtime_do_manifesto(tmp_path):
    pac = _pacote_falso(tmp_path)
    os.utime(pac / "MANIFESTO.json", (1_000, 1_000))
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), desde=time.time() - 5)
    assert res["status"] == "falhou" and "pacote novo" in res["detalhe"]


def test_executar_pacote_novo_aceito(tmp_path):
    agora = time.time()
    _pacote_falso(tmp_path, gerado_em="2099-01-01T00:00:00.000Z")
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), desde=agora)
    assert res["status"] == "pronto"


def test_executar_retomar_com_run_conhecido_aceita_pacote_anterior(tmp_path):
    _pacote_falso(tmp_path, gerado_em="2020-01-01T00:00:00Z")
    executor.marcar_squad(tmp_path, "peticao-inicial-jec", 7)  # squad deste cartão
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), desde=time.time(),
                       retomar={"squad": "peticao-inicial-jec", "run_id": "2026-10-06-202440"})
    assert res["status"] == "pronto"
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), desde=time.time(),
                       retomar={"squad": "peticao-inicial-jec", "run_id": "outro-run"})
    assert res["status"] == "falhou"


def test_executar_recusa_squad_ligado_a_outro_caso(tmp_path):
    _pacote_falso(tmp_path)
    (tmp_path / "squads" / "peticao-inicial-jec" / "caso.json").write_text(
        json.dumps({"pasta": "9999999-cartao1"}))
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())))
    assert res["status"] == "falhou" and res["detalhe"] == "o squad não é deste caso; pacote recusado"


def test_executar_aceita_squad_ligado_a_este_caso(tmp_path):
    _pacote_falso(tmp_path)
    (tmp_path / "squads" / "peticao-inicial-jec" / "caso.json").write_text(
        json.dumps({"pasta": PASTA_CASO}))
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())))
    assert res["status"] == "pronto"
    (tmp_path / "squads" / "peticao-inicial-jec" / "caso.json").write_text(
        json.dumps({"pasta": str(tmp_path / PASTA_CASO)}))
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())))
    assert res["status"] == "pronto"


@pytest.mark.parametrize("anterior", [{"squad_anterior": "s", "run_anterior": ""},
                                      {"squad_anterior": "", "run_anterior": "r"}])
def test_executar_ajuste_sem_execucao_anterior_falha(tmp_path, anterior):
    popen = PopenFalso(_stream("x"))
    res, _ = _executar(tmp_path, popen, pacote=dict(PACOTE, modo="ajuste", ajuste="Reduza",
                                                     **anterior))
    assert res["status"] == "falhou" and res["detalhe"] == "ajuste sem a execução anterior"
    assert popen.chamadas == []


def test_executar_cancelado_pelo_progresso_encerra(tmp_path):
    avisos = []

    def cancela(squad, etapa):
        avisos.append(etapa)
        if len(avisos) > 1:  # o 1º aviso é o de autos prontos, antes do claude
            raise executor.Cancelado("o painel respondeu 409")

    popen = PopenFalso([], termina=False)
    res, _ = _executar(tmp_path, popen, relogio=Relogio(passo=120), ao_progresso=cancela)
    assert res["status"] == "cancelado"
    assert popen.processos[0].morto


def test_executar_sinaliza_o_grupo_ao_terminar_normalmente(tmp_path, monkeypatch):
    import signal
    import sys
    sinais = []
    monkeypatch.setattr(executor.os, "killpg", lambda pid, sig: sinais.append(sig))
    texto = json.dumps({"type": "result", "result": "sem json"})

    def popen(args, **kw):
        if args[0] == "claude":
            return subprocess.Popen([sys.executable, "-c", f"print({texto!r})"], **kw)
        return subprocess.Popen(["true"], **kw)

    res = executor.executar(tmp_path, PACOTE, None, "t", lambda *a: None, popen=popen)
    assert res["status"] == "falhou"
    assert signal.SIGTERM in sinais


def test_laco_batida_409_cancela_sem_falha_e_mantem_estado(tmp_path):
    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        _squad_do_caso(casa)
        _state_do_run(casa, "peticao-inicial-jec", _agora_iso(+1))  # run desta execução
        ao_progresso("peticao-inicial-jec", "Pesquisa (3/13)")
        pytest.fail("devia ter cancelado")

    painel = PainelFalso([PACOTE], erro_batida=cliente.PainelErro(409), erro_batida_desde=1)
    estado = tmp_path / "e.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(1), executar=_que_cancela(executar_falso))
    assert not _so(painel, "falha") and not _so(painel, "resultado")
    assert json.loads(estado.read_text())["7"]["squad"] == "peticao-inicial-jec"


def test_laco_batida_404_cancela_e_apaga_estado(tmp_path):
    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        ao_progresso("peticao-inicial-jec", "Pesquisa (3/13)")

    painel = PainelFalso([PACOTE], erro_batida=cliente.PainelErro(404), erro_batida_desde=1)
    estado = tmp_path / "e.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(1), executar=_que_cancela(executar_falso))
    assert not _so(painel, "falha")
    assert "7" not in json.loads(estado.read_text())


def _que_cancela(fn):
    def envolta(*a, **k):
        try:
            fn(*a, **k)
        except executor.Cancelado:
            return {"status": "cancelado", "squad": "", "run_id": "", "arquivos": {},
                    "campos": {}, "detalhe": ""}
        return {"status": "falhou", "squad": "", "run_id": "", "arquivos": {}, "campos": {},
                "detalhe": "não cancelou"}
    return envolta


def test_laco_batida_inicial_409_nao_executa(tmp_path):
    painel = PainelFalso([PACOTE], erro_batida=cliente.PainelErro(409))
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1), executar=lambda *a, **k: pytest.fail("não devia"))
    assert not _so(painel, "falha")


def test_laco_resultado_409_mantem_estado(tmp_path):
    pac = _pacote_falso(tmp_path)

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        executor.marcar_squad(casa, "peticao-inicial-jec", 7)  # o executor marca na entrega
        return {"status": "pronto", "squad": "peticao-inicial-jec", "run_id": "r",
                "arquivos": executor.arquivos_do_pacote(casa, "peticao-inicial-jec", pac),
                "campos": {"gate_status": "aprovado"}, "detalhe": ""}

    painel = PainelFalso([PACOTE], erro_resultado=cliente.PainelErro(409))
    estado = tmp_path / "e.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(1), executar=executar_falso)
    assert len(_so(painel, "resultado")) == 1
    assert not _so(painel, "falha")
    # M3: a entrada fica; a retomada no mesmo episódio aceita o pacote do run conhecido
    assert json.loads(estado.read_text())["7"]["squad"] == "peticao-inicial-jec"


def test_laco_resultado_sem_rede_mantem_estado_para_retomar(tmp_path):
    pac = _pacote_falso(tmp_path)

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        executor.marcar_squad(casa, "peticao-inicial-jec", 7)  # o executor marca na entrega
        return {"status": "pronto", "squad": "peticao-inicial-jec", "run_id": "r",
                "arquivos": executor.arquivos_do_pacote(casa, "peticao-inicial-jec", pac),
                "campos": {"gate_status": "aprovado"}, "detalhe": ""}

    painel = PainelFalso([PACOTE], erro_resultado=urllib.error.URLError("sem rede"))
    estado = tmp_path / "e.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(1), executar=executar_falso)
    assert len(_so(painel, "resultado")) == laco.TENTATIVAS_ENVIO
    entrada = json.loads(estado.read_text())["7"]
    assert entrada["squad"] == "peticao-inicial-jec"
    assert entrada["run_id"] == ""  # o run que o Lex disse não vale sem o ledger confirmar


@pytest.mark.parametrize("pacote,espera_falha", [
    ({"foo": 1}, False),
    ({"demanda": "x", "n": 1}, False),
    (["lista"], False),
    (dict(PACOTE, modo="outro"), True),
    (dict(PACOTE, publicacoes="texto"), True),
    (dict(PACOTE, intimacoes=[1, 2]), True),
])
def test_laco_pacote_malformado_nao_derruba(tmp_path, pacote, espera_falha):
    painel = PainelFalso([pacote])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(2), executar=lambda *a, **k: pytest.fail("não devia"))
    falhas = _so(painel, "falha")
    assert len(_so(painel, "proximo")) == 2
    if espera_falha:
        assert falhas == [("falha", 7, 1, "erro", "pacote inválido")]
    else:
        assert falhas == []


def test_laco_guarda_desde_e_pasta_e_reinicia_no_ajuste(tmp_path):
    vistos = []

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        vistos.append((desde, retomar))
        ao_progresso("peticao-inicial-jec", "Redação (6/13)")
        return {"status": "tempo", "squad": "peticao-inicial-jec", "run_id": "",
                "arquivos": {}, "campos": {}, "detalhe": ""}

    relogio = Relogio(passo=100)
    ajuste = dict(PACOTE, n=3, modo="ajuste", ajuste="Reduza", squad_anterior="s",
                  run_anterior="r")
    painel = PainelFalso([PACOTE, dict(PACOTE, n=2), ajuste])
    estado = tmp_path / "e.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(3), executar=executar_falso, relogio=relogio)
    (d1, r1), (d2, r2), (d3, r3) = vistos
    assert r1 is None and d1 == d2  # a 2ª execução da mesma demanda mantém o `desde`
    assert r2 is None  # sem run validado (squad sem caso.json): execução nova
    assert r3 is None and d3 > d2  # ajuste: `desde` desta execução, sem retomar
    entrada = json.loads(estado.read_text())["7"]
    assert entrada["pasta"] == str(tmp_path / PASTA_CASO)
    assert entrada["desde"] == d3


def test_laco_run_de_outro_caso_nao_vai_para_o_estado(tmp_path):
    _state_do_run(tmp_path, "peticao-inicial-jec", "2020-01-01T00:00:00Z")  # run antigo
    _squad_do_caso(tmp_path)

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        ao_progresso("peticao-inicial-jec", "Redação (6/13)")
        return {"status": "limite", "squad": "peticao-inicial-jec", "run_id": "",
                "arquivos": {}, "campos": {}, "detalhe": ""}

    estado = tmp_path / "e.json"
    laco.rodar(PainelFalso([PACOTE]), tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(1), executar=executar_falso)
    entrada = json.loads(estado.read_text())["7"]
    assert entrada.get("run_id", "") == "" and "squad" not in entrada  # nem adotado


# --- rodada de correção 2: só squad deste caso (caso.json), em todos os caminhos -------

def test_squad_do_caso(tmp_path):
    pasta = tmp_path / PASTA_CASO
    assert not executor.squad_do_caso(tmp_path, "s", pasta)  # sem caso.json
    _squad_do_caso(tmp_path, "s", "outra-pasta")
    assert not executor.squad_do_caso(tmp_path, "s", pasta)
    _squad_do_caso(tmp_path, "s")
    assert executor.squad_do_caso(tmp_path, "s", pasta)
    _squad_do_caso(tmp_path, "s", str(pasta))
    assert executor.squad_do_caso(tmp_path, "s", pasta)
    (tmp_path / "squads" / "s" / "caso.json").write_text("{quebrado")
    assert not executor.squad_do_caso(tmp_path, "s", pasta)
    for nome in ("", "../s", ".s"):
        assert not executor.squad_do_caso(tmp_path, nome, pasta)


def test_executar_recusa_squad_sem_caso_json(tmp_path):
    _pacote_falso(tmp_path, caso=None, gerado_em="2099-01-01T00:00:00Z")
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())))
    assert res["status"] == "falhou"
    assert res["detalhe"] == "o squad não é deste caso; pacote recusado"


def test_executar_atalho_do_run_conhecido_exige_o_mesmo_squad(tmp_path):
    _pacote_falso(tmp_path, gerado_em="2020-01-01T00:00:00Z")
    _squad_do_caso(tmp_path, "outro-squad")
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), desde=time.time(),
                       retomar={"squad": "outro-squad", "run_id": "2026-10-06-202440"})
    assert res["status"] == "falhou" and "pacote novo" in res["detalhe"]


def test_executar_recusa_manifesto_de_outro_run(tmp_path):
    pac = _pacote_falso(tmp_path, gerado_em="2099-01-01T00:00:00Z")
    (pac / "MANIFESTO.json").write_text(json.dumps({"run_id": "r-de-outro",
                                                    "gerado_em": "2099-01-01T00:00:00Z"}))
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())))
    assert res["status"] == "falhou" and "outro run" in res["detalhe"]


def test_executar_retomar_sem_caso_json_vira_execucao_nova(tmp_path):
    popen = PopenFalso(_stream("x"))
    _executar(tmp_path, popen, retomar={"squad": "legado", "run_id": "r9"})
    prompt = popen.chamadas[0][0][2]
    assert "retome" not in prompt and "--caso" in prompt


def test_montar_prompt_retomar_sem_run_vira_execucao_nova(tmp_path):
    texto = executor.montar_prompt(PACOTE, tmp_path / PASTA_CASO,
                                   {"squad": "peticao-inicial-jec", "run_id": ""})
    assert "retome" not in texto and "mais recente" not in texto
    assert "--criar --caso" in texto


AJUSTE = dict(PACOTE, n=2, modo="ajuste", ajuste="Reduza o valor",
              squad_anterior="peticao-inicial-jec", run_anterior="2026-10-06-202440")


def test_executar_ajuste_usa_o_run_anterior(tmp_path):
    _pacote_falso(tmp_path, gerado_em="2099-01-01T00:00:00Z")
    _pacote_falso(tmp_path, run="2026-10-07-090000", gerado_em="2099-01-02T00:00:00Z")
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), pacote=AJUSTE)
    assert res["status"] == "pronto" and res["run_id"] == "2026-10-06-202440"
    assert res["arquivos"]["peca_docx"].parent.name == "2026-10-06-202440"


@pytest.mark.parametrize("extra", [{"squad": "outro-squad"}, {"run_id": "2026-10-07-090000"}])
def test_executar_ajuste_divergente_falha(tmp_path, extra):
    _pacote_falso(tmp_path, gerado_em="2099-01-01T00:00:00Z")
    _pacote_falso(tmp_path, run="2026-10-07-090000", gerado_em="2099-01-02T00:00:00Z")
    _pacote_falso(tmp_path, squad="outro-squad", gerado_em="2099-01-01T00:00:00Z")
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim(**extra))), pacote=AJUSTE)
    assert res["status"] == "falhou" and res["detalhe"] == "ajuste fora da execução anterior"


def test_limpar_sobras_e_rodar_limpa_ao_iniciar(tmp_path):
    sobra = tmp_path / PASTA_CASO / ".autos-abc"
    sobra.mkdir(parents=True)
    (sobra / "x.pdf").write_bytes(b"1")
    (tmp_path / PASTA_CASO / "autos").mkdir()
    laco.rodar(PainelFalso([]), tmp_path, lambda: "tok", tmp_path / "e.json",
               dormir=lambda s: None, parar=_parar_depois(0))
    assert not sobra.exists() and (tmp_path / PASTA_CASO / "autos").exists()


class PainelComAutos(PainelFalso):
    def __init__(self, pacotes, erro_autos):
        super().__init__(pacotes)
        self.erro_autos = erro_autos

    def autos(self, demanda):
        super().autos(demanda)
        raise self.erro_autos


@pytest.mark.parametrize("status,falhas", [(404, [("falha", 7, 1, "erro", "autos indisponíveis")]),
                                           (409, [])])
def test_laco_autos_indisponiveis(tmp_path, status, falhas):
    painel = PainelComAutos([PACOTE], cliente.PainelErro(status))
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1), executar=lambda *a, **k: pytest.fail("não devia"))
    assert _so(painel, "falha") == falhas


def test_esperar_painel_nao_sai_com_configuracao_invalida(caplog):
    cfgs = iter([{"painel": "http://processos.exemplo", "casa": "/x"},
                 {"painel": "https://processos.exemplo", "casa": "/x"}])
    dormidas = []
    criados = []

    def criar(cfg):
        try:
            p = cliente.Painel(cfg["painel"], "k")
        except ValueError:
            return None
        criados.append(p)
        return p

    cfg, painel = principal.esperar_painel(ler_cfg=lambda: next(cfgs), criar=criar,
                                           dormir=dormidas.append)
    assert dormidas == [300] and painel is criados[0]
    assert cfg["painel"] == "https://processos.exemplo"
    assert principal.esperar_painel(ler_cfg=lambda: {}, criar=lambda c: None,
                                    dormir=lambda s: None, parar=_parar_depois(2)) is None


# --- ponta a ponta: duas reservas do mesmo cartão (laço + executor reais, claude falso) -

class ProcessoComVoltas(ProcessoFalso):
    def __init__(self, linhas, voltas):
        super().__init__(linhas, termina=True)
        self.voltas = voltas

    def wait(self, timeout=None):
        if self.voltas > 0 and not self.morto:
            self.voltas -= 1
            raise subprocess.TimeoutExpired("claude", timeout)
        return super().wait(timeout)


class ClaudeEmSequencia:
    """Uma execução falsa do `claude` por reserva: (linhas, ao_iniciar, voltas)."""

    def __init__(self, *execucoes):
        self.execucoes = list(execucoes)
        self.prompts = []

    def __call__(self, args, **kw):
        if args[0] != "claude":
            return ProcessoFalso([], termina=True)
        self.prompts.append(args[2])
        linhas, ao_iniciar, voltas = self.execucoes.pop(0)
        if ao_iniciar:
            ao_iniciar()
        return ProcessoComVoltas(linhas, voltas)


class RelogioDoLaco:
    """O executor vê o mesmo tempo do laço (tempo real + as horas puladas nas esperas) e,
    a cada leitura dentro da mesma volta, 61 s a mais (a batida sai a cada volta)."""

    def __init__(self, avanco):
        self.avanco, self.visto, self.passos = avanco, None, 0

    def __call__(self):
        if self.visto != self.avanco[0]:  # volta nova do laço: execução nova
            self.visto, self.passos = self.avanco[0], 0
        valor = time.time() + self.avanco[0] + 61 * self.passos
        self.passos += 1
        return valor


def _ponta_a_ponta(casa, pacotes, claude, avanco=None):
    painel = PainelFalso(pacotes)
    estado = casa / "estado.json"
    # cada espera do laço vale 1 h: a pausa do limite já passou na volta seguinte
    avanco = [0.0] if avanco is None else avanco
    executar = functools.partial(executor.executar, popen=claude, relogio=RelogioDoLaco(avanco))

    def dormir(s):
        avanco[0] += 3600

    laco.rodar(painel, casa, lambda: "tok", estado, dormir=dormir,
               parar=_parar_depois(len(pacotes)), executar=executar,
               relogio=lambda: time.time() + avanco[0],
               validar=lambda *a: pytest.fail("não devia validar o acesso"))
    return painel, ler_json_ou_vazio(estado)


def ler_json_ou_vazio(caminho):
    return json.loads(caminho.read_text()) if caminho.exists() else {}


def test_ponta_a_ponta_squad_legado_sem_caso_json_recusado_nas_duas(tmp_path):
    _pacote_falso(tmp_path, squad="legado", run="r-legado", caso=None,
                  gerado_em="2099-01-01T00:00:00Z")
    _state_do_run(tmp_path, "legado", "2099-01-01T00:00:00Z")
    fim = _fim(squad="legado", run_id="r-legado")
    claude = ClaudeEmSequencia((_stream(fim), None, 0), (_stream(fim), None, 0))
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, dict(PACOTE, n=2)], claude)
    assert not _so(painel, "resultado")
    assert [f[3:] for f in _so(painel, "falha")] == [
        ("erro", "o squad não é deste caso; pacote recusado")] * 2
    assert "squad" not in estado["7"]
    assert all("retome" not in p and "--caso" in p for p in claude.prompts)


def test_ponta_a_ponta_run_recusado_por_antigo_nao_vira_conhecido(tmp_path):
    _pacote_falso(tmp_path, squad="pi-c7", run="r-velho", gerado_em="2020-01-01T00:00:00Z")
    _state_do_run(tmp_path, "pi-c7", "2020-01-01T00:00:00Z")
    fim = _fim(squad="pi-c7", run_id="r-velho")
    claude = ClaudeEmSequencia((_stream(fim), None, 0), (_stream(fim), None, 0))
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, dict(PACOTE, n=2)], claude)
    assert not _so(painel, "resultado")
    assert [f[3:] for f in _so(painel, "falha")] == [
        ("erro", "o Lex não gerou pacote novo para este caso")] * 2
    assert "squad" not in estado["7"]  # squad de run antigo, sem marcador: nem adotado
    assert "retome" not in claude.prompts[1]


def test_ponta_a_ponta_run_validado_e_retomado_na_segunda(tmp_path):
    def comeca_run():
        _squad_do_caso(tmp_path, "pi-c7")
        (tmp_path / "squads" / "pi-c7" / "state.json").write_text(
            json.dumps({"step": {"current": 6, "total": 13, "label": "Redação"}}))
        (tmp_path / "squads" / "pi-c7" / "run-state.json").write_text(
            json.dumps({"runId": "r-novo", "startedAt": _agora_iso(+1)}))

    def termina_run():
        _pacote_falso(tmp_path, squad="pi-c7", run="r-novo", gerado_em=_agora_iso(+2))

    limite = [_linha(type="result", is_error=True, result="Claude AI usage limit reached|1")]
    claude = ClaudeEmSequencia((limite, comeca_run, 2),
                               (_stream(_fim(squad="pi-c7", run_id="r-novo")), termina_run, 0))
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, dict(PACOTE, n=2)], claude)
    assert [f[3] for f in _so(painel, "falha")] == ["limite"]
    assert ("batida", 7, 1, "pi-c7", "Redação (6/13)") in painel.chamadas
    assert "retome o run r-novo do squad pi-c7" in claude.prompts[1]
    (res,) = _so(painel, "resultado")
    assert res[1:3] == (7, 2) and res[3]["run_id"] == "r-novo"
    assert res[4]["peca_docx"].parent.name == "r-novo"
    assert "7" not in estado


def test_ponta_a_ponta_ajuste_divergente_falha(tmp_path):
    for squad, run in (("pi-c7", "r1"), ("pi-c7", "r2"), ("outro-c7", "r9")):
        _pacote_falso(tmp_path, squad=squad, run=run, gerado_em="2099-01-01T00:00:00Z")
    ajuste = dict(PACOTE, modo="ajuste", ajuste="Reduza", squad_anterior="pi-c7",
                  run_anterior="r1")
    claude = ClaudeEmSequencia((_stream(_fim(squad="outro-c7", run_id="r9")), None, 0),
                               (_stream(_fim(squad="pi-c7", run_id="r2")), None, 0),
                               (_stream(_fim(squad="pi-c7")), None, 0))
    painel, _ = _ponta_a_ponta(tmp_path, [dict(ajuste, n=2), dict(ajuste, n=3),
                                          dict(ajuste, n=4)], claude)
    assert [f[1:4] + (f[4],) for f in _so(painel, "falha")] == [
        (7, 2, "erro", "ajuste fora da execução anterior"),
        (7, 3, "erro", "ajuste fora da execução anterior")]
    (res,) = _so(painel, "resultado")  # sem run_id no JSON: vale o run_anterior, não o mais novo
    assert res[2] == 4 and res[3]["run_id"] == "r1"


# --- onda final B ----------------------------------------------------------------------

class RespostaQueCai(Resposta):
    def __init__(self, exc):
        super().__init__(200)
        self.exc = exc

    def read(self):
        raise self.exc


@pytest.mark.parametrize("exc", [
    http.client.IncompleteRead(b"meio"),
    http.client.BadStatusLine("lixo"),
    http.client.RemoteDisconnected("caiu"),
])
def test_painel_conexao_interrompida_vira_oserror(exc):
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(RespostaQueCai(exc)))
    with pytest.raises(OSError) as erro:
        p.proximo()
    assert isinstance(erro.value, cliente.ConexaoInterrompida)
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(exc))
    with pytest.raises(cliente.ConexaoInterrompida):
        p.batida(7, 1, "", "x")


def test_laco_conexao_interrompida_nao_derruba(tmp_path):
    p = cliente.Painel("https://painel.exemplo", "k",
                       abrir=AbrirFalso(*[http.client.IncompleteRead(b"")] * 6))
    dormidas = []
    laco.rodar(p, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=dormidas.append,
               parar=_parar_depois(2), executar=lambda *a, **k: pytest.fail("não devia"))
    assert dormidas == [60, 60]


def test_painel_401_marca_chave_recusada():
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(_erro_http(409)))
    with pytest.raises(cliente.PainelErro):
        p.proximo()
    assert p.chave_recusada is False
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(_erro_http(401)))
    with pytest.raises(cliente.PainelErro):
        p.proximo()
    assert p.chave_recusada is True


def test_painel_contato_e_squad_nome():
    abrir = AbrirFalso(Resposta(200, b'{"ok":true}'), Resposta(200, b'{"ok":true}'),
                       _erro_http(404, b""), Resposta(200, b'{"ok":true}'))
    p = cliente.Painel("https://painel.exemplo", "k", abrir=abrir)
    p.contato(True)
    p.contato(False)
    p.contato(True)  # servidor antigo: 404 ignorado
    p.batida(7, 1, "replica", "Pesquisa", squad_nome="Réplica à contestação")
    c1, c0, _, b = abrir.pedidos
    assert c1.full_url == "https://painel.exemplo/ponte/contato"
    assert c1.get_header("Authorization") == "Bearer k"
    assert json.loads(c1.data) == {"claude_ok": "1"}
    assert json.loads(c0.data) == {"claude_ok": "0"}
    assert json.loads(b.data)["squad_nome"] == "Réplica à contestação"
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(_erro_http(500)))
    with pytest.raises(cliente.PainelErro):
        p.contato(True)


def test_rodar_relê_a_chave_depois_de_401(tmp_path):
    velho = AbrirFalso(_erro_http(401), _erro_http(401))  # pastas e proximo
    novo = AbrirFalso(Resposta(200, b'{"pendentes": []}'), Resposta(204),
                      Resposta(200, b'{"ok":true}'))  # pastas, proximo, contato
    p_velho = cliente.Painel("https://painel.exemplo", "chave-velha", abrir=velho)
    recriados = []

    def recriar():
        recriados.append(1)
        return cliente.Painel("https://painel.exemplo", "chave-nova", abrir=novo)

    laco.rodar(p_velho, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(2), executar=lambda *a, **k: pytest.fail("não devia"),
               recriar=recriar)
    assert len(recriados) == 1
    assert [r.get_header("Authorization") for r in velho.pedidos] == ["Bearer chave-velha"] * 2
    assert novo.pedidos[0].full_url.endswith("/ponte/pastas")
    assert novo.pedidos[1].full_url.endswith("/ponte/proximo")
    assert all(r.get_header("Authorization") == "Bearer chave-nova" for r in novo.pedidos)


def test_rodar_sem_chave_nova_continua_tentando(tmp_path):
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(*[_erro_http(401)] * 6))
    chamadas = []
    laco.rodar(p, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(3), recriar=lambda: chamadas.append(1))
    assert len(chamadas) == 2  # antes da 2ª e da 3ª volta; None mantém o painel


def test_main_rodar_recria_o_painel_lendo_as_chaves(monkeypatch, tmp_path):
    lidas = iter(["chave-velha" + "x" * 40, "chave-nova" + "y" * 40])
    monkeypatch.setattr(principal.chaves, "ler", lambda servico: next(lidas))
    capturado = {}

    def rodar_falso(painel, casa, token_fn, estado, recriar=None, reler_chave=None):
        capturado["painel"], capturado["recriar"] = painel, recriar

    monkeypatch.setattr(principal.laco, "rodar", rodar_falso)
    monkeypatch.setattr(principal, "ler_configuracao",
                        lambda: {"painel": "https://painel.exemplo", "casa": str(tmp_path)})
    assert principal.main(["rodar"]) == 0
    assert capturado["painel"].url == "https://painel.exemplo"
    assert capturado["painel"]._chave.startswith("chave-velha")
    assert capturado["recriar"]()._chave.startswith("chave-nova")


def test_guardar_chave_reinicia_o_servico(monkeypatch):
    monkeypatch.setattr(principal.chaves, "guardar", lambda s, v: None)
    reinicios = []
    boa = "Ab3_-" * 9
    assert principal.guardar_chave(colar=lambda: boa, copiar=lambda t: None,
                                   reiniciar=lambda: reinicios.append(1) or False) == 0
    assert reinicios == [1]
    assert principal.guardar_chave(colar=lambda: "curta", copiar=lambda t: None,
                                   reiniciar=lambda: pytest.fail("não devia")) == 1


def test_iniciar_servico_usa_kickstart_sem_k_e_falha_em_silencio():
    vistos = []

    def rodar(args, **kw):
        vistos.append(args)
        return subprocess.CompletedProcess(args, 113, "", "Could not find service")

    assert principal._iniciar_servico(rodar) is False
    assert vistos == [["launchctl", "kickstart",
                       f"gui/{os.getuid()}/br.com.despertaia.controladoria-ponte"]]

    def quebra(args, **kw):
        raise FileNotFoundError("launchctl")

    assert principal._iniciar_servico(quebra) is False
    assert principal._iniciar_servico(
        lambda a, **k: subprocess.CompletedProcess(a, 0, "", "")) is True


# D4: autos/ só muda quando o zip muda, e nunca perde o que o motor criou ali

def test_preparar_pasta_mesmo_zip_nao_mexe_nos_autos(tmp_path):
    z = _zip({"a.pdf": b"1", "sub/b.pdf": b"22"})
    pasta = executor.preparar_pasta(tmp_path, PACOTE, z)
    (pasta / "autos" / "_index.yaml").write_text("indice: 1")
    a = pasta / "autos" / "a.pdf"
    os.utime(a, (1_000, 1_000))
    executor.preparar_pasta(tmp_path, PACOTE, z)
    assert a.stat().st_mtime == 1_000  # não foi extraído de novo
    assert (pasta / "autos" / "_index.yaml").read_text() == "indice: 1"
    controle = json.loads((pasta / executor.CONTROLE_AUTOS).read_text())
    assert controle["arquivos"] == [["a.pdf", 1], ["sub/b.pdf", 2]]
    assert [p.name for p in pasta.iterdir() if p.name.startswith(".autos")] == []


def test_preparar_pasta_mesmo_zip_mas_arquivo_mexido_fica_o_do_advogado(tmp_path):
    z = _zip({"a.pdf": b"1"})
    pasta = executor.preparar_pasta(tmp_path, PACOTE, z)
    (pasta / "autos" / "a.pdf").write_bytes(b"mexido pelo advogado")
    executor.preparar_pasta(tmp_path, PACOTE, z)
    assert (pasta / "autos" / "a.pdf").read_bytes() == b"mexido pelo advogado"
    assert (pasta / "autos" / "a (PJe).pdf").read_bytes() == b"1"
    controle = json.loads((pasta / executor.CONTROLE_AUTOS).read_text())
    assert controle["arquivos"] == [["a (PJe).pdf", 1]]


def test_preparar_pasta_mesmo_zip_mas_arquivo_apagado_extrai_de_novo(tmp_path):
    z = _zip({"a.pdf": b"1"})
    pasta = executor.preparar_pasta(tmp_path, PACOTE, z)
    (pasta / "autos" / "a.pdf").unlink()
    executor.preparar_pasta(tmp_path, PACOTE, z)
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["a.pdf"]
    assert (pasta / "autos" / "a.pdf").read_bytes() == b"1"


def test_preparar_pasta_zip_novo_preserva_arquivos_do_motor(tmp_path):
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"velho.pdf": b"1", "d/x.pdf": b"2"}))
    (pasta / "autos" / "_index.yaml").write_text("indice: 1")
    (pasta / "autos" / "d" / "notas-do-motor.md").write_text("n")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"novo.pdf": b"3", "velho.pdf": b"11"}))
    autos = pasta / "autos"
    assert sorted(str(p.relative_to(autos)) for p in autos.rglob("*") if p.is_file()) == [
        "_index.yaml", "d/notas-do-motor.md", "novo.pdf", "velho.pdf"]
    assert (autos / "velho.pdf").read_bytes() == b"11"
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"novo.pdf": b"3"}))
    assert not (autos / "velho.pdf").exists() and (autos / "_index.yaml").exists()


def test_preparar_pasta_autos_legados_sem_controle_nao_perdem_nada(tmp_path):
    pasta = tmp_path / PASTA_CASO / "autos"
    pasta.mkdir(parents=True)
    (pasta / "_index.yaml").write_text("i")
    (pasta / "antigo.pdf").write_bytes(b"0")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"novo.pdf": b"1"}))
    assert sorted(p.name for p in pasta.iterdir()) == ["_index.yaml", "antigo.pdf", "novo.pdf"]


def test_preparar_pasta_zip_recusado_mantem_autos_e_controle(tmp_path, monkeypatch):
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"velho.pdf": b"1"}))
    antes = (pasta / executor.CONTROLE_AUTOS).read_text()
    monkeypatch.setattr(executor, "LIMITE_AUTOS_BYTES", 10)
    with pytest.raises(ValueError):
        executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"x" * 20}))
    assert (pasta / executor.CONTROLE_AUTOS).read_text() == antes
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["velho.pdf"]


# D5: plist com ThrottleInterval e bootstrap repetido logo após o bootout

def _instalador(tmp_path, falhas_bootstrap):
    import plistlib
    import shutil
    import stat
    repo = tmp_path / "repo"
    (repo / "ponte_mac").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    py = repo / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    shutil.copy(Path(__file__).resolve().parent.parent / "ponte_mac" / "instalar.sh",
                repo / "ponte_mac" / "instalar.sh")
    home = tmp_path / "home"
    home.mkdir()
    chamadas = tmp_path / "chamadas.log"
    contador = tmp_path / "contador"
    contador.write_text("0")
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        f'#!/bin/sh\necho "$1" >> "{chamadas}"\n'
        f'if [ "$1" = "bootstrap" ]; then n=$(cat "{contador}"); n=$((n+1)); '
        f'echo $n > "{contador}"; [ $n -le {falhas_bootstrap} ] && exit 5; fi\nexit 0\n')
    security = tmp_path / "security"
    security.write_text("#!/bin/sh\nexit 44\n")
    for f in (launchctl, security):
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    destino = tmp_path / "LaunchAgents"
    r = subprocess.run(["bash", str(repo / "ponte_mac" / "instalar.sh")], capture_output=True,
                       text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "HOME": str(home),
                            "DESTINO": str(destino), "LAUNCHCTL": str(launchctl),
                            "SECURITY": str(security), "ESPERA_BOOTSTRAP": "0"})
    plist = destino / "br.com.despertaia.controladoria-ponte.plist"
    dados = plistlib.loads(plist.read_bytes()) if plist.exists() else {}
    return r, chamadas.read_text().split(), dados


def test_instalar_plist_tem_throttle_interval(tmp_path):
    r, chamadas, plist = _instalador(tmp_path, 0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert plist["ThrottleInterval"] == 60
    assert chamadas == ["bootout", "bootstrap"]


def test_instalar_repete_bootstrap_ate_tres_vezes(tmp_path):
    r, chamadas, _ = _instalador(tmp_path, 2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert chamadas == ["bootout", "bootstrap", "bootstrap", "bootstrap"]


def test_instalar_desiste_depois_de_tres_bootstraps(tmp_path):
    r, chamadas, _ = _instalador(tmp_path, 3)
    assert r.returncode != 0
    assert chamadas.count("bootstrap") == 3
    assert "não aceitou o serviço" in r.stdout


# I4: retomar só dentro do mesmo envio ao Lex (episódio)

def _guarda_retomar(vistos, status="falhou"):
    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        vistos.append((desde, retomar))
        _squad_do_caso(casa)
        _state_do_run(casa, "peticao-inicial-jec", _agora_iso(+1))
        ao_progresso("peticao-inicial-jec", "Redação (6/13)")
        return {"status": status, "squad": "peticao-inicial-jec", "run_id": "",
                "arquivos": {}, "campos": {}, "detalhe": ""}
    return executar_falso


@pytest.mark.parametrize("segundo,retoma", [
    (dict(PACOTE, n=2), True),                        # mesmo episódio
    (dict(PACOTE, n=2, episodio="502"), False),       # envio novo ao Lex
    ({k: v for k, v in dict(PACOTE, n=2).items() if k != "episodio"}, False),  # servidor antigo
    (dict(PACOTE, n=2, episodio=None), False),
])
def test_laco_retoma_so_no_mesmo_episodio(tmp_path, segundo, retoma):
    vistos = []
    estado = tmp_path / "e.json"
    laco.rodar(PainelFalso([PACOTE, segundo]), tmp_path, lambda: "tok", estado,
               dormir=lambda s: None, parar=_parar_depois(2), executar=_guarda_retomar(vistos),
               relogio=Relogio(passo=10))
    (d1, r1), (d2, r2) = vistos
    assert r1 is None
    if retoma:
        assert r2 == {"squad": "peticao-inicial-jec", "run_id": "r-novo"} and d2 == d1
    else:
        assert r2 is None and d2 > d1  # execução nova, com o `desde` dela
    entrada = json.loads(estado.read_text())["7"]
    assert entrada["episodio"] == str(segundo.get("episodio") or "")


def test_ponta_a_ponta_falhou_e_reenvio_com_episodio_novo_cria_squad_novo(tmp_path):
    def comeca_run():
        _squad_do_caso(tmp_path, "pi-c7")
        (tmp_path / "squads" / "pi-c7" / "state.json").write_text(
            json.dumps({"step": {"current": 6, "total": 13, "label": "Redação"}}))
        (tmp_path / "squads" / "pi-c7" / "run-state.json").write_text(
            json.dumps({"runId": "r-novo", "startedAt": _agora_iso(+1)}))

    quebrou = _stream("O Lex parou no meio.")
    claude = ClaudeEmSequencia((quebrou, comeca_run, 2), (quebrou, None, 0), (quebrou, None, 0))
    painel, estado = _ponta_a_ponta(
        tmp_path, [PACOTE, dict(PACOTE, n=2), dict(PACOTE, n=3, episodio=777)], claude)
    assert [f[3] for f in _so(painel, "falha")] == ["erro"] * 3
    assert "retome o run r-novo do squad pi-c7" in claude.prompts[1]  # mesmo envio: retoma
    assert "retome" not in claude.prompts[2] and "--criar --caso" in claude.prompts[2]
    assert estado["7"]["episodio"] == "777" and "squad" not in estado["7"]


# M3: 409 no envio não apaga a entrada; a retomada reenvia o pacote do run conhecido

def test_ponta_a_ponta_resultado_409_e_retomada_reenvia_o_mesmo_run(tmp_path):
    def comeca_run():
        _squad_do_caso(tmp_path, "pi-c7")
        (tmp_path / "squads" / "pi-c7" / "state.json").write_text(
            json.dumps({"step": {"current": 13, "total": 13, "label": "Entrega"}}))
        (tmp_path / "squads" / "pi-c7" / "run-state.json").write_text(
            json.dumps({"runId": "r-novo", "startedAt": _agora_iso(+1)}))
        _pacote_falso(tmp_path, squad="pi-c7", run="r-novo", gerado_em=_agora_iso(+2))

    fim = _stream(_fim(squad="pi-c7", run_id="r-novo"))
    claude = ClaudeEmSequencia((fim, comeca_run, 2), (fim, None, 0))

    class Painel409UmaVez(PainelFalso):
        def resultado(self, demanda, n, campos, arquivos):
            super().resultado(demanda, n, campos, arquivos)
            if len(_so(self, "resultado")) == 1:
                raise cliente.PainelErro(409)

    painel = Painel409UmaVez([PACOTE, dict(PACOTE, n=2)])
    estado = tmp_path / "estado.json"
    executar = functools.partial(executor.executar, popen=claude, relogio=Relogio(passo=61))
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(2), executar=executar)
    r1, r2 = _so(painel, "resultado")
    assert "retome o run r-novo do squad pi-c7" in claude.prompts[1]
    assert r2[2] == 2 and r2[3]["run_id"] == "r-novo"
    assert "7" not in ler_json_ou_vazio(estado)


# V4: nome do squad (squad.yaml) na batida e no resultado

@pytest.mark.parametrize("linha,nome", [
    ('name: "Réplica à contestação"', "Réplica à contestação"),
    ("name: 'Embargos de declaração'", "Embargos de declaração"),
    ("name: Petição inicial no JEC  # comentário", "Petição inicial no JEC"),
    ("name:   ", ""),
    ('name: "' + "x" * 200 + '"', "x" * 120),
    ('name: "Com\ttab\x07 e controle"', "Com tab e controle"),
])
def test_nome_do_squad(tmp_path, linha, nome):
    (tmp_path / "squads" / "s").mkdir(parents=True)
    (tmp_path / "squads" / "s" / "squad.yaml").write_text(
        f"# cabeçalho\nid: s\n{linha}\nagents:\n  - id: a\n    name: \"Rita Resumo\"\n",
        encoding="utf-8")
    assert executor.nome_do_squad(tmp_path, "s") == nome


def test_nome_do_squad_ausente_ou_invalido(tmp_path):
    assert executor.nome_do_squad(tmp_path, "nao-existe") == ""
    (tmp_path / "squads" / "s").mkdir(parents=True)
    (tmp_path / "squads" / "s" / "squad.yaml").write_text("agents:\n  - name: \"Rita\"\n")
    assert executor.nome_do_squad(tmp_path, "s") == ""  # só o name do topo vale
    for ruim in ("", "../s", ".s", "a/b"):
        assert executor.nome_do_squad(tmp_path, ruim) == ""


def test_laco_manda_squad_nome_na_batida_e_no_resultado(tmp_path):
    pac = _pacote_falso(tmp_path)
    (tmp_path / "squads" / "peticao-inicial-jec" / "squad.yaml").write_text(
        'name: "Petição inicial no Juizado"\n', encoding="utf-8")

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        ao_progresso("", "autos baixados; iniciando o Lex")
        ao_progresso("peticao-inicial-jec", "Pesquisa (3/13)")
        return {"status": "pronto", "squad": "peticao-inicial-jec", "run_id": "r",
                "arquivos": executor.arquivos_do_pacote(casa, "peticao-inicial-jec", pac),
                "campos": {"squad": "peticao-inicial-jec", "gate_status": "aprovado"},
                "detalhe": ""}

    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1), executar=executar_falso)
    assert painel.nomes == ["", "", "Petição inicial no Juizado"]
    (res,) = _so(painel, "resultado")
    assert res[3]["squad_nome"] == "Petição inicial no Juizado"


def test_painel_resultado_leva_squad_nome_no_multipart(tmp_path):
    docx = tmp_path / "p.docx"
    docx.write_bytes(b"D")
    abrir = AbrirFalso(Resposta(200, b'{"ok":true}'))
    cliente.Painel("https://painel.exemplo", "k", abrir=abrir).resultado(
        7, 1, {"squad": "s", "squad_nome": "Réplica à contestação"}, {"peca_docx": docx})
    assert 'name="squad_nome"\r\n\r\nRéplica à contestação\r\n'.encode() in abrir.pedidos[0].data


# M7: batida logo depois dos autos, antes do claude

def test_executar_avisa_autos_prontos_antes_do_claude(tmp_path):
    ordem = []

    class PopenQueAnota(PopenFalso):
        def __call__(self, args, **kw):
            ordem.append(("popen", args[0]))
            return super().__call__(args, **kw)

    executor.executar(tmp_path, PACOTE, _zip({"a.pdf": b"1"}), "t",
                      lambda squad, etapa: ordem.append(("aviso", squad, etapa)),
                      popen=PopenQueAnota(_stream("x")), relogio=Relogio())
    assert ordem[0] == ("aviso", "", "autos baixados; iniciando o Lex")
    assert ordem[1] == ("popen", "claude")
    assert (tmp_path / PASTA_CASO / "autos" / "a.pdf").exists()


def test_executar_cancelado_antes_de_comecar_nao_abre_o_claude(tmp_path):
    def cancela(squad, etapa):
        raise executor.Cancelado("o painel respondeu 409")

    popen = PopenFalso(_stream("x"))
    res, _ = _executar(tmp_path, popen, ao_progresso=cancela)
    assert res["status"] == "cancelado" and popen.chamadas == []


def test_laco_batida_depois_dos_autos(tmp_path):
    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1),
               executar=functools.partial(executor.executar, popen=PopenFalso(_stream("x")),
                                          relogio=Relogio()))
    batidas = [c[4] for c in _so(painel, "batida")]
    assert batidas[:2] == ["preparando o caso", "caso preparado; iniciando o Lex"]


# M4: limite do plano (pausa global) e acesso do Claude recusado (sem cascata)

class Tempo:
    """Relógio do laço que só anda quando o laço dorme."""

    def __init__(self, inicio=1_000_000.0):
        self.t = inicio

    def __call__(self):
        return self.t

    def dormir(self, s):
        self.t += s


def _resultado_falso(status, **extra):
    return {"status": status, "squad": "", "run_id": "", "arquivos": {}, "campos": {},
            "detalhe": "", "ate": None, **extra}


def test_parece_erro_de_acesso():
    for texto in ("Invalid API key · Please run /login",
                  'API Error: 401 {"type":"error","error":{"type":"authentication_error"}}',
                  "OAuth token has expired. Please obtain a new token",
                  "Failed to authenticate. API Error: 401"):
        assert executor.parece_erro_de_acesso(texto), texto
    for texto in ("art. 401 do CPC", "Peça pronta.", "Claude AI usage limit reached|1760000000"):
        assert not executor.parece_erro_de_acesso(texto), texto


def test_hora_do_limite():
    from datetime import datetime
    assert executor.hora_do_limite("Claude AI usage limit reached|1760000000") == 1760000000.0
    agora = datetime(2026, 10, 7, 13, 0).timestamp()
    assert executor.hora_do_limite("5-hour limit reached ∙ resets 3pm", agora) == \
        datetime(2026, 10, 7, 15, 0).timestamp()
    assert executor.hora_do_limite("Your limit will reset at 9:30am (America/Cuiaba)", agora) == \
        datetime(2026, 10, 8, 9, 30).timestamp()  # já passou hoje: amanhã
    assert executor.hora_do_limite("usage limit reached", agora) is None
    assert executor.hora_do_limite("resets 13pm", agora) is None


def test_executar_erro_de_acesso_vira_status_acesso(tmp_path):
    linhas = [_linha(type="result", subtype="error", is_error=True,
                     result="Invalid API key · Please run /login")]
    res, _ = _executar(tmp_path, PopenFalso(linhas, rc=1))
    assert res["status"] == "acesso" and res["detalhe"] == executor.DETALHE_ACESSO
    res, _ = _executar(tmp_path, PopenFalso(["OAuth token has expired"], rc=1))
    assert res["status"] == "acesso"


def test_executar_limite_traz_a_hora_e_o_texto_novo(tmp_path):
    linhas = [_linha(type="result", is_error=True, result="Claude AI usage limit reached|1760000000")]
    res, _ = _executar(tmp_path, PopenFalso(linhas, rc=1))
    assert res["status"] == "limite" and res["ate"] == 1760000000.0
    assert res["detalhe"] == "pausado: limite do plano"


def test_validar_acesso():
    def rodar(stdout="", stderr="", rc=0, exc=None):
        vistos = []

        def f(args, **kw):
            vistos.append((args, kw))
            if exc:
                raise exc
            return subprocess.CompletedProcess(args, rc, stdout, stderr)
        return f, vistos

    ok, vistos = rodar(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
    assert executor.validar_acesso("tok", Path("/tmp"), ok) is True
    args, kw = vistos[0]
    assert args[:2] == ["claude", "-p"] and kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert executor.validar_acesso("t", Path("/tmp"), rodar(
        "", "Invalid API key · Please run /login", rc=1)[0]) is False
    assert executor.validar_acesso("t", Path("/tmp"), rodar("", "sem rede", rc=1)[0]) is None
    assert executor.validar_acesso("t", Path("/tmp"), rodar(
        exc=subprocess.TimeoutExpired("claude", 300))[0]) is None
    assert executor.validar_acesso("t", Path("/tmp"), rodar(exc=FileNotFoundError("claude"))[0]) is None


def _executar_registrando(tempo, resultados, vezes):
    def executar(*a, **k):
        vezes.append(tempo.t)
        return next(resultados)
    return executar


def test_laco_limite_pausa_tudo_por_30_min(tmp_path):
    tempo, vezes = Tempo(), []
    inicio = tempo.t
    resultados = iter([_resultado_falso("limite"), _resultado_falso("falhou")])
    painel = PainelFalso([PACOTE, dict(PACOTE, n=2)])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=tempo.dormir,
               parar=_parar_depois(32), executar=_executar_registrando(tempo, resultados, vezes),
               relogio=tempo)
    assert vezes == [inicio, inicio + 1800]  # nada reservado durante a pausa
    proximos = [i for i, c in enumerate(painel.chamadas) if c[0] == "proximo"]
    parado = [c for c in painel.chamadas[proximos[0]:proximos[1]] if c[0] == "contato"]
    assert len(parado) == 29 and all(c[1] is True for c in parado)
    assert [f[3:] for f in _so(painel, "falha")] == [("limite", "pausado: limite do plano"),
                                                     ("erro", "a execução do Lex falhou")]


def test_laco_limite_respeita_a_hora_informada(tmp_path):
    tempo, vezes = Tempo(), []
    inicio = tempo.t
    resultados = iter([_resultado_falso("limite", ate=inicio + 5 * 60),
                       _resultado_falso("falhou")])
    laco.rodar(PainelFalso([PACOTE, dict(PACOTE, n=2)]), tmp_path, lambda: "tok",
               tmp_path / "e.json", dormir=tempo.dormir, parar=_parar_depois(8),
               executar=_executar_registrando(tempo, resultados, vezes), relogio=tempo)
    assert vezes == [inicio, inicio + 300]


def test_laco_acesso_recusado_para_de_reservar_e_revalida(tmp_path):
    tempo, vezes = Tempo(), []
    inicio = tempo.t
    validacoes = []
    respostas = iter([False, True])

    def validar(token, casa):
        validacoes.append(tempo.t)
        return next(respostas)

    resultados = iter([_resultado_falso("acesso", detalhe=executor.DETALHE_ACESSO),
                       _resultado_falso("falhou")])
    painel = PainelFalso([PACOTE, dict(PACOTE, demanda=8, n=1)])
    estado = tmp_path / "e.json"
    laco.rodar(painel, tmp_path, lambda: "tok", estado, dormir=tempo.dormir,
               parar=_parar_depois(23), executar=_executar_registrando(tempo, resultados, vezes),
               relogio=tempo, validar=validar)
    assert validacoes == [inicio + 600, inicio + 1200]  # a cada 10 min
    assert vezes == [inicio, inicio + 1200]  # nada reservado enquanto recusado
    # um cartão volta com erro uma vez; nada de falha em cascata
    assert [f[1:] for f in _so(painel, "falha")] == [(7, 1, "erro", executor.DETALHE_ACESSO),
                                                     (8, 1, "erro", "a execução do Lex falhou")]
    contatos = [c[1] for c in _so(painel, "contato")]
    assert contatos[:20] == [False] * 20
    assert "7" in json.loads(estado.read_text())  # a retomada do cartão continua possível


def test_laco_acesso_recusado_revalida_logo_quando_o_token_muda(tmp_path):
    tempo = Tempo()
    tokens = iter(["velho", "velho", "novo", "novo"])
    validacoes = []
    painel = PainelFalso([PACOTE, dict(PACOTE, n=2)])
    resultados = iter([_resultado_falso("acesso"), _resultado_falso("falhou")])
    laco.rodar(painel, tmp_path, lambda: next(tokens), tmp_path / "e.json",
               dormir=tempo.dormir, parar=_parar_depois(3),
               executar=lambda *a, **k: next(resultados), relogio=tempo,
               validar=lambda token, casa: validacoes.append(token) or True)
    assert validacoes == ["novo"]
    assert len(_so(painel, "proximo")) == 2


# M5: contato quando ocioso

def test_laco_ocioso_manda_contato_com_claude_ok(tmp_path):
    painel = PainelFalso([])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(2))
    assert _so(painel, "contato") == [("contato", True)] * 2


def test_laco_contato_sem_rede_nao_derruba(tmp_path):
    class SemContato(PainelFalso):
        def contato(self, claude_ok):
            super().contato(claude_ok)
            raise urllib.error.URLError("sem rede")

    painel = SemContato([])
    laco.rodar(painel, tmp_path, lambda: None, tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(2))
    assert len(_so(painel, "contato")) == 2


# M12: o painel só recebe frases fixas; o cru fica no registro local

@pytest.mark.parametrize("status,detalhe,frase", [
    ("sem_tipo", "", "o Lex não teve segurança sobre o tipo de peça"),
    ("sem_tipo", "Li squads/x/caso.json: a publicação não deixa claro o recurso.",
     "Li a publicação não deixa claro o recurso."),
    ("falhou", "pacote da peça não encontrado em squads/x/output/pacote",
     "pacote da peça não encontrado"),
    ("falhou", "O Lex disse algo livre sobre o caso de Maria Exemplo",
     "a execução do Lex falhou"),
    ("falhou", "o squad não é deste caso; pacote recusado",
     "o squad não é deste caso; pacote recusado"),
    ("limite", "Claude AI usage limit reached|1", "pausado: limite do plano"),
    ("tempo", "o Lex passou de 3 h e foi interrompido", "passou de 3 h"),
])
def test_laco_falha_vai_com_frase_fixa(tmp_path, caplog, status, detalhe, frase):
    caplog.set_level("INFO")
    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1),
               executar=lambda *a, **k: _resultado_falso(status, detalhe=detalhe))
    (f,) = _so(painel, "falha")
    assert f[4] == frase
    assert executor.limpar_detalhe(detalhe) in caplog.text


def test_laco_erro_no_mac_vai_sem_o_texto_da_excecao(tmp_path):
    def explode(*a, **k):
        raise RuntimeError("quebrou lendo o caso de Maria Exemplo")

    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1), executar=explode)
    (f,) = _so(painel, "falha")
    assert f[3:] == ("erro", "erro no Mac")


# --- onda final, correção 1 ------------------------------------------------------------

def test_sigterm_durante_a_execucao_encerra_o_filho_e_sai(tmp_path, caplog):
    """SIGTERM no serviço (launchctl) com o Lex rodando: o grupo do `claude` é encerrado
    antes de sair, em vez de ficar órfão."""
    import signal
    import sys
    import threading

    filhos = []

    def popen(args, **kw):
        if args[0] == "claude":  # um "claude" de verdade, que nunca terminaria sozinho
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    start_new_session=True)
            filhos.append(proc)
            threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
            return proc
        return ProcessoFalso([], termina=True)

    def executar(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        return executor.executar(casa, pacote, autos_zip, token, ao_progresso,
                                 retomar=retomar, popen=popen, desde=desde)

    anterior = signal.getsignal(signal.SIGTERM)
    painel = PainelFalso([PACOTE])
    try:
        with pytest.raises(SystemExit):
            laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json",
                       dormir=lambda s: None, parar=_parar_depois(1), executar=executar)
        (filho,) = filhos
        assert filho.poll() is not None  # o "claude" foi encerrado
        assert "execução do Lex em andamento foi encerrada" in caplog.text  # pelo tratador
        assert not executor._EM_ANDAMENTO
        assert signal.getsignal(signal.SIGTERM) is anterior  # tratador restaurado
        assert not _so(painel, "resultado")
    finally:
        for f in filhos:
            if f.poll() is None:
                f.kill()
                f.wait()


def test_parece_erro_de_acesso_ignora_palavras_soltas():
    for texto in ("A autenticação OAuth do tribunal estava fora do ar",
                  "authentication do PJe falhou ao consultar o processo",
                  "o portal pede /login para ver os autos"):
        assert not executor.parece_erro_de_acesso(texto), texto
    assert executor.parece_erro_de_acesso("Error: authentication_error (token revogado)")


def test_laco_acesso_volta_e_avisa_o_painel_logo(tmp_path):
    tempo = Tempo()
    painel = PainelFalso([])
    situacao = {"acesso_recusado": {"token": "outro", "revalidar_em": 0}}
    laco.uma_volta(painel, tmp_path, lambda: "tok", tmp_path / "e.json", relogio=tempo,
                   situacao=situacao, validar=lambda token, casa: True)
    assert not situacao.get("acesso_recusado")
    # o contato com claude_ok=1 vem logo depois da revalidação, antes da consulta
    assert painel.chamadas[:2] == [("contato", True), ("proximo",)]


class PainelComChave(PainelFalso):
    """Batida com 401 uma vez (a chave foi trocada no painel); depois aceita a nova."""

    def __init__(self, pacotes):
        super().__init__(pacotes)
        self.chave, self.chave_recusada = "velha", False

    def trocar_chave(self, chave):
        self.chave, self.chave_recusada = chave, False

    def batida(self, demanda, n, squad, etapa, squad_nome=""):
        self.chamadas.append(("batida", demanda, n, squad, etapa, self.chave))
        if self.chave == "velha" and squad:
            self.chave_recusada = True
            raise cliente.PainelErro(401, "chave inválida")

    def resultado(self, demanda, n, campos, arquivos):
        self.chamadas.append(("resultado", demanda, n, self.chave))


def test_laco_401_na_batida_nao_cancela_e_rele_a_chave(tmp_path):
    pac = _pacote_falso(tmp_path)
    continuou = []

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        ao_progresso("peticao-inicial-jec", "Pesquisa (3/13)")  # 401: segue
        ao_progresso("peticao-inicial-jec", "Redação (6/13)")
        continuou.append(1)
        return {"status": "pronto", "squad": "peticao-inicial-jec",
                "run_id": "2026-10-06-202440", "detalhe": "",
                "arquivos": executor.arquivos_do_pacote(casa, "peticao-inicial-jec", pac),
                "campos": {"gate_status": "aprovado"}}

    painel = PainelComChave([PACOTE])
    lidas = []
    laco.uma_volta(painel, tmp_path, lambda: "tok", tmp_path / "e.json",
                   executar=executar_falso, dormir=lambda s: None,
                   reler_chave=lambda: lidas.append(1) or "nova")
    assert continuou == [1] and lidas == [1]
    batidas = _so(painel, "batida")
    assert batidas[-1][4:] == ("Redação (6/13)", "nova")  # a próxima batida usa a nova
    assert _so(painel, "resultado") == [("resultado", 7, 1, "nova")]
    assert not _so(painel, "falha")


def test_laco_401_na_batida_sem_chave_nova_tambem_segue(tmp_path):
    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        ao_progresso("peticao-inicial-jec", "Pesquisa (3/13)")
        return _resultado_falso("falhou", detalhe="x")

    painel = PainelComChave([PACOTE])
    laco.uma_volta(painel, tmp_path, lambda: "tok", tmp_path / "e.json",
                   executar=executar_falso, reler_chave=lambda: None)
    assert [f[3] for f in _so(painel, "falha")] == ["erro"]  # não virou "cancelado"


def test_painel_trocar_chave():
    abrir = AbrirFalso(_erro_http(401), Resposta(204))
    p = cliente.Painel("https://painel.exemplo", "velha", abrir=abrir)
    with pytest.raises(cliente.PainelErro):
        p.proximo()
    p.trocar_chave("nova")
    assert p.chave_recusada is False and p.proximo() is None
    assert [r.get_header("Authorization") for r in abrir.pedidos] == ["Bearer velha",
                                                                      "Bearer nova"]


@pytest.mark.parametrize("bruto", [
    "aprovado", "  aprovado\x00 com\x7fressalvas\n", "a" * 39 + " b", "x" * 80,
    "aprovado linha", "", None, 42,
])
def test_gate_status_normalizado_igual_ao_servidor(bruto):
    from nucleo import ponte as nucleo_ponte
    assert executor.uma_linha(bruto, 40) == nucleo_ponte.uma_linha(bruto, 40)
    gate = {"gate_status": bruto, "citations": []}
    servidor = nucleo_ponte.campos_do_gate(gate)["gate_status"]
    # o que o Mac manda, normalizado de novo pelo servidor, bate com o do arquivo
    enviado = executor.uma_linha(bruto, 40) or "desconhecido"
    assert nucleo_ponte.uma_linha(enviado.strip(), 40) == servidor



def test_explicacao_do_lex_higienizada():
    texto = ("Li /Users/fulano/Desktop/caso/publicacao.md e squads/peticao/caso.json;\n"
             "a intimação de \"despacho.pdf\" (anexo.docx) não diz se cabe réplica ou "
             "embargos (art. 1.022). sk-ant-abc123 " + "x" * 400)
    saida = executor.explicacao_do_lex(texto)
    assert "\n" not in saida and len(saida) <= 300
    for proibido in ("/Users", "squads/", ".md", ".json", ".pdf", ".docx", "sk-ant", '""', "()"):
        assert proibido not in saida, proibido
    assert saida.startswith("Li e a intimação de não diz se cabe réplica ou embargos (art. 1.022).")
    assert executor.explicacao_do_lex("e/ou a peça certa") == "e/ou a peça certa"
    assert executor.explicacao_do_lex("  /tmp/x.md  ") == ""
    casa = "/Users/fulano/Desktop/Claude Cowork/Processos pendentes"
    assert executor.explicacao_do_lex(f"Li {casa}/0001/autos.pdf e não decidi.") == "Li e não decidi."
    assert executor.explicacao_do_lex(f"veja {casa}/ e decida") == "veja e decida"
    assert executor.explicacao_do_lex("leia docs/leia.txt antes") == "leia antes"


# --- Pastas do caso (Projeto 1, Task 9) --------------------------------------------------

CNJ = "00007640820164013606"
CNJ_F = "0000764-08.2016.4.01.3606"


def test_nome_da_pasta_formata_cnj_e_usa_o_primeiro_nome():
    assert pastas.nome_da_pasta(CNJ, "JOÃO DA SILVA; MARIA") == f"{CNJ_F} - JOÃO DA SILVA"
    assert pastas.nome_da_pasta(CNJ_F, "  Ana   Lima ") == f"{CNJ_F} - Ana Lima"


def test_nome_da_pasta_tira_caracteres_proibidos():
    entrada = 'Ana/Lima: "A*B?" <C>|D' + chr(92) + 'E' + chr(7)
    assert pastas.nome_da_pasta(CNJ, entrada) == f"{CNJ_F} - AnaLima AB CDE"


def test_nome_da_pasta_nunca_gera_dois_pontos_seguidos():
    nome = pastas.nome_da_pasta(CNJ, "SILVA... & CIA.")
    assert ".." not in nome
    assert nome == f"{CNJ_F} - SILVA. & CIA"
    assert ".." not in pastas.nome_da_pasta(CNJ, "A.:.B")
    assert pastas.nome_da_pasta(CNJ, " .. ") == CNJ_F


def test_nome_da_pasta_corta_em_palavra_com_ate_40():
    cliente = " ".join(["Palavra"] * 10)  # 79 caracteres
    nome = pastas.nome_da_pasta(CNJ, cliente)
    parte = nome.removeprefix(f"{CNJ_F} - ")
    assert len(parte) <= 40 and parte == " ".join(["Palavra"] * 5)
    assert len(pastas.nome_da_pasta(CNJ, "x" * 80).removeprefix(f"{CNJ_F} - ")) == 40


def test_nome_da_pasta_sem_cliente_so_o_numero():
    assert pastas.nome_da_pasta(CNJ, "") == CNJ_F
    assert pastas.nome_da_pasta(CNJ, None) == CNJ_F
    assert pastas.nome_da_pasta(CNJ, "///") == CNJ_F


def test_nome_da_pasta_numero_fora_do_padrao_usa_digitos_crus():
    assert pastas.nome_da_pasta("123-45", "Ana") == "12345 - Ana"


LEIA_ME = ('Coloque dentro da pasta "autos" o PDF (ou o .zip) do processo baixado do PJe.\n'
           "Assim que o arquivo terminar de copiar, a Controladoria avisa o painel e o Lex "
           "pode começar.\n"
           "Nada do que você puser aqui é apagado ou substituído.\n")


def test_achar_ou_criar_cria_pasta_autos_e_leia_me(tmp_path):
    pasta = pastas.achar_ou_criar(tmp_path, CNJ, "Maria Souza")
    assert pasta == tmp_path / f"{CNJ_F} - Maria Souza"
    assert (pasta / "autos").is_dir()
    assert (pasta / "LEIA-ME.txt").read_text(encoding="utf-8") == LEIA_ME


def test_achar_ou_criar_reaproveita_pasta_com_o_cnj(tmp_path):
    antiga = tmp_path / f"{CNJ_F} - Outro Nome"
    antiga.mkdir()
    assert pastas.achar_ou_criar(tmp_path, CNJ, "Maria") == antiga
    assert (antiga / "autos").is_dir() and (antiga / "LEIA-ME.txt").exists()
    soltos = tmp_path / f"{CNJ} Fulano"
    antiga.rename(tmp_path / "x")
    soltos.mkdir()
    assert pastas.achar_ou_criar(tmp_path, CNJ, "Maria") == soltos


def test_achar_ou_criar_ignora_a_pasta_antiga_de_cartao(tmp_path):
    (tmp_path / f"{CNJ}-cartao3").mkdir()
    pasta = pastas.achar_ou_criar(tmp_path, CNJ, "Maria")
    assert pasta == tmp_path / f"{CNJ_F} - Maria"
    assert not (tmp_path / f"{CNJ}-cartao3" / "autos").exists()


def test_achar_ou_criar_nao_sobrescreve_leia_me_editado(tmp_path):
    pasta = pastas.achar_ou_criar(tmp_path, CNJ, "Maria")
    (pasta / "LEIA-ME.txt").write_text("meu texto", encoding="utf-8")
    pastas.achar_ou_criar(tmp_path, CNJ, "Maria")
    assert (pasta / "LEIA-ME.txt").read_text(encoding="utf-8") == "meu texto"


def _grava(caminho, n=10):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(b"x" * n)
    return caminho


def test_arquivos_prontos_so_conta_com_tamanho_estavel(tmp_path):
    autos, memoria = tmp_path / "autos", {}
    autos.mkdir()
    _grava(autos / "Processo.PDF", 10)
    assert pastas.arquivos_prontos(autos, memoria) == []
    assert pastas.arquivos_prontos(autos, memoria) == [{"nome": "Processo.PDF", "tamanho": 10}]
    _grava(autos / "Processo.PDF", 25)  # ainda copiando
    assert pastas.arquivos_prontos(autos, memoria) == []
    assert pastas.arquivos_prontos(autos, memoria) == [{"nome": "Processo.PDF", "tamanho": 25}]


def test_arquivos_prontos_ignora_vazios_ocultos_e_parciais(tmp_path):
    autos, memoria = tmp_path / "autos", {}
    for nome in ("vazio.pdf", ".oculto.pdf", "LEIA-ME.txt", "LEIA-ME.pdf", "~$aux.zip",
                 "a.pdf.crdownload", "b.pdf.part", "c.pdf.download", "nota.txt", "ok.p7s"):
        _grava(autos / nome, 0 if nome == "vazio.pdf" else 5)
    pastas.arquivos_prontos(autos, memoria)
    assert pastas.arquivos_prontos(autos, memoria) == [{"nome": "ok.p7s", "tamanho": 5}]


def test_arquivos_prontos_inclui_subpastas_e_pasta_inexistente(tmp_path):
    autos, memoria = tmp_path / "autos", {}
    assert pastas.arquivos_prontos(autos, memoria) == []  # sem a pasta: nada, sem erro
    _grava(autos / "volume 1" / "p.zip", 7)
    pastas.arquivos_prontos(autos, memoria)
    (pronto,) = pastas.arquivos_prontos(autos, memoria)
    assert pronto["tamanho"] == 7 and pronto["nome"].endswith("p.zip")


class PainelDePastas:
    """Painel falso das duas rotas novas: o cartão só sai dos pendentes ao receber os autos."""

    def __init__(self, pendentes, erro_autos=None):
        self.pendentes = list(pendentes)
        self.erro_autos = erro_autos
        self.criadas_recebidas, self.avisos = [], []

    def pastas(self, criadas):
        self.criadas_recebidas.append(list(criadas))
        return [dict(p) for p in self.pendentes]

    def autos_na_pasta(self, demanda, arquivos):
        self.avisos.append((demanda, arquivos))
        if self.erro_autos:
            raise self.erro_autos
        self.pendentes = [p for p in self.pendentes if p["demanda"] != demanda]
        return True


def test_sincronizar_cria_avisa_a_pasta_e_depois_os_autos(tmp_path):
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Maria Souza"}])
    memoria = {}
    pasta = tmp_path / f"{CNJ_F} - Maria Souza"
    assert pastas.sincronizar(painel, tmp_path, memoria) == 0  # volta 1: cria a pasta
    assert painel.criadas_recebidas == [[]] and (pasta / "autos").is_dir()
    _grava(pasta / "autos" / "processo.pdf", 100)
    assert pastas.sincronizar(painel, tmp_path, memoria) == 0  # volta 2: vê o arquivo
    assert painel.criadas_recebidas[1] == [{"demanda": 5, "pasta_nome": pasta.name}]
    assert painel.avisos == []
    assert pastas.sincronizar(painel, tmp_path, memoria) == 1  # volta 3: tamanho estável
    assert painel.avisos == [(5, [{"nome": "processo.pdf", "tamanho": 100}])]
    assert painel.criadas_recebidas[2] == []  # o nome já foi entregue
    assert pastas.sincronizar(painel, tmp_path, memoria) == 0  # saiu dos pendentes
    assert len(painel.avisos) == 1


def test_sincronizar_reenvia_criadas_se_a_chamada_falhou(tmp_path):
    class Cai(PainelDePastas):
        def pastas(self, criadas):
            self.criadas_recebidas.append(list(criadas))
            if len(self.criadas_recebidas) == 2:
                raise OSError("sem rede")
            return [dict(p) for p in self.pendentes]

    painel = Cai([{"demanda": 5, "numero": CNJ, "cliente": "Maria"}])
    memoria = {}
    pastas.sincronizar(painel, tmp_path, memoria)
    with pytest.raises(OSError):
        pastas.sincronizar(painel, tmp_path, memoria)
    pastas.sincronizar(painel, tmp_path, memoria)
    assert painel.criadas_recebidas[2] == painel.criadas_recebidas[1] != []


def test_sincronizar_409_vai_so_para_o_log(tmp_path, caplog):
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Maria"}],
                            erro_autos=cliente.PainelErro(409, "o cartão saiu de faltam"))
    memoria = {}
    pastas.sincronizar(painel, tmp_path, memoria)
    _grava(tmp_path / f"{CNJ_F} - Maria" / "autos" / "p.pdf")
    pastas.sincronizar(painel, tmp_path, memoria)
    caplog.set_level("INFO")
    assert pastas.sincronizar(painel, tmp_path, memoria) == 0  # não levanta
    assert "409" in caplog.text


def test_sincronizar_erro_do_sistema_de_arquivos_so_log_e_segue(tmp_path, caplog):
    casa = tmp_path / "arquivo-no-lugar-da-casa"
    casa.write_text("não sou pasta")
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Maria"}])
    assert pastas.sincronizar(painel, casa, {}) == 0
    assert "5" in caplog.text


def test_sincronizar_log_nao_traz_conteudo_de_arquivo(tmp_path, caplog):
    caplog.set_level("INFO")
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Maria"}])
    memoria = {}
    pastas.sincronizar(painel, tmp_path, memoria)
    (tmp_path / f"{CNJ_F} - Maria" / "autos" / "segredo.pdf").write_bytes(b"CONTEUDO-SIGILOSO")
    pastas.sincronizar(painel, tmp_path, memoria)
    pastas.sincronizar(painel, tmp_path, memoria)
    assert "CONTEUDO-SIGILOSO" not in caplog.text
    assert f"{CNJ_F} - Maria" in caplog.text


def test_painel_pastas_manda_criadas_e_devolve_pendentes():
    corpo = json.dumps({"pendentes": [{"demanda": 5, "numero": CNJ, "cliente": "Ana"}]})
    abrir = AbrirFalso(Resposta(200, corpo.encode()), Resposta(200, corpo.encode()))
    p = cliente.Painel("https://painel.exemplo", "k", abrir=abrir)
    assert p.pastas([]) == [{"demanda": 5, "numero": CNJ, "cliente": "Ana"}]
    p.pastas([{"demanda": 5, "pasta_nome": "x"}])
    primeiro, segundo = abrir.pedidos
    assert primeiro.full_url == "https://painel.exemplo/ponte/pastas"
    assert primeiro.get_method() == "POST"
    assert json.loads(primeiro.data) == {"criadas": []}
    assert json.loads(segundo.data) == {"criadas": [{"demanda": 5, "pasta_nome": "x"}]}


def test_painel_pastas_servidor_antigo_404_vira_lista_vazia():
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(_erro_http(404)))
    assert p.pastas([]) == []


def test_painel_pastas_outros_erros_sobem():
    p = cliente.Painel("https://painel.exemplo", "k", abrir=AbrirFalso(_erro_http(400)))
    with pytest.raises(cliente.PainelErro) as exc:
        p.pastas([{"demanda": 1, "pasta_nome": ""}])
    assert exc.value.status == 400


def test_painel_autos_na_pasta_devolve_na_fila_e_409_sobe():
    abrir = AbrirFalso(Resposta(200, b'{"ok": true, "na_fila": true}'),
                       Resposta(200, b'{"ok": true, "na_fila": false}'), _erro_http(409))
    p = cliente.Painel("https://painel.exemplo", "k", abrir=abrir)
    arquivos = [{"nome": "p.pdf", "tamanho": 9}]
    assert p.autos_na_pasta(5, arquivos) is True
    assert p.autos_na_pasta(5, arquivos) is False
    pedido = abrir.pedidos[0]
    assert pedido.full_url == "https://painel.exemplo/ponte/autos-na-pasta"
    assert json.loads(pedido.data) == {"demanda": 5, "arquivos": arquivos}
    with pytest.raises(cliente.PainelErro) as exc:
        p.autos_na_pasta(5, arquivos)
    assert exc.value.status == 409


def test_uma_volta_sem_token_ainda_sincroniza_as_pastas(tmp_path):
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Maria"}])
    painel.contato = lambda claude_ok: None
    situacao = {}
    assert laco.uma_volta(painel, tmp_path, lambda: "", tmp_path / "e.json",
                          situacao=situacao) is False
    assert (tmp_path / f"{CNJ_F} - Maria" / "autos").is_dir()
    assert "pastas" in situacao


def test_uma_volta_erro_nas_pastas_nao_derruba_a_volta(tmp_path, caplog):
    class Cai(PainelFalso):
        def pastas(self, criadas):
            raise cliente.PainelErro(500, "falhou")

    painel = Cai([])
    laco.uma_volta(painel, tmp_path, lambda: "tok", tmp_path / "e.json")
    assert ("proximo",) in painel.chamadas
    assert "pastas do caso" in caplog.text


# --- Task 9, rodada 1 ------------------------------------------------------------------

def test_pasta_do_advogado_com_nome_invalido_nao_e_anunciada_mas_os_autos_sim(tmp_path):
    pasta = tmp_path / f"{CNJ_F} - Silva..Cia"
    (pasta / "autos").mkdir(parents=True)
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Silva"}])
    memoria = {}
    pastas.sincronizar(painel, tmp_path, memoria)
    _grava(pasta / "autos" / "p.pdf", 40)
    pastas.sincronizar(painel, tmp_path, memoria)
    assert pastas.sincronizar(painel, tmp_path, memoria) == 1
    assert painel.avisos == [(5, [{"nome": "p.pdf", "tamanho": 40}])]
    assert all(not lote for lote in painel.criadas_recebidas)  # nada de ".." enviado


@pytest.mark.parametrize("nome,aceito", [
    ("0000764-08 - Ana", True), ("", False), ("   ", False), ("a" * 201, False),
    ("a" * 200, True), ("a/b", False), ("a\\b", False), ("a..b", False), ("a.b", True),
])
def test_nome_aceito_pelo_painel_segue_a_regra_do_servidor(nome, aceito):
    assert pastas.nome_aceito_pelo_painel(nome) is aceito


def test_400_do_painel_descarta_as_criadas_pendentes(tmp_path, caplog):
    class Recusa(PainelDePastas):
        def pastas(self, criadas):
            self.criadas_recebidas.append(list(criadas))
            if len(self.criadas_recebidas) == 2:
                raise cliente.PainelErro(400, "Campo “pasta_nome” inválido.")
            return [dict(p) for p in self.pendentes]

    painel = Recusa([{"demanda": 5, "numero": CNJ, "cliente": "Maria"}])
    memoria = {}
    pastas.sincronizar(painel, tmp_path, memoria)
    assert pastas.sincronizar(painel, tmp_path, memoria) == 0  # 400: log, sem levantar
    assert "recusou" in caplog.text
    pastas.sincronizar(painel, tmp_path, memoria)
    assert painel.criadas_recebidas[2] == []  # o lote recusado não volta a ser enviado


def test_dois_cartoes_do_mesmo_processo_respeitam_as_duas_voltas(tmp_path):
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Maria"},
                             {"demanda": 6, "numero": CNJ, "cliente": "Maria"}])
    memoria = {}
    pastas.sincronizar(painel, tmp_path, memoria)
    _grava(tmp_path / f"{CNJ_F} - Maria" / "autos" / "p.pdf", 30)
    assert pastas.sincronizar(painel, tmp_path, memoria) == 0  # 1ª volta com o arquivo
    assert painel.avisos == []
    assert pastas.sincronizar(painel, tmp_path, memoria) == 2  # estável: os dois são avisados
    assert [a[0] for a in painel.avisos] == [5, 6]


def test_pacote_de_download_do_safari_e_ocultas_nao_sao_vasculhados(tmp_path):
    autos, memoria = tmp_path / "autos", {}
    for sub in ("x.pdf.download", "y.pdf.crdownload", "z.part", ".oculta"):
        _grava(autos / sub / "dentro.pdf", 9)
    _grava(autos / "volume" / "ok.pdf", 9)
    pastas.arquivos_prontos(autos, memoria)
    (pronto,) = pastas.arquivos_prontos(autos, memoria)
    assert pronto["nome"].endswith("ok.pdf")


def test_prefixo_do_numero_termina_ali(tmp_path):
    (tmp_path / "12345 - Ana").mkdir()
    pasta = pastas.achar_ou_criar(tmp_path, "123", "Bia")
    assert pasta == tmp_path / "123 - Bia"
    (tmp_path / "123 - Carla").mkdir()
    assert pastas.achar_ou_criar(tmp_path, "123", "Bia") == tmp_path / "123 - Bia"
    assert pastas.achar_ou_criar(tmp_path, f"{CNJ_F}", "X") == tmp_path / f"{CNJ_F} - X"
    (tmp_path / f"{CNJ_F}9 - Outro").mkdir()
    assert pastas.achar_ou_criar(tmp_path, CNJ, "Y") == tmp_path / f"{CNJ_F} - X"


def test_lista_de_arquivos_avisada_tem_no_maximo_50(tmp_path):
    painel = PainelDePastas([{"demanda": 5, "numero": CNJ, "cliente": "Maria"}])
    memoria = {}
    pastas.sincronizar(painel, tmp_path, memoria)
    for i in range(70):
        _grava(tmp_path / f"{CNJ_F} - Maria" / "autos" / f"p{i:02}.pdf", 3)
    pastas.sincronizar(painel, tmp_path, memoria)
    pastas.sincronizar(painel, tmp_path, memoria)
    assert len(painel.avisos[0][1]) == 50


def test_uma_volta_qualquer_erro_nas_pastas_nao_impede_a_reserva(tmp_path, monkeypatch):
    def explode(*a, **k):
        raise ValueError("inesperado")

    monkeypatch.setattr(laco.pastas, "sincronizar", explode)
    painel = PainelFalso([])
    laco.uma_volta(painel, tmp_path, lambda: "tok", tmp_path / "e.json")
    assert ("proximo",) in painel.chamadas


# --- Task 10: o Lex na pasta do processo, sem sobrescrever os autos do advogado --------

def test_pasta_do_processo_sem_pasta_antiga(tmp_path):
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"inicial.pdf": b"%PDF-1"}))
    assert pasta == tmp_path / PASTA_CASO == executor.pasta_do_caso(tmp_path, PACOTE)
    assert "Fica a parte intimada" in (pasta / PUBLICACAO).read_text(encoding="utf-8")
    assert not (pasta / "publicacao.md").exists()
    assert (pasta / "autos" / "inicial.pdf").read_bytes() == b"%PDF-1"
    assert (pasta / "LEIA-ME.txt").exists()
    assert not (tmp_path / PASTA_ANTIGA).exists()


def test_cartao_com_pasta_antiga_continua_nela(tmp_path):
    (tmp_path / PASTA_ANTIGA).mkdir()
    (tmp_path / "10000011120268110001-cartao3").mkdir()  # de outro cartão: não conta
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"1"}))
    assert pasta == tmp_path / PASTA_ANTIGA == executor.pasta_do_caso(tmp_path, PACOTE)
    assert (pasta / "publicacao.md").exists() and not (pasta / PUBLICACAO).exists()
    assert (pasta / "autos" / "a.pdf").read_bytes() == b"1"
    assert not (tmp_path / PASTA_CASO).exists()
    outro = executor.pasta_do_caso(tmp_path, dict(PACOTE, demanda=8))
    assert outro == tmp_path / PASTA_CASO


def test_pasta_antiga_traz_os_autos_postos_na_pasta_do_processo(tmp_path):
    antiga = tmp_path / PASTA_ANTIGA
    (antiga / "autos").mkdir(parents=True)
    (antiga / "autos" / "igual.pdf").write_bytes(b"MESMO")
    (antiga / "autos" / "minuta.pdf").write_bytes(b"DO ADVOGADO NA ANTIGA")
    nova = pastas.achar_ou_criar(tmp_path, PACOTE["numero"], PACOTE["cliente"])
    (nova / "autos" / "sub").mkdir()
    (nova / "autos" / "inicial.pdf").write_bytes(b"%PDF-novo")
    (nova / "autos" / "sub" / "doc.pdf").write_bytes(b"%PDF-sub")
    (nova / "autos" / "igual.pdf").write_bytes(b"MESMO")
    (nova / "autos" / "minuta.pdf").write_bytes(b"OUTRA VERSAO")
    (nova / "autos" / ".DS_Store").write_bytes(b"x")
    pasta = executor.preparar_pasta(tmp_path, PACOTE, None)
    assert pasta == antiga
    autos = antiga / "autos"
    assert (autos / "inicial.pdf").read_bytes() == b"%PDF-novo"
    assert (autos / "sub" / "doc.pdf").read_bytes() == b"%PDF-sub"
    assert (autos / "igual.pdf").read_bytes() == b"MESMO"
    assert not (autos / "igual (PJe).pdf").exists()
    assert (autos / "minuta.pdf").read_bytes() == b"DO ADVOGADO NA ANTIGA"  # nunca sobrescrito
    assert (autos / "minuta (PJe).pdf").read_bytes() == b"OUTRA VERSAO"
    assert not (autos / ".DS_Store").exists()
    # a pasta do processo não é tocada; uma segunda volta não duplica nada
    assert sorted(p.name for p in (nova / "autos").iterdir()) == [
        ".DS_Store", "igual.pdf", "inicial.pdf", "minuta.pdf", "sub"]
    executor.preparar_pasta(tmp_path, PACOTE, None)
    assert not (autos / "minuta (PJe 2).pdf").exists()
    assert sorted(p.name for p in autos.iterdir()) == [
        "igual.pdf", "inicial.pdf", "minuta (PJe).pdf", "minuta.pdf", "sub"]


def test_pasta_antiga_sem_pasta_do_processo_nao_cria_nada(tmp_path):
    (tmp_path / PASTA_ANTIGA).mkdir()
    executor.preparar_pasta(tmp_path, PACOTE, None)
    assert not (tmp_path / PASTA_CASO).exists()


def test_dois_cartoes_do_mesmo_processo_dividem_a_pasta(tmp_path):
    p7 = executor.preparar_pasta(tmp_path, PACOTE, None)
    p8 = executor.preparar_pasta(tmp_path, dict(PACOTE, demanda=8), None)
    assert p7 == p8 == tmp_path / PASTA_CASO
    assert (p7 / "publicacao-cartao7.md").exists() and (p7 / "publicacao-cartao8.md").exists()


def _autos_do_advogado(casa, nome="sentenca.pdf", dados=b"DO ADVOGADO"):
    pasta = pastas.achar_ou_criar(casa, PACOTE["numero"], PACOTE["cliente"])
    (pasta / "autos" / nome).write_bytes(dados)
    return pasta


def test_arquivo_do_advogado_com_mesmo_nome_e_outro_conteudo_fica_e_o_do_pje_ao_lado(tmp_path):
    pasta = _autos_do_advogado(tmp_path)
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"DO PJE"}))
    autos = pasta / "autos"
    assert (autos / "sentenca.pdf").read_bytes() == b"DO ADVOGADO"
    assert (autos / "sentenca (PJe).pdf").read_bytes() == b"DO PJE"
    controle = json.loads((pasta / executor.CONTROLE_AUTOS).read_text())
    assert controle["arquivos"] == [["sentenca (PJe).pdf", 6]]


def test_arquivo_do_advogado_com_mesmo_conteudo_fica_um_so(tmp_path):
    pasta = _autos_do_advogado(tmp_path, dados=b"IGUAL")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"IGUAL"}))
    autos = pasta / "autos"
    assert sorted(p.name for p in autos.iterdir()) == ["sentenca.pdf"]
    controle = json.loads((pasta / executor.CONTROLE_AUTOS).read_text())
    assert controle["arquivos"] == [["sentenca.pdf", 5]]
    # o zip seguinte sem a sentença não apaga o arquivo do advogado
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"outra.pdf": b"2"}))
    assert (autos / "sentenca.pdf").read_bytes() == b"IGUAL"
    # e um zip com outra versão dela não o sobrescreve
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"NOVA"}))
    assert (autos / "sentenca.pdf").read_bytes() == b"IGUAL"
    assert (autos / "sentenca (PJe).pdf").read_bytes() == b"NOVA"


def test_zip_novo_tira_so_o_que_veio_do_zip_anterior(tmp_path):
    pasta = _autos_do_advogado(tmp_path)
    (pasta / "autos" / "minhas-notas.pdf").write_bytes(b"N")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"PJE-1", "a.pdf": b"1"}))
    autos = pasta / "autos"
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"PJE-22"}))
    assert sorted(p.name for p in autos.iterdir()) == [
        "minhas-notas.pdf", "sentenca (PJe).pdf", "sentenca.pdf"]
    assert (autos / "sentenca (PJe).pdf").read_bytes() == b"PJE-22"  # o do zip anterior, trocado
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"b.pdf": b"3"}))
    assert sorted(p.name for p in autos.iterdir()) == ["b.pdf", "minhas-notas.pdf", "sentenca.pdf"]
    assert (autos / "sentenca.pdf").read_bytes() == b"DO ADVOGADO"


def test_mesmo_zip_com_arquivo_renomeado_nao_extrai_de_novo(tmp_path):
    pasta = _autos_do_advogado(tmp_path)
    z = _zip({"sentenca.pdf": b"DO PJE"})
    executor.preparar_pasta(tmp_path, PACOTE, z)
    pje = pasta / "autos" / "sentenca (PJe).pdf"
    os.utime(pje, (1_000, 1_000))
    executor.preparar_pasta(tmp_path, PACOTE, z)
    assert pje.stat().st_mtime == 1_000
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["sentenca (PJe).pdf",
                                                                   "sentenca.pdf"]


def test_pasta_do_advogado_com_nome_de_arquivo_do_zip_nao_e_apagada(tmp_path):
    pasta = pastas.achar_ou_criar(tmp_path, PACOTE["numero"], PACOTE["cliente"])
    (pasta / "autos" / "sentenca.pdf").mkdir()
    (pasta / "autos" / "sentenca.pdf" / "dentro.txt").write_text("x")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"DO PJE"}))
    assert (pasta / "autos" / "sentenca.pdf" / "dentro.txt").read_text() == "x"
    assert (pasta / "autos" / "sentenca (PJe).pdf").read_bytes() == b"DO PJE"


def test_nome_do_pje_ja_usado_pelo_advogado_ganha_numero(tmp_path):
    pasta = _autos_do_advogado(tmp_path)
    (pasta / "autos" / "sentenca (PJe).pdf").write_bytes(b"TAMBEM DELE")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"DO PJE"}))
    autos = pasta / "autos"
    assert (autos / "sentenca (PJe).pdf").read_bytes() == b"TAMBEM DELE"
    assert (autos / "sentenca (PJe 2).pdf").read_bytes() == b"DO PJE"


def test_prompt_com_espaco_e_acento_chega_intacto_ao_comando(tmp_path):
    pacote = dict(PACOTE, cliente="JOÃO DA SILVA; OUTRO")
    pasta = executor.pasta_do_caso(tmp_path, pacote)
    assert pasta.name == "1000001-11.2026.8.11.0001 - JOÃO DA SILVA"
    texto = executor.montar_prompt(pacote, pasta, None)
    (linha,) = [l for l in texto.splitlines() if "squad-modelo" in l]
    args = shlex.split(linha)
    assert args[args.index("--caso") + 1] == pasta.name
    assert "publicacao-cartao7.md" in texto and "LEIA-ME.txt" in texto
    # e pela execução: o prompt que vai ao `claude` leva o mesmo caminho
    popen = PopenFalso(_stream(_fim()))
    executor.executar(tmp_path, pacote, None, "tok", lambda *a: None, popen=popen,
                      relogio=Relogio())
    (claude_args, _), *_ = popen.chamadas
    assert f"--caso {shlex.quote(pasta.name)} --json" in claude_args[2]


def test_prompt_da_pasta_antiga_cita_publicacao_md(tmp_path):
    texto = executor.montar_prompt(PACOTE, tmp_path / PASTA_ANTIGA, None)
    assert "`publicacao.md`" in texto and "publicacao-cartao" not in texto


def test_limpar_sobras_na_pasta_do_processo(tmp_path):
    for nome in (PASTA_CASO, PASTA_ANTIGA):
        (tmp_path / nome / ".autos-x").mkdir(parents=True)
    (tmp_path / PASTA_CASO / "autos").mkdir()
    assert executor.limpar_sobras(tmp_path) == 2
    assert (tmp_path / PASTA_CASO / "autos").exists()


def test_ponta_a_ponta_segundo_cartao_do_mesmo_processo_nao_aceita_o_run_do_primeiro(tmp_path):
    def termina_run_do_7():
        _pacote_falso(tmp_path, squad="pi-c7", run="r-7", gerado_em=_agora_iso(+2))
        _state_do_run(tmp_path, "pi-c7", _agora_iso(+1))

    fim = _fim(squad="pi-c7", run_id="r-7")
    claude = ClaudeEmSequencia((_stream(fim), termina_run_do_7, 0), (_stream(fim), None, 0))
    oito = dict(PACOTE, demanda=8, n=1, episodio=601, orientacao="Recurso inominado.")
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, oito], claude)
    (res,) = _so(painel, "resultado")
    assert res[1:3] == (7, 1) and res[3]["run_id"] == "r-7"
    # o pi-c7 ficou amarrado ao 7 na entrega: o 8 já é recusado por ele (antes do frescor)
    assert [f[1:] for f in _so(painel, "falha")] == [
        (8, 1, "erro", "o squad não é deste caso; pacote recusado")]
    assert estado["8"]["pasta"] == str(tmp_path / PASTA_CASO)
    assert estado["8"].get("run_id", "") == ""  # o run do cartão 7 não vira run do 8
    assert all(f"--caso {shlex.quote(PASTA_CASO)} --json" in p for p in claude.prompts)
    assert "retome" not in claude.prompts[1]
    assert (tmp_path / PASTA_CASO / "publicacao-cartao7.md").exists()
    assert (tmp_path / PASTA_CASO / "publicacao-cartao8.md").exists()


def test_laco_pasta_do_caso_que_nao_pode_ser_criada_vira_falha(tmp_path, monkeypatch):
    def quebra(casa, numero, cliente):
        raise PermissionError("sem permissão")

    monkeypatch.setattr(pastas, "achar_ou_criar", quebra)
    painel = PainelFalso([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1),
               executar=lambda *a, **k: pytest.fail("não devia executar"))
    assert [f[1:4] for f in _so(painel, "falha")] == [(7, 1, "erro")]


# rodada pré-review: arquivo do zip anterior mexido pelo advogado passa a ser dele

def test_arquivo_do_zip_mexido_com_o_mesmo_tamanho_tambem_fica(tmp_path):
    z = _zip({"a.pdf": b"AAAA"})
    pasta = executor.preparar_pasta(tmp_path, PACOTE, z)
    (pasta / "autos" / "a.pdf").write_bytes(b"BBBB")
    executor.preparar_pasta(tmp_path, PACOTE, z)
    assert (pasta / "autos" / "a.pdf").read_bytes() == b"BBBB"
    assert (pasta / "autos" / "a (PJe).pdf").read_bytes() == b"AAAA"
    pje = pasta / "autos" / "a (PJe).pdf"
    os.utime(pje, (1_000, 1_000))
    executor.preparar_pasta(tmp_path, PACOTE, z)  # agora estável: nada muda
    assert pje.stat().st_mtime == 1_000
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["a (PJe).pdf", "a.pdf"]


def test_arquivo_do_zip_mexido_nao_sai_com_zip_novo_sem_ele(tmp_path):
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"1", "b.pdf": b"2"}))
    (pasta / "autos" / "a.pdf").write_bytes(b"anotado")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"c.pdf": b"3"}))
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["a.pdf", "c.pdf"]
    assert (pasta / "autos" / "a.pdf").read_bytes() == b"anotado"


def test_arquivo_do_zip_mexido_com_zip_novo_que_o_traz_de_novo(tmp_path):
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"1"}))
    (pasta / "autos" / "a.pdf").write_bytes(b"anotado")
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"versao 2"}))
    assert (pasta / "autos" / "a.pdf").read_bytes() == b"anotado"
    assert (pasta / "autos" / "a (PJe).pdf").read_bytes() == b"versao 2"


def test_controle_guarda_o_sha256_de_cada_arquivo(tmp_path):
    import hashlib
    pasta = executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"1", "d/b.pdf": b"22"}))
    controle = json.loads((pasta / executor.CONTROLE_AUTOS).read_text())
    assert controle["hashes"] == {"a.pdf": hashlib.sha256(b"1").hexdigest(),
                                  "d/b.pdf": hashlib.sha256(b"22").hexdigest()}


def test_controle_antigo_sem_sha_trata_como_hoje(tmp_path):
    z = _zip({"a.pdf": b"1", "b.pdf": b"2"})
    pasta = executor.preparar_pasta(tmp_path, PACOTE, z)
    controle_path = pasta / executor.CONTROLE_AUTOS
    controle = json.loads(controle_path.read_text())
    del controle["hashes"], controle["advogado"]
    controle_path.write_text(json.dumps(controle))
    a = pasta / "autos" / "a.pdf"
    os.utime(a, (1_000, 1_000))
    executor.preparar_pasta(tmp_path, PACOTE, z)  # mesmo zip: nada muda, nada quebra
    assert a.stat().st_mtime == 1_000
    controle_path.write_text(json.dumps(controle))
    a.write_bytes(b"mexido")  # sem sha, o arquivo do zip anterior conta como intacto
    executor.preparar_pasta(tmp_path, PACOTE, _zip({"a.pdf": b"1"}))
    assert sorted(p.name for p in (pasta / "autos").iterdir()) == ["a.pdf"]
    assert a.read_bytes() == b"1"


# --- Task 10, rodada 1: squad amarrado ao cartão na pasta compartilhada -----------------

def _marca(casa, squad):
    caminho = casa / "squads" / squad / executor.MARCA_CARTAO
    return json.loads(caminho.read_text()) if caminho.exists() else None


def test_marcar_squad_grava_uma_vez_so(tmp_path):
    _squad_do_caso(tmp_path, "pi-c7")
    executor.marcar_squad(tmp_path, "pi-c7", 7)
    executor.marcar_squad(tmp_path, "pi-c7", 8)  # já tem dono: não muda
    assert _marca(tmp_path, "pi-c7") == {"demanda": 7}
    assert executor.dono_do_squad(tmp_path, "pi-c7") == 7
    assert executor.dono_do_squad(tmp_path, "sem-marca") is None
    executor.marcar_squad(tmp_path, "../fora", 7)  # nome inválido: nada
    assert not (tmp_path / "fora").exists()
    assert [p.name for p in (tmp_path / "squads" / "pi-c7").iterdir()
            if p.name.startswith(".ponte-cartao.json.")] == []


def test_squad_marcado_para_outro_cartao_nao_e_deste_caso(tmp_path):
    pasta = tmp_path / PASTA_CASO
    for s, dono in (("pi-c7", 7), ("pi-c8", 8), ("sem-marca", None)):
        _squad_do_caso(tmp_path, s)
        if dono:
            executor.marcar_squad(tmp_path, s, dono)
    assert executor.squad_do_caso(tmp_path, "pi-c7", pasta, 7)
    assert not executor.squad_do_caso(tmp_path, "pi-c8", pasta, 7)
    assert executor.squad_do_caso(tmp_path, "sem-marca", pasta, 7)
    assert executor.squad_do_caso(tmp_path, "pi-c8", pasta)  # sem demanda: regra de antes


def test_progresso_ignora_squad_de_outro_cartao(tmp_path):
    pasta = tmp_path / PASTA_CASO
    for nome, mtime, dono in (("pi-c7", 1_000, 7), ("pi-c8", 2_000, 8)):
        _squad_do_caso(tmp_path, nome)
        executor.marcar_squad(tmp_path, nome, dono)
        st = tmp_path / "squads" / nome / "state.json"
        st.write_text(json.dumps({"status": "rodando " + nome}))
        os.utime(st, (mtime, mtime))
    assert executor.progresso(tmp_path, 0, pasta, 7) == ("pi-c7", "rodando pi-c7")
    assert executor.progresso(tmp_path, 0, pasta, 8) == ("pi-c8", "rodando pi-c8")


def test_executar_recusa_squad_de_outro_cartao_mesmo_com_pacote_novo(tmp_path):
    _pacote_falso(tmp_path, gerado_em="2099-01-01T00:00:00Z")
    executor.marcar_squad(tmp_path, "peticao-inicial-jec", 8)
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())))
    assert res["status"] == "falhou"
    assert res["detalhe"] == "o squad não é deste caso; pacote recusado"


def test_executar_retomar_e_atalho_recusam_squad_de_outro_cartao(tmp_path):
    _pacote_falso(tmp_path, run="r-8", gerado_em="2020-01-01T00:00:00Z")
    executor.marcar_squad(tmp_path, "peticao-inicial-jec", 8)
    popen = PopenFalso(_stream(_fim(run_id="r-8")))
    res, _ = _executar(tmp_path, popen, retomar={"squad": "peticao-inicial-jec", "run_id": "r-8"})
    assert "retome" not in popen.chamadas[0][0][2]
    assert res["status"] == "falhou"
    assert res["detalhe"] == "o squad não é deste caso; pacote recusado"


def test_executar_pronto_marca_o_squad_para_o_cartao(tmp_path):
    _pacote_falso(tmp_path, gerado_em="2099-01-01T00:00:00Z")
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())))
    assert res["status"] == "pronto"
    assert _marca(tmp_path, "peticao-inicial-jec") == {"demanda": 7}


def test_laco_anotar_squad_marca_e_recusa_o_de_outro_cartao(tmp_path):
    for s, dono in (("pi-c7", None), ("pi-c8", 8)):
        _squad_do_caso(tmp_path, s)
        if dono:
            executor.marcar_squad(tmp_path, s, dono)
    _state_do_run(tmp_path, "pi-c7", "2099-01-01T00:00:00Z")  # run começado nesta demanda

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        ao_progresso("pi-c8", "Redação (6/13)")
        ao_progresso("pi-c7", "Redação (7/13)")
        return {"status": "tempo", "squad": "", "run_id": "", "arquivos": {}, "campos": {},
                "detalhe": ""}

    estado = tmp_path / "e.json"
    laco.rodar(PainelFalso([PACOTE]), tmp_path, lambda: "tok", estado, dormir=lambda s: None,
               parar=_parar_depois(1), executar=executar_falso)
    assert json.loads(estado.read_text())["7"]["squad"] == "pi-c7"
    assert _marca(tmp_path, "pi-c7") == {"demanda": 7}
    assert _marca(tmp_path, "pi-c8") == {"demanda": 8}


def test_ponta_a_ponta_7_interrompido_8_completo_7_retomado(tmp_path):
    # o relógio do laço anda 1 h a cada espera: o 8 é reservado horas "depois" do 7
    def estado_do(squad, run, inicio):
        _squad_do_caso(tmp_path, squad)
        (tmp_path / "squads" / squad / "state.json").write_text(
            json.dumps({"step": {"current": 6, "total": 13, "label": "Redação " + squad}}))
        (tmp_path / "squads" / squad / "run-state.json").write_text(
            json.dumps({"runId": run, "startedAt": _agora_iso(inicio)}))

    def roda_o_8():
        estado_do("pi-c8", "r-8", +5 * 3600)
        _pacote_falso(tmp_path, squad="pi-c8", run="r-8", gerado_em=_agora_iso(+6 * 3600))

    def mexe_no_8():  # o 8 é o squad mais recente da pasta quando o 7 volta
        st = tmp_path / "squads" / "pi-c8" / "state.json"
        os.utime(st, (time.time() + 10, time.time() + 10))

    limite = [_linha(type="result", is_error=True, result="Claude AI usage limit reached|1")]
    claude = ClaudeEmSequencia(
        (limite, lambda: estado_do("pi-c7", "r-7", +1), 2),
        (_stream(_fim(squad="pi-c8", run_id="r-8")), roda_o_8, 2),
        (_stream(_fim(squad="pi-c8", run_id="r-8")), mexe_no_8, 2))
    oito = dict(PACOTE, demanda=8, n=1, episodio=601)
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, oito, dict(PACOTE, n=2)], claude)
    (res,) = _so(painel, "resultado")
    assert res[1:3] == (8, 1) and res[3]["run_id"] == "r-8"
    assert "retome o run r-7 do squad pi-c7" in claude.prompts[2]
    batidas_do_7 = [b for b in _so(painel, "batida") if b[1] == 7]
    assert all(b[3] != "pi-c8" and "pi-c8" not in b[4] for b in batidas_do_7)
    assert ("batida", 7, 1, "pi-c7", "Redação pi-c7 (6/13)") in painel.chamadas
    assert [f[1:] for f in _so(painel, "falha")] == [
        (7, 1, "limite", "pausado: limite do plano"),
        (7, 2, "erro", "o squad não é deste caso; pacote recusado")]
    assert estado["7"]["squad"] == "pi-c7" and estado["7"]["run_id"] == "r-7"
    assert _marca(tmp_path, "pi-c7") == {"demanda": 7}
    assert _marca(tmp_path, "pi-c8") == {"demanda": 8}


def test_versao_pje_com_dois_digitos(tmp_path):
    assert executor._e_versao_pje("d/a (PJe 10).pdf", "d/a.pdf")
    assert executor._e_versao_pje("a (PJe 2).pdf", "a.pdf")
    assert not executor._e_versao_pje("a (PJe 1).pdf", "a.pdf")
    assert not executor._e_versao_pje("a (PJe 01).pdf", "a.pdf")


def test_autos_como_link_do_advogado_nao_e_desfeito(tmp_path):
    pasta = pastas.achar_ou_criar(tmp_path, PACOTE["numero"], PACOTE["cliente"])
    fora = tmp_path / "drive"
    fora.mkdir()
    (fora / "sentenca.pdf").write_bytes(b"DELE")
    (pasta / "autos").rmdir()
    (pasta / "autos").symlink_to(fora)
    with pytest.raises(ValueError):
        executor.preparar_pasta(tmp_path, PACOTE, _zip({"sentenca.pdf": b"PJE", "b.pdf": b"2"}))
    assert (pasta / "autos").is_symlink()
    assert sorted(p.name for p in fora.iterdir()) == ["sentenca.pdf"]
    assert (fora / "sentenca.pdf").read_bytes() == b"DELE"
    assert [p.name for p in pasta.iterdir() if p.name.startswith(".")] == []
    # pelo laço, vira a frase fixa "erro no Mac"
    class PainelComZip(PainelFalso):
        def autos(self, demanda):
            super().autos(demanda)
            return _zip({"b.pdf": b"2"})

    painel = PainelComZip([PACOTE])
    laco.rodar(painel, tmp_path, lambda: "tok", tmp_path / "e.json", dormir=lambda s: None,
               parar=_parar_depois(1),
               executar=functools.partial(executor.executar, popen=PopenFalso([]),
                                          relogio=Relogio()))
    assert [f[3:] for f in _so(painel, "falha")] == [("erro", "erro no Mac")]


def test_limpar_sobras_so_nas_pastas_de_caso(tmp_path):
    caso = [PASTA_CASO, PASTA_ANTIGA, "10000011120268110001 - Ana"]
    outras = ["squads", "acervo", "1000001-11.2026.8.11.00012 - errado", "notas"]
    for nome in caso + outras:
        (tmp_path / nome / ".autos-x").mkdir(parents=True)
    assert executor.limpar_sobras(tmp_path) == len(caso)
    assert all(not (tmp_path / n / ".autos-x").exists() for n in caso)
    assert all((tmp_path / n / ".autos-x").exists() for n in outras)


def test_laco_squad_sem_run_desta_demanda_nao_e_amarrado(tmp_path):
    # squad antigo (de antes do marcador) citado por outro cartão não vira dele
    _squad_do_caso(tmp_path, "pi-c7")
    _state_do_run(tmp_path, "pi-c7", "2020-01-01T00:00:00Z")

    def executar_falso(casa, pacote, autos_zip, token, ao_progresso, retomar=None, desde=None):
        ao_progresso("pi-c7", "Redação (7/13)")
        return {"status": "falhou", "squad": "pi-c7", "run_id": "", "arquivos": {},
                "campos": {}, "detalhe": "o Lex não gerou pacote novo para este caso"}

    laco.rodar(PainelFalso([dict(PACOTE, demanda=8)]), tmp_path, lambda: "tok",
               tmp_path / "e.json", dormir=lambda s: None, parar=_parar_depois(1),
               executar=executar_falso)
    assert _marca(tmp_path, "pi-c7") is None



# --- Task 10, rodada 2: squad sem marcador só vale se nasceu nesta execução -------------

LIMITE_DO_PLANO = [_linha(type="result", is_error=True, result="Claude AI usage limit reached|1")]


def _no_laco(avanco, delta):
    """Instante (ISO) no relógio do laço: `delta` segundos depois de agora nesta volta."""
    return _agora_iso(avanco[0] + delta)


def _squad_rodando(casa, avanco, squad, run, *, com_inicio=True):
    _squad_do_caso(casa, squad)
    st = casa / "squads" / squad / "state.json"
    st.write_text(json.dumps({"step": {"current": 6, "total": 13, "label": "Redação " + squad}}))
    agora = time.time() + avanco[0] + 30
    os.utime(st, (agora, agora))  # mexido nesta execução: aparece no progresso
    (casa / "squads" / squad / "run-state.json").write_text(json.dumps(
        {"runId": run, **({"startedAt": _no_laco(avanco, 20)} if com_inicio else {})}))


def _entrega(casa, avanco, squad, run):
    _pacote_falso(casa, squad=squad, run=run, gerado_em=_no_laco(avanco, 40))


def _mexe_agora(casa, avanco, squad):
    st = casa / "squads" / squad / "state.json"
    agora = time.time() + avanco[0] + 50
    os.utime(st, (agora, agora))


OITO = dict(PACOTE, demanda=8, n=1, episodio=601)


def test_rodada2_b_8_cortado_sem_marca_nao_e_entregue_pelo_7(tmp_path):
    av = [0.0]

    def roda_o_8():  # escreve o pacote e é cortado antes de qualquer batida
        _squad_rodando(tmp_path, av, "pi-c8", "r-8")
        _entrega(tmp_path, av, "pi-c8", "r-8")

    claude = ClaudeEmSequencia(
        (LIMITE_DO_PLANO, lambda: _squad_rodando(tmp_path, av, "pi-c7", "r-7"), 2),
        (LIMITE_DO_PLANO, roda_o_8, 0),
        (_stream(_fim(squad="pi-c8", run_id="r-8")), None, 0))
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, OITO, dict(PACOTE, n=2)], claude, av)
    assert not _so(painel, "resultado")
    assert [f[1:] for f in _so(painel, "falha")] == [
        (7, 1, "limite", "pausado: limite do plano"), (8, 1, "limite", "pausado: limite do plano"),
        (7, 2, "erro", "o Lex não gerou pacote novo para este caso")]
    assert "retome o run r-7 do squad pi-c7" in claude.prompts[2]
    assert executor.dono_do_squad(tmp_path, "pi-c7") == 7
    assert executor.dono_do_squad(tmp_path, "pi-c8") is None
    assert estado["7"]["squad"] == "pi-c7" and estado["7"]["run_id"] == "r-7"


def test_rodada2_b2_lex_do_7_toca_o_squad_do_8_sem_marca(tmp_path):
    av = [0.0]
    claude = ClaudeEmSequencia(
        (LIMITE_DO_PLANO, lambda: _squad_rodando(tmp_path, av, "pi-c7", "r-7"), 2),
        (LIMITE_DO_PLANO, lambda: _squad_rodando(tmp_path, av, "pi-c8", "r-8"), 0),
        (LIMITE_DO_PLANO, lambda: _mexe_agora(tmp_path, av, "pi-c8"), 2))
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, OITO, dict(PACOTE, n=2)], claude, av)
    batidas = [b[3:] for b in _so(painel, "batida") if b[1:3] == (7, 2)]
    assert batidas and all(b[0] != "pi-c8" and "pi-c8" not in b[1] for b in batidas)
    assert executor.dono_do_squad(tmp_path, "pi-c8") is None  # o 8 não perde o squad
    assert estado["7"]["squad"] == "pi-c7" and estado["7"]["run_id"] == "r-7"


def test_rodada2_c_7_cortado_sem_marca_nao_e_entregue_pelo_8(tmp_path):
    av = [0.0]

    def roda_o_7():
        _squad_rodando(tmp_path, av, "pi-c7", "r-7")
        _entrega(tmp_path, av, "pi-c7", "r-7")

    claude = ClaudeEmSequencia(
        (LIMITE_DO_PLANO, lambda: _squad_rodando(tmp_path, av, "pi-c8", "r-8"), 2),
        (LIMITE_DO_PLANO, roda_o_7, 0),
        (_stream(_fim(squad="pi-c7", run_id="r-7")), None, 0))
    painel, _ = _ponta_a_ponta(tmp_path, [OITO, PACOTE, dict(OITO, n=2)], claude, av)
    assert not _so(painel, "resultado")
    assert [f[1:] for f in _so(painel, "falha")][-1] == (
        8, 2, "erro", "o Lex não gerou pacote novo para este caso")


def test_rodada2_c_seguro_7_completo_nao_e_entregue_pelo_8(tmp_path):
    av = [0.0]

    def roda_o_7():
        _squad_rodando(tmp_path, av, "pi-c7", "r-7")
        _entrega(tmp_path, av, "pi-c7", "r-7")

    claude = ClaudeEmSequencia(
        (LIMITE_DO_PLANO, lambda: _squad_rodando(tmp_path, av, "pi-c8", "r-8"), 2),
        (_stream(_fim(squad="pi-c7", run_id="r-7")), roda_o_7, 2),
        (_stream(_fim(squad="pi-c7", run_id="r-7")), lambda: _mexe_agora(tmp_path, av, "pi-c7"),
         2))
    painel, _ = _ponta_a_ponta(tmp_path, [OITO, PACOTE, dict(OITO, n=2)], claude, av)
    assert [r[1:3] for r in _so(painel, "resultado")] == [(7, 1)]
    assert [f[1:] for f in _so(painel, "falha")][-1] == (
        8, 2, "erro", "o squad não é deste caso; pacote recusado")
    assert all(b[3] != "pi-c7" for b in _so(painel, "batida") if b[1] == 8)


def test_rodada2_proprio_run_sem_marca_recomeca_do_zero(tmp_path):
    av = [0.0]

    def roda_o_7():  # cortado antes de qualquer batida: o squad fica sem marcador
        _squad_rodando(tmp_path, av, "pi-c7", "r-7")
        _entrega(tmp_path, av, "pi-c7", "r-7")

    claude = ClaudeEmSequencia((LIMITE_DO_PLANO, roda_o_7, 0),
                               (_stream(_fim(squad="pi-c7", run_id="r-7")), None, 0))
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, dict(PACOTE, n=2)], claude, av)
    assert "retome" not in claude.prompts[1] and "--caso" in claude.prompts[1]
    assert not _so(painel, "resultado")
    assert [f[1:] for f in _so(painel, "falha")][-1] == (
        7, 2, "erro", "o Lex não gerou pacote novo para este caso")
    assert executor.dono_do_squad(tmp_path, "pi-c7") is None


def test_rodada2_b3_run_sem_startedat_nao_e_adotado_nem_entregue(tmp_path):
    av = [0.0]

    def roda_o_8():
        _squad_rodando(tmp_path, av, "pi-c8", "r-8", com_inicio=False)
        _entrega(tmp_path, av, "pi-c8", "r-8")

    claude = ClaudeEmSequencia(
        (LIMITE_DO_PLANO, lambda: _squad_rodando(tmp_path, av, "pi-c7", "r-7"), 2),
        (LIMITE_DO_PLANO, roda_o_8, 3),
        (_stream(_fim(squad="pi-c8", run_id="r-8")), None, 0))
    painel, estado = _ponta_a_ponta(tmp_path, [PACOTE, OITO, dict(PACOTE, n=2)], claude, av)
    assert not _so(painel, "resultado")
    assert "squad" not in estado.get("8", {})  # sem run confirmado nesta execução
    assert executor.dono_do_squad(tmp_path, "pi-c8") is None
    assert [f[1:] for f in _so(painel, "falha")][-1] == (
        7, 2, "erro", "o Lex não gerou pacote novo para este caso")


def test_executar_squad_sem_marca_exige_pacote_desta_execucao(tmp_path):
    # pacote depois do `desde` do episódio, mas antes do início desta execução
    _pacote_falso(tmp_path, gerado_em=_agora_iso(-50))
    relogio = lambda: time.time()  # noqa: E731
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), relogio=relogio,
                       desde=time.time() - 100)
    assert res["detalhe"] == "o Lex não gerou pacote novo para este caso"
    # com o marcador deste cartão, vale o `desde` do episódio
    executor.marcar_squad(tmp_path, "peticao-inicial-jec", 7)
    res, _ = _executar(tmp_path, PopenFalso(_stream(_fim())), relogio=relogio,
                       desde=time.time() - 100)
    assert res["status"] == "pronto"


def test_progresso_squad_sem_marca_so_com_run_desta_execucao(tmp_path):
    pasta = tmp_path / PASTA_CASO
    _squad_do_caso(tmp_path, "pi-x")
    (tmp_path / "squads" / "pi-x" / "state.json").write_text(json.dumps({"status": "rodando"}))
    _state_do_run(tmp_path, "pi-x", "2020-01-01T00:00:00Z")
    piso = time.time() - 10
    assert executor.progresso(tmp_path, 0, pasta, 7, piso) is None
    _state_do_run(tmp_path, "pi-x", _agora_iso())
    assert executor.progresso(tmp_path, 0, pasta, 7, piso) == ("pi-x", "rodando")
    _state_do_run(tmp_path, "pi-x", "2020-01-01T00:00:00Z")
    executor.marcar_squad(tmp_path, "pi-x", 7)  # do próprio cartão: basta o state mexido
    assert executor.progresso(tmp_path, 0, pasta, 7, piso) == ("pi-x", "rodando")


def test_marcador_ilegivel_e_sem_dono_e_pode_ser_regravado(tmp_path):
    _squad_do_caso(tmp_path, "pi-c7")
    marca = tmp_path / "squads" / "pi-c7" / executor.MARCA_CARTAO
    marca.write_text("{quebrado")
    assert executor.dono_do_squad(tmp_path, "pi-c7") is None
    executor.marcar_squad(tmp_path, "pi-c7", 7)
    assert executor.dono_do_squad(tmp_path, "pi-c7") == 7
    executor.marcar_squad(tmp_path, "pi-c7", 8)
    assert executor.dono_do_squad(tmp_path, "pi-c7") == 7
    assert sorted(p.name for p in marca.parent.iterdir() if p.name.startswith(".")) == [
        executor.MARCA_CARTAO]


# --- sonda do acesso: recusado x sem login x indefinido ----------------------------------

import json as _json
import subprocess as _sp

from ponte_mac import executor as _ex

# o "result" que o claude 2.1.312 devolveu no CI (Windows e Mac)
_SAIDA_401 = "Failed to authenticate. API Error: 401 OAuth access token is invalid."
_SAIDA_SEM_LOGIN = "Not logged in · Please run /login"


def _rodar_com(stdout, returncode=1):
    def rodar(argv, **kw):
        return _sp.CompletedProcess(argv, returncode, stdout=stdout, stderr="")
    return rodar


def _json_do_claude(resultado, erro=True):
    return _json.dumps({"is_error": erro, "result": resultado, "terminal_reason": "api_error"})


@pytest.mark.parametrize("stdout,returncode,esperado", [
    (_json_do_claude("ok", erro=False), 0, "ok"),
    (_json_do_claude(_SAIDA_401), 1, "recusado"),
    (_json_do_claude(_SAIDA_SEM_LOGIN), 1, "sem_login"),
    ("", 1, "indefinido"),
])
def test_classificar_acesso(tmp_path, stdout, returncode, esperado):
    assert _ex.classificar_acesso("x", tmp_path, rodar=_rodar_com(stdout, returncode)) == esperado


@pytest.mark.parametrize("stdout,returncode,esperado", [
    (_json_do_claude("ok", erro=False), 0, True),
    (_json_do_claude(_SAIDA_401), 1, False),
    (_json_do_claude(_SAIDA_SEM_LOGIN), 1, False),  # a ponte trata igual a recusado
    ("", 1, None),
    (_json_do_claude("Not logged in"), 1, None),  # sem "please run /login": como sempre, None
    ("OAuth token has expired", 1, False),  # "oauth token" em texto puro
    (_json_do_claude("The OAuth token is invalid"), 1, False),
])
def test_validar_acesso_continua_igual(tmp_path, stdout, returncode, esperado):
    assert _ex.validar_acesso("x", tmp_path, rodar=_rodar_com(stdout, returncode)) is esperado


def test_classificar_sem_claude_e_indefinido(tmp_path):
    def rodar(argv, **kw):
        raise FileNotFoundError("claude")
    assert _ex.classificar_acesso("x", tmp_path, rodar=rodar) == "indefinido"


def test_validar_acesso_pedido_de_login_no_stderr_puro(tmp_path):
    def rodar(argv, **kw):
        return _sp.CompletedProcess(argv, 1, stdout="", stderr="Please run /login")
    assert _ex.validar_acesso("x", tmp_path, rodar=rodar) is False


def test_validar_acesso_roda_o_claude_uma_vez(tmp_path):
    chamadas = []

    def rodar(argv, **kw):
        chamadas.append(argv)
        return _sp.CompletedProcess(argv, 1, stdout=_json_do_claude(_SAIDA_401), stderr="")
    assert _ex.validar_acesso("x", tmp_path, rodar=rodar) is False
    assert len(chamadas) == 1
