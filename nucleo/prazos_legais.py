"""Prazos legais comuns: a lista fechada da 1.6.0. Junto com a tabela do calendário, é o
único lugar com número de lei no código. Copiada à mão de
docs/fontes/2026-11-01-prazos-legais.md, lida nas fontes oficiais; um teste compara linha a
linha. O Daniel confere a lista antes de publicar (spec, seção 8)."""

from __future__ import annotations

from dataclasses import dataclass

from nucleo.prazos import CORRIDOS, UTEIS


@dataclass(frozen=True)
class PrazoLegal:
    codigo: str
    ato: str
    dias: int
    unidade: str
    artigo: str
    ressalva: str = ""


_R_219 = ("CPC 219, parágrafo único, limita os dias úteis a prazos processuais; o artigo não "
          "classifica este: conferir")
_LISTA = (
    PrazoLegal("contestacao", "Contestação", 15, UTEIS, "CPC 335",
               "Termo inicial varia (audiência, cancelamento, art. 231): conferir"),
    PrazoLegal("replica_fato_impeditivo", "Réplica a fato impeditivo, modificativo ou extintivo",
               15, UTEIS, "CPC 350"),
    PrazoLegal("replica_preliminar", "Réplica às matérias do art. 337", 15, UTEIS, "CPC 351"),
    PrazoLegal("emenda_inicial", "Emenda à inicial", 15, UTEIS, "CPC 321"),
    PrazoLegal("manifestacao_documentos", "Manifestação sobre documentos", 15, UTEIS,
               "CPC 437, § 1º"),
    PrazoLegal("ed_cpc", "Embargos de declaração", 5, UTEIS, "CPC 1.023"),
    PrazoLegal("apelacao", "Apelação", 15, UTEIS, "CPC 1.003, § 5º"),
    PrazoLegal("contrarrazoes_apelacao", "Contrarrazões à apelação", 15, UTEIS, "CPC 1.010, § 1º"),
    PrazoLegal("agravo_instrumento", "Agravo de instrumento", 15, UTEIS, "CPC 1.003, § 5º"),
    PrazoLegal("resposta_agravo_instrumento", "Resposta ao agravo de instrumento", 15, UTEIS,
               "CPC 1.019, II"),
    PrazoLegal("agravo_interno", "Agravo interno", 15, UTEIS, "CPC 1.003, § 5º, e 1.021"),
    PrazoLegal("resposta_agravo_interno", "Resposta ao agravo interno", 15, UTEIS,
               "CPC 1.021, § 2º"),
    PrazoLegal("resp_re", "Recurso especial ou extraordinário", 15, UTEIS, "CPC 1.003, § 5º"),
    PrazoLegal("contrarrazoes_resp_re", "Contrarrazões a RE ou REsp", 15, UTEIS, "CPC 1.030"),
    PrazoLegal("pagamento_cumprimento", "Pagamento no cumprimento de sentença", 15, UTEIS,
               "CPC 523", _R_219),
    PrazoLegal("impugnacao_cumprimento", "Impugnação ao cumprimento", 15, UTEIS, "CPC 525",
               "Conta do fim do prazo do art. 523 sem pagamento, não da publicação: conferir"),
    PrazoLegal("embargos_execucao", "Embargos à execução", 15, UTEIS, "CPC 915",
               "Na forma do art. 231: conferir o termo inicial"),
    PrazoLegal("pagamento_execucao", "Pagamento na execução por quantia", 3, UTEIS, "CPC 829",
               "Mesma ressalva do art. 219; conta da citação: conferir"),
    PrazoLegal("recurso_inominado", "Recurso inominado, Juizados", 10, UTEIS, "Lei 9.099, art. 42",
               "Preparo em 48 horas da interposição (§ 1º)"),
    PrazoLegal("resposta_recurso_inominado", "Resposta ao recurso inominado", 10, UTEIS,
               "Lei 9.099, art. 42, § 2º"),
    PrazoLegal("ed_juizados", "Embargos de declaração, Juizados Cíveis", 5, UTEIS,
               "Lei 9.099, art. 49"),
    PrazoLegal("recurso_ordinario_vara", "Recurso ordinário contra decisão de Vara", 8, UTEIS,
               "CLT 895, I; Lei 5.584, art. 6º"),
    PrazoLegal("recurso_ordinario_trt", "Recurso ordinário contra decisão de TRT", 8, UTEIS,
               "CLT 895, II"),
    PrazoLegal("agravo_peticao", "Agravo de petição", 8, UTEIS, 'CLT 897, "a"'),
    PrazoLegal("agravo_instrumento_trab", "Agravo de instrumento trabalhista", 8, UTEIS,
               'CLT 897, "b"'),
    PrazoLegal("ed_trabalhista", "Embargos de declaração trabalhistas", 5, UTEIS, "CLT 897-A"),
    PrazoLegal("contrarrazoes_trabalhista", "Contrarrazões trabalhistas", 8, UTEIS,
               "Lei 5.584, art. 6º; CLT 900"),
    PrazoLegal("resposta_acusacao", "Resposta à acusação", 10, CORRIDOS, "CPP 396",
               "Citação por edital: conta do comparecimento: conferir"),
    PrazoLegal("apelacao_penal", "Apelação criminal", 5, CORRIDOS, "CPP 593"),
    PrazoLegal("razoes_apelacao_penal", "Razões de apelação criminal", 8, CORRIDOS, "CPP 600",
               "3 dias em contravenção: conferir"),
    PrazoLegal("rese", "Recurso em sentido estrito", 5, CORRIDOS, "CPP 586",
               "20 dias no caso do art. 581, XIV"),
    PrazoLegal("ed_sentenca_penal", "Embargos de declaração de sentença penal", 2, CORRIDOS,
               "CPP 382"),
    PrazoLegal("ed_acordao_penal", "Embargos de declaração de acórdão penal", 2, CORRIDOS,
               "CPP 619"),
)
PRAZOS: dict[str, PrazoLegal] = {p.codigo: p for p in _LISTA}

# Prazo sem número na decisão nem na lei (A9, CPC 218, § 3º): calcula, com ressalva.
SEM_NUMERO = "sem_numero"
DIAS_SEM_NUMERO = 5
RESSALVA_SEM_NUMERO = ("A decisão não diz o número de dias. Usados os 5 dias do CPC 218, § 3º: "
                       "conferir")
# Ciência que acontece sozinha no PJe: dias corridos do envio (B2, Lei 11.419, art. 5º, § 3º).
DIAS_CIENCIA_TACITA = 10
