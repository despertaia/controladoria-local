"""Banco SQLite da controladoria (um arquivo, sem servidor de banco).

Migrações versionadas por PRAGMA user_version: o item i de MIGRACOES leva o
banco da versão i para i+1. Nunca edite uma migração já publicada; acrescente
uma nova ao fim da lista.
"""

from __future__ import annotations

import os
import sqlite3
import threading

CAMINHO_PADRAO = "dados/controladoria.db"

MIGRACOES = [
    """
    CREATE TABLE processo (
        numero TEXT PRIMARY KEY,
        tribunal TEXT NOT NULL,
        instancia TEXT,
        classe TEXT,
        orgao_julgador TEXT,
        valor_causa TEXT,
        data_ajuizamento TEXT,
        polo_cliente TEXT,
        cliente TEXT,
        parte_contraria TEXT,
        advogado_atua INTEGER,
        data_arquivamento TEXT,
        arquivado INTEGER NOT NULL DEFAULT 0,
        ultimo_andamento_data TEXT,
        ultimo_andamento_texto TEXT,
        ultima_publicacao_data TEXT,
        partes_json TEXT NOT NULL DEFAULT '[]',
        fontes TEXT NOT NULL DEFAULT '[]',
        sincronizado_em TEXT,
        erro_sincronizacao TEXT
    );
    CREATE TABLE publicacao (
        id INTEGER PRIMARY KEY,
        numero TEXT NOT NULL,
        tribunal TEXT NOT NULL,
        data_disponibilizacao TEXT NOT NULL,
        tipo TEXT,
        orgao TEXT,
        classe TEXT,
        texto TEXT,
        link TEXT,
        oab_confirmada INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX publicacao_numero ON publicacao (numero);
    CREATE TABLE andamento (
        numero TEXT NOT NULL,
        data_ordenavel TEXT NOT NULL,
        codigo INTEGER,
        texto TEXT NOT NULL,
        PRIMARY KEY (numero, data_ordenavel, texto)
    );
    CREATE TABLE evento (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quando TEXT NOT NULL,
        tipo TEXT NOT NULL,
        numero TEXT,
        dados TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX evento_numero ON evento (numero);
    """,
    """
    ALTER TABLE processo ADD COLUMN descartado_em TEXT;
    CREATE TABLE demanda (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero TEXT NOT NULL,
        coluna TEXT NOT NULL
            CHECK (coluna IN ('chegou', 'acao', 'autos', 'acompanhar', 'resolvida')),
        referencia_em TEXT NOT NULL,
        criada_em TEXT NOT NULL,
        atualizada_em TEXT NOT NULL,
        autos_estado TEXT NOT NULL DEFAULT ''
            CHECK (autos_estado IN ('', 'na_fila', 'baixando', 'ok', 'falhou')),
        autos_detalhe TEXT NOT NULL DEFAULT ''
    );
    CREATE UNIQUE INDEX demanda_um_chegou_por_processo ON demanda (numero)
        WHERE coluna = 'chegou';
    CREATE INDEX demanda_coluna ON demanda (coluna);
    CREATE TABLE demanda_item (
        demanda_id INTEGER NOT NULL REFERENCES demanda (id),
        tipo TEXT NOT NULL CHECK (tipo IN ('publicacao', 'aviso')),
        ref TEXT NOT NULL,
        data TEXT NOT NULL,
        UNIQUE (tipo, ref)
    );
    CREATE INDEX demanda_item_demanda ON demanda_item (demanda_id);
    CREATE TABLE aviso (
        instancia TEXT NOT NULL,
        id TEXT NOT NULL,
        numero TEXT NOT NULL,
        tipo_comunicacao TEXT NOT NULL DEFAULT '',
        data_disponibilizacao TEXT NOT NULL DEFAULT '',
        orgao TEXT NOT NULL DEFAULT '',
        visto_em TEXT NOT NULL,
        pendente INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (instancia, id)
    );
    CREATE INDEX aviso_numero ON aviso (numero);
    CREATE TABLE tarefa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT NOT NULL CHECK (tipo IN ('varredura', 'autos')),
        ref TEXT NOT NULL DEFAULT '',
        estado TEXT NOT NULL
            CHECK (estado IN ('na_fila', 'rodando', 'ok', 'falhou', 'cancelada')),
        pedida_em TEXT NOT NULL,
        pedida_por TEXT NOT NULL,
        iniciada_em TEXT,
        terminada_em TEXT,
        erro TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX tarefa_estado ON tarefa (estado);
    """,
    # 3 — Fase 2A (revisão final): consultas por tipo de evento (atividade,
    # última varredura, quadro ligado) sem varrer a tabela inteira.
    """
    CREATE INDEX evento_tipo ON evento (tipo);
    """,
    # 4 — Fase 3 (ponte com o Lex): colunas novas do quadro, execuções e configuração.
    # Recria `demanda` porque o SQLite não altera CHECK.
    """
    CREATE TABLE demanda_nova (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero TEXT NOT NULL,
        coluna TEXT NOT NULL CHECK (coluna IN ('chegou', 'acao', 'autos', 'acompanhar',
            'resolvida', 'lex', 'revisao', 'protocolado')),
        referencia_em TEXT NOT NULL,
        criada_em TEXT NOT NULL,
        atualizada_em TEXT NOT NULL,
        autos_estado TEXT NOT NULL DEFAULT ''
            CHECK (autos_estado IN ('', 'na_fila', 'baixando', 'ok', 'falhou')),
        autos_detalhe TEXT NOT NULL DEFAULT '',
        lex_estado TEXT NOT NULL DEFAULT '' CHECK (lex_estado IN ('', 'na_fila', 'reservado',
            'trabalhando', 'pronto', 'falhou', 'pausado_limite', 'sem_tipo')),
        lex_orientacao TEXT NOT NULL DEFAULT '',
        lex_ajuste TEXT NOT NULL DEFAULT '',
        lex_squad TEXT NOT NULL DEFAULT '',
        lex_etapa TEXT NOT NULL DEFAULT '',
        lex_detalhe TEXT NOT NULL DEFAULT '',
        lex_reservado_ate TEXT,
        lex_tentar_depois TEXT,
        protocolado_em TEXT
    );
    INSERT INTO demanda_nova (id, numero, coluna, referencia_em, criada_em, atualizada_em,
                              autos_estado, autos_detalhe)
        SELECT id, numero, coluna, referencia_em, criada_em, atualizada_em,
               autos_estado, autos_detalhe FROM demanda;
    DROP TABLE demanda;
    ALTER TABLE demanda_nova RENAME TO demanda;
    CREATE UNIQUE INDEX demanda_um_chegou_por_processo ON demanda (numero)
        WHERE coluna = 'chegou';
    CREATE INDEX demanda_coluna ON demanda (coluna);
    CREATE TABLE execucao_lex (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        demanda_id INTEGER NOT NULL REFERENCES demanda (id),
        n INTEGER NOT NULL,
        modo TEXT NOT NULL CHECK (modo IN ('novo', 'ajuste')),
        iniciada_em TEXT NOT NULL,
        terminada_em TEXT,
        resultado TEXT NOT NULL DEFAULT '' CHECK (resultado IN ('', 'pronto', 'falhou',
            'limite', 'sem_tipo', 'tempo', 'reserva_vencida')),
        squad TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        gate_status TEXT NOT NULL DEFAULT '',
        citacoes_total INTEGER NOT NULL DEFAULT 0,
        citacoes_falhas INTEGER NOT NULL DEFAULT 0,
        pasta TEXT NOT NULL DEFAULT '',
        detalhe TEXT NOT NULL DEFAULT '',
        UNIQUE (demanda_id, n)
    );
    CREATE TABLE config (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
    """,
    # 5 — Fase 3 (onda final): nome legível do squad (o `name` do squad.yaml), que o
    # Mac manda na batida e no resultado; a tela mostra o nome no lugar do slug.
    """
    ALTER TABLE demanda ADD COLUMN lex_squad_nome TEXT NOT NULL DEFAULT '';
    ALTER TABLE execucao_lex ADD COLUMN squad_nome TEXT NOT NULL DEFAULT '';
    """,
    # 6 — Íntegra no painel: o advogado confirma que o processo é dele (coluna própria,
    # que a sincronização não toca) e as partes que o DJEN traz em cada publicação
    # ([{nome, polo}]; as já gravadas ficam vazias até a próxima varredura).
    """
    ALTER TABLE processo ADD COLUMN confirmado_em TEXT;
    ALTER TABLE publicacao ADD COLUMN partes_json TEXT NOT NULL DEFAULT '[]';
    """,
]


def caminho_do_banco() -> str:
    return os.getenv("CONTROLADORIA_BANCO", "").strip() or CAMINHO_PADRAO


# Na instalação local o painel, o trabalhador e a ponte do Lex abrem o banco ao mesmo
# tempo, em threads do mesmo processo; num banco novo, duas delas aplicavam a mesma
# migração ("duplicate column name"). A trava faz uma esperar a outra, e a versão é
# relida dentro dela.
_TRAVA_MIGRAR = threading.Lock()


def migrar(conn: sqlite3.Connection) -> int:
    """Aplica as migrações pendentes, cada uma numa transação. Retorna a versão."""
    with _TRAVA_MIGRAR:
        versao = conn.execute("PRAGMA user_version").fetchone()[0]
        if versao > len(MIGRACOES):
            raise RuntimeError("banco de dados mais novo que este código — atualize o código "
                               "antes de usar este banco")
        for i in range(versao, len(MIGRACOES)):
            conn.executescript(
                f"BEGIN;\n{MIGRACOES[i]}\nPRAGMA user_version = {i + 1};\nCOMMIT;")
    return len(MIGRACOES)


def conectar(caminho: str | None = None) -> sqlite3.Connection:
    caminho = caminho or caminho_do_banco()
    if caminho != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if caminho != ":memory:":
        # Trocar o modo do diário não espera o banco ocupado ("database is locked" na
        # hora) quando duas threads abrem o banco novo juntas: vai sob a mesma trava.
        with _TRAVA_MIGRAR:
            conn.execute("PRAGMA journal_mode = WAL")
    migrar(conn)
    return conn
