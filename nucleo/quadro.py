"""Quadro de demandas (Fase 2A): cada novidade relevante vira um cartão que anda
pelas colunas. Regras em docs/specs/2026-10-06-fase-2a-monitor-e-quadro-design.md.

Nada aqui faz commit: quem chama agrupa numa transação (`with conn:`)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta

from nucleo import config, eventos, prazos_banco, tarefas_fila

log = logging.getLogger(__name__)

JANELA_DIAS = 15
COLUNAS = {"chegou": "Chegou", "acao": "Precisa de ação",
           "autos": "Autos baixados", "acompanhar": "Só acompanhar",
           "resolvida": "Resolvido", "lex": "Lex minutando",
           "revisao": "Sua revisão", "protocolado": "Protocolado"}
COLUNAS_VISIVEIS = ("chegou", "acao", "autos", "lex", "revisao")
# (de, para) → quem pode fazer o movimento. Qualquer outro é recusado.
MOVIMENTOS = {
    ("chegou", "acao"): ("advogado",),
    ("chegou", "acompanhar"): ("advogado",),
    ("acao", "autos"): ("trabalhador",),
    ("autos", "resolvida"): ("advogado", "sistema"),
    ("acompanhar", "chegou"): ("advogado",),
    ("acao", "chegou"): ("advogado",),
    ("autos", "chegou"): ("advogado",),
    ("resolvida", "autos"): ("advogado", "sistema"),
    ("autos", "lex"): ("advogado", "sistema"),
    ("lex", "autos"): ("advogado", "mac"),
    ("lex", "revisao"): ("mac", "advogado"),  # advogado: "Desistir do ajuste"
    ("revisao", "lex"): ("advogado",),
    ("revisao", "protocolado"): ("advogado",),
    ("protocolado", "revisao"): ("advogado",),
    # "Não é meu" (processo descartado pelo advogado): o cartão da triagem vai para
    # Resolvido e sai da vista; restaurar ou confirmar o processo o traz de volta.
    ("chegou", "resolvida"): ("advogado", "sistema"),
    ("resolvida", "chegou"): ("advogado", "sistema"),
    # Arquivar ou excluir o processo (1.5.0): o sistema tira os cartões sem peça pronta e
    # os devolve ao desarquivar ou restaurar. O advogado não faz estes à mão.
    ("acao", "resolvida"): ("sistema",),
    ("acompanhar", "resolvida"): ("sistema",),
    ("resolvida", "acao"): ("sistema",),
    ("resolvida", "acompanhar"): ("sistema",),
}
# Movimento → para onde o "Desfazer" leva de volta.
DESFAZER = {("chegou", "acao"): "chegou", ("chegou", "acompanhar"): "chegou",
            ("autos", "resolvida"): "autos", ("autos", "lex"): "autos",
            ("revisao", "protocolado"): "revisao"}
AUTOS_ESTADOS = ("", "na_fila", "baixando", "ok", "falhou")
# Pasta do caso: "faltam" = o cartão espera o PDF dos autos que o advogado põe na pasta;
# "na_pasta" = o PDF apareceu. "" = não se aplica (autos baixados pelo PJe).
PASTA_ESTADOS = ("", "faltam", "na_pasta")
PASTA_MAX_ARQUIVOS = 50
PASTA_MAX_NOME = 200
# Marca, no evento do movimento, por que o cartão foi para Resolvido sem o advogado
# resolvê-lo: "Não é meu" (processo descartado) ou processo arquivado/excluído (1.5.0).
MOTIVO_NAO_E_MEU = "nao_e_meu"
MOTIVO_ARQUIVADO = "arquivado"
MOTIVO_EXCLUIDO = "excluido"
# Evento da carteira registrado logo antes de tirar os cartões com o motivo
# (carteira.arquivar / carteira.excluir).
_EVENTO_DO_MOTIVO = {MOTIVO_ARQUIVADO: "processo_arquivado", MOTIVO_EXCLUIDO: "processo_excluido"}
# Colunas de onde o cartão sai ao arquivar ou excluir o processo. "Lex minutando" impede a
# ação (lex_trabalhando); "Sua revisão" e "Protocolado" ficam (há peça pronta).
SAEM_AO_ARQUIVAR = ("chegou", "acao", "autos", "acompanhar")
# Estados do Lex em que o cartão está parado (ninguém trabalhando nele agora).
LEX_PARADO = ("na_fila", "falhou", "pausado_limite")
# Tudo o que o Lex guarda no cartão; volta ao vazio quando o cartão volta à triagem.
CAMPOS_LEX_VAZIOS = {"lex_estado": "", "lex_orientacao": "", "lex_ajuste": "", "lex_squad": "",
                     "lex_squad_nome": "", "lex_etapa": "", "lex_detalhe": "",
                     "lex_reservado_ate": None, "lex_tentar_depois": None}


class MovimentoInvalido(ValueError):
    """Movimento fora da tabela ou impossível agora (a mensagem vai para a tela)."""


MSG_PRAZOS_NAO_VOLTARAM = ("Não foi possível mover o cartão. Os prazos dele continuam como "
                           "estavam. Tente de novo.")


class PrazosNaoVoltaram(MovimentoInvalido):
    """Ao tirar o cartão de Protocolado, os prazos que ele cumpriu não voltaram: o movimento
    inteiro foi desfeito. A mensagem é fixa; o texto do erro de baixo não vai para a tela."""


def processo_elegivel(conn: sqlite3.Connection, numero: str) -> bool:
    """Gera cartão: está no banco, não está arquivado (pelo tribunal ou por você), não está
    na lixeira e não foi descartado (descartado que passou a ter a OAB do advogado volta a
    valer)."""
    p = conn.execute("SELECT arquivado, advogado_atua, descartado_em, arquivado_por_voce_em, "
                     "excluido_em FROM processo WHERE numero = ?", (numero,)).fetchone()
    if p is None or p["arquivado"] or p["arquivado_por_voce_em"] or p["excluido_em"]:
        return False
    return not (p["descartado_em"] and p["advogado_atua"] != 1)


def lex_trabalhando(conn: sqlite3.Connection, numero: str) -> bool:
    """Há cartão do processo em "Lex minutando" (na fila ou trabalhando): arquivar e
    excluir esperam o Lex terminar ou o advogado desistir do pedido."""
    return conn.execute("SELECT 1 FROM demanda WHERE numero = ? AND coluna = 'lex' LIMIT 1",
                        (numero,)).fetchone() is not None


def numeros_com_lex(conn: sqlite3.Connection) -> set[str]:
    """Os processos com cartão em "Lex minutando" (uma consulta para a lista inteira)."""
    return {r[0] for r in conn.execute("SELECT DISTINCT numero FROM demanda WHERE coluna = 'lex'")}


def publicacao_gera_cartao(conn: sqlite3.Connection, numero: str, data_iso: str,
                           hoje: date) -> bool:
    try:
        dia = date.fromisoformat(str(data_iso)[:10])
    except ValueError:
        return False
    return dia >= hoje - timedelta(days=JANELA_DIAS) and processo_elegivel(conn, numero)


def adicionar_item(conn: sqlite3.Connection, numero: str, tipo: str, ref: str,
                   data_iso: str) -> tuple[int, bool] | None:
    """Põe a novidade num cartão: junta ao cartão do processo que espera triagem
    ou abre um novo. Devolve (id do cartão, criou?); None se já estava num cartão."""
    if conn.execute("SELECT 1 FROM demanda_item WHERE tipo = ? AND ref = ?",
                    (tipo, ref)).fetchone():
        return None
    agora = eventos.agora()
    aberto = conn.execute("SELECT id FROM demanda WHERE numero = ? AND coluna = 'chegou'",
                          (numero,)).fetchone()
    if aberto:
        demanda_id, criou = aberto["id"], False
        conn.execute("UPDATE demanda SET referencia_em = MIN(referencia_em, ?), "
                     "atualizada_em = ? WHERE id = ?", (data_iso, agora, demanda_id))
        eventos.registrar(conn, "demanda_item_juntado", numero,
                          {"demanda": demanda_id, "tipo": tipo, "ref": ref})
    else:
        demanda_id = conn.execute(
            "INSERT INTO demanda (numero, coluna, referencia_em, criada_em, atualizada_em) "
            "VALUES (?, 'chegou', ?, ?, ?)", (numero, data_iso, agora, agora)).lastrowid
        criou = True
        eventos.registrar(conn, "demanda_criada", numero,
                          {"demanda": demanda_id, "tipo": tipo, "ref": ref})
    conn.execute("INSERT INTO demanda_item (demanda_id, tipo, ref, data) VALUES (?, ?, ?, ?)",
                 (demanda_id, tipo, ref, data_iso))
    prazos_banco.criar_para_item(conn, numero, demanda_id, tipo, ref, data_iso)
    return demanda_id, criou


def _demanda(conn: sqlite3.Connection, demanda_id: int) -> sqlite3.Row:
    d = conn.execute("SELECT * FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    if d is None:
        raise MovimentoInvalido("Cartão não encontrado.")
    return d


def mover(conn: sqlite3.Connection, demanda_id: int, para: str, por: str, *,
          orientacao: str = "", agora: datetime | None = None, ajuste: str = "",
          motivo: str = "") -> dict:
    """Move o cartão (ver `_mover`). Ao sair de Protocolado, o movimento e o desfazer dos
    prazos são uma coisa só: se o desfazer falhar, o cartão não se move."""
    de = conn.execute("SELECT coluna FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    if de is not None and de["coluna"] == "protocolado":
        with prazos_banco.isolado(conn):
            return _mover(conn, demanda_id, para, por, orientacao=orientacao, ajuste=ajuste,
                          agora=agora, motivo=motivo)
    return _mover(conn, demanda_id, para, por, orientacao=orientacao, ajuste=ajuste,
                  agora=agora, motivo=motivo)


def _mover(conn: sqlite3.Connection, demanda_id: int, para: str, por: str, *,
           orientacao: str = "", ajuste: str = "", agora: datetime | None = None,
           motivo: str = "") -> dict:
    """Move o cartão. A gravação é condicional ao que foi lido (coluna e, no Lex, o
    estado): se outro processo mexeu no cartão no meio do caminho (o Mac reservou, a
    reserva venceu), nada muda e o movimento é recusado com mensagem amigável."""
    from nucleo import ponte  # import tardio: ponte importa este módulo
    d = _demanda(conn, demanda_id)
    de = d["coluna"]
    estado_lido = d["lex_estado"]
    orientacao = (orientacao or "").strip()
    ajuste = (ajuste or "").strip()
    if por not in MOVIMENTOS.get((de, para), ()):
        raise MovimentoInvalido(f"Não dá para mover de “{COLUNAS.get(de, de)}” "
                                f"para “{COLUNAS.get(para, para)}”.")
    agora_iso = eventos.agora()
    campos = {"coluna": para, "atualizada_em": agora_iso}
    if para == "chegou":
        if conn.execute("SELECT 1 FROM demanda WHERE numero = ? AND coluna = 'chegou'",
                        (d["numero"],)).fetchone():
            raise MovimentoInvalido("Já há um cartão novo deste processo esperando triagem.")
        if de == "acao":
            if d["autos_estado"] == "baixando":
                raise MovimentoInvalido("Os autos já começaram a baixar; espere terminar.")
            tarefas_fila.cancelar(conn, "autos", str(demanda_id))
        # De volta à triagem: o cartão recomeça sem nada do Lex (estado, pedidos, squad).
        campos.update(autos_estado="", autos_detalhe="", pasta_estado="",
                      pasta_arquivos="[]", **CAMPOS_LEX_VAZIOS)
    if de == "acao" and para == "resolvida":
        # Processo arquivado ou excluído: a busca de autos que não começou sai da fila; a
        # que já começou termina e, vendo o cartão fora de "Precisa de ação", não mexe em
        # mais nada (autos._ainda_em_acao).
        tarefas_fila.cancelar(conn, "autos", str(demanda_id))
        if d["autos_estado"] != "baixando":
            campos.update(autos_estado="", autos_detalhe="")
    if para == "acao":
        campos.update(autos_estado="na_fila", autos_detalhe="")
        tarefas_fila.pedir(conn, "autos", str(demanda_id), por)
    modo = ""
    if para == "lex":
        if de == "autos" and d["pasta_estado"] == "faltam" and not orientacao:
            raise MovimentoInvalido(
                "Faltam os autos. Para mandar sem eles, escreva a orientação ao Lex.")
        modo = "ajuste" if de == "revisao" else "novo"
        campos.update(lex_estado="na_fila", lex_detalhe="", lex_etapa="",
                      lex_reservado_ate=None, lex_tentar_depois=None)
        if modo == "novo":
            # Peça nova do zero: sem ajuste herdado (senão a execução sairia como "ajuste").
            campos.update(lex_orientacao=orientacao, lex_ajuste="")
        else:
            if not ajuste:
                raise MovimentoInvalido("Escreva o que o Lex deve ajustar.")
            campos["lex_ajuste"] = ajuste
    if de == "lex" and para == "autos":
        if por == "advogado" and estado_lido in ("reservado", "trabalhando"):
            # Reserva vencida (ou sem data): o Mac sumiu, então o cartão pode sair do Lex.
            if not ponte.vencer_reserva(conn, d, agora or datetime.now(eventos.FUSO)):
                raise MovimentoInvalido(
                    "O Lex já está trabalhando neste cartão; espere terminar.")
            estado_lido = "na_fila"
        campos.update(lex_estado="sem_tipo" if por == "mac" else "",
                      lex_reservado_ate=None, lex_tentar_depois=None)
        if por != "mac":
            campos.update(lex_etapa="", lex_detalhe="")
    if de == "lex" and para == "revisao" and por == "advogado":
        # "Desistir do ajuste": a peça pronta anterior volta para a revisão.
        if estado_lido not in LEX_PARADO or d["lex_ajuste"] == "" or not conn.execute(
                "SELECT 1 FROM execucao_lex WHERE demanda_id = ? AND resultado = 'pronto'",
                (demanda_id,)).fetchone():
            raise MovimentoInvalido("Não há ajuste para desistir neste cartão agora.")
        campos.update(lex_estado="pronto", lex_ajuste="", lex_etapa="", lex_detalhe="",
                      lex_reservado_ate=None, lex_tentar_depois=None)
    if para == "protocolado":
        campos["protocolado_em"] = agora_iso
    if de == "protocolado":
        campos["protocolado_em"] = None
    condicao, args = "id = ? AND coluna = ?", [demanda_id, de]
    if de == "lex":
        condicao += " AND lex_estado = ?"
        args.append(estado_lido)
    cur = conn.execute(f"UPDATE demanda SET {', '.join(f'{c} = ?' for c in campos)} "
                       f"WHERE {condicao}", (*campos.values(), *args))
    if cur.rowcount != 1:
        raise MovimentoInvalido("O cartão mudou enquanto isso; confira o quadro e tente de novo.")
    dados = {"demanda": demanda_id, "de": de, "para": para, "por": por}
    if motivo:
        dados["motivo"] = motivo
    eventos.registrar(conn, "demanda_movida", d["numero"], dados)
    if para == "lex":
        eventos.registrar(conn, "lex_enfileirado", d["numero"],
                          {"demanda": demanda_id, "por": por, "modo": modo})
    resultado = {"de": de, "para": para, "desfazer": DESFAZER.get((de, para))}
    if para == "protocolado":
        eventos.registrar(conn, "protocolado", d["numero"],
                          {"demanda": demanda_id, "por": por})
        # Falha nos prazos não desfaz o movimento, mas a tela avisa: o prazo fica aberto.
        cumpridos = prazos_banco.sem_quebrar(
            conn, "marcar os prazos do cartão como cumpridos",
            lambda: prazos_banco.cumprir_do_cartao(conn, demanda_id))
        if cumpridos is None:
            resultado.update(prazos_cumpridos=0, prazos_nao_lidos=0, prazos_falha=True)
        else:
            resultado.update(prazos_cumpridos=cumpridos, prazos_nao_lidos=
                             prazos_banco.contar_sem_data_do_cartao(conn, demanda_id))
    if de == "protocolado":
        # Sem isolar nem engolir: um prazo que ficasse cumprido com o cartão fora de
        # Protocolado seria o lado errado. Se falhar, o movimento todo falha (ver `mover`).
        try:
            prazos_banco.desfazer_do_cartao(conn, demanda_id)
        except prazos_banco.TransacaoPerdida:
            raise
        except Exception as exc:
            log.warning("quadro: o desfazer dos prazos falhou (%s)", type(exc).__name__)
            raise PrazosNaoVoltaram(MSG_PRAZOS_NAO_VOLTARAM) from exc
    return resultado


def tirar_do_quadro(conn: sqlite3.Connection, numero: str, por: str = "advogado",
                    motivo: str = MOTIVO_NAO_E_MEU) -> list[int]:
    """Leva para Resolvido os cartões do processo, marcados com `motivo`. "Não é meu": só
    os que o advogado pode mandar para Resolvido (triagem e autos baixados). Arquivar ou
    excluir (MOTIVO_ARQUIVADO, MOTIVO_EXCLUIDO): os de SAEM_AO_ARQUIVAR, movidos pelo
    "sistema" (a busca de autos na fila é cancelada). Cartão no Lex, na revisão ou
    protocolado fica onde está. Devolve os ids movidos."""
    movidos = []
    for d in conn.execute("SELECT id, coluna FROM demanda WHERE numero = ? ORDER BY id",
                          (numero,)).fetchall():
        if motivo == MOTIVO_NAO_E_MEU:
            if por not in MOVIMENTOS.get((d["coluna"], "resolvida"), ()):
                continue
            quem = por
        elif d["coluna"] in SAEM_AO_ARQUIVAR:
            quem = "sistema"
        else:
            continue
        mover(conn, d["id"], "resolvida", quem, motivo=motivo)
        movidos.append(d["id"])
    return movidos


def devolver_ao_quadro(conn: sqlite3.Connection, numero: str, por: str = "advogado",
                       motivo: str = MOTIVO_NAO_E_MEU) -> list[int]:
    """Desfaz o `tirar_do_quadro` do mesmo motivo (processo restaurado, confirmado,
    desarquivado ou tirado da lixeira): cada cartão que foi para Resolvido por esse
    motivo, e não mexeu mais desde então, volta à coluna de onde saiu, se o movimento de
    volta for permitido agora (o que volta para "Precisa de ação" pede os autos de novo).
    Com os motivos novos, quem move é o "sistema" e só contam os movimentos depois do
    último arquivamento/exclusão do processo: os cartões de uma vez anterior ficaram em
    Resolvido quando ele voltou por publicação nova e não voltam agora. Do mais novo ao
    mais antigo, para o cartão novo ficar com a vaga da triagem. Devolve os ids movidos."""
    quem = por if motivo == MOTIVO_NAO_E_MEU else "sistema"
    desde = 0
    if motivo in _EVENTO_DO_MOTIVO:
        desde = conn.execute("SELECT COALESCE(MAX(id), 0) FROM evento WHERE tipo = ? "
                             "AND numero = ?", (_EVENTO_DO_MOTIVO[motivo], numero)).fetchone()[0]
    ultimo: dict[int, dict] = {}
    for e in conn.execute("SELECT dados FROM evento WHERE tipo = 'demanda_movida' "
                          "AND numero = ? AND id > ? ORDER BY id", (numero, desde)).fetchall():
        try:
            dados = json.loads(e["dados"] or "{}")
        except ValueError:
            continue
        if isinstance(dados, dict) and isinstance(dados.get("demanda"), int):
            ultimo[dados["demanda"]] = dados
    devolvidos = []
    ordem = "id" if motivo == MOTIVO_NAO_E_MEU else "id DESC"
    for d in conn.execute("SELECT id FROM demanda WHERE numero = ? AND coluna = 'resolvida' "
                          f"ORDER BY {ordem}", (numero,)).fetchall():
        dados = ultimo.get(d["id"]) or {}
        if dados.get("motivo") != motivo or dados.get("para") != "resolvida":
            continue
        try:
            mover(conn, d["id"], dados.get("de") or "chegou", quem)
        except MovimentoInvalido:
            continue  # ex.: já chegou outro cartão do processo na triagem
        devolvidos.append(d["id"])
    return sorted(devolvidos)


def marcar_autos(conn: sqlite3.Connection, demanda_id: int, estado: str,
                 detalhe: str = "") -> None:
    if estado not in AUTOS_ESTADOS:
        raise ValueError(f"estado de autos desconhecido: {estado!r}")
    conn.execute("UPDATE demanda SET autos_estado = ?, autos_detalhe = ?, atualizada_em = ? "
                 "WHERE id = ?", (estado, detalhe, eventos.agora(), demanda_id))


PREFIXO_SEM_SENHA = "sem senha do PJe do "


def detalhe_sem_senha(sigla: str) -> str:
    """Detalhe do cartão de um tribunal configurado sem a senha do PJe (o PDF na pasta
    do caso continua valendo)."""
    return (f"{PREFIXO_SEM_SENHA}{sigla}: cadastre em Configurações › Tribunais e volte o "
            "cartão à triagem, ou coloque o PDF na pasta do caso")


def marcar_faltam_autos(conn: sqlite3.Connection, demanda_id: int, detalhe: str) -> None:
    """Cartão em "Autos baixados" sem os autos: espera o PDF na pasta do caso."""
    conn.execute("UPDATE demanda SET pasta_estado = 'faltam', autos_estado = 'ok', "
                 "autos_detalhe = ?, atualizada_em = ? WHERE id = ?",
                 (detalhe, eventos.agora(), demanda_id))


def autos_na_pasta(conn: sqlite3.Connection, demanda_id: int, arquivos: list[dict]) -> bool:
    """O PDF dos autos apareceu na pasta do caso: o cartão deixa de esperar. Com o Lex
    automático ligado segue para o Lex; devolve se moveu."""
    d = _demanda(conn, demanda_id)
    if d["coluna"] != "autos" or d["pasta_estado"] != "faltam":
        raise MovimentoInvalido("Este cartão não está esperando os autos da pasta.")
    lista = [{"nome": str(a.get("nome", ""))[:PASTA_MAX_NOME], "tamanho": a.get("tamanho")}
             for a in list(arquivos)[:PASTA_MAX_ARQUIVOS]]
    conn.execute("UPDATE demanda SET pasta_estado = 'na_pasta', pasta_arquivos = ?, "
                 "atualizada_em = ? WHERE id = ?",
                 (json.dumps(lista, ensure_ascii=False), eventos.agora(), demanda_id))
    eventos.registrar(conn, "autos_na_pasta", d["numero"],
                      {"demanda": demanda_id, "arquivos": len(lista)})
    return enfileirar_automatico(conn, demanda_id)


def registrar_pasta(conn: sqlite3.Connection, demanda_id: int, pasta_nome: str) -> None:
    """Guarda o nome da pasta do caso exatamente como o Mac a criou (a rota já validou):
    é com ele que o painel local monta o caminho para abrir a pasta."""
    nome = str(pasta_nome or "")[:PASTA_MAX_NOME]
    conn.execute("UPDATE demanda SET pasta_nome = ? WHERE id = ?", (nome, demanda_id))


def autos_prontos(d: sqlite3.Row) -> bool:
    """Os autos não estão em falta: o cartão pode seguir para o Lex."""
    return d["pasta_estado"] != "faltam"


def tentar_autos_de_novo(conn: sqlite3.Connection, demanda_id: int,
                         por: str = "advogado") -> None:
    d = _demanda(conn, demanda_id)
    if d["coluna"] != "acao" or d["autos_estado"] != "falhou":
        raise MovimentoInvalido("Este cartão não tem download de autos para repetir.")
    marcar_autos(conn, demanda_id, "na_fila")
    tarefas_fila.pedir(conn, "autos", str(demanda_id), por)
    eventos.registrar(conn, "autos_pedidos_de_novo", d["numero"], {"demanda": demanda_id})


def enfileirar_automatico(conn: sqlite3.Connection, demanda_id: int) -> bool:
    """Com a chave do Lex automático ligada, manda para o Lex o cartão que acabou de
    chegar em "Autos baixados". Devolve se moveu."""
    if not config.lex_automatico(conn):
        return False
    d = conn.execute("SELECT coluna, pasta_estado, lex_estado FROM demanda WHERE id = ?",
                     (demanda_id,)).fetchone()
    if d is None or d["coluna"] != "autos" or not autos_prontos(d):
        return False
    if d["lex_estado"] == "sem_tipo":  # o Lex já devolveu este cartão: não reenvia sozinho
        return False
    mover(conn, demanda_id, "lex", "sistema")
    return True


def tentar_lex_de_novo(conn: sqlite3.Connection, demanda_id: int,
                       por: str = "advogado") -> None:
    d = _demanda(conn, demanda_id)
    if d["coluna"] != "lex" or d["lex_estado"] not in ("falhou", "pausado_limite"):
        raise MovimentoInvalido("Este cartão não tem trabalho do Lex para repetir.")
    conn.execute("UPDATE demanda SET lex_estado = 'na_fila', lex_detalhe = '', lex_etapa = '', "
                 "lex_reservado_ate = NULL, lex_tentar_depois = NULL, atualizada_em = ? "
                 "WHERE id = ?", (eventos.agora(), demanda_id))
    eventos.registrar(conn, "lex_pedido_de_novo", d["numero"],
                      {"demanda": demanda_id, "por": por})


def ja_ligado(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM evento WHERE tipo = 'quadro_ligado' LIMIT 1"
                        ).fetchone() is not None


def gerar_pendentes(conn: sqlite3.Connection, hoje: date) -> int:
    """Põe em cartão o que está no banco e ainda não tem cartão: publicações dos
    últimos JANELA_DIAS dias de processos elegíveis e intimações pendentes.
    Lê do banco (não da memória da varredura): o que uma varredura interrompida
    ou o `carteira.py atualizar` gravou vira cartão na próxima. Devolve quantos
    cartões novos abriu; idempotente pelo UNIQUE(tipo, ref) de demanda_item."""
    corte = (hoje - timedelta(days=JANELA_DIAS)).isoformat()
    criados = 0
    for p in conn.execute(
            "SELECT id, numero, data_disponibilizacao FROM publicacao p "
            "WHERE data_disponibilizacao >= ? AND NOT EXISTS (SELECT 1 FROM demanda_item i "
            "WHERE i.tipo = 'publicacao' AND i.ref = CAST(p.id AS TEXT)) "
            "ORDER BY data_disponibilizacao, id", (corte,)).fetchall():
        if processo_elegivel(conn, p["numero"]):
            r = adicionar_item(conn, p["numero"], "publicacao", str(p["id"]),
                               p["data_disponibilizacao"])
            criados += bool(r and r[1])
    for a in conn.execute(
            "SELECT instancia, id, numero, data_disponibilizacao FROM aviso a "
            "WHERE pendente = 1 AND NOT EXISTS (SELECT 1 FROM demanda_item i "
            "WHERE i.tipo = 'aviso' AND i.ref = a.instancia || ':' || a.id) "
            "ORDER BY data_disponibilizacao, id").fetchall():
        r = adicionar_item(conn, a["numero"], "aviso", f"{a['instancia']}:{a['id']}",
                           a["data_disponibilizacao"] or hoje.isoformat())
        criados += bool(r and r[1])
    return criados


def ligar(conn: sqlite3.Connection, hoje: date) -> int:
    """Primeira ligada (uma vez só): os cartões de `gerar_pendentes` e o registro
    `quadro_ligado`."""
    if ja_ligado(conn):
        return 0
    criados = gerar_pendentes(conn, hoje)
    eventos.registrar(conn, "quadro_ligado", None, {"cartoes": criados})
    return criados


def contar_na_triagem(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM demanda WHERE coluna = 'chegou'").fetchone()[0]
