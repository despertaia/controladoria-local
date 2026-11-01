"""Carteira do advogado.

Descobre os processos (DJEN + listas que o sistema já tinha), consulta o
PJe do tribunal do escritório (TJMT, TJMG…; ver nucleo/tribunal.py) para partes e
andamentos, identifica cliente e parte contrária pela OAB, marca os arquivados e
grava tudo no banco, registrando cada passo como evento. Fora do tribunal do
escritório (TRF1, TRF3…), o processo entra pelo DJEN e a atuação é
confirmada pela OAB vinculada à publicação; o enriquecimento pelo PJe desses
tribunais vem depois.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from captura.mni_client import (CredencialInvalidaError, MNIError,
                                ProcessoNaoEncontradoError)
from captura.planilha import PASTA_PLANILHAS, ler_processos
from captura.processo_parser import processo_para_dict, validar_cnj
from nucleo import eventos, tribunal
from nucleo.advogado import e_o_advogado
from nucleo.arquivamento import data_arquivamento, esta_arquivado
from nucleo.djen import buscar_por_advogado
from nucleo.mni_fabrica import criar_cliente

INSTANCIAS_DE_BUSCA = ("1grau", "2grau")  # as do TJMT; quem busca usa instancias_de_busca()
LIMITE_RECUSAS_SEGUIDAS = 3
_OPOSTO = {"Polo Ativo": "Polo Passivo", "Polo Passivo": "Polo Ativo"}
# Situações (rótulos mostrados no painel e na planilha): um lugar só.
ATIVO = "Ativo"
ATUA_PELO_DJEN = "Atua (pelo DJEN; situação não conferida)"
SEM_ACESSO = "Sem acesso pelo PJe"
A_CONFERIR_NAO_SINCRONIZADO = "A conferir (não sincronizado)"
A_CONFERIR_FORA_DO_TJMT = "A conferir (fora do TJMT)"  # rótulo do TJMT; ver a_conferir_fora()
A_CONFERIR_OAB_NAO_CONSTA = "A conferir (OAB não consta)"
ARQUIVADO = "Arquivado"
ARQUIVADO_POR_VOCE = "Arquivado por você"
NA_LIXEIRA = "Na lixeira"
# Lixeira (1.5.0): o excluído aparece na aba por este número de dias; depois some dela,
# mas nada é apagado do banco (uma publicação nova ainda o traz de volta).
DIAS_NA_LIXEIRA = 30

# Da carteira: a OAB do advogado consta (PJe ou DJEN) ou ele mesmo confirmou no painel
# ("É meu"); a confirmação é coluna própria, que a sincronização não desfaz. Arquivado por
# você e excluído (lixeira) saem das abas comuns.
_DA_CARTEIRA = "(advogado_atua = 1 OR confirmado_em IS NOT NULL)"
_FORA_DO_ARQUIVO = "arquivado_por_voce_em IS NULL AND excluido_em IS NULL"
FILTRO_ATIVA = f"{_DA_CARTEIRA} AND arquivado = 0 AND {_FORA_DO_ARQUIVO}"
_SEM_ATUACAO = ("arquivado = 0 AND (advogado_atua IS NULL OR advogado_atua = 0) "
                f"AND confirmado_em IS NULL AND {_FORA_DO_ARQUIVO}")
_FILTROS = {
    "ativa": FILTRO_ATIVA,
    "a_conferir": f"{_SEM_ATUACAO} AND descartado_em IS NULL",
    # Arquivados por você (vindos de qualquer aba) e os do tribunal que são da carteira.
    "arquivados": ("excluido_em IS NULL AND (arquivado_por_voce_em IS NOT NULL "
                   f"OR (arquivado = 1 AND {_DA_CARTEIRA}))"),
    # Excluídos há menos de DIAS_NA_LIXEIRA dias; `listar` preenche :corte_lixeira.
    "lixeira": "excluido_em IS NOT NULL AND substr(excluido_em, 1, 10) > :corte_lixeira",
    "descartados": f"{_SEM_ATUACAO} AND descartado_em IS NOT NULL",
}


@dataclass(frozen=True)
class Advogado:
    nome: str
    numero_oab: str
    uf_oab: str


def advogado_do_env() -> Advogado:
    nome = os.getenv("CARTEIRA_ADVOGADO_NOME", "").strip()
    numero = os.getenv("CARTEIRA_OAB_NUMERO", "").strip()
    uf = os.getenv("CARTEIRA_OAB_UF", "").strip().upper()
    if not (nome and numero and uf):
        raise ValueError("Preencha CARTEIRA_ADVOGADO_NOME, CARTEIRA_OAB_NUMERO e "
                         "CARTEIRA_OAB_UF no .env.")
    return Advogado(nome, numero, uf)


def tribunal_de(numero_ou_tribunal=None) -> tribunal.Tribunal:
    """Tribunal configurado do número CNJ (ou o próprio tribunal/sigla); o principal
    quando nada casa."""
    if numero_ou_tribunal is None or isinstance(numero_ou_tribunal, tribunal.Tribunal):
        return tribunal.obter(numero_ou_tribunal)
    if str(numero_ou_tribunal).strip().upper() in tribunal.TRIBUNAIS:
        return tribunal.obter(numero_ou_tribunal)
    return tribunal.do_numero(numero_ou_tribunal) or tribunal.atual()


def instancias_de_busca(numero_ou_tribunal=None) -> tuple[str, ...]:
    """Instâncias do PJe consultadas, na ordem (1º grau, depois 2º), do tribunal do
    número (ou do tribunal dado); sem argumento, as do principal."""
    return tuple(chave for chave, *_ in tribunal_de(numero_ou_tribunal).instancias)


def criar_para(fabrica, instancia: str, trib: tribunal.Tribunal):
    """Chama a fábrica de clientes para a instância do tribunal. O principal vai sem
    `tribunal=` (fábricas antigas e de teste recebem só a instância)."""
    if trib == tribunal.atual():
        return fabrica(instancia)
    return fabrica(instancia, tribunal=trib)


def a_conferir_fora() -> str:
    return f"A conferir (fora do {'/'.join(t.sigla for t in tribunal.configurados())})"


def e_do_tribunal(numero: str) -> bool:
    """O processo é do tribunal do escritório (o único que vai ao PJe)?"""
    return validar_cnj(numero)["do_tribunal"]


e_tjmt = e_do_tribunal  # nome antigo (era fixo no TJMT)


_TRIBUNAIS = {("8", "11"): "TJMT", ("8", "13"): "TJMG", ("8", "12"): "TJMS",
              ("8", "04"): "TJAM", ("4", "01"): "TRF1", ("4", "03"): "TRF3"}


def tribunal_do_numero(numero: str) -> str:
    """Sigla do tribunal pelo J.TR do número CNJ ("?" se não reconhecido)."""
    d = _digitos(numero)
    if len(d) != 20:
        return "?"
    return _TRIBUNAIS.get((d[13], d[14:16]), "?")


def _digitos(valor) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def candidatos_do_sistema(raiz: str = ".") -> dict[str, str]:
    """Números que o sistema já conhecia: cache JSON, grupos e planilha mestra."""
    achados: dict[str, str] = {}
    for caminho in sorted(glob.glob(os.path.join(raiz, "cache", "*.json"))):
        numero = _digitos(os.path.basename(caminho))
        if len(numero) == 20:
            achados.setdefault(numero, "sistema:cache")
    for caminho in sorted(glob.glob(os.path.join(raiz, "grupos", "*.json"))):
        if caminho.endswith(".status.json"):
            continue
        try:
            with open(caminho, encoding="utf-8") as f:
                numeros = json.load(f).get("numeros", [])
        except (OSError, ValueError):
            continue
        for n in numeros:
            if len(_digitos(n)) == 20:
                achados.setdefault(_digitos(n), "sistema:grupo")
    for caminho in sorted(glob.glob(os.path.join(raiz, PASTA_PLANILHAS, "*.xlsx"))):
        if os.path.basename(caminho).startswith("~$"):  # trava do Excel (arquivo aberto)
            continue
        try:
            linhas = ler_processos(caminho)
        except Exception:  # planilha ilegível não pode derrubar a varredura
            continue
        for linha in linhas:
            numero = _digitos(linha.get("numero"))
            if len(numero) == 20:
                achados.setdefault(numero, "sistema:planilha")
    return achados


def registrar_candidato(conn: sqlite3.Connection, numero: str, tribunal: str,
                        fonte: str) -> bool:
    """Garante a linha do processo e soma a fonte. True se o processo é novo."""
    linha = conn.execute("SELECT fontes FROM processo WHERE numero = ?", (numero,)).fetchone()
    if linha is None:
        conn.execute("INSERT INTO processo (numero, tribunal, fontes) VALUES (?, ?, ?)",
                     (numero, tribunal, json.dumps([fonte])))
        eventos.registrar(conn, "processo_descoberto", numero, {"fonte": fonte})
        return True
    fontes = json.loads(linha["fontes"])
    if fonte not in fontes:
        fontes.append(fonte)
        conn.execute("UPDATE processo SET fontes = ? WHERE numero = ?",
                     (json.dumps(fontes), numero))
    return False


def descartar(conn: sqlite3.Connection, numero: str) -> bool:
    """Tira o processo da aba "A conferir" (ruído; "Não é meu"). A sincronização do
    PJe deixa de consultá-lo: só o DJEN segue acompanhando-o. Se a OAB do advogado
    passar a constar numa publicação, ele aparece na carteira ativa. Desfaz uma
    confirmação anterior: vale a decisão mais recente."""
    cursor = conn.execute("UPDATE processo SET descartado_em = ?, confirmado_em = NULL "
                          "WHERE numero = ? AND descartado_em IS NULL",
                          (eventos.agora(), numero))
    if cursor.rowcount:
        eventos.registrar(conn, "processo_descartado", numero)
    return bool(cursor.rowcount)


def confirmar(conn: sqlite3.Connection, numero: str) -> bool:
    """"É meu, acompanhar": o advogado põe o processo na carteira (aba Ativa), mesmo
    sem a OAB dele nos dados do tribunal. Desfaz um descarte anterior. True se mudou."""
    cursor = conn.execute("UPDATE processo SET confirmado_em = ?, descartado_em = NULL "
                          "WHERE numero = ? AND confirmado_em IS NULL",
                          (eventos.agora(), numero))
    if cursor.rowcount:
        eventos.registrar(conn, "processo_confirmado", numero)
    return bool(cursor.rowcount)


def restaurar(conn: sqlite3.Connection, numero: str) -> bool:
    cursor = conn.execute("UPDATE processo SET descartado_em = NULL "
                          "WHERE numero = ? AND descartado_em IS NOT NULL", (numero,))
    if cursor.rowcount:
        eventos.registrar(conn, "processo_restaurado", numero)
    return bool(cursor.rowcount)


def arquivar(conn: sqlite3.Connection, numero: str) -> bool:
    """«Arquivar»: o processo sai da Ativa (ou de «A conferir») e vai para «Arquivados»,
    marcado "por você". A varredura do PJe deixa de consultá-lo; o DJEN segue de olho e
    uma publicação nova o traz de volta. Não vale na lixeira nem em Descartados (o
    descartado que passou a ter a OAB está na Ativa e vale). True se mudou."""
    cursor = conn.execute(
        "UPDATE processo SET arquivado_por_voce_em = ? WHERE numero = ? "
        "AND arquivado_por_voce_em IS NULL AND excluido_em IS NULL "
        "AND NOT (descartado_em IS NOT NULL AND (advogado_atua IS NULL OR advogado_atua = 0) "
        "AND confirmado_em IS NULL)",
        (eventos.agora(), numero))
    if cursor.rowcount:
        eventos.registrar(conn, "processo_arquivado", numero)
    return bool(cursor.rowcount)


def desarquivar(conn: sqlite3.Connection, numero: str) -> bool:
    """«Desarquivar»: vale para o arquivado por você e para o arquivado pelo tribunal (as
    duas marcas saem). Do tribunal, guarda a data de arquivamento desfeita
    (`desarquivado_ref`): ele só volta a ser arquivado se o tribunal arquivar de novo,
    com outra data. Não vale na lixeira. True se mudou."""
    p = conn.execute("SELECT arquivado, data_arquivamento, arquivado_por_voce_em, excluido_em "
                     "FROM processo WHERE numero = ?", (numero,)).fetchone()
    if p is None or p["excluido_em"] or not (p["arquivado_por_voce_em"] or p["arquivado"]):
        return False
    if p["arquivado"]:
        conn.execute("UPDATE processo SET arquivado_por_voce_em = NULL, arquivado = 0, "
                     "desarquivado_ref = ? WHERE numero = ?", (p["data_arquivamento"], numero))
    else:
        conn.execute("UPDATE processo SET arquivado_por_voce_em = NULL WHERE numero = ?",
                     (numero,))
    eventos.registrar(conn, "processo_desarquivado", numero,
                      {"por": "tribunal" if p["arquivado"] else "voce"})
    return True


def excluir(conn: sqlite3.Connection, numero: str) -> bool:
    """«Excluir»: o processo vai para a lixeira por DIAS_NA_LIXEIRA dias e sai de todas as
    outras abas. Nada é apagado: publicações, andamentos, cartões, autos e a pasta do caso
    ficam. True se mudou."""
    cursor = conn.execute("UPDATE processo SET excluido_em = ? "
                          "WHERE numero = ? AND excluido_em IS NULL", (eventos.agora(), numero))
    if cursor.rowcount:
        eventos.registrar(conn, "processo_excluido", numero)
    return bool(cursor.rowcount)


def restaurar_da_lixeira(conn: sqlite3.Connection, numero: str) -> bool:
    """«Restaurar» (lixeira): o processo volta para onde estava (Ativa, A conferir,
    Descartados ou Arquivados), mesmo depois dos 30 dias. True se mudou."""
    cursor = conn.execute("UPDATE processo SET excluido_em = NULL "
                          "WHERE numero = ? AND excluido_em IS NOT NULL", (numero,))
    if cursor.rowcount:
        eventos.registrar(conn, "processo_restaurado_da_lixeira", numero)
    return bool(cursor.rowcount)


def _campo(linha, nome: str):
    """Valor da coluna, ou None se a linha não a tem (dicts de teste, linhas antigas)."""
    return linha[nome] if nome in linha.keys() else None


def estado(linha) -> str:
    """Onde o processo está, na prioridade das abas: "lixeira", "arquivado_voce",
    "arquivado_tribunal", "ativa", "descartado" ou "a_conferir"."""
    if _campo(linha, "excluido_em"):
        return "lixeira"
    if _campo(linha, "arquivado_por_voce_em"):
        return "arquivado_voce"
    if _campo(linha, "arquivado"):
        return "arquivado_tribunal"
    if _campo(linha, "advogado_atua") == 1 or _confirmado(linha):
        return "ativa"
    if _campo(linha, "descartado_em"):
        return "descartado"
    return "a_conferir"


def voltar_por_publicacao(conn: sqlite3.Connection, numero: str, data_iso: str,
                          publicacao_id=None) -> str | None:
    """Publicação nova de um processo arquivado por você ou na lixeira, com data de
    disponibilização posterior ao dia da marca: ele volta. O arquivado volta para onde
    estava (Ativa ou A conferir); o da lixeira volta sem «É meu», sem descarte e sem
    arquivo (cai em «A conferir»; na Ativa, se a OAB dele consta no tribunal). Registra
    `processo_voltou`. Devolve de onde voltou ("arquivado" ou "lixeira") ou None."""
    p = conn.execute("SELECT arquivado_por_voce_em, excluido_em FROM processo "
                     "WHERE numero = ?", (numero,)).fetchone()
    if p is None:
        return None
    dia = str(data_iso or "")[:10]
    if p["excluido_em"]:
        if dia <= p["excluido_em"][:10]:
            return None
        conn.execute("UPDATE processo SET excluido_em = NULL, arquivado_por_voce_em = NULL, "
                     "confirmado_em = NULL, descartado_em = NULL WHERE numero = ?", (numero,))
        de = "lixeira"
    elif p["arquivado_por_voce_em"]:
        if dia <= p["arquivado_por_voce_em"][:10]:
            return None
        conn.execute("UPDATE processo SET arquivado_por_voce_em = NULL WHERE numero = ?",
                     (numero,))
        de = "arquivado"
    else:
        return None
    eventos.registrar(conn, "processo_voltou", numero,
                      {"de": de, "publicacao": publicacao_id, "data": dia})
    return de


def gravar_publicacoes_novas(conn: sqlite3.Connection, publicacoes: list[dict]) -> list[dict]:
    """Grava as publicações e devolve só as que ainda não estavam no banco.
    Publicação inédita traz de volta o processo arquivado por você ou na lixeira
    (voltar_por_publicacao)."""
    novas: list[dict] = []
    for p in publicacoes:
        partes_json = json.dumps(p.get("partes") or [], ensure_ascii=False)
        cursor = conn.execute(
            """INSERT OR IGNORE INTO publicacao
                   (id, numero, tribunal, data_disponibilizacao, tipo, orgao, classe,
                    texto, link, oab_confirmada, partes_json)
               VALUES (:id, :numero, :tribunal, :data_disponibilizacao, :tipo, :orgao,
                       :classe, :texto, :link, :oab_confirmada, :partes_json)""",
            {**p, "oab_confirmada": int(p["oab_confirmada"]), "partes_json": partes_json})
        if not cursor.rowcount and p.get("partes"):
            # Já gravada antes da migração 6 (sem partes): completa, sem regravar o resto.
            conn.execute("UPDATE publicacao SET partes_json = ? "
                         "WHERE id = ? AND partes_json = '[]'", (partes_json, p["id"]))
        if cursor.rowcount:
            novas.append(p)
            eventos.registrar(conn, "publicacao_detectada", p["numero"],
                              {"id": p["id"], "data": p["data_disponibilizacao"],
                               "tribunal": p["tribunal"]})
            voltar_por_publicacao(conn, p["numero"], p["data_disponibilizacao"], p["id"])
    return novas


def gravar_publicacoes(conn: sqlite3.Connection, publicacoes: list[dict]) -> int:
    return len(gravar_publicacoes_novas(conn, publicacoes))


def analisar_processo(dados: dict, adv: Advogado) -> dict:
    """Quem é o cliente (partes em que a OAB do advogado aparece, em qualquer
    polo), quem é a parte contrária, situação de arquivamento e último andamento."""
    polos_cliente: list[str] = []
    clientes: list[str] = []
    for polo in dados.get("partes", []):
        for integrante in polo.get("integrantes", []):
            if any(e_o_advogado(a.get("oab", ""), adv.numero_oab, adv.uf_oab)
                   for a in integrante.get("advogados", [])):
                if polo["polo"] not in polos_cliente:
                    polos_cliente.append(polo["polo"])
                if integrante["nome"] not in clientes:
                    clientes.append(integrante["nome"])
    contrarios: list[str] = []
    if polos_cliente:
        opostos = (_OPOSTO[polos_cliente[0]]
                   if len(polos_cliente) == 1 and polos_cliente[0] in _OPOSTO else None)
        alvo = [opostos] if opostos else ["Polo Ativo", "Polo Passivo"]
        contrarios = [nome for polo in dados.get("partes", []) if polo["polo"] in alvo
                      for nome in polo.get("nomes", []) if nome not in clientes]
    andamentos = dados.get("andamentos", [])
    ultimo = andamentos[0] if andamentos else {}
    return {
        "advogado_atua": 1 if polos_cliente else 0,
        "polo_cliente": "; ".join(polos_cliente),
        "cliente": "; ".join(clientes),
        "parte_contraria": "; ".join(dict.fromkeys(contrarios)),
        "data_arquivamento": data_arquivamento(andamentos),
        "ultimo_andamento_data": str(ultimo.get("data_ordenavel", ""))[:8],
        "ultimo_andamento_texto": ultimo.get("texto", ""),
    }


def _registrar_falha(conn: sqlite3.Connection, numero: str, erro: str) -> None:
    conn.execute("UPDATE processo SET erro_sincronizacao = ?, sincronizado_em = ? "
                 "WHERE numero = ?", (erro, eventos.agora(), numero))
    eventos.registrar(conn, "falha_sincronizacao", numero, {"erro": erro})


def _juntar_erros(erros: list[tuple[str, str, str]]) -> str:
    """Erro de cada instância numa linha. "Não encontrado" numa instância é ruído
    quando a outra deu um erro real do PJe (ex.: segredo de justiça); com uma só
    mensagem restante, ela vai sem prefixo."""
    if any(tipo == "pje" for _, _, tipo in erros):
        erros = [e for e in erros if e[2] != "nao_encontrado"]
    if len(erros) == 1:
        return erros[0][1]
    return "; ".join(f"{instancia}: {msg}" for instancia, msg, _ in erros) or "não consultado"


def sincronizar_processo(conn: sqlite3.Connection, numero: str, clientes: dict,
                         adv: Advogado) -> bool:
    """Consulta o PJe (1º grau, depois 2º) e grava partes, análise e andamentos.
    True se conseguiu; False (com os erros de cada instância gravados) se não.
    Só passa para o 2º grau quando o 1º não achou o processo ou veio sem partes;
    qualquer outro erro do PJe (timeout, serviço fora do ar, sigilo) encerra a
    tentativa e mantém os dados já gravados. Senha recusada sobe
    (CredencialInvalidaError). Resposta sem partes (sigilo ou incompleta) conta
    como falha da instância e nunca sobrescreve dados já gravados."""
    erros: list[tuple[str, str, str]] = []  # (instância, mensagem, tipo)
    for instancia in instancias_de_busca(numero):
        try:
            resposta = clientes[instancia].consultar_processo(numero, incluir_movimentos=True)
        except CredencialInvalidaError:
            raise
        except MNIError as exc:
            nao_encontrado = isinstance(exc, ProcessoNaoEncontradoError)
            erros.append((instancia, str(exc), "nao_encontrado" if nao_encontrado else "pje"))
            if nao_encontrado:
                continue
            break  # erro real do PJe (lento, fora do ar, sigilo): o 2º grau não resolve
        dados = processo_para_dict(numero, resposta.processo)
        if not dados["partes"]:
            erros.append((instancia, "resposta sem partes (sigilo ou resposta incompleta)",
                          "sem_partes"))
            continue
        analise = analisar_processo(dados, adv)
        conn.execute(
            """UPDATE processo SET instancia = :instancia, classe = :classe,
                   orgao_julgador = :orgao_julgador, valor_causa = :valor_causa,
                   data_ajuizamento = :data_ajuizamento, polo_cliente = :polo_cliente,
                   cliente = :cliente, parte_contraria = :parte_contraria,
                   advogado_atua = :advogado_atua, data_arquivamento = :data_arquivamento,
                   ultimo_andamento_data = :ultimo_andamento_data,
                   ultimo_andamento_texto = :ultimo_andamento_texto,
                   partes_json = :partes_json, sincronizado_em = :sincronizado_em,
                   erro_sincronizacao = NULL
               WHERE numero = :numero""",
            {**analise, "instancia": instancia, "classe": dados["classe"],
             "orgao_julgador": dados["orgao_julgador"], "valor_causa": dados["valor_causa"],
             "data_ajuizamento": dados.get("data_ajuizamento", ""),
             "partes_json": json.dumps(dados["partes"], ensure_ascii=False),
             "sincronizado_em": eventos.agora(), "numero": numero})
        conn.executemany(
            "INSERT OR IGNORE INTO andamento (numero, data_ordenavel, codigo, texto) "
            "VALUES (?, ?, ?, ?)",
            [(numero, a["data_ordenavel"], a.get("codigo"), a["texto"])
             for a in dados["andamentos"]])
        recalcular_situacao(conn, numero)
        eventos.registrar(conn, "processo_sincronizado", numero,
                          {"instancia": instancia, "andamentos": len(dados["andamentos"])})
        return True
    _registrar_falha(conn, numero, _juntar_erros(erros))
    return False


def recalcular_situacao(conn: sqlite3.Connection, numero: str | None = None) -> None:
    """Última publicação de cada processo, atuação fora do tribunal do escritório
    (pela OAB no DJEN)
    e arquivado (publicação posterior ao arquivamento reabre). Um arquivamento que o
    advogado desfez (desarquivado_ref igual à data_arquivamento) não conta. Com `numero`,
    só aquele processo."""
    sql = "SELECT numero, advogado_atua, data_arquivamento, desarquivado_ref FROM processo"
    linhas = (conn.execute(sql + " WHERE numero = ?", (numero,)) if numero
              else conn.execute(sql)).fetchall()
    for linha in linhas:
        pub = conn.execute(
            "SELECT MAX(data_disponibilizacao) AS ultima, MAX(oab_confirmada) AS oab "
            "FROM publicacao WHERE numero = ?", (linha["numero"],)).fetchone()
        ultima = (pub["ultima"] or "").replace("-", "") or None
        atua = linha["advogado_atua"]
        if atua is None and pub["oab"]:
            atua = 1
        arquivado = esta_arquivado(linha["data_arquivamento"], ultima)
        if arquivado and linha["desarquivado_ref"] == linha["data_arquivamento"]:
            arquivado = False  # o advogado desfez este arquivamento; outro, com outra data, vale
        conn.execute(
            "UPDATE processo SET ultima_publicacao_data = ?, advogado_atua = ?, arquivado = ? "
            "WHERE numero = ?", (ultima, atua, int(arquivado), linha["numero"]))


def situacao(linha) -> str:
    """Rótulo honesto da situação do processo, para a planilha e o painel. Confirmado
    pelo advogado ("É meu") conta como da carteira: Ativo se o PJe também achou a
    atuação; senão, o rótulo de quem atua pelo DJEN. Na lixeira, arquivado por você e
    arquivado (pelo tribunal) vêm antes de tudo."""
    if _campo(linha, "excluido_em"):
        return NA_LIXEIRA
    if _campo(linha, "arquivado_por_voce_em"):
        return ARQUIVADO_POR_VOCE
    if _campo(linha, "arquivado"):
        return ARQUIVADO
    if linha["erro_sincronizacao"]:
        return SEM_ACESSO
    atua = linha["advogado_atua"]
    if _confirmado(linha) and atua != 1:
        return ATUA_PELO_DJEN
    if atua == 1:
        if linha["instancia"] is None:
            return ATUA_PELO_DJEN
        return ATIVO
    if atua is None:
        if linha["tribunal"] in {t.sigla for t in tribunal.configurados()}:
            return A_CONFERIR_NAO_SINCRONIZADO
        return a_conferir_fora()
    return A_CONFERIR_OAB_NAO_CONSTA


def _confirmado(linha) -> bool:
    return "confirmado_em" in linha.keys() and bool(linha["confirmado_em"])


def data_de_referencia(linha) -> str | None:
    """A mais recente entre o último andamento e a última publicação (AAAAMMDD)."""
    datas = [(linha[c] or "")[:8] for c in ("ultimo_andamento_data", "ultima_publicacao_data")]
    return max(datas) or None


def resumo(conn: sqlite3.Connection) -> dict:
    def contar(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "processos": contar("SELECT COUNT(*) FROM processo"),
        "carteira_ativa": contar(f"SELECT COUNT(*) FROM processo WHERE {_FILTROS['ativa']}"),
        "arquivados": contar("SELECT COUNT(*) FROM processo WHERE arquivado = 1"),
        "arquivados_por_voce": contar(
            "SELECT COUNT(*) FROM processo "
            "WHERE arquivado_por_voce_em IS NOT NULL AND excluido_em IS NULL"),
        "excluidos": contar("SELECT COUNT(*) FROM processo WHERE excluido_em IS NOT NULL"),
        "a_conferir": contar(f"SELECT COUNT(*) FROM processo WHERE {_FILTROS['a_conferir']}"),
        "falhas_pje": contar("SELECT COUNT(*) FROM processo WHERE erro_sincronizacao IS NOT NULL"),
        "publicacoes": contar("SELECT COUNT(*) FROM publicacao"),
    }


def _hoje() -> date:
    return datetime.now(tribunal.fuso()).date()


def listar(conn: sqlite3.Connection, situacao: str = "ativa",
           hoje: date | None = None) -> list[sqlite3.Row]:
    """Processos de uma aba, o de movimento mais recente primeiro. `hoje` só pesa na
    lixeira (o que foi excluído há DIAS_NA_LIXEIRA dias ou mais some dela)."""
    if situacao not in _FILTROS:
        raise ValueError(f"situacao deve ser uma de: {', '.join(_FILTROS)} "
                         f"(recebido: {situacao!r}).")
    corte = ((hoje or _hoje()) - timedelta(days=DIAS_NA_LIXEIRA)).isoformat()
    return conn.execute(
        f"SELECT * FROM processo WHERE {_FILTROS[situacao]} "
        "ORDER BY MAX(COALESCE(ultimo_andamento_data, ''), "
        "COALESCE(ultima_publicacao_data, '')) DESC, numero",
        {"corte_lixeira": corte}).fetchall()


def sincronizar_lista(conn: sqlite3.Connection, numeros: list[str], clientes: dict,
                      adv: Advogado, progresso=print) -> tuple[int, int]:
    """Sincroniza os números em ordem (um commit por processo). Devolve (ok, falhas).
    Processo em segredo costuma responder "não autorizado": é falha do processo,
    não senha ruim; só LIMITE_RECUSAS_SEGUIDAS recusas seguidas interrompem."""
    recusas_seguidas = ok_total = falhas = 0
    for i, numero in enumerate(numeros, start=1):
        try:
            with conn:
                ok = sincronizar_processo(conn, numero, clientes, adv)
            recusas_seguidas = 0
        except CredencialInvalidaError as exc:
            recusas_seguidas += 1
            with conn:
                _registrar_falha(conn, numero, str(exc))
            ok = False
            if recusas_seguidas >= LIMITE_RECUSAS_SEGUIDAS:
                raise
        except Exception as exc:  # resposta malformada de um processo não derruba a varredura
            with conn:
                _registrar_falha(conn, numero, f"{type(exc).__name__}: {exc}")
            ok = False
            recusas_seguidas = 0
        ok_total += ok
        falhas += not ok
        progresso(f"[{i}/{len(numeros)}] {numero} — {'ok' if ok else 'falhou'}")
    return ok_total, falhas


def atualizar(conn: sqlite3.Connection, adv: Advogado, *, desde: date,
              ate: date | None = None, obter_djen=None, fabrica=criar_cliente,
              raiz: str = ".", pausa_djen: float = 0.4, progresso=print) -> dict:
    """Varredura completa: DJEN → candidatos → PJe (tribunal do escritório) → situação.
    Idempotente.
    Antes de qualquer coisa, sonda a senha em cada instância: senha recusada
    interrompe a varredura sem tocar no banco."""
    por_tribunal = {trib.sigla: {instancia: criar_para(fabrica, instancia, trib)
                                 for instancia in instancias_de_busca(trib)}
                    for trib in tribunal.configurados()}
    for clientes in por_tribunal.values():
        for instancia, cliente in clientes.items():
            try:
                cliente.consultar_avisos_pendentes()
            except CredencialInvalidaError:
                raise
            except MNIError as exc:  # PJe lento/fora do ar: cada processo registra a sua falha
                progresso(f"Aviso: sonda do PJe ({instancia}) falhou: {exc}")

    publicacoes = buscar_por_advogado(adv.nome, adv.numero_oab, adv.uf_oab, desde,
                                      ate or date.today(), obter=obter_djen, pausa=pausa_djen)
    with conn:
        novas = gravar_publicacoes(conn, publicacoes)
        novos = sum(registrar_candidato(conn, p["numero"], p["tribunal"], "djen")
                    for p in publicacoes)
        novos += sum(registrar_candidato(conn, numero, tribunal_do_numero(numero), fonte)
                     for numero, fonte in candidatos_do_sistema(raiz).items())
    progresso(f"DJEN: {len(publicacoes)} publicação(ões), {novas} nova(s); "
              f"{novos} processo(s) novo(s).")

    # Arquivado por você e lixeira: só o DJEN acompanha (a publicação nova os traz de volta).
    numeros = [r["numero"] for r in conn.execute(
        "SELECT numero FROM processo WHERE arquivado_por_voce_em IS NULL "
        "AND excluido_em IS NULL ORDER BY numero")]
    try:
        for trib in tribunal.configurados():
            sincronizar_lista(conn, [n for n in numeros if tribunal.do_numero(n) == trib],
                              por_tribunal[trib.sigla], adv, progresso)
    finally:
        with conn:
            recalcular_situacao(conn)
    return resumo(conn)
