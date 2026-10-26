"""Instalador e desinstalador do Mac (local/instalar.sh e desinstalar.sh) contra stubs:
uv e launchctl falsos, pastas temporárias. Não tocam no Mac de quem roda os testes."""

import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import zipfile

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="scripts do Mac (bash)")

RAIZ = pathlib.Path(__file__).resolve().parent.parent
ROTULO = "br.com.despertaia.controladoria"


@pytest.fixture
def amb(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    chamadas = tmp_path / "chamadas.txt"
    chamadas.touch()
    # uv falso: "venv" cria um python que repassa ao Python dos testes; "pip" só registra.
    uv = bin_ / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'echo "uv $*" >> "{chamadas}"\n'
        'if [ "$1" = venv ]; then\n'
        '  mkdir -p "$4/bin"\n'
        f'  printf \'#!/bin/sh\\nexec "{sys.executable}" "$@"\\n\' > "$4/bin/python"\n'
        '  chmod +x "$4/bin/python"\n'
        "fi\n")
    launchctl = bin_ / "launchctl"
    launchctl.write_text(f'#!/bin/sh\necho "launchctl $*" >> "{chamadas}"\n')
    for f in (uv, launchctl):
        f.chmod(0o755)
    area = tmp_path / "Mesa"
    area.mkdir()
    env = {**os.environ, "HOME": str(tmp_path / "home"), "UV": str(uv),
           "LAUNCHCTL": str(launchctl), "AGENTES": str(tmp_path / "agentes"),
           "AREA_DE_TRABALHO": str(area), "ESPERA_PAINEL": "0", "ESPERA_BOOTSTRAP": "0",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    return {"tmp": tmp_path, "env": env, "chamadas": chamadas, "area": area,
            "destino": tmp_path / "Controladoria", "agentes": tmp_path / "agentes"}


def _rodar(amb, script, *args):
    return subprocess.run(["bash", str(RAIZ / "local" / script), *args], env=amb["env"],
                          capture_output=True, text=True, timeout=120)


def test_scripts_tem_sintaxe_valida():
    for nome in ("instalar.sh", "desinstalar.sh"):
        subprocess.run(["bash", "-n", str(RAIZ / "local" / nome)], check=True)


def test_instala_da_origem_e_atualiza_preservando_dados(amb):
    destino = amb["destino"]
    r = _rodar(amb, "instalar.sh", "--origem", str(RAIZ), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (destino / "local" / "iniciar.py").exists()
    assert (destino / "painel.py").exists() and (destino / "dados").is_dir()
    assert not (destino / ".git").exists()
    chamadas = amb["chamadas"].read_text()
    assert "uv venv --python 3.12 .venv" in chamadas
    assert "-r requirements-local.txt" in chamadas
    assert "launchctl" not in chamadas  # sem início automático
    webloc = plistlib.loads((amb["area"] / "Controladoria.webloc").read_bytes())
    assert webloc["URL"] == "http://127.0.0.1:5056"

    # Segunda vez = atualização: dados e .env ficam; arquivo de código velho é trocado.
    (destino / "dados" / "controladoria.db").write_text("dados do advogado")
    (destino / ".env").write_text("X=1")
    (destino / "nucleo" / "sobra_velha.py").write_text("")
    (destino / "painel.py").write_text("velho")
    amb["chamadas"].write_text("")
    r = _rodar(amb, "instalar.sh", "--origem", str(RAIZ), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (destino / "dados" / "controladoria.db").read_text() == "dados do advogado"
    assert (destino / ".env").read_text() == "X=1"
    assert not (destino / "nucleo" / "sobra_velha.py").exists()
    assert (destino / "painel.py").read_text() == (RAIZ / "painel.py").read_text()
    assert "uv venv" not in amb["chamadas"].read_text()  # Python 3.12 já preparado


def test_instalador_diz_a_versao_instalada_e_a_atualizacao(amb):
    destino = amb["destino"]
    nova = (RAIZ / "VERSAO").read_text(encoding="utf-8").strip()
    r = _rodar(amb, "instalar.sh", "--origem", str(RAIZ), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Controladoria versão {nova} instalada." in r.stdout
    assert (destino / "VERSAO").read_text(encoding="utf-8").strip() == nova

    (destino / "VERSAO").write_text("1.0.0\n", encoding="utf-8")  # instalação antiga
    r = _rodar(amb, "instalar.sh", "--origem", str(RAIZ), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Controladoria atualizada da versão 1.0.0 para a {nova}." in r.stdout

    r = _rodar(amb, "instalar.sh", "--origem", str(RAIZ), "--destino", str(destino),
               "--sem-inicio-automatico")  # mesma versão de novo: reinstalação
    assert f"Controladoria versão {nova} instalada." in r.stdout


def test_instalador_do_windows_diz_a_versao_e_continua_ascii():
    texto = (RAIZ / "local" / "instalar.ps1").read_bytes()
    assert all(b < 128 for b in texto), "o .ps1 precisa ser 100% ASCII (acentos como \\uXXXX)"
    ps1 = texto.decode("ascii")
    assert "Get-VersaoDe $Destino" in ps1 and "Get-VersaoDe $fonte" in ps1
    assert ps1.count("Write-Versao $versaoAntiga $versaoNova") == 3  # sem início automático, painel no ar e painel ainda iniciando
    assert "Controladoria atualizada da vers\\u00e3o {0} para a {1}." in ps1
    assert "Controladoria vers\\u00e3o {0} instalada." in ps1


def test_instala_do_zip_e_cria_o_servico(amb):
    # zip como o do GitHub: tudo dentro de uma pasta "controladoria-local-main/"
    zipado = amb["tmp"] / "main.zip"
    with zipfile.ZipFile(zipado, "w") as z:
        for rel in ("local/__init__.py", "local/iniciar.py", "requirements-local.txt",
                    "painel.py"):
            z.write(RAIZ / rel, f"controladoria-local-main/{rel}")
    destino = amb["destino"]
    r = _rodar(amb, "instalar.sh", "--url", zipado.as_uri(), "--destino", str(destino))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (destino / "local" / "iniciar.py").exists() and (destino / "painel.py").exists()
    plist = plistlib.loads((amb["agentes"] / f"{ROTULO}.plist").read_bytes())
    assert plist["Label"] == ROTULO
    assert plist["ProgramArguments"] == [str(destino / ".venv" / "bin" / "python"), "-m",
                                         "local.iniciar", "--sem-navegador"]
    assert plist["WorkingDirectory"] == str(destino)
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    chamadas = amb["chamadas"].read_text()
    assert f"launchctl bootstrap gui/{os.getuid()}" in chamadas
    assert "Pronto!" in r.stdout
    assert "Controladoria versão" not in r.stdout  # zip sem VERSAO: não inventa versão


def test_falha_com_mensagem_clara(amb):
    r = _rodar(amb, "instalar.sh", "--origem", str(amb["tmp"]), "--destino",
               str(amb["destino"]))
    assert r.returncode != 0
    assert "não parece a Controladoria" in r.stderr


def test_desinstala_mantendo_e_apagando_dados(amb):
    destino = amb["destino"]
    assert _rodar(amb, "instalar.sh", "--origem", str(RAIZ), "--destino", str(destino)
                  ).returncode == 0
    (destino / "dados" / "controladoria.db").write_text("x")
    r = _rodar(amb, "desinstalar.sh", "--destino", str(destino))
    assert r.returncode == 0, r.stderr
    assert not (amb["agentes"] / f"{ROTULO}.plist").exists()
    assert not (amb["area"] / "Controladoria.webloc").exists()
    assert (destino / "dados" / "controladoria.db").exists()
    assert f"launchctl bootout gui/{os.getuid()}/{ROTULO}" in amb["chamadas"].read_text()

    r = _rodar(amb, "desinstalar.sh", "--destino", str(destino), "--apagar-dados")
    assert r.returncode == 0, r.stderr
    assert not destino.exists()
    assert _rodar(amb, "desinstalar.sh", "--destino", str(destino)).returncode == 0


def test_desinstalar_nao_apaga_pasta_alheia(amb):
    alheia = amb["tmp"] / "Documentos"
    alheia.mkdir()
    (alheia / "contrato.docx").write_text("x")
    r = _rodar(amb, "desinstalar.sh", "--destino", str(alheia), "--apagar-dados")
    assert r.returncode != 0 and (alheia / "contrato.docx").exists()
    shutil.rmtree(alheia)
