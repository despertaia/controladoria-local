"""local/melhorias.py: o instalador guarda o que o mentorado mudou antes de atualizar."""

import io
import json
import subprocess
import sys
from pathlib import Path

from local import melhorias


def _arvore(raiz: Path, arquivos: dict[str, str]) -> Path:
    for rel, texto in arquivos.items():
        alvo = raiz / rel
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_bytes(texto.encode("utf-8"))
    return raiz


ORIGINAL = {"VERSAO": "1.4.3\n", "painel.py": "app = 1\n", "nucleo/quadro.py": "x = 1\n",
            "nucleo/banco.py": "y = 2\n", "local/iniciar.py": "z = 3\n"}


def _instalada_com_manifesto(tmp_path, alteracoes=None, apagar=()):
    nova = _arvore(tmp_path / "nova", {**ORIGINAL, "VERSAO": "1.4.4\n"})
    melhorias.gravar_manifesto(_arvore(tmp_path / "orig", ORIGINAL), "1.4.3",
                               nova / melhorias.MANIFESTOS / "1.4.3.json")
    inst = _arvore(tmp_path / "inst", {**ORIGINAL, **(alteracoes or {})})
    for rel in apagar:
        (inst / rel).unlink()
    return inst, nova


def test_impressao_ignora_o_fim_de_linha(tmp_path):
    a = _arvore(tmp_path / "a", {"f.py": "x = 1\n"}) / "f.py"
    b = tmp_path / "b.py"
    b.write_bytes(b"x = 1\r\n")
    assert melhorias.impressao(a) == melhorias.impressao(b)


def test_manifesto_ignora_dados_ambiente_e_cache(tmp_path):
    raiz = _arvore(tmp_path / "r", {**ORIGINAL, "dados/c.db": "", ".venv/x": "",
                                    "nucleo/__pycache__/q.pyc": "", "local/manifestos/1.json": "{}",
                                    "melhorias-guardadas/a.py": "", ".env": "S=1"})
    assert sorted(melhorias.manifesto(raiz)) == sorted(ORIGINAL)


def test_sem_alteracao_nao_guarda_nada(tmp_path):
    inst, nova = _instalada_com_manifesto(tmp_path)
    (inst / "nucleo" / "__pycache__").mkdir()
    (inst / "nucleo" / "__pycache__" / "q.pyc").write_bytes(b"\0")
    (inst / "dados").mkdir()
    (inst / "dados" / "controladoria.db").write_text("dados")
    pasta, dif = melhorias.guardar(inst, nova, "2026-10-29")
    assert pasta is None and dif["alterados"] == dif["criados"] == dif["apagados"] == []
    assert dif["base"] == "versão 1.4.3 original"


def test_guarda_alterado_criado_e_lista_apagado(tmp_path):
    inst, nova = _instalada_com_manifesto(
        tmp_path, {"nucleo/quadro.py": "x = 1  # melhoria\n", "nucleo/minha.py": "m = 1\n"},
        apagar=["nucleo/banco.py"])
    (inst / "painel.py").write_bytes(b"app = 1\r\n")  # só o fim de linha: não é alteração
    pasta, dif = melhorias.guardar(inst, nova, "2026-10-29")
    assert dif["alterados"] == ["nucleo/quadro.py"]
    assert dif["criados"] == ["nucleo/minha.py"]
    assert dif["apagados"] == ["nucleo/banco.py"]
    assert pasta == inst / "melhorias-guardadas" / "2026-10-29-v1.4.3"
    assert (pasta / "nucleo" / "quadro.py").read_text() == "x = 1  # melhoria\n"
    assert (pasta / "nucleo" / "minha.py").exists()
    leia = (pasta / "LEIA-ME.txt").read_text(encoding="utf-8")
    assert "nucleo/quadro.py" in leia and "nucleo/minha.py" in leia and "nucleo/banco.py" in leia
    assert "Reaplique as melhorias guardadas da Controladoria do Lex Lab" in leia
    pasta2, _ = melhorias.guardar(inst, nova, "2026-10-29")  # de novo no mesmo dia
    assert pasta2.name == "2026-10-29-v1.4.3-2"


def test_sem_manifesto_compara_com_a_versao_nova(tmp_path):
    nova = _arvore(tmp_path / "nova", {**ORIGINAL, "nucleo/novo.py": "n = 1\n"})
    inst = _arvore(tmp_path / "inst", {**ORIGINAL, "VERSAO": "1.0.0\n",
                                       "nucleo/quadro.py": "x = 2\n"})
    dif = melhorias.diferencas(inst, nova)
    assert dif["alterados"] == ["VERSAO", "nucleo/quadro.py"]
    assert dif["apagados"] == []  # sem o original não dá para dizer o que ele apagou
    assert dif["base"].startswith("versão nova")


def test_item_do_topo_que_a_versao_nova_nao_traz_fica_de_fora(tmp_path):
    inst, nova = _instalada_com_manifesto(tmp_path, {"minhas_notas/a.md": "nota"})
    assert melhorias.diferencas(inst, nova)["criados"] == []  # o instalador não toca nela


def test_linha_de_comando(tmp_path):
    inst, nova = _instalada_com_manifesto(tmp_path, {"nucleo/quadro.py": "x = 9\n"})
    script = Path(melhorias.__file__)
    r = subprocess.run([sys.executable, "-I", str(script), "conferir", str(inst), str(nova)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "alterado: nucleo/quadro.py" in r.stdout
    r = subprocess.run([sys.executable, "-I", str(script), "guardar", str(inst), str(nova),
                        "2026-10-29"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0
    assert r.stdout.strip().startswith("Guardei 1 alteração sua em ")
    saida = tmp_path / "m.json"
    r = subprocess.run([sys.executable, "-I", str(script), "manifesto", str(nova), str(saida)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(saida.read_text())["versao"] == "1.4.4"


def test_main_escreve_utf8_mesmo_com_console_em_outra_pagina_de_codigo(tmp_path, monkeypatch):
    inst, nova = _instalada_com_manifesto(tmp_path, {"nucleo/quadro.py": "x = 9\n"})
    bruto = io.BytesIO()
    saida = io.TextIOWrapper(bruto, encoding="cp1252", write_through=True)
    monkeypatch.setattr(sys, "stdout", saida)
    assert melhorias.main(["conferir", str(inst), str(nova)]) == 0
    saida.flush()
    assert "alteração" in bruto.getvalue().decode("utf-8")


def test_ignora_lixo_do_sistema_operacional(tmp_path):
    raiz = _arvore(tmp_path / "r", {**ORIGINAL, "Thumbs.db": "", "desktop.ini": "",
                                    "._painel.py": "", "nucleo/._quadro.py": "",
                                    "nucleo/Thumbs.db": ""})
    assert sorted(melhorias.manifesto(raiz)) == sorted(ORIGINAL)


def test_instalacao_inexistente_nao_tem_o_que_guardar(tmp_path):
    _, nova = _instalada_com_manifesto(tmp_path)
    ausente = tmp_path / "nao-existe"
    dif = melhorias.diferencas(ausente, nova)
    assert dif["alterados"] == dif["criados"] == dif["apagados"] == []
    assert melhorias.guardar(ausente, nova, "2026-10-29")[0] is None
