"""Arquivar, desarquivar, excluir e restaurar da lixeira, um ou em lote (1.5.0)."""

from datetime import date

import pytest

from nucleo import acoes_processo, banco, carteira, quadro, tarefas_fila

A = "00000010220248110041"
B = "00000020220248110041"


@pytest.fixture
def conn():
    c = banco.conectar(":memory:")
    for numero in (A, B):
        c.execute("INSERT INTO processo (numero, tribunal, advogado_atua) "
                  "VALUES (?, 'TJMT', 1)", (numero,))
    yield c
    c.close()


def _colunas(conn, numero):
    return [r[0] for r in conn.execute(
        "SELECT coluna FROM demanda WHERE numero = ? ORDER BY id", (numero,))]


def _marcas(conn, numero):
    return tuple(conn.execute("SELECT arquivado_por_voce_em IS NOT NULL, excluido_em IS NOT NULL "
                              "FROM processo WHERE numero = ?", (numero,)).fetchone())


def test_arquivar_em_lote_tira_cartoes_e_desarquivar_devolve(conn):
    quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    r = acoes_processo.aplicar(conn, "arquivar", [A, B, A])  # repetido conta uma vez
    assert (r.feitos, r.sem_mudanca, r.com_lex, r.cartoes) == ([A, B], [], [], 1)
    assert _colunas(conn, A) == ["resolvida"]
    assert acoes_processo.mensagem("arquivar", r) == (
        "2 processos arquivados. Eles ficam em «Arquivados». O cartão saiu do quadro.")
    r = acoes_processo.aplicar(conn, "desarquivar", [A])
    assert r.feitos == [A] and r.cartoes == 1 and _colunas(conn, A) == ["chegou"]
    assert acoes_processo.mensagem("desarquivar", r) == (
        "Desarquivado: o processo voltou para a carteira. O cartão voltou ao quadro.")


def test_excluir_e_restaurar(conn):
    quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    r = acoes_processo.aplicar(conn, "excluir", [A])
    assert r.feitos == [A] and _marcas(conn, A) == (0, 1) and _colunas(conn, A) == ["resolvida"]
    assert acoes_processo.mensagem("excluir", r) == (
        "Foi para a lixeira. Dá para restaurar por 30 dias. O cartão saiu do quadro.")
    r = acoes_processo.aplicar(conn, "restaurar", [A])
    assert r.feitos == [A] and _marcas(conn, A) == (0, 0) and _colunas(conn, A) == ["chegou"]


def test_lex_trabalhando_bloqueia_arquivar_e_excluir(conn):
    d, _ = quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    conn.execute("UPDATE demanda SET coluna = 'lex', lex_estado = 'trabalhando' WHERE id = ?", (d,))
    r = acoes_processo.aplicar(conn, "arquivar", [A, B])
    assert r.com_lex == [A] and r.feitos == [B]
    assert acoes_processo.mensagem("arquivar", r) == (
        "Arquivado. Ele fica em «Arquivados» e volta sozinho se sair publicação nova. "
        "1 processo ficou de fora: o Lex está trabalhando nele. "
        "Espere o Lex terminar ou desista do pedido.")
    assert acoes_processo.deu_certo(r)
    r = acoes_processo.aplicar(conn, "excluir", [A])
    assert r.com_lex == [A] and r.feitos == []
    assert acoes_processo.mensagem("excluir", r) == (
        "O Lex está trabalhando neste processo. Espere o Lex terminar ou desista do pedido.")
    assert not acoes_processo.deu_certo(r)
    assert _marcas(conn, A) == (0, 0) and _colunas(conn, A) == ["lex"]


def test_excluir_cancela_a_busca_de_autos_na_fila(conn):
    d, _ = quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-05")
    quadro.mover(conn, d, "acao", "advogado")
    assert tarefas_fila.ativa(conn, "autos", str(d))
    acoes_processo.aplicar(conn, "excluir", [A])
    assert conn.execute("SELECT estado FROM tarefa WHERE ref = ?",
                        (str(d),)).fetchone()[0] == "cancelada"


def test_limite_do_lote_recusa_tudo(conn):
    numeros = [f"{i:07d}0220248110041" for i in range(1, acoes_processo.LIMITE_LOTE + 2)]
    with pytest.raises(acoes_processo.LoteGrande, match="até 200"):
        acoes_processo.aplicar(conn, "arquivar", numeros)
    assert _marcas(conn, A) == (0, 0)


def test_acao_desconhecida(conn):
    with pytest.raises(ValueError):
        acoes_processo.aplicar(conn, "apagar", [A])


def test_nada_mudou(conn):
    r = acoes_processo.aplicar(conn, "desarquivar", [A])
    assert r.sem_mudanca == [A] and r.feitos == []
    assert acoes_processo.mensagem("desarquivar", r) == (
        "Nada mudou: este processo não estava arquivado.")
    assert acoes_processo.deu_certo(r)
    r = acoes_processo.aplicar(conn, "restaurar", [A, B])
    assert acoes_processo.mensagem("restaurar", r) == "Nada mudou."


# Volta por publicação nova e de novo arquivado/excluído: só o cartão novo volta (os
# antigos ficaram em Resolvido na volta automática, decisão 4).

def _tres_cartoes(conn):
    """Cartão 1 em «Autos baixados», 2 em «Precisa de ação» (autos na fila), 3 em Chegou."""
    d1, _ = quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-01")
    quadro.mover(conn, d1, "acao", "advogado")
    conn.execute("UPDATE demanda SET coluna = 'autos', autos_estado = 'ok' WHERE id = ?", (d1,))
    d2, _ = quadro.adicionar_item(conn, A, "publicacao", "2", "2026-10-02")
    quadro.mover(conn, d2, "acao", "advogado")
    d3, _ = quadro.adicionar_item(conn, A, "publicacao", "3", "2026-10-03")
    return d1, d2, d3


def _volta_por_publicacao(conn, marca, pub_id, dia):
    """A marca (arquivado_por_voce_em ou excluido_em) fica num dia antes da publicação
    nova; a varredura grava a publicação e o quadro abre o cartão dela."""
    conn.execute(f"UPDATE processo SET {marca} = ? WHERE numero = ?",
                 ("2026-10-20T10:00:00-03:00", A))
    pub = {"id": pub_id, "numero": A, "tribunal": "TJMT", "data_disponibilizacao": dia,
           "tipo": "Intimação", "orgao": "1ª Vara", "classe": "", "texto": "t", "link": "",
           "oab_confirmada": True, "partes": []}
    assert carteira.gravar_publicacoes_novas(conn, [pub])
    assert quadro.gerar_pendentes(conn, date(2026, 10, 22)) == 1
    return conn.execute("SELECT MAX(id) FROM demanda WHERE numero = ?", (A,)).fetchone()[0]


@pytest.mark.parametrize("tira, devolve, marca", [
    ("arquivar", "desarquivar", "arquivado_por_voce_em"),
    ("excluir", "restaurar", "excluido_em"),
])
def test_depois_da_volta_por_publicacao_so_o_cartao_novo_volta(conn, tira, devolve, marca):
    d1, d2, d3 = _tres_cartoes(conn)
    assert acoes_processo.aplicar(conn, tira, [A]).cartoes == 3
    d4 = _volta_por_publicacao(conn, marca, 99, "2026-10-21")
    assert _colunas(conn, A) == ["resolvida", "resolvida", "resolvida", "chegou"]
    r = acoes_processo.aplicar(conn, tira, [A])
    assert r.feitos == [A] and r.cartoes == 1
    r = acoes_processo.aplicar(conn, devolve, [A])
    assert r.feitos == [A] and r.cartoes == 1
    assert _colunas(conn, A) == ["resolvida", "resolvida", "resolvida", "chegou"]
    assert conn.execute("SELECT coluna FROM demanda WHERE id = ?", (d4,)).fetchone()[0] == "chegou"
    assert not tarefas_fila.ativa(conn, "autos", str(d2))  # os autos do antigo não são pedidos


def test_desarquivar_nao_devolve_nao_e_meu_nem_resolvido_a_mao_e_vale_a_cadeia(conn):
    y, _ = quadro.adicionar_item(conn, A, "publicacao", "1", "2026-10-01")
    assert quadro.tirar_do_quadro(conn, A) == [y]  # «Não é meu»
    x, _ = quadro.adicionar_item(conn, A, "publicacao", "2", "2026-10-02")
    quadro.mover(conn, x, "resolvida", "advogado")  # resolvido à mão
    z, _ = quadro.adicionar_item(conn, A, "publicacao", "3", "2026-10-03")
    quadro.mover(conn, z, "acompanhar", "advogado")
    acoes_processo.aplicar(conn, "arquivar", [A])
    acoes_processo.aplicar(conn, "excluir", [A])
    assert acoes_processo.aplicar(conn, "restaurar", [A]).cartoes == 0
    assert _marcas(conn, A) == (1, 0)  # voltou para «Arquivados»
    r = acoes_processo.aplicar(conn, "desarquivar", [A])
    assert r.feitos == [A] and r.cartoes == 1
    assert _colunas(conn, A) == ["resolvida", "resolvida", "acompanhar"]
