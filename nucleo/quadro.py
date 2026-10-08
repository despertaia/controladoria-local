"""Quadro de demandas (Fase 2A): cada novidade relevante vira um cartão que anda
pelas colunas. Regras em docs/specs/2026-10-06-fase-2a-monitor-e-quadro-design.md.

Nada aqui faz commit: quem chama agrupa numa transação (`with conn:`)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from nucleo import config, eventos, tarefas_fila

JANELA_DIAS = 15
COLUNAS = {"chegou": "Chegou do PJe", "acao": "Precisa de ação",
           "autos": "Autos baixados", "acompanhar": "Só acompanhar",
           "resolvida": "Resolvido", "lex": "Lex minutando",
           "revisao": "Sua revisão", "protocolado": "Protocolado"}
COLUNAS_VISIVEIS = ("chegou", "acao", "autos", "lex", "revisao")
# (de, para) → quem pode fazer o movimento. Qualquer outro é recusado.
MOVIMENTOS = {
    ("chegou", "acao"): ("advogado",),
    ("chegou", "acompanhar"): ("advogado",),
    ("acao", "autos"): ("trabalhador",),
    ("autos", "resolvida"): ("advogado",),
    ("acompanhar", "chegou"): ("advogado",),
    ("acao", "chegou"): ("advogado",),
    ("autos", "chegou"): ("advogado",),
    ("resolvida", "autos"): ("advogado",),
    ("autos", "lex"): ("advogado", "sistema"),
    ("lex", "autos"): ("advogado", "mac"),
    ("lex", "revisao"): ("mac", "advogado"),  # advogado: "Desistir do ajuste"
    ("revisao", "lex"): ("advogado",),
    ("revisao", "protocolado"): ("advogado",),
    ("protocolado", "revisao"): ("advogado",),
    # "Não é meu" (processo descartado pelo advogado): o cartão da triagem vai para
    # Resolvido e sai da vista; restaurar ou confirmar o processo o traz de volta.
    ("chegou", "resolvida"): ("advogado",),
    ("resolvida", "chegou"): ("advogado",),
}
# Movimento → para onde o "Desfazer" leva de volta.
DESFAZER = {("chegou", "acao"): "chegou", ("chegou", "acompanhar"): "chegou",
            ("autos", "resolvida"): "autos", ("autos", "lex"): "autos",
            ("revisao", "protocolado"): "revisao"}
AUTOS_ESTADOS = ("", "na_fila", "baixando", "ok", "falhou")
# Marca, no evento do movimento, o cartão que foi para Resolvido pelo "Não é meu".
MOTIVO_NAO_E_MEU = "nao_e_meu"
# Estados do Lex em que o cartão está parado (ninguém trabalhando nele agora).
LEX_PARADO = ("na_fila", "falhou", "pausado_limite")
# Tudo o que o Lex guarda no cartão; volta ao vazio quando o cartão volta à triagem.
CAMPOS_LEX_VAZIOS = {"lex_estado": "", "lex_orientacao": "", "lex_ajuste": "", "lex_squad": "",
                     "lex_squad_nome": "", "lex_etapa": "", "lex_detalhe": "",
                     "lex_reservado_ate": None, "lex_tentar_depois": None}


class MovimentoInvalido(ValueError):
    """Movimento fora da tabela ou impossível agora (a mensagem vai para a tela)."""


def processo_elegivel(conn: sqlite3.Connection, numero: str) -> bool:
    """Gera cartão: está na carteira, não está arquivado e não foi descartado
    (descartado que passou a ter a OAB do advogado volta a valer)."""
    p = conn.execute("SELECT arquivado, advogado_atua, descartado_em FROM processo "
                     "WHERE numero = ?", (numero,)).fetchone()
    if p is None or p["arquivado"]:
        return False
    return not (p["descartado_em"] and p["advogado_atua"] != 1)


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
    return demanda_id, criou


def _demanda(conn: sqlite3.Connection, demanda_id: int) -> sqlite3.Row:
    d = conn.execute("SELECT * FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    if d is None:
        raise MovimentoInvalido("Cartão não encontrado.")
    return d


def mover(conn: sqlite3.Connection, demanda_id: int, para: str, por: str, *,
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
        campos.update(autos_estado="", autos_detalhe="", **CAMPOS_LEX_VAZIOS)
    if para == "acao":
        campos.update(autos_estado="na_fila", autos_detalhe="")
        tarefas_fila.pedir(conn, "autos", str(demanda_id), por)
    modo = ""
    if para == "lex":
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
    if para == "protocolado":
        eventos.registrar(conn, "protocolado", d["numero"],
                          {"demanda": demanda_id, "por": por})
    return {"de": de, "para": para, "desfazer": DESFAZER.get((de, para))}


def tirar_do_quadro(conn: sqlite3.Connection, numero: str, por: str = "advogado") -> list[int]:
    """"Não é meu": os cartões do processo que podem ir para Resolvido (triagem e autos
    baixados) vão, marcados com MOTIVO_NAO_E_MEU. Cartão no Lex, na revisão ou
    protocolado fica onde está (há trabalho em curso). Devolve os ids movidos."""
    movidos = []
    for d in conn.execute("SELECT id, coluna FROM demanda WHERE numero = ? ORDER BY id",
                          (numero,)).fetchall():
        if por in MOVIMENTOS.get((d["coluna"], "resolvida"), ()):
            mover(conn, d["id"], "resolvida", por, motivo=MOTIVO_NAO_E_MEU)
            movidos.append(d["id"])
    return movidos


def devolver_ao_quadro(conn: sqlite3.Connection, numero: str,
                       por: str = "advogado") -> list[int]:
    """Desfaz o "Não é meu" (processo restaurado ou confirmado): cada cartão que foi
    para Resolvido por esse motivo, e não mexeu mais desde então, volta à coluna de
    onde saiu, se o movimento de volta for permitido agora. Devolve os ids movidos."""
    ultimo: dict[int, dict] = {}
    for e in conn.execute("SELECT dados FROM evento WHERE tipo = 'demanda_movida' "
                          "AND numero = ? ORDER BY id", (numero,)).fetchall():
        try:
            dados = json.loads(e["dados"] or "{}")
        except ValueError:
            continue
        if isinstance(dados, dict) and isinstance(dados.get("demanda"), int):
            ultimo[dados["demanda"]] = dados
    devolvidos = []
    for d in conn.execute("SELECT id FROM demanda WHERE numero = ? AND coluna = 'resolvida' "
                          "ORDER BY id", (numero,)).fetchall():
        dados = ultimo.get(d["id"]) or {}
        if dados.get("motivo") != MOTIVO_NAO_E_MEU or dados.get("para") != "resolvida":
            continue
        try:
            mover(conn, d["id"], dados.get("de") or "chegou", por)
        except MovimentoInvalido:
            continue  # ex.: já chegou outro cartão do processo na triagem
        devolvidos.append(d["id"])
    return devolvidos


def marcar_autos(conn: sqlite3.Connection, demanda_id: int, estado: str,
                 detalhe: str = "") -> None:
    if estado not in AUTOS_ESTADOS:
        raise ValueError(f"estado de autos desconhecido: {estado!r}")
    conn.execute("UPDATE demanda SET autos_estado = ?, autos_detalhe = ?, atualizada_em = ? "
                 "WHERE id = ?", (estado, detalhe, eventos.agora(), demanda_id))


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
    d = conn.execute("SELECT coluna FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    if d is None or d["coluna"] != "autos":
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
