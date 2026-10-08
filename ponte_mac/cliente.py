"""Cliente HTTP das rotas /ponte/* do painel (só urllib)."""

import http.client
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TEMPO_PEDIDO_S = 60
TEMPO_ENVIO_S = 600  # o upload do pacote pode passar de alguns MB


class PainelErro(Exception):
    """O painel respondeu com erro HTTP (401 chave, 409 estado, 400 dado, 5xx)."""

    def __init__(self, status: int, mensagem: str = ""):
        super().__init__(f"painel respondeu {status}" + (f": {mensagem}" if mensagem else ""))
        self.status = status
        self.mensagem = mensagem


class ConexaoInterrompida(ConnectionError):
    """A conexão com o painel caiu no meio da resposta (IncompleteRead, BadStatusLine,
    RemoteDisconnected...). É um OSError: o laço trata como falta de rede."""


def _mensagem_de_erro(corpo: bytes) -> str:
    try:
        dados = json.loads(corpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    return str(dados.get("erro") or "")[:200] if isinstance(dados, dict) else ""


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """Não segue redirecionamento: o 3xx vira HTTPError (e daí PainelErro), para a chave
    nunca ser reenviada a outro endereço."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


abrir_sem_redirecionar = urllib.request.build_opener(_SemRedirecionar()).open

_LOCAIS = {"localhost", "127.0.0.1", "::1"}


def conferir_url(url: str) -> str:
    """Só https (http apenas para localhost/127.0.0.1); devolve sem a barra final."""
    partes = urllib.parse.urlsplit(str(url or "").strip())
    local = (partes.hostname or "") in _LOCAIS
    if not partes.hostname or partes.scheme not in ("https", "http") or (
            partes.scheme == "http" and not local):
        raise ValueError("o endereço do painel precisa ser https://")
    return str(url).strip().rstrip("/")


class Painel:
    def __init__(self, url: str, chave: str, abrir=abrir_sem_redirecionar):
        self.url = conferir_url(url)
        self._chave = chave
        self._abrir = abrir
        self.chave_recusada = False  # o painel respondeu 401: reler a chave das Chaves

    def trocar_chave(self, chave: str) -> None:
        """Passa a usar a chave nova (relida das Chaves depois de um 401)."""
        self._chave = chave
        self.chave_recusada = False

    def __repr__(self):  # a chave nunca aparece em log
        return f"Painel({self.url!r})"

    def _pedir(self, metodo: str, caminho: str, corpo: bytes | None = None,
               tipo: str | None = None, tempo: int = TEMPO_PEDIDO_S) -> tuple[int, bytes]:
        pedido = urllib.request.Request(self.url + caminho, data=corpo, method=metodo)
        pedido.add_header("Authorization", f"Bearer {self._chave}")
        pedido.add_header("Accept", "application/json")
        if tipo:
            pedido.add_header("Content-Type", tipo)
        try:
            try:
                with self._abrir(pedido, timeout=tempo) as resposta:
                    status = getattr(resposta, "status", None) or resposta.getcode()
                    dados = resposta.read()
            except urllib.error.HTTPError as exc:
                try:
                    dados = exc.read() or b""
                finally:
                    exc.close()
                status = exc.code
        except http.client.HTTPException as exc:
            raise ConexaoInterrompida(
                f"conexão com o painel interrompida ({type(exc).__name__})") from None
        if status == 401:
            self.chave_recusada = True
        if status >= 300:
            raise PainelErro(status, _mensagem_de_erro(dados))
        return status, dados

    def _json(self, caminho: str, corpo: dict) -> None:
        self._pedir("POST", caminho, json.dumps(corpo).encode("utf-8"), "application/json")

    def proximo(self) -> dict | None:
        status, dados = self._pedir("POST", "/ponte/proximo", b"")
        if status == 204 or not dados:
            return None
        return json.loads(dados.decode("utf-8"))

    def autos(self, demanda: int) -> bytes | None:
        status, dados = self._pedir("GET", f"/ponte/demanda/{int(demanda)}/autos",
                                    tempo=TEMPO_ENVIO_S)
        return None if status == 204 or not dados else dados

    def batida(self, demanda: int, n: int, squad: str, etapa: str, squad_nome: str = "") -> None:
        corpo = {"n": n, "squad": squad, "etapa": etapa}
        if squad_nome:
            corpo["squad_nome"] = squad_nome
        self._json(f"/ponte/demanda/{int(demanda)}/batida", corpo)

    def contato(self, claude_ok: bool) -> None:
        """Avisa que o Mac está vivo e se o acesso do Claude está bom. Servidor antigo (sem
        a rota) responde 404: ignorado."""
        try:
            self._json("/ponte/contato", {"claude_ok": "1" if claude_ok else "0"})
        except PainelErro as exc:
            if exc.status != 404:
                raise

    def falha(self, demanda: int, n: int, motivo: str, detalhe: str) -> None:
        self._json(f"/ponte/demanda/{int(demanda)}/falha",
                   {"n": n, "motivo": motivo, "detalhe": detalhe})

    def resultado(self, demanda: int, n: int, campos: dict, arquivos: dict[str, Path]) -> None:
        corpo, tipo = multipart({"n": n, **campos}, arquivos)
        self._pedir("POST", f"/ponte/demanda/{int(demanda)}/resultado", corpo, tipo,
                    tempo=TEMPO_ENVIO_S)


def _aspas(texto: str) -> str:
    return texto.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")


def multipart(campos: dict, arquivos: dict[str, Path]) -> tuple[bytes, str]:
    """Corpo multipart/form-data montado à mão: (bytes, Content-Type)."""
    fronteira = "----ponte" + uuid.uuid4().hex
    partes: list[bytes] = []
    for nome, valor in campos.items():
        partes.append(
            f'--{fronteira}\r\nContent-Disposition: form-data; name="{_aspas(nome)}"'
            f"\r\n\r\n{valor}\r\n".encode("utf-8"))
    for nome, caminho in arquivos.items():
        caminho = Path(caminho)
        tipo = mimetypes.guess_type(caminho.name)[0] or "application/octet-stream"
        partes.append(
            f'--{fronteira}\r\nContent-Disposition: form-data; name="{_aspas(nome)}"; '
            f'filename="{_aspas(caminho.name)}"\r\nContent-Type: {tipo}\r\n\r\n'.encode("utf-8"))
        partes.append(caminho.read_bytes())
        partes.append(b"\r\n")
    partes.append(f"--{fronteira}--\r\n".encode("utf-8"))
    return b"".join(partes), f"multipart/form-data; boundary={fronteira}"
