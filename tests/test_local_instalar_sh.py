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
    assert "[switch]$SoConferir" in ps1 and "CONTROLADORIA_SO_CONFERIR" in ps1
    assert "melhorias.py" in ps1 and "'melhorias-guardadas'" in ps1
    # fim sem início automático, painel ainda iniciando, painel no ar e o catch da falha
    assert ps1.count("Write-Guardado $script:Guardado") == 4
    assert "$guardado" not in ps1  # só a variável do script: o catch final a enxerga
    assert ps1.index("Write-Guardado $script:Guardado", ps1.index("} catch {\n    Write-Host ''")) \
        > ps1.index("Detalhe: {0}")


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


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    if not (RAIZ / "scripts" / "montar_dist.py").exists():
        pytest.skip("distribuição: só no repositório privado")
    from scripts import montar_dist
    destino = tmp_path_factory.mktemp("dist") / "controladoria-local"
    montar_dist.montar(destino)
    return destino


def test_atualizar_guarda_as_melhorias_do_mentorado(amb, dist):
    destino = amb["destino"]
    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    (destino / "nucleo" / "quadro.py").write_text(
        (destino / "nucleo" / "quadro.py").read_text() + "# melhoria do mentorado\n")
    (destino / "nucleo" / "minha_melhoria.py").write_text("x = 1\n")

    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--so-conferir")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "alterado: nucleo/quadro.py" in r.stdout and "criado: nucleo/minha_melhoria.py" in r.stdout
    assert "Encontrei 2 alterações suas" in r.stdout
    assert (destino / "nucleo" / "minha_melhoria.py").exists()  # conferir não muda nada

    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    ultima = r.stdout.strip().splitlines()[-1]
    assert "Reaplique as melhorias guardadas" in ultima
    assert "Guardei 2 alterações suas em" in r.stdout
    guardadas = list((destino / "melhorias-guardadas").iterdir())
    assert len(guardadas) == 1
    assert (guardadas[0] / "nucleo" / "minha_melhoria.py").exists()
    assert "# melhoria do mentorado" in (guardadas[0] / "nucleo" / "quadro.py").read_text()
    assert (destino / "nucleo" / "quadro.py").read_text() == (dist / "nucleo" / "quadro.py").read_text()

    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--sem-inicio-automatico")  # sem alteração nova: não guarda de novo
    assert "Guardei" not in r.stdout
    assert len(list((destino / "melhorias-guardadas").iterdir())) == 1


def test_atualizacao_que_falha_ainda_avisa_a_copia_guardada(amb, dist):
    destino = amb["destino"]
    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    (destino / "nucleo" / "minha_melhoria.py").write_text("x = 1\n")
    # uv que quebra ao instalar as dependências: a falha vem depois de guardar.
    uv = pathlib.Path(amb["env"]["UV"])
    uv.write_text(uv.read_text().replace("#!/bin/sh\n",
                                         '#!/bin/sh\n[ "$1" = pip ] && exit 1\n', 1))
    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode != 0
    saida = r.stdout + r.stderr
    assert "instalar as dependências" in saida
    assert saida.count("Guardei 1 alteração sua em") == 1, saida
    assert saida.count("Reaplique as melhorias guardadas da Controladoria do Lex Lab") == 1
    assert len(list((destino / "melhorias-guardadas").iterdir())) == 1


def test_sem_python_da_instalacao_guarda_todo_o_codigo(amb, dist):
    destino = amb["destino"]
    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    (destino / ".venv" / "bin" / "python").unlink()
    r = _rodar(amb, "instalar.sh", "--origem", str(dist), "--destino", str(destino),
               "--sem-inicio-automatico")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "guardei uma cópia de todo o código antigo" in r.stdout
    copia = next((destino / "melhorias-guardadas").iterdir())
    assert (copia / "nucleo" / "quadro.py").exists() and not (copia / "dados").exists()


def test_so_conferir_sem_instalacao(amb):
    r = _rodar(amb, "instalar.sh", "--origem", str(RAIZ), "--destino", str(amb["destino"]),
               "--so-conferir")
    assert r.returncode == 0 and "ainda não está instalada" in r.stdout
    assert not (amb["destino"] / "painel.py").exists()
