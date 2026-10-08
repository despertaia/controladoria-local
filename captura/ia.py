"""
Camada de IA: gera um resumo do processo a partir dos andamentos, usando Claude.

Privacidade: enviamos APENAS os andamentos (texto) e dados básicos — nunca o
conteúdo das peças. Os andamentos contêm nomes de partes; isso é o mínimo
necessário para o resumo fazer sentido.

Custo: o system prompt leva cache_control (prompt caching). O resumo é gravado
no cache do processo, então só gera de novo quando você pedir.
"""

from __future__ import annotations

import anthropic

MODELO_PADRAO = "claude-opus-4-7"

# Limite de andamentos enviados, para controlar o tamanho (e o custo) do pedido.
MAX_ANDAMENTOS = 80

SISTEMA = """Você é um assistente jurídico que ajuda advogados a entender rapidamente \
a situação de um processo judicial a partir da lista de andamentos (movimentações).

Escreva em português do Brasil, de forma objetiva e profissional. Não invente \
informações que não estejam nos andamentos. Se algo não estiver claro, diga que \
não é possível determinar pelo andamento.

Estruture a resposta em markdown, com exatamente estas três seções:

## Situação atual
Uma a três frases sobre em que pé está o processo hoje (fase, última decisão relevante, \
se está concluso, arquivado, com recurso pendente, transitado em julgado, etc.).

## O que andou recentemente
Três a cinco marcadores com os movimentos mais relevantes recentes (ignore movimentos \
puramente burocráticos repetitivos).

## Próximos passos e alertas
Marcadores com o que tende a acontecer ou exige atenção (prazos correndo, audiência \
marcada, necessidade de manifestação, risco de prescrição/arquivamento). Se houver \
trânsito em julgado ou arquivamento, destaque isso claramente."""


class IAError(Exception):
    """Erro ao gerar o resumo com a IA."""


def _montar_conteudo(dados: dict) -> str:
    linhas = [
        f"Processo: {dados.get('numero_formatado', '')}",
        f"Órgão julgador: {dados.get('orgao_julgador', '')}",
        f"Classe (código CNJ): {dados.get('classe', '')}",
        f"Valor da causa: {dados.get('valor_causa', '')}",
    ]
    partes = dados.get("partes") or []
    if partes:
        linhas.append("Partes:")
        for p in partes:
            linhas.append(f"  - {p.get('polo', '')}: {', '.join(p.get('nomes', []))}")

    linhas.append("")
    linhas.append("Andamentos (do mais recente para o mais antigo):")
    for a in (dados.get("andamentos") or [])[:MAX_ANDAMENTOS]:
        linhas.append(f"- {a.get('data', '')} — {a.get('texto', '')}")

    return "\n".join(linhas)


def resumir_processo(dados: dict, api_key: str, modelo: str = MODELO_PADRAO) -> str:
    if not api_key:
        raise IAError("Chave de API da IA ausente. Preencha LLM_API_KEY no .env.")

    client = anthropic.Anthropic(api_key=api_key)
    conteudo = _montar_conteudo(dados)

    try:
        resposta = client.messages.create(
            model=modelo,
            max_tokens=1500,
            system=[
                {
                    "type": "text",
                    "text": SISTEMA,
                    # Prompt caching: o system é estável entre processos.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": conteudo}],
        )
    except anthropic.AuthenticationError as exc:
        raise IAError("Chave de API rejeitada. Verifique LLM_API_KEY no .env.") from exc
    except anthropic.RateLimitError as exc:
        raise IAError("Limite de uso da API atingido. Tente novamente em instantes.") from exc
    except anthropic.NotFoundError as exc:
        raise IAError(f"Modelo '{modelo}' indisponível para esta chave.") from exc
    except anthropic.APIError as exc:
        raise IAError(f"Erro ao falar com a IA: {exc}") from exc

    texto = "".join(b.text for b in resposta.content if b.type == "text").strip()
    if not texto:
        raise IAError("A IA não retornou texto.")
    return texto
