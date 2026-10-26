"""Vigilância da conexão com o PJe de cada tribunal: o estado do último login de cada
instância (ok, senha recusada, fora do ar ou sem senha cadastrada), a regra das duas
falhas seguidas e os avisos que a tela mostra.

Nada aqui grava senha: a mensagem de `testar` vem da sonda e nunca a traz. `registrar`
não faz commit: quem chama usa `with conn:`."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from nucleo import credenciais, eventos, mni_fabrica, tribunal

FALHAS_PARA_AVISAR = 2


def testar(sigla: str, instancia: str, cpf: str, senha: str) -> tuple[str, str]:
    """Testa o login numa instância (só lista os avisos pendentes: não abre teor nem
    registra ciência). Devolve ("ok" | "recusada" | "fora", mensagem)."""
    trib = tribunal.obter(sigla)
    cred = {"cpf": cpf, "senha": senha, "senha_2grau": senha}
    resultado = mni_fabrica.sondar_login(
        cred, [inst for inst in trib.instancias if inst[0] == instancia],
        forcar_endereco=trib.forcar_endereco, sigla=trib.sigla)
    estado, mensagem = resultado[instancia]
    if estado == mni_fabrica.INDISPONIVEL:
        estado = "fora"
    return estado, mensagem


def registrar(conn: sqlite3.Connection, sigla: str, instancia: str, estado: str,
              detalhe: str = "", agora: str | None = None) -> bool:
    """Grava o estado da conexão. Devolve True se o estado mudou (e então registra o
    evento `conexao_mudou`). `fora` soma uma falha seguida; qualquer outro estado zera."""
    agora = agora or eventos.agora()
    anterior = conn.execute(
        "SELECT estado, falhas_seguidas, mudou_em FROM conexao_tribunal "
        "WHERE sigla = ? AND instancia = ?", (sigla, instancia)).fetchone()
    mudou = anterior is None or anterior["estado"] != estado
    falhas = (anterior["falhas_seguidas"] if anterior else 0) + 1 if estado == "fora" else 0
    mudou_em = agora if mudou else anterior["mudou_em"]
    conn.execute(
        """INSERT INTO conexao_tribunal
               (sigla, instancia, estado, falhas_seguidas, verificado_em, mudou_em, detalhe)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (sigla, instancia) DO UPDATE SET
               estado = excluded.estado, falhas_seguidas = excluded.falhas_seguidas,
               verificado_em = excluded.verificado_em, mudou_em = excluded.mudou_em,
               detalhe = excluded.detalhe""",
        (sigla, instancia, estado, falhas, agora, mudou_em, detalhe))
    if mudou:
        eventos.registrar(conn, "conexao_mudou", None,
                          {"sigla": sigla, "instancia": instancia,
                           "de": anterior["estado"] if anterior else None, "para": estado},
                          quando=agora)
    return mudou


AVISO_COFRE = ("As senhas dos tribunais guardadas no painel não puderam ser usadas. "
               "Abra Configurações › Tribunais.")


def _selo(cadastrada: bool, estado: str | None, falhas: int, ilegivel: bool = False) -> str:
    if not cadastrada:
        return "nao_cadastrado"
    if ilegivel:
        return "redigitar"  # a chave atual do cofre não abre a senha guardada
    if estado == "recusada":
        return "recusada"
    if estado == "fora":
        return "fora" if falhas >= FALHAS_PARA_AVISAR else "instavel"
    if estado == "ok":
        return "ok"
    return "nao_verificado"  # senha cadastrada, mas ainda sem varredura que a teste


def situacao(conn: sqlite3.Connection) -> list[dict]:
    """Uma linha por instância homologada, com o selo que a tela mostra."""
    cadastradas = credenciais.cadastradas(conn)
    ilegiveis = credenciais.ilegiveis(conn)
    guardado = {(r["sigla"], r["instancia"]): r
                for r in conn.execute("SELECT * FROM conexao_tribunal")}
    linhas = []
    for sigla, instancia, rotulo in credenciais.INSTANCIAS_HOMOLOGADAS:
        cadastrada = (sigla, instancia) in cadastradas
        r = guardado.get((sigla, instancia))
        estado = r["estado"] if r else None
        falhas = r["falhas_seguidas"] if r else 0
        linhas.append({
            "sigla": sigla, "instancia": instancia, "rotulo": rotulo,
            "cadastrada": cadastrada,
            "estado": estado if cadastrada else "nao_cadastrado",
            "selo": _selo(cadastrada, estado, falhas, (sigla, instancia) in ilegiveis),
            "falhas_seguidas": falhas,
            "verificado_em": r["verificado_em"] if r else None,
            "mudou_em": r["mudou_em"] if r else None,
            "detalhe": r["detalhe"] if r else "",
        })
    return linhas


def _desde(mudou_em: str | None) -> str:
    try:
        quando = datetime.fromisoformat(str(mudou_em))
    except ValueError:
        return "algum tempo"
    return f"{quando:%H:%M} de {quando:%d/%m}"


def avisos(conn: sqlite3.Connection) -> list[dict]:
    """Avisos de conexão das instâncias com senha cadastrada: senha recusada, ou PJe
    sem responder em duas varreduras seguidas. Antes de todos, o do cofre: senhas
    guardadas que a chave atual não abre, ou a chave sumida com senhas no banco."""
    saida = []
    estado_cofre = credenciais.estado_do_cofre(conn)
    if estado_cofre == "nao_abre" or (estado_cofre == "sem_chave"
                                      and credenciais.tem_credenciais_cifradas(conn)):
        saida.append({"texto": AVISO_COFRE, "sigla": ""})
    for s in situacao(conn):
        if s["selo"] == "recusada":
            texto = (f"A senha do PJe do {s['sigla']} ({s['rotulo']}) foi recusada. "
                     "Atualize em Configurações › Tribunais.")
        elif s["selo"] == "fora":
            texto = (f"O PJe do {s['sigla']} ({s['rotulo']}) não responde desde "
                     f"{_desde(s['mudou_em'])}. Os autos desse tribunal esperam ele voltar.")
        else:
            continue
        saida.append({"texto": texto, "sigla": s["sigla"]})
    return saida
