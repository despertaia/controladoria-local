"""
Cliente SOAP para o MNI (Modelo Nacional de Interoperabilidade) do PJe do
tribunal do escritório (TJMT, TJMG…; ver nucleo/tribunal.py).

Encapsula:
- Carregamento do WSDL.
- Suporte a anexos MTOM (xop:Include) — necessário para baixar peças no futuro.
- Rate limit de 1 requisição por segundo (proteção ao tribunal).
- Tradução de erros técnicos em mensagens humanas.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import requests
from requests import Session
from zeep import Client, Settings
from zeep.exceptions import Fault, TransportError
from zeep.transports import Transport

from nucleo import tribunal


def _sigla() -> str:
    return tribunal.atual().sigla


# ---------------------------------------------------------------------------
# Exceções de domínio — falamos a língua do usuário, não a do SOAP.
# ---------------------------------------------------------------------------

class MNIError(Exception):
    """Erro genérico ao falar com o MNI."""


class CredencialInvalidaError(MNIError):
    """CPF ou senha rejeitados pelo PJe."""


class ProcessoNaoEncontradoError(MNIError):
    """Número de processo não localizado ou sem acesso."""


class TimeoutTJMTError(MNIError):
    """O tribunal demorou demais para responder."""


class ServicoIndisponivelError(MNIError):
    """O webservice está fora do ar ou retornou erro de transporte."""


class OperacaoBloqueadaError(MNIError):
    """Operação que registra ciência/inicia prazo chamada sem ordem humana."""


# Abrir o teor de uma comunicação ou confirmar o recebimento REGISTRA CIÊNCIA e
# inicia o prazo legal. Só pode ser chamada com ordem expressa do advogado.
OPERACOES_QUE_REGISTRAM_CIENCIA = frozenset(
    {"consultarTeorComunicacao", "confirmarRecebimento"}
)


# ---------------------------------------------------------------------------
# Rate limiter simples: garante intervalo mínimo entre chamadas.
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, intervalo_minimo_segundos: float = 1.0) -> None:
        self._intervalo = intervalo_minimo_segundos
        self._ultima_chamada = 0.0
        self._lock = threading.Lock()

    def aguardar(self) -> None:
        with self._lock:
            agora = time.monotonic()
            decorrido = agora - self._ultima_chamada
            if decorrido < self._intervalo:
                time.sleep(self._intervalo - decorrido)
            self._ultima_chamada = time.monotonic()


# ---------------------------------------------------------------------------
# Resultado bruto do consultarProcesso (deixamos o parsing fino no main.py).
# ---------------------------------------------------------------------------

@dataclass
class RespostaConsulta:
    sucesso: bool
    mensagem: str
    processo: Any  # estrutura zeep do <processo>, pode ser None


# ---------------------------------------------------------------------------
# Cliente.
# ---------------------------------------------------------------------------

class MNIClient:
    def __init__(
        self,
        wsdl_url: str,
        cpf: str,
        senha: str,
        timeout: int = 60,
        intervalo_minimo_segundos: float = 1.0,
        endereco: str | None = None,
    ) -> None:
        """`endereco`: para onde mandar as chamadas quando o `soap:address` do WSDL
        aponta para um host que não responde (ver Tribunal.forcar_endereco). Sem
        ele, vale o endereço declarado no WSDL."""
        if not cpf or not senha:
            raise ValueError(
                "Credenciais ausentes. Preencha o CPF e a senha do PJe na Configuração "
                "(PJE_CPF e PJE_SENHA; TJMT_CPF e TJMT_SENHA também valem)."
            )

        self._cpf = cpf
        self._senha = senha
        self._rate_limiter = _RateLimiter(intervalo_minimo_segundos)

        # Sessão HTTP reutilizada (mais eficiente que abrir conexão a cada chamada).
        session = Session()
        transport = Transport(session=session, timeout=timeout, operation_timeout=timeout)

        # strict=False tolera pequenas divergências do XSD (o MNI varia entre tribunais).
        # xml_huge_tree permite respostas grandes (processos com muitos documentos).
        settings = Settings(strict=False, xml_huge_tree=True, raw_response=False)

        try:
            self._client = Client(wsdl=wsdl_url, transport=transport, settings=settings)
        except requests.exceptions.RequestException as exc:
            raise ServicoIndisponivelError(
                f"Não foi possível baixar o WSDL do {_sigla()} ({wsdl_url}). "
                f"Verifique sua conexão. Detalhe técnico: {exc}"
            ) from exc
        self._servico = None
        if endereco:
            binding = self._client.service._binding.name
            self._servico = self._client.create_service(str(binding), endereco)

    # ------------------------------------------------------------------
    # Chamada de baixo nível: rate limit + tradução de erros.
    # ------------------------------------------------------------------
    def _chamar(self, operacao: str, *, ordem_humana: bool = False, **parametros: Any):
        if operacao in OPERACOES_QUE_REGISTRAM_CIENCIA and not ordem_humana:
            raise OperacaoBloqueadaError(
                f"A operação '{operacao}' registra ciência e inicia prazo legal. "
                "Ela só pode ser executada com ordem expressa do advogado."
            )
        self._rate_limiter.aguardar()
        try:
            servico = getattr(self, "_servico", None) or self._client.service
            return getattr(servico, operacao)(**parametros)
        except Fault as exc:
            # SOAP Fault — o servidor respondeu, mas com erro estruturado.
            raise _traduzir_fault(exc) from exc
        except requests.exceptions.Timeout as exc:
            raise TimeoutTJMTError(
                f"O {_sigla()} não respondeu dentro do tempo limite. "
                "Tente novamente em alguns instantes."
            ) from exc
        except (TransportError, requests.exceptions.ConnectionError) as exc:
            raise ServicoIndisponivelError(
                f"Falha de comunicação com o {_sigla()}. Detalhe técnico: {exc}"
            ) from exc

    def _executar_consulta(self, **parametros: Any):
        return self._chamar(
            "consultarProcesso",
            idConsultante=self._cpf,
            senhaConsultante=self._senha,
            **parametros,
        )

    # ------------------------------------------------------------------
    # Avisos pendentes (intimações/citações aguardando ciência).
    # ------------------------------------------------------------------
    def consultar_avisos_pendentes(self) -> list:
        """Lista os avisos de comunicação pendentes do consultante.

        Só lista: NÃO abre o teor (consultarTeorComunicacao), portanto não
        registra ciência nem inicia prazo.
        """
        resposta = self._chamar(
            "consultarAvisosPendentes",
            idConsultante=self._cpf,
            senhaConsultante=self._senha,
        )
        mensagem = str(getattr(resposta, "mensagem", "") or "")
        if not bool(getattr(resposta, "sucesso", False)):
            _interpretar_falha_logica(mensagem)
        return list(getattr(resposta, "aviso", None) or [])

    # ------------------------------------------------------------------
    # Operação 1: listar dados do processo + metadados dos documentos.
    # ------------------------------------------------------------------
    def consultar_processo(
        self, numero_processo: str, incluir_movimentos: bool = False
    ) -> RespostaConsulta:
        """
        Lista dados básicos, partes e a lista de documentos (sem conteúdo).

        incluir_movimentos=True traz também os andamentos (em processo.movimento).
        numero_processo: 20 dígitos (com ou sem máscara NNNNNNN-DD.AAAA.J.TR.OOOO).
        """
        numero_limpo = _normalizar_numero_processo(numero_processo)
        resposta = self._executar_consulta(
            numeroProcesso=numero_limpo,
            incluirCabecalho=True,
            incluirDocumentos=True,
            movimentos=incluir_movimentos,
        )

        sucesso = bool(getattr(resposta, "sucesso", False))
        mensagem = str(getattr(resposta, "mensagem", "") or "")
        processo = getattr(resposta, "processo", None)

        if not sucesso:
            _interpretar_falha_logica(mensagem)

        return RespostaConsulta(sucesso=sucesso, mensagem=mensagem, processo=processo)

    # ------------------------------------------------------------------
    # Operação 2: baixar o conteúdo de documentos específicos por ID.
    # ------------------------------------------------------------------
    def baixar_documentos(self, numero_processo: str, ids_documento: list[str]) -> list:
        """
        Baixa o conteúdo (anexo MTOM) das peças cujos idDocumento forem informados.

        Retorna a lista de objetos 'documento', cada um com o campo 'conteudo'
        preenchido (bytes). Faz UMA requisição para o lote de IDs informado.
        """
        if not ids_documento:
            return []

        numero_limpo = _normalizar_numero_processo(numero_processo)
        resposta = self._executar_consulta(
            numeroProcesso=numero_limpo,
            incluirCabecalho=False,
            incluirDocumentos=False,
            documento=[str(i) for i in ids_documento],
        )

        sucesso = bool(getattr(resposta, "sucesso", False))
        mensagem = str(getattr(resposta, "mensagem", "") or "")
        if not sucesso:
            _interpretar_falha_logica(mensagem)

        processo = getattr(resposta, "processo", None)
        if processo is None:
            return []
        return getattr(processo, "documento", None) or []


# ---------------------------------------------------------------------------
# Funções auxiliares.
# ---------------------------------------------------------------------------

def _normalizar_numero_processo(numero: str) -> str:
    """Remove pontuação e espaços; valida que sobraram 20 dígitos."""
    apenas_digitos = "".join(c for c in numero if c.isdigit())
    if len(apenas_digitos) != 20:
        raise ValueError(
            f"Número de processo inválido: esperava 20 dígitos, recebi {len(apenas_digitos)}. "
            f"Use o formato CNJ (ex.: 0000000-00.0000.0.00.0000)."
        )
    return apenas_digitos


def _traduzir_fault(fault: Fault) -> MNIError:
    """Transforma um SOAP Fault genérico em uma exceção de domínio mais clara."""
    msg = (fault.message or "").lower()
    if any(t in msg for t in ("senha", "credencia", "autentic", "não autoriz", "nao autoriz",
                              "login", "postauthenticate", "post authenticate")):
        return CredencialInvalidaError(
            "Credenciais rejeitadas pelo PJe (CPF ou senha incorretos, "
            "ou conta sem acesso ao processo)."
        )
    if any(t in msg for t in ("não encontrad", "nao encontrad", "inexisten")):
        return ProcessoNaoEncontradoError(
            "Processo não encontrado ou você não tem acesso a ele."
        )
    return MNIError(f"Erro retornado pelo {_sigla()}: {fault.message}")


def _interpretar_falha_logica(mensagem: str) -> None:
    """
    O MNI às vezes responde HTTP 200 com sucesso=False e uma mensagem no corpo.
    Tentamos classificar para erguer a exceção certa.
    """
    msg_baixa = mensagem.lower()
    if any(t in msg_baixa for t in ("senha", "credencia", "autentic", "não autoriz", "nao autoriz",
                                    "login", "postauthenticate", "post authenticate")):
        raise CredencialInvalidaError(
            "Falha no login do PJe/MNI. Verifique se o CPF e a senha na Configuração estão "
            "corretos e se a senha do PJe não expirou (o PJe exige troca periódica). "
            f"Mensagem do tribunal: {mensagem}"
        )
    if any(t in msg_baixa for t in ("não encontrad", "nao encontrad", "inexisten", "não localiz", "nao localiz")):
        raise ProcessoNaoEncontradoError(
            f"Processo não encontrado. Mensagem do tribunal: {mensagem}"
        )
    raise MNIError(f"O {_sigla()} recusou a consulta. Mensagem: {mensagem}")
