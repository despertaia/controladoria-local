"""
Salvamento das peças baixadas do MNI do PJe.

Decide nome de arquivo e extensão (a partir do mimetype) e grava o conteúdo
em disco, dentro de peticoes/<numero_processo>/ (pasta ignorada pelo Git).
"""

from __future__ import annotations

import base64
import os
import re
import unicodedata
from dataclasses import dataclass

# Mapeia o tipo de conteúdo (mimetype) para a extensão de arquivo apropriada.
MIME_PARA_EXTENSAO = {
    "text/html": ".html",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/zip": ".zip",
}


@dataclass
class ResultadoSalvamento:
    id_documento: str
    descricao: str
    caminho: str | None
    tamanho_bytes: int
    erro: str | None = None


def _extensao(mimetype: str | None) -> str:
    return MIME_PARA_EXTENSAO.get((mimetype or "").lower().strip(), ".bin")


def _sanitizar(texto: str, limite: int = 60) -> str:
    """Remove acentos e caracteres problemáticos para virar nome de arquivo."""
    texto = (texto or "documento").strip()
    # Tira acentos (Decisão -> Decisao) para evitar problemas entre sistemas.
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^\w\s-]", "", texto)
    texto = re.sub(r"\s+", "_", texto).strip("_")
    return (texto[:limite] or "documento")


def _data_para_nome(valor: str) -> str:
    """AAAAMMDDhhmmss -> AAAA-MM-DD."""
    s = "".join(c for c in str(valor) if c.isdigit())
    if len(s) >= 8:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return "sem-data"


def _conteudo_em_bytes(conteudo) -> bytes | None:
    """zeep entrega o anexo MTOM como bytes; toleramos base64 por segurança."""
    if conteudo is None:
        return None
    if isinstance(conteudo, (bytes, bytearray)):
        return bytes(conteudo)
    if isinstance(conteudo, str):
        try:
            return base64.b64decode(conteudo)
        except Exception:  # noqa: BLE001
            return conteudo.encode("utf-8", "ignore")
    return None


def pasta_do_processo(
    numero_processo: str, base: str = "peticoes", subpasta: str | None = None
) -> str:
    numero = "".join(c for c in str(numero_processo) if c.isdigit())
    partes = [base, numero]
    if subpasta:
        partes.append(subpasta)
    caminho = os.path.join(*partes)
    os.makedirs(caminho, exist_ok=True)
    return caminho


def salvar_documento(pasta: str, indice: int, doc, metadados=None) -> ResultadoSalvamento:
    """
    Salva uma peça em disco.

    doc: objeto documento retornado por baixar_documentos (com 'conteudo').
    metadados: objeto documento da listagem inicial, usado como fonte de
               descricao/mimetype/data caso o download não os traga.
    """
    def campo(nome, padrao=None):
        valor = getattr(doc, nome, None)
        if valor in (None, "") and metadados is not None:
            valor = getattr(metadados, nome, None)
        return valor if valor not in (None, "") else padrao

    id_doc = str(campo("idDocumento", "sem-id"))
    descricao = str(campo("descricao", "documento"))
    mimetype = campo("mimetype")
    data = _data_para_nome(campo("dataHora", ""))

    conteudo = _conteudo_em_bytes(getattr(doc, "conteudo", None))
    if conteudo is None or len(conteudo) == 0:
        return ResultadoSalvamento(
            id_documento=id_doc,
            descricao=descricao,
            caminho=None,
            tamanho_bytes=0,
            erro="conteúdo vazio ou não retornado",
        )

    nome_arquivo = f"{indice:03d}_{_sanitizar(descricao)}_{data}{_extensao(mimetype)}"
    caminho = os.path.join(pasta, nome_arquivo)
    with open(caminho, "wb") as f:
        f.write(conteudo)

    return ResultadoSalvamento(
        id_documento=id_doc,
        descricao=descricao,
        caminho=caminho,
        tamanho_bytes=len(conteudo),
    )
