"""Melhorias do mentorado na atualização: o que ele mudou no código da Controladoria.

O instalador (instalar.sh / instalar.ps1) troca as pastas de código inteiras. Antes de
trocar, ele roda este arquivo (da versão nova) com o Python da instalação antiga, para:

- conferir: comparar a instalação com a impressão digital (sha256) da versão instalada,
  em `local/manifestos/<versão>.json`, e listar o que foi alterado, criado ou apagado;
- guardar: copiar o alterado e o criado para `melhorias-guardadas/<data>-v<versão>/`,
  com um LEIA-ME.txt.

Sem a impressão digital da versão instalada, compara com a versão nova e guarda tudo o
que difere. Só biblioteca padrão: roda antes das dependências da versão nova.

    python -I melhorias.py conferir <instalação> <versão nova>
    python -I melhorias.py guardar <instalação> <versão nova> [AAAA-MM-DD]
    python -I melhorias.py manifesto <pasta> <arquivo.json>
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

# Itens do topo que não são código da versão: o instalador nunca os troca.
PRESERVAR = frozenset({"dados", "peticoes", "cache", "grupos", ".venv", ".env", ".git",
                       ".pytest_cache", "__pycache__", "melhorias-guardadas"})
IGNORAR = frozenset({"__pycache__", ".pytest_cache", ".DS_Store", "Thumbs.db", "desktop.ini"})
MANIFESTOS = "local/manifestos"
PASTA_GUARDADAS = "melhorias-guardadas"
PEDIDO = "Reaplique as melhorias guardadas da Controladoria do Lex Lab"


def impressao(caminho: Path) -> str:
    """sha256 do arquivo com o fim de linha normalizado (CRLF vira LF)."""
    return hashlib.sha256(Path(caminho).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _arquivos(raiz: Path, nomes: set[str] | None = None):
    """Caminhos relativos (com /) dos arquivos de código sob `raiz`; com `nomes`, só nos
    itens do topo com esses nomes."""
    for item in sorted(raiz.iterdir()):
        if item.name in PRESERVAR or item.name in IGNORAR or item.name.startswith("._") or (
                nomes is not None and item.name not in nomes):
            continue
        if item.is_file():
            yield item.name
            continue
        for arquivo in sorted(item.rglob("*")):
            rel = arquivo.relative_to(raiz)
            if (arquivo.is_file() and not IGNORAR.intersection(rel.parts)
                    and not any(parte.startswith("._") for parte in rel.parts)
                    and arquivo.suffix not in (".pyc", ".pyo")
                    and not rel.as_posix().startswith(MANIFESTOS + "/")):
                yield rel.as_posix()


def manifesto(raiz: Path) -> dict[str, str]:
    raiz = Path(raiz)
    return {rel: impressao(raiz / rel) for rel in _arquivos(raiz)}


def gravar_manifesto(raiz: Path, versao: str, saida: Path) -> None:
    saida = Path(saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    dados = {"versao": versao, "arquivos": manifesto(raiz)}
    saida.write_text(json.dumps(dados, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def ler_versao(pasta: Path) -> str:
    try:
        return (Path(pasta) / "VERSAO").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _referencia(instalacao: Path, nova: Path) -> tuple[dict[str, str], str, bool]:
    """(impressões de referência, de onde vieram, é o original da versão instalada?)"""
    versao = ler_versao(instalacao)
    arquivo = nova / MANIFESTOS / f"{versao}.json"
    if versao and arquivo.is_file():
        try:
            return (json.loads(arquivo.read_text(encoding="utf-8"))["arquivos"],
                    f"versão {versao} original", True)
        except (OSError, ValueError, KeyError):
            pass
    return manifesto(nova), "versão nova (sem a impressão digital da versão instalada)", False


def diferencas(instalacao: Path, nova: Path) -> dict:
    instalacao, nova = Path(instalacao), Path(nova)
    ref, base, original = _referencia(instalacao, nova)
    if not instalacao.is_dir():  # nada instalado: nada a guardar
        return {"base": base, "alterados": [], "criados": [], "apagados": []}
    # o instalador só troca os itens do topo que vêm na versão nova
    trocados = {item.name for item in nova.iterdir() if item.name not in PRESERVAR}
    atuais = {rel: impressao(instalacao / rel) for rel in _arquivos(instalacao, trocados)}
    return {
        "base": base,
        "alterados": sorted(r for r in atuais if r in ref and atuais[r] != ref[r]),
        "criados": sorted(r for r in atuais if r not in ref),
        "apagados": (sorted(r for r in ref if r not in atuais
                            and r.split("/")[0] in trocados) if original else []),
    }


def _total(dif: dict) -> int:
    return len(dif["alterados"]) + len(dif["criados"]) + len(dif["apagados"])


def _leia_me(dif: dict, antiga: str, nova: str, hoje: str) -> str:
    partes = [f"Melhorias guardadas em {hoje}, antes de atualizar a Controladoria da "
              f"versão {antiga or 'desconhecida'} para a {nova or 'nova'}.",
              f"Comparado com: {dif['base']}.", ""]
    for chave, titulo in (("alterados", "Arquivos que você alterou (a cópia aqui é a sua):"),
                          ("criados", "Arquivos que você criou:"),
                          ("apagados", "Arquivos que você apagou (a atualização os trouxe de volta):")):
        if dif[chave]:
            partes += [titulo, *(f"  {rel}" for rel in dif[chave]), ""]
    partes += ["Para refazer as melhorias na versão nova, peça ao Claude:", f"«{PEDIDO}».", ""]
    return "\n".join(partes)


def guardar(instalacao: Path, nova: Path, hoje: str | None = None) -> tuple[Path | None, dict]:
    instalacao, nova = Path(instalacao), Path(nova)
    dif = diferencas(instalacao, nova)
    if not _total(dif):
        return None, dif
    hoje = hoje or datetime.date.today().isoformat()
    antiga = ler_versao(instalacao)
    nome = f"{hoje}-v{antiga}" if antiga else f"{hoje}-sem-versao"
    pasta, n = instalacao / PASTA_GUARDADAS / nome, 2
    while pasta.exists():
        pasta, n = instalacao / PASTA_GUARDADAS / f"{nome}-{n}", n + 1
    for rel in dif["alterados"] + dif["criados"]:
        alvo = pasta / rel
        alvo.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(instalacao / rel, alvo)
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "LEIA-ME.txt").write_text(_leia_me(dif, antiga, ler_versao(nova), hoje),
                                       encoding="utf-8")
    return pasta, dif


def _plural(n: int) -> str:
    return "1 alteração sua" if n == 1 else f"{n} alterações suas"


def main(argv: list[str] | None = None) -> int:
    # contrato: este arquivo sempre escreve UTF-8, seja qual for a página de código do console
    # (no Windows, `python -I` ignora PYTHONUTF8 e PYTHONIOENCODING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:] if argv is None else argv
    if len(args) >= 3 and args[0] == "conferir":
        dif = diferencas(Path(args[1]), Path(args[2]))
        if not _total(dif):
            print("Nenhuma alteração sua no código da Controladoria.")
            return 0
        print(f"Encontrei {_plural(_total(dif))} no código (comparado com a {dif['base']}):")
        for chave, rotulo in (("alterados", "alterado"), ("criados", "criado"),
                              ("apagados", "apagado")):
            for rel in dif[chave]:
                print(f"  {rotulo}: {rel}")
        return 0
    if len(args) >= 3 and args[0] == "guardar":
        pasta, dif = guardar(Path(args[1]), Path(args[2]), args[3] if len(args) > 3 else None)
        if pasta:
            print(f"Guardei {_plural(_total(dif))} em {pasta}.")
        return 0
    if len(args) == 3 and args[0] == "manifesto":
        gravar_manifesto(Path(args[1]), ler_versao(Path(args[1])), Path(args[2]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
