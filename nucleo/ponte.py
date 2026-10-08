"""Ponte com o Lex (Fase 3): o Mac do advogado puxa do painel o próximo cartão da
fila, bate o ponto enquanto trabalha e devolve o resultado. Aqui fica a máquina de
estados do lado do servidor; as rotas `/ponte/*` só chamam estas funções.

Estados do cartão (`demanda.lex_estado`): na_fila → reservado → trabalhando →
pronto | falhou | pausado_limite | sem_tipo. Toda função recusa estado incoerente com
`PonteInvalida` (a rota vira 409).

Nada aqui faz commit: quem chama agrupa numa transação (`with conn:`)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta

from captura.processo_parser import formatar_numero_cnj
from nucleo import config, eventos, quadro

RESERVA_MINUTOS = 10
PAUSA_LIMITE_MINUTOS = 30
MOTIVOS = {"erro": "falhou", "tempo": "tempo", "limite": "limite", "sem_tipo": "sem_tipo"}
# sem_tipo: o Mac manda a explicação do Lex já higienizada (uma linha, sem caminho nem
# nome de arquivo); a frase padrão do Mac (servidor/Mac antigo) não é explicação
SEM_TIPO_SEM_EXPLICACAO = "o lex não teve segurança sobre o tipo de peça"
CHAVE_HASH = "ponte_chave_hash"
CHAVE_CRIADA_EM = "ponte_chave_criada_em"
ULTIMO_CONTATO = "ponte_ultimo_contato"
CLAUDE_OK = "ponte_claude_ok"  # "1"/"0": o Mac tem (ou não) acesso ao plano do Lex
CLAUDE_OK_EM = "ponte_claude_ok_em"
CHAVE_INVALIDA_N = "ponte_chave_invalida_n"  # tentativas com chave errada (sem evento)
CHAVE_INVALIDA_EM = "ponte_chave_invalida_em"
# Contato do Mac nos últimos N minutos = conectado (quadro, cartão e Configurações).
CONTATO_RECENTE_MINUTOS = 5
# Tetos do que o Mac manda em texto livre (uma linha cada).
LIMITE_SQUAD = 120
LIMITE_ETAPA = 200
LIMITE_DETALHE = 300


class PonteInvalida(ValueError):
    """Pedido incoerente com o estado do cartão ou da execução (a rota devolve 409)."""


# --- chave da ponte -------------------------------------------------------------

def _hash(chave: str) -> str:
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()


def gerar_chave(conn: sqlite3.Connection) -> str:
    """Gera uma chave nova (a antiga deixa de valer). Só o hash fica no banco; a chave
    em claro é devolvida uma única vez. O contador de chave inválida recomeça do zero."""
    chave = secrets.token_urlsafe(32)
    config.gravar(conn, CHAVE_HASH, _hash(chave))
    config.gravar(conn, CHAVE_CRIADA_EM, eventos.agora())
    conn.execute("DELETE FROM config WHERE chave IN (?, ?)",
                 (CHAVE_INVALIDA_N, CHAVE_INVALIDA_EM))
    eventos.registrar(conn, "ponte_chave_gerada", None, {})
    return chave


def revogar_chave(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM config WHERE chave IN (?, ?)", (CHAVE_HASH, CHAVE_CRIADA_EM))
    eventos.registrar(conn, "ponte_chave_revogada", None, {})


def chave_valida(conn: sqlite3.Connection, enviada) -> bool:
    gravado = config.ler(conn, CHAVE_HASH)
    if not gravado or not isinstance(enviada, str) or not enviada:
        return False
    return hmac.compare_digest(_hash(enviada).encode("ascii"), gravado.encode("ascii"))


def registrar_contato(conn: sqlite3.Connection, claude_ok: bool | None = None) -> None:
    """Anota o contato do Mac; `claude_ok` (de `/ponte/contato`) diz se o Mac tem acesso
    ao plano do Lex."""
    agora = eventos.agora()
    config.gravar(conn, ULTIMO_CONTATO, agora)
    if claude_ok is not None:
        config.gravar(conn, CLAUDE_OK, "1" if claude_ok else "0")
        config.gravar(conn, CLAUDE_OK_EM, agora)


def ultimo_contato(conn: sqlite3.Connection) -> str | None:
    return config.ler(conn, ULTIMO_CONTATO) or None


def registrar_chave_invalida(conn: sqlite3.Connection) -> None:
    """Conta a tentativa com chave inválida (contador e horário; sem evento, para um
    robô batendo na porta não inchar o registro)."""
    try:
        n = int(config.ler(conn, CHAVE_INVALIDA_N, "0"))
    except ValueError:
        n = 0
    config.gravar(conn, CHAVE_INVALIDA_N, str(n + 1))
    config.gravar(conn, CHAVE_INVALIDA_EM, eventos.agora())


def chaves_invalidas(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """(quantas tentativas com chave inválida, quando foi a última)."""
    try:
        n = int(config.ler(conn, CHAVE_INVALIDA_N, "0"))
    except ValueError:
        n = 0
    return n, (config.ler(conn, CHAVE_INVALIDA_EM) or None)


def _recente(texto: str | None, agora: datetime) -> bool:
    try:
        momento = _dt(texto or "")
    except ValueError:
        return False
    return _com_fuso(agora) - momento <= timedelta(minutes=CONTATO_RECENTE_MINUTOS)


def mac_conectado(conn: sqlite3.Connection, agora: datetime) -> bool:
    """O Mac falou com o painel nos últimos `CONTATO_RECENTE_MINUTOS` minutos."""
    return _recente(ultimo_contato(conn), agora)


def mac_sem_plano(conn: sqlite3.Connection, agora: datetime) -> bool:
    """O Mac está conectado, mas avisou há pouco que não tem acesso ao plano do Lex."""
    return (mac_conectado(conn, agora) and config.ler(conn, CLAUDE_OK) == "0"
            and _recente(config.ler(conn, CLAUDE_OK_EM), agora))


def uma_linha(texto, limite: int) -> str:
    """Texto livre do Mac numa linha só: sem caracteres de controle, espaços juntos e
    cortado em `limite` caracteres. `ponte_mac.executor.uma_linha` é a mesma função (o
    Mac normaliza o gate_status igual); mudou aqui, mude lá."""
    limpo = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                    for c in str(texto or ""))
    return " ".join(limpo.split())[:limite].rstrip()


def _verificada(status) -> bool:
    # A mesma regra do Mac (ponte_mac/executor.py): só "verificada"/"verified" conta.
    return str(status or "").strip().lower().startswith(("verificada", "verified"))


def campos_do_gate(gate: dict) -> dict:
    """gate_status e contagem de citações tirados do citation-gate.json, pela mesma
    regra do Mac: total = itens de `citations` que são objeto; falha = status que não
    começa com "verificada"/"verified"; gate_status em uma linha (até 40), ou
    "desconhecido"."""
    citacoes = gate.get("citations")
    lista = ([c for c in citacoes if isinstance(c, dict)]
             if isinstance(citacoes, list) else [])
    return {"gate_status": uma_linha(gate.get("gate_status"), 40) or "desconhecido",
            "citacoes_total": len(lista),
            "citacoes_falhas": sum(1 for c in lista if not _verificada(c.get("status")))}


# --- tempo ----------------------------------------------------------------------

def _iso(momento: datetime) -> str:
    return momento.isoformat(timespec="seconds")


def _dt(texto: str) -> datetime:
    return _com_fuso(datetime.fromisoformat(texto))


@contextmanager
def _sob_trava(conn: sqlite3.Connection):
    """Lê o estado já com a trava de escrita (BEGIN IMMEDIATE), para a conferência e a
    gravação ficarem na mesma transação. Se a transação foi aberta aqui e algo falha,
    desfaz (nada tinha sido gravado antes); no sucesso, quem chama faz o commit."""
    abriu = not conn.in_transaction
    if abriu:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if abriu:
            conn.rollback()
        raise


def _com_fuso(momento: datetime) -> datetime:
    """Tudo em horário de Cuiabá: momento sem fuso é lido como Cuiabá e momento com
    outro fuso é convertido (os textos gravados no banco ficam comparáveis)."""
    if momento.tzinfo is None:
        return momento.replace(tzinfo=eventos.FUSO)
    return momento.astimezone(eventos.FUSO)


# --- reservas e pausas ----------------------------------------------------------

def _execucao_aberta(conn: sqlite3.Connection, demanda_id: int,
                     n: int | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM execucao_lex WHERE demanda_id = ? AND terminada_em IS NULL"
    args: list = [demanda_id]
    if n is not None:
        sql += " AND n = ?"
        args.append(n)
    return conn.execute(sql + " ORDER BY n DESC LIMIT 1", args).fetchone()


def vencer_reserva(conn: sqlite3.Connection, d: sqlite3.Row, agora: datetime) -> bool:
    """Devolve à fila UM cartão cuja reserva venceu (sem data de reserva conta como
    vencida). Devolve False, sem mexer em nada, se outro processo já resolveu o cartão
    (batida, nova reserva, conclusão): o UPDATE condicional é quem decide, e só depois
    dele (já com a trava de escrita) fecha a execução aberta como `reserva_vencida`."""
    agora = _com_fuso(agora)
    cur = conn.execute(
        "UPDATE demanda SET lex_estado = 'na_fila', lex_etapa = '', lex_reservado_ate = NULL, "
        "atualizada_em = ? WHERE id = ? AND coluna = 'lex' "
        "AND lex_estado IN ('reservado', 'trabalhando') "
        "AND (lex_reservado_ate IS NULL OR lex_reservado_ate < ?)",
        (eventos.agora(), d["id"], _iso(agora)))
    if cur.rowcount != 1:
        return False
    ex = _execucao_aberta(conn, d["id"])
    if ex:
        conn.execute("UPDATE execucao_lex SET resultado = 'reserva_vencida', "
                     "terminada_em = ? WHERE id = ?", (_iso(agora), ex["id"]))
    eventos.registrar(conn, "lex_reserva_vencida", d["numero"],
                      {"demanda": d["id"], "n": ex["n"] if ex else None})
    return True


def vencer_reservas(conn: sqlite3.Connection, agora: datetime) -> int:
    """Devolve à fila o cartão cujo Mac sumiu (reserva sem batida há mais que
    `RESERVA_MINUTOS`). Devolve quantos."""
    agora = _com_fuso(agora)
    n = 0
    for d in conn.execute(
            "SELECT id, numero, lex_reservado_ate FROM demanda WHERE coluna = 'lex' "
            "AND lex_estado IN ('reservado', 'trabalhando') ORDER BY id").fetchall():
        if d["lex_reservado_ate"] and _dt(d["lex_reservado_ate"]) >= agora:
            continue
        n += vencer_reserva(conn, d, agora)
    return n


def liberar_pausados(conn: sqlite3.Connection, agora: datetime) -> int:
    """Devolve à fila o cartão pausado pelo limite do plano quando a espera acabou."""
    agora = _com_fuso(agora)
    n = 0
    for d in conn.execute(
            "SELECT id, numero, lex_tentar_depois FROM demanda WHERE coluna = 'lex' "
            "AND lex_estado = 'pausado_limite' ORDER BY id").fetchall():
        if d["lex_tentar_depois"] and _dt(d["lex_tentar_depois"]) > agora:
            continue
        conn.execute("UPDATE demanda SET lex_estado = 'na_fila', lex_detalhe = '', "
                     "lex_tentar_depois = NULL, atualizada_em = ? WHERE id = ?",
                     (eventos.agora(), d["id"]))
        eventos.registrar(conn, "lex_liberado_apos_limite", d["numero"], {"demanda": d["id"]})
        n += 1
    return n


# --- o ciclo do Mac -------------------------------------------------------------

def _pacote(conn: sqlite3.Connection, d: sqlite3.Row, n: int, modo: str) -> dict:
    p = conn.execute("SELECT * FROM processo WHERE numero = ?", (d["numero"],)).fetchone()
    anterior = run_anterior = ""
    if modo == "ajuste":
        r = conn.execute("SELECT squad, run_id FROM execucao_lex WHERE demanda_id = ? AND "
                         "resultado = 'pronto' ORDER BY n DESC LIMIT 1", (d["id"],)).fetchone()
        anterior = (r["squad"] or "") if r else ""
        run_anterior = (r["run_id"] or "") if r else ""
    publicacoes = [
        {"data": r["data_disponibilizacao"], "tipo": r["tipo"] or "", "orgao": r["orgao"] or "",
         "texto": r["texto"] or "", "link": r["link"] or ""}
        for r in conn.execute(
            "SELECT p.* FROM demanda_item i JOIN publicacao p ON CAST(p.id AS TEXT) = i.ref "
            "WHERE i.demanda_id = ? AND i.tipo = 'publicacao' "
            "ORDER BY p.data_disponibilizacao, p.id", (d["id"],))]
    intimacoes = [
        {"data": r["data"], "tipo": r["tipo"] or ""}
        for r in conn.execute(
            "SELECT COALESCE(NULLIF(a.data_disponibilizacao, ''), i.data) AS data, "
            "a.tipo_comunicacao AS tipo FROM demanda_item i "
            "LEFT JOIN aviso a ON a.instancia || ':' || a.id = i.ref "
            "WHERE i.demanda_id = ? AND i.tipo = 'aviso' ORDER BY i.data, i.ref", (d["id"],))]
    return {
        "demanda": d["id"], "n": n, "modo": modo, "numero": d["numero"],
        "numero_formatado": formatar_numero_cnj(d["numero"]),
        "tribunal": (p["tribunal"] if p else "") or "",
        "cliente": (p["cliente"] if p else "") or "",
        "parte_contraria": (p["parte_contraria"] if p else "") or "",
        "orgao": (p["orgao_julgador"] if p else "") or "",
        "orientacao": d["lex_orientacao"], "ajuste": d["lex_ajuste"],
        "squad_anterior": anterior, "run_anterior": run_anterior, "publicacoes": publicacoes, "intimacoes": intimacoes,
    }


def episodio(conn: sqlite3.Connection, d: sqlite3.Row) -> int | None:
    """Id do envio ao Lex mais recente do cartão (o evento `lex_enfileirado` que
    `quadro.mover(..., "lex")` grava). Não muda em `tentar_lex_de_novo` nem em
    `liberar_pausados`: o Mac usa este número para saber se é o mesmo pedido (e retomar)
    ou um pedido novo do advogado (e começar do zero)."""
    for e in conn.execute("SELECT id, dados FROM evento WHERE tipo = 'lex_enfileirado' "
                          "AND numero = ? ORDER BY id DESC", (d["numero"],)):
        try:
            dados = json.loads(e["dados"] or "{}")
        except ValueError:
            continue
        if isinstance(dados, dict) and dados.get("demanda") == d["id"]:
            return e["id"]
    return None


def proximo(conn: sqlite3.Connection, agora: datetime) -> dict | None:
    """Reserva o cartão mais antigo da fila do Lex e devolve o pacote para o Mac;
    None se não há nada para fazer.

    O pacote leva `episodio`: o id do evento do envio ao Lex mais recente do cartão (ver
    `episodio`). Ele não muda quando o advogado pede "Tentar de novo" nem quando a pausa
    do limite acaba; muda a cada novo envio (peça nova ou ajuste)."""
    agora = _com_fuso(agora)
    vencer_reservas(conn, agora)
    liberar_pausados(conn, agora)
    d = conn.execute("SELECT * FROM demanda WHERE coluna = 'lex' AND lex_estado = 'na_fila' "
                     "ORDER BY referencia_em, id LIMIT 1").fetchone()
    if d is None:
        return None
    n = (conn.execute("SELECT MAX(n) FROM execucao_lex WHERE demanda_id = ?",
                      (d["id"],)).fetchone()[0] or 0) + 1
    modo = "ajuste" if d["lex_ajuste"] else "novo"
    cur = conn.execute(
        "UPDATE demanda SET lex_estado = 'reservado', lex_etapa = '', lex_detalhe = '', "
        "lex_reservado_ate = ?, atualizada_em = ? WHERE id = ? AND lex_estado = 'na_fila'",
        (_iso(agora + timedelta(minutes=RESERVA_MINUTOS)), eventos.agora(), d["id"]))
    if cur.rowcount != 1:  # outro pedido chegou primeiro
        return None
    conn.execute("INSERT INTO execucao_lex (demanda_id, n, modo, iniciada_em) "
                 "VALUES (?, ?, ?, ?)", (d["id"], n, modo, _iso(agora)))
    eventos.registrar(conn, "lex_iniciado", d["numero"],
                      {"demanda": d["id"], "n": n, "modo": modo})
    return {**_pacote(conn, d, n, modo), "episodio": episodio(conn, d)}


def _ativa(conn: sqlite3.Connection, demanda_id: int, n: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    """O cartão e a execução `n`, se o cartão está mesmo com o Lex e a execução aberta."""
    d = conn.execute("SELECT * FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    if d is None:
        raise PonteInvalida("Cartão não encontrado.")
    if d["coluna"] != "lex" or d["lex_estado"] not in ("reservado", "trabalhando"):
        raise PonteInvalida("Este cartão não está sendo trabalhado pelo Lex.")
    ex = _execucao_aberta(conn, demanda_id, n)
    if ex is None:
        raise PonteInvalida("Esta execução do Lex não está aberta.")
    return d, ex


def batida(conn: sqlite3.Connection, demanda_id: int, n: int, *, squad: str = "",
           etapa: str = "", squad_nome: str = "", agora: datetime) -> None:
    """O Mac avisa que continua trabalhando: renova a reserva e mostra a etapa. A
    conferência do estado e a gravação ficam na mesma transação (trava de escrita)."""
    agora = _com_fuso(agora)
    squad, etapa = uma_linha(squad, LIMITE_SQUAD), uma_linha(etapa, LIMITE_ETAPA)
    squad_nome = uma_linha(squad_nome, LIMITE_SQUAD)
    with _sob_trava(conn):
        d, _ = _ativa(conn, demanda_id, n)
        # Squad trocado sem nome novo: o nome antigo não vale para o squad novo.
        nome = squad_nome or (d["lex_squad_nome"] if not squad or squad == d["lex_squad"]
                              else "")
        conn.execute("UPDATE demanda SET lex_estado = 'trabalhando', lex_squad = ?, "
                     "lex_squad_nome = ?, lex_etapa = ?, lex_reservado_ate = ?, "
                     "atualizada_em = ? WHERE id = ?",
                     (squad or d["lex_squad"], nome, etapa,
                      _iso(agora + timedelta(minutes=RESERVA_MINUTOS)), eventos.agora(),
                      demanda_id))
        if squad:
            conn.execute("UPDATE execucao_lex SET squad = ?, squad_nome = ? "
                         "WHERE demanda_id = ? AND n = ?", (squad, nome, demanda_id, n))


def concluir(conn: sqlite3.Connection, demanda_id: int, n: int, *, squad: str, run_id: str,
             gate_status: str, citacoes_total: int, citacoes_falhas: int, pasta: str,
             agora: datetime, squad_nome: str = "", detalhe: str = "") -> None:
    """A peça ficou pronta: grava a execução e leva o cartão para "Sua revisão"."""
    agora = _com_fuso(agora)
    squad, squad_nome = uma_linha(squad, LIMITE_SQUAD), uma_linha(squad_nome, LIMITE_SQUAD)
    detalhe = uma_linha(detalhe, LIMITE_DETALHE)
    with _sob_trava(conn):
        d, ex = _ativa(conn, demanda_id, n)
        if not squad_nome and squad and squad == d["lex_squad"]:
            squad_nome = d["lex_squad_nome"]
        conn.execute(
            "UPDATE execucao_lex SET resultado = 'pronto', terminada_em = ?, squad = ?, "
            "squad_nome = ?, run_id = ?, gate_status = ?, citacoes_total = ?, "
            "citacoes_falhas = ?, pasta = ?, detalhe = ? WHERE id = ?",
            (_iso(agora), squad, squad_nome, run_id, gate_status, citacoes_total,
             citacoes_falhas, pasta, detalhe, ex["id"]))
        quadro.mover(conn, demanda_id, "revisao", "mac")
        conn.execute("UPDATE demanda SET lex_estado = 'pronto', lex_etapa = '', lex_detalhe = '', "
                     "lex_squad = ?, lex_squad_nome = ?, lex_reservado_ate = NULL WHERE id = ?",
                     (squad, squad_nome, demanda_id))
        minutos = max(0, round((agora - _dt(ex["iniciada_em"])).total_seconds() / 60))
        dados = {"demanda": demanda_id, "n": n, "squad": squad, "squad_nome": squad_nome,
                 "minutos_lex": minutos}
        if detalhe:
            dados["detalhe"] = detalhe
        eventos.registrar(conn, "lex_pronto", d["numero"], dados)


def falhar(conn: sqlite3.Connection, demanda_id: int, n: int, motivo: str, detalhe: str,
           agora: datetime) -> None:
    """O Lex não conseguiu: erro/tempo (falhou), limite do plano (pausa e tenta depois)
    ou sem segurança no tipo de peça (volta para "Autos baixados")."""
    if motivo not in MOTIVOS:
        raise PonteInvalida(f"Motivo desconhecido: {motivo!r}.")
    agora = _com_fuso(agora)
    detalhe = uma_linha(detalhe, LIMITE_DETALHE)
    with _sob_trava(conn):
        d, ex = _ativa(conn, demanda_id, n)
        conn.execute("UPDATE execucao_lex SET resultado = ?, terminada_em = ?, detalhe = ? "
                     "WHERE id = ?", (MOTIVOS[motivo], _iso(agora), detalhe, ex["id"]))
        if motivo == "sem_tipo":
            quadro.mover(conn, demanda_id, "autos", "mac")
            explicacao = ("" if detalhe.lower().rstrip(".").startswith(SEM_TIPO_SEM_EXPLICACAO)
                          else detalhe)
            conn.execute("UPDATE demanda SET lex_detalhe = ?, lex_etapa = '' WHERE id = ?",
                         (explicacao, demanda_id))
        else:
            estado = "pausado_limite" if motivo == "limite" else "falhou"
            depois = (_iso(agora + timedelta(minutes=PAUSA_LIMITE_MINUTOS))
                      if motivo == "limite" else None)
            conn.execute("UPDATE demanda SET lex_estado = ?, lex_detalhe = ?, lex_etapa = '', "
                         "lex_reservado_ate = NULL, lex_tentar_depois = ?, atualizada_em = ? "
                         "WHERE id = ?", (estado, detalhe, depois, eventos.agora(), demanda_id))
        eventos.registrar(conn, "lex_falhou", d["numero"],
                          {"demanda": demanda_id, "n": n, "motivo": motivo, "detalhe": detalhe})
