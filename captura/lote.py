"""
Download em lote: baixa o processo na íntegra (todas as peças) de vários
processos de uma vez, salvando cada um em peticoes/<numero>/.

Reaproveita a mesma lógica do baixar.py, mas exposta como um gerador que
emite eventos de progresso — assim o painel mostra o andamento em tempo real
em vez de travar a tela durante um download demorado.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Iterator

from captura import cache
from captura.dossie import gerar_dossie_html
from captura.downloader import pasta_do_processo, salvar_documento
from captura.mni_client import MNIClient, MNIError, ProcessoNaoEncontradoError
from captura.processo_parser import processo_para_dict


def formatar_tamanho(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def numeros_da_lista(texto: str) -> list[str]:
    """Extrai números CNJ (20 dígitos) de um texto colado livremente.

    Aceita um número por linha, separados por vírgula/ponto-e-vírgula, com ou
    sem máscara. Ignora trechos que não tenham exatamente 20 dígitos e remove
    duplicatas preservando a ordem.
    """
    numeros: list[str] = []
    vistos: set[str] = set()
    for bruto in re.split(r"[\n,;]+", texto or ""):
        digitos = "".join(c for c in bruto if c.isdigit())
        if len(digitos) == 20 and digitos not in vistos:
            vistos.add(digitos)
            numeros.append(digitos)
    return numeros


def _lotes(lista, tamanho):
    for i in range(0, len(lista), tamanho):
        yield lista[i:i + tamanho]


def baixar_processo_completo(
    cliente: MNIClient, numero: str, tamanho_lote: int = 5,
    subpasta: str | None = None, pular_existentes: bool = False,
) -> Iterator[dict]:
    """Baixa TODAS as peças de um processo, emitindo eventos de progresso.

    subpasta: se informado, salva em peticoes/<numero>/<subpasta>/ (usado para
    separar 1ª e 2ª instância). Caso contrário, salva em peticoes/<numero>/.
    pular_existentes: se True, não rebaixa peças cujo arquivo já existe em disco
    (torna o download retomável após interrupção).

    Efeito colateral: ao baixar a 1ª instância (ou sem subpasta), grava o cache
    do processo (andamentos/partes/advogados) para alimentar lista e planilha.

    Eventos (dicionários com a chave 'evento'):
      - {"evento": "processo_ausente", "numero"}   # não consta nesta instância
      - {"evento": "erro_processo", "numero", "msg"}
      - {"evento": "processo_inicio", "numero", "total"}
      - {"evento": "peca", "indice", "total", "nome", "tamanho"|"erro"}
      - {"evento": "processo_fim", "numero", "ok", "falhas", "pulados", "bytes", "pasta"}
    """
    try:
        # incluir_movimentos=True: precisamos dos andamentos para o dossiê.
        resp = cliente.consultar_processo(numero, incluir_movimentos=True)
    except ProcessoNaoEncontradoError:
        yield {"evento": "processo_ausente", "numero": numero}
        return
    except (MNIError, ValueError) as exc:
        yield {"evento": "erro_processo", "numero": numero, "msg": str(exc)}
        return

    docs = getattr(resp.processo, "documento", None) or []
    total = len(docs)
    yield {"evento": "processo_inicio", "numero": numero, "total": total}

    pasta = pasta_do_processo(numero, subpasta=subpasta)

    # Dossiê HTML (partes + advogados + linha do tempo de andamentos), salvo
    # mesmo que o processo não tenha peças — é o que permite avaliar prazos.
    dados = processo_para_dict(numero, resp.processo)
    try:
        with open(os.path.join(pasta, "000_DOSSIE.html"), "w", encoding="utf-8") as f:
            f.write(gerar_dossie_html(dados, instancia=subpasta))
        yield {"evento": "dossie", "andamentos": len(dados.get("andamentos", []))}
    except OSError as exc:
        yield {"evento": "dossie_erro", "msg": str(exc)}

    # Cache (visão do painel) = 1ª instância apenas, para não sobrescrever com a 2ª.
    if subpasta in (None, "1grau"):
        try:
            cache.gravar(dados)
        except OSError:
            pass

    if total == 0:
        yield {"evento": "processo_fim", "numero": numero, "ok": 0,
               "falhas": 0, "pulados": 0, "bytes": 0, "pasta": pasta}
        return

    # Pares (índice posicional, doc); o índice vira o prefixo do nome do arquivo.
    indexados = list(enumerate(docs, start=1))
    pulados = 0
    if pular_existentes:
        pendentes = []
        for indice, doc_meta in indexados:
            if glob.glob(os.path.join(pasta, f"{indice:03d}_*")):
                pulados += 1
            else:
                pendentes.append((indice, doc_meta))
        if pulados:
            yield {"evento": "pulados", "n": pulados, "total": total}
    else:
        pendentes = indexados

    ok = falhas = bytes_totais = 0
    for lote in _lotes(pendentes, tamanho_lote):
        ids = [str(getattr(dm, "idDocumento", "")) for _i, dm in lote]
        try:
            baixados = cliente.baixar_documentos(numero, ids)
        except (MNIError, ValueError) as exc:
            for indice, doc_meta in lote:
                falhas += 1
                yield {"evento": "peca", "indice": indice, "total": total,
                       "nome": str(getattr(doc_meta, "descricao", "documento")),
                       "erro": str(exc)}
            continue

        baixados_por_id = {str(getattr(d, "idDocumento", "")): d for d in baixados}
        for indice, doc_meta in lote:
            id_d = str(getattr(doc_meta, "idDocumento", ""))
            descricao = str(getattr(doc_meta, "descricao", "documento"))
            doc_baixado = baixados_por_id.get(id_d)

            if doc_baixado is None:
                falhas += 1
                yield {"evento": "peca", "indice": indice, "total": total,
                       "nome": descricao, "erro": "não retornou"}
                continue

            r = salvar_documento(pasta, indice, doc_baixado, metadados=doc_meta)
            if r.erro:
                falhas += 1
                yield {"evento": "peca", "indice": indice, "total": total,
                       "nome": descricao, "erro": r.erro}
            else:
                ok += 1
                bytes_totais += r.tamanho_bytes
                yield {"evento": "peca", "indice": indice, "total": total,
                       "nome": os.path.basename(r.caminho),
                       "tamanho": r.tamanho_bytes}

    yield {"evento": "processo_fim", "numero": numero, "ok": ok, "falhas": falhas,
           "pulados": pulados, "bytes": bytes_totais, "pasta": pasta}
