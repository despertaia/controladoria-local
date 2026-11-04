"""
Painel web local da controladoria processual.

Navegação 100% a partir do cache (rápido, offline). Só fala com o PJe do
tribunal do escritório (TJMT, TJMG…) em duas ações: baixar uma peça e "buscar novos andamentos" (sincronizar).

Uso:
    python painel.py
    # abra http://localhost:5000 no navegador
"""

from __future__ import annotations

import functools
import hmac
import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import warnings
import zipfile
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask, Response, abort, flash, jsonify, make_response, redirect, render_template,
    request, send_file, session, url_for,
)
from markupsafe import Markup

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from captura import cache, credenciais, grupos, tarefas  # noqa: E402
from captura.instancias import INSTANCIAS  # noqa: E402
from captura.downloader import _conteudo_em_bytes, _extensao, _sanitizar  # noqa: E402
from captura.ia import IAError, resumir_processo  # noqa: E402
from captura.exportar import gerar_planilha_xlsx  # noqa: E402
from captura.lote import (  # noqa: E402
    baixar_processo_completo, formatar_tamanho, numeros_da_lista,
)
from captura.mni_client import (  # noqa: E402
    CredencialInvalidaError, MNIClient, MNIError,
)
from captura.processo_parser import (  # noqa: E402
    classificar_lista, formatar_numero_cnj, validar_cnj,
)
from nucleo import acoes_processo  # noqa: E402
from nucleo import agenda as nucleo_agenda  # noqa: E402
from nucleo import apresentacao  # noqa: E402
from nucleo import banco  # noqa: E402
from nucleo import djen  # noqa: E402
from nucleo import carteira as nucleo_carteira  # noqa: E402
from nucleo import cofre as nucleo_cofre  # noqa: E402
from nucleo import cofre_servidor  # noqa: E402
from nucleo import conexao as nucleo_conexao  # noqa: E402
from nucleo import config as nucleo_config  # noqa: E402
from nucleo import credenciais as nucleo_credenciais  # noqa: E402
from nucleo import painel_dados  # noqa: E402
from nucleo import ponte as nucleo_ponte  # noqa: E402
from nucleo import prazos_banco as nucleo_prazos_banco  # noqa: E402
from nucleo import prazos_tela  # noqa: E402
from nucleo import quadro as nucleo_quadro  # noqa: E402
from nucleo import tarefas_fila  # noqa: E402
from nucleo import trabalhador as nucleo_trabalhador  # noqa: E402
from nucleo import tribunal as nucleo_tribunal  # noqa: E402
from nucleo import versao as nucleo_versao  # noqa: E402
from nucleo.exportar_carteira import gerar_xlsx  # noqa: E402
from nucleo.mni_fabrica import criar_cliente  # noqa: E402
from sincronizar import sincronizar_um  # noqa: E402
from ponte_mac.tela_acesso import bp as acesso_lex_bp  # noqa: E402  (modo local)

load_dotenv()


def _carregar_credenciais_do_cofre() -> None:
    """Fora do modo local, põe no ambiente as credenciais do cofre da VPS (o que o
    `.env` já tinha continua valendo se o cofre estiver vazio ou sem chave). No modo
    local quem carrega é o lançador (`local/iniciar.py`). Falha só vai ao log: o painel
    sobe do mesmo jeito, com o `.env`."""
    if nucleo_credenciais._local() or not cofre_servidor.pronto():
        return
    try:
        conn = banco.conectar()
        try:
            nucleo_credenciais.carregar_no_ambiente(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — o painel não deixa de subir por causa do cofre
        logging.getLogger("painel").exception("não consegui carregar as credenciais do cofre")


_carregar_credenciais_do_cofre()

app = Flask(__name__)
# Chave de sessão: em produção vem do .env (SECRET_KEY). O padrão só serve para
# uso local; nunca deve valer num servidor público.
app.secret_key = os.getenv("SECRET_KEY", "controladoria-local-dev")
# Teto do que o Mac do advogado pode enviar de uma vez (peça em Word e PDF, etc.).
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024
app.register_blueprint(acesso_lex_bp)  # tela "Acesso do Lex" (só CONTROLADORIA_LOCAL=1)

# Credenciais de acesso ao painel (login). Se PAINEL_SENHA estiver vazia, o
# login fica desligado (modo local antigo) — em produção é sempre preenchido.
PAINEL_USUARIO = os.getenv("PAINEL_USUARIO", "").strip()
PAINEL_SENHA = os.getenv("PAINEL_SENHA", "")
# Cookie de sessão: não segue em requisições de outros sites; em produção (com
# login ligado) só trafega por HTTPS.
app.config.update(SESSION_COOKIE_SAMESITE="Lax")
if PAINEL_SENHA:
    app.config.update(SESSION_COOKIE_SECURE=True)
# Rotas acessíveis sem login (a própria tela de login e os arquivos estáticos).
# A agenda assinada (`agenda_ics`) entra pelo token próprio: o serviço de calendário não faz login.
_ROTAS_PUBLICAS = {"login", "static", "saude", "agenda_ics"}
# Rotas da ponte do Lex: autenticam pela chave dedicada, sem login nem CSRF. O conjunto
# é preenchido por `_exige_chave_ponte` (só rota com o decorador entra; o caminho
# começar com /ponte/ não basta).
_ENDPOINTS_PONTE: set[str] = set()


def _na_ponte() -> bool:
    return request.endpoint in _ENDPOINTS_PONTE


def _destino_seguro(valor: str | None, padrao: str) -> str:
    """Só aceita caminhos internos (evita open redirect): começa com "/", não com
    "//", e não tem barra invertida nem caractere de controle (o navegador ignora
    tabs e quebras de linha, então "/\\t/site" viraria "//site")."""
    if not valor or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in valor):
        return padrao
    if valor.startswith("/") and not valor.startswith("//"):
        return valor
    return padrao


def _login_exigido() -> bool:
    return bool(PAINEL_SENHA)


@app.before_request
def _exigir_login():
    if not _login_exigido():
        return  # sem senha configurada → comportamento local, sem barreira
    if request.endpoint in _ROTAS_PUBLICAS or _na_ponte():
        return  # a ponte do Lex se autentica pela própria chave
    if session.get("logado"):
        return
    return redirect(url_for("login", proximo=request.path))


app.config.setdefault("CSRF_EXIGIDO", True)
MENSAGEM_400 = "Sessão expirada ou formulário inválido. Recarregue a página."
MENSAGEM_400_PONTE = "Pedido inválido para a ponte do Lex."


def csrf_token() -> str:
    """Token por sessão contra cliques forjados (CSRF)."""
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _conferir_csrf():
    if request.method != "POST" or not app.config.get("CSRF_EXIGIDO", True):
        return
    if _na_ponte():
        return  # a ponte usa a chave dedicada, não sessão
    enviado = request.form.get("csrf") or request.headers.get("X-CSRF") or ""
    esperado = session.get("csrf") or ""
    # Em bytes: compare_digest com str recusa caractere não-ASCII (viraria 500).
    if not esperado or not hmac.compare_digest(enviado.encode(), esperado.encode()):
        abort(400, description=MENSAGEM_400)


@app.errorhandler(400)
def _requisicao_invalida(exc):
    """400 em português: o quadro (JSON) recebe {"ok": false, "erro"}; formulário
    volta para a página de origem (só do mesmo site) com o aviso na tela."""
    if _na_ponte():
        return jsonify({"ok": False, "erro": MENSAGEM_400_PONTE}), 400
    if _quer_json():
        return jsonify({"ok": False, "erro": MENSAGEM_400}), 400
    if request.method != "POST":
        return Response(MENSAGEM_400, status=400, mimetype="text/plain")
    flash(MENSAGEM_400, "erro")
    destino = url_for("lista")
    ref = request.referrer
    if ref:
        p = urlparse(ref)
        if p.netloc == urlparse(request.host_url).netloc and p.path:
            destino = _destino_seguro(p.path + (f"?{p.query}" if p.query else ""), destino)
    return redirect(destino)


# ---------------------------------------------------------------------------
# Modo apresentação: mostrar o sistema sem expor clientes (nomes fictícios,
# números CNJ mascarados, textos livres ocultos, downloads e busca indisponíveis).
# ---------------------------------------------------------------------------

MENSAGEM_APRESENTACAO = "Indisponível no modo apresentação."
_INDISPONIVEIS_NA_APRESENTACAO = {
    "buscar", "demanda_peca_pdf", "demanda_peca_docx", "carteira_exportar", "baixar_zip",
    "documento", "grupo_exportar", "grupo_zip", "baixar_lote_stream", "baixar_processo_stream",
    "grupo_detalhe", "grupo_status", "grupo_baixar", "grupo_atualizar", "grupo_excluir",
    "grupo_incluir", "grupo_retirar",
    "grupo_novo", "configuracoes_lex_chave", "configuracoes_lex_revogar",
    "carteira_arquivar", "carteira_desarquivar", "carteira_excluir",
    "carteira_restaurar_da_lixeira",  # 1.5.0: as ações de arquivo somem no modo
    "demanda_pasta_abrir",  # o caminho da pasta do caso fica oculto na apresentação
    # 1.6.0: as ações de prazo somem no modo
    "prazo_conferir", "prazo_corrigir", "prazo_descartar", "prazo_trabalho", "prazo_dobro",
    "prazo_cumprido", "prazo_desfazer_cumprido", "prazo_fatal", "prazo_ciencia",
    "prazo_ler_de_novo", "configuracoes_prazos_padrao", "configuracoes_prazos_incluir",
    "configuracoes_prazos_corrigir_dia", "configuracoes_prazos_desativar",
    "configuracoes_prazos_agenda_gerar", "configuracoes_prazos_agenda_revogar", "agenda_baixar",
}
PREFIXO_TRIBUNAIS = "/configuracoes/tribunais"
_APELIDO = re.compile(r"p[0-9a-f]{12}")
_VINTE_DIGITOS = re.compile(r"(?<!\d)\d{20}(?!\d)")


def _modo_apresentacao() -> bool:
    return bool(session.get("apresentacao"))


def _chave_derivada(finalidade: bytes) -> bytes:
    return hmac.new(str(app.secret_key).encode(), finalidade, "sha256").digest()


def _apelido(numero: str) -> str:
    """Apelido estável do número na URL (o número real não aparece na tela nem na
    barra de endereço); o painel volta do apelido ao número pelo banco."""
    return "p" + hmac.new(_chave_derivada(b"apresentacao-apelido"), numero.encode(),
                          "sha256").hexdigest()[:12]


_SQL_NUMEROS = "SELECT numero FROM processo UNION SELECT numero FROM demanda"
_SQL_ASSINATURA = ("SELECT (SELECT COALESCE(MAX(id), 0) FROM evento), "
                   "(SELECT COUNT(*) FROM processo), (SELECT COALESCE(MAX(rowid), 0) FROM processo), "
                   "(SELECT COALESCE(MAX(id), 0) FROM demanda)")
_cache_apresentacao: dict = {}


def _dados_da_apresentacao(conn: sqlite3.Connection) -> dict:
    """{nomes, apelidos (número → apelido), reverso}, recalculados só quando o banco
    muda (versão do quadro = último evento, mais contagem de processos e cartões).
    O cache novo é montado à parte e trocado de uma vez (threads do gunicorn)."""
    global _cache_apresentacao
    chave = (banco.caminho_do_banco(), str(app.secret_key),
             tuple(conn.execute(_SQL_ASSINATURA).fetchone()))
    atual = _cache_apresentacao
    if atual.get("chave") != chave:
        apelidos = {r[0]: _apelido(r[0]) for r in conn.execute(_SQL_NUMEROS)}
        atual = {"chave": chave, "nomes": apresentacao.nomes_conhecidos(conn),
                 "apelidos": apelidos, "reverso": {a: n for n, a in apelidos.items()}}
        _cache_apresentacao = atual
    return atual


def _resolver_apelido(valor: str | None) -> str:
    valor = (valor or "").strip()
    if not _APELIDO.fullmatch(valor):
        return valor
    conn = banco.conectar()
    try:
        return _dados_da_apresentacao(conn)["reverso"].get(valor, valor)
    finally:
        conn.close()


def _indisponivel():
    return render_template("indisponivel.html", mensagem=MENSAGEM_APRESENTACAO), 403


@app.before_request
def _bloquear_na_apresentacao():
    """Busca no DJEN, peça (PDF/Word), downloads e a tela de senhas dos tribunais:
    indisponíveis no modo (403)."""
    if not _modo_apresentacao():
        return None
    if (request.endpoint in _INDISPONIVEIS_NA_APRESENTACAO
            or request.path.startswith(PREFIXO_TRIBUNAIS)):
        return _indisponivel()


@app.url_value_preprocessor
def _numero_por_apelido(endpoint, valores):
    if valores and isinstance(valores.get("numero"), str):
        valores["numero"] = _resolver_apelido(valores["numero"])


def _trocar_numeros(texto: str, apelidos: dict[str, str]) -> str:
    return _VINTE_DIGITOS.sub(lambda m: apelidos.get(m.group(0), m.group(0)), texto)


@app.context_processor
def _ctx_apresentacao():
    return {"modo_apresentacao": _modo_apresentacao(), "texto_oculto": apresentacao.TEXTO_OCULTO}


# ---------------------------------------------------------------------------
# Versão (rodapé, /novidades) e aviso de versão nova (só no modo local). A consulta ao
# GitHub roda numa thread; a página só lê o cache.
# ---------------------------------------------------------------------------

@app.before_request
def _verificar_versao_publicada():
    nucleo_versao.verificar_em_segundo_plano()  # sem efeito fora do modo local


@app.context_processor
def _ctx_versao():
    return {"versao_atual": nucleo_versao.atual(),
            "aviso_versao": None if _modo_apresentacao() else nucleo_versao.aviso_de_versao()}


@app.route("/novidades")
def novidades():
    return render_template("novidades.html", novidades=nucleo_versao.novidades())


@app.route("/apresentacao", methods=["POST"])
def apresentacao_alternar():
    ligado = not _modo_apresentacao()
    session["apresentacao"] = ligado
    flash("Modo apresentação ligado: nomes fictícios e números mascarados." if ligado
          else "Modo apresentação desligado.", "ok")
    return redirect(_destino_seguro(request.form.get("voltar"), url_for("lista")))


@app.after_request
def _mascarar_na_apresentacao(resposta):
    """Com o modo ligado: toda página HTML sai com nomes fictícios e números
    mascarados; redirecionamentos levam o apelido no lugar do número. JSON e arquivos
    seguem como estão (não levam nomes; os arquivos ficam indisponíveis no modo)."""
    html = (resposta.mimetype == "text/html" and not resposta.direct_passthrough
            and resposta.status_code not in (204, 304))
    if not (html or resposta.location) or not _modo_apresentacao():
        return resposta
    conn = banco.conectar()
    try:
        dados = _dados_da_apresentacao(conn)
    finally:
        conn.close()
    nomes, apelidos = dados["nomes"], dados["apelidos"]
    if resposta.location:
        resposta.location = _trocar_numeros(resposta.location, apelidos)
    if html:
        # Nada de página mascarada (ou não) guardada pelo navegador para o Voltar.
        resposta.headers["Cache-Control"] = "no-store"
        texto = _trocar_numeros(resposta.get_data(as_text=True), apelidos)
        resposta.set_data(apresentacao.mascarar_html(
            texto, nomes, _chave_derivada(b"apresentacao-cnj"),
            chave_nomes=_chave_derivada(b"apresentacao-nome")))
    return resposta


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _login_exigido() or session.get("logado"):
        return redirect(url_for("lista"))
    if request.method == "POST":
        usuario = (request.form.get("usuario") or "").strip()
        senha = request.form.get("senha") or ""
        # compare_digest evita vazar, pelo tempo de resposta, se acertou o usuário.
        # Em bytes: aceita acento sem erro interno.
        ok_usuario = hmac.compare_digest(usuario.encode(), PAINEL_USUARIO.encode())
        ok_senha = hmac.compare_digest(senha.encode(), PAINEL_SENHA.encode())
        if ok_usuario and ok_senha:
            session["logado"] = True
            session.permanent = True
            return redirect(_destino_seguro(request.args.get("proximo"), url_for("lista")))
        flash("Usuário ou senha incorretos.", "erro")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Sessão encerrada.", "ok")
    return redirect(url_for("login"))

_clientes: dict[tuple, MNIClient] = {}


class CredencialPJeAusente(MNIError):
    """Não há credencial do PJe disponível (modo sessão e usuário não conectou)."""


def _modo_sessao() -> bool:
    """True quando a senha do PJe NÃO está no ambiente (PJE_SENHA/TJMT_SENHA) →
    vem da sessão (caminho 2)."""
    return not nucleo_tribunal.senha()


def _credencial_atual(sigla: str | None = None) -> dict | None:
    """Credencial do PJe vigente: do .env (modo armazenado; a do tribunal `sigla`,
    padrão o principal) ou da sessão (modo caminho 2).
    Retorna {'cpf','senha'[, 'senha_2grau']} ou None se não houver."""
    if not _modo_sessao():
        return credenciais.credencial_do_env(sigla)
    return credenciais.obter(session.get("pje_token"))


def pje_conectado() -> bool:
    return _credencial_atual() is not None


@app.template_filter("md")
def markdown_basico(texto: str) -> str:
    """Converte um subconjunto seguro de markdown (##, -, **) em HTML."""
    if not texto:
        return ""

    def negrito(s: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    partes: list[str] = []
    em_lista = False
    for linha in texto.split("\n"):
        bruto = linha.strip()
        if not bruto:
            if em_lista:
                partes.append("</ul>")
                em_lista = False
            continue
        if bruto.startswith("## "):
            if em_lista:
                partes.append("</ul>")
                em_lista = False
            partes.append(f"<h3>{html.escape(bruto[3:].strip())}</h3>")
        elif bruto.startswith("- "):
            if not em_lista:
                partes.append("<ul>")
                em_lista = True
            partes.append(f"<li>{negrito(html.escape(bruto[2:].strip()))}</li>")
        else:
            if em_lista:
                partes.append("</ul>")
                em_lista = False
            partes.append(f"<p>{negrito(html.escape(bruto))}</p>")
    if em_lista:
        partes.append("</ul>")
    return "".join(partes)


def obter_cliente(instancia: str, cpf: str, senha: str, tribunal=None) -> MNIClient:
    """Cria (e reaproveita) o cliente MNI para (tribunal, instância, cpf). O WSDL é o
    mesmo por instância do tribunal (padrão, o principal); a credencial é fornecida
    pelo chamador (sessão ou env)."""
    if not cpf or not senha:
        raise CredencialPJeAusente(
            "Conecte-se ao PJe (informe CPF e senha) para consultar o tribunal.")
    trib = nucleo_tribunal.obter(tribunal)
    # inclui a senha (reconectar troca o cliente) e o tribunal (cada um tem o seu PJe)
    chave = (trib.sigla, instancia, cpf, senha)
    cliente = _clientes.get(chave)
    if cliente is None:
        cliente = criar_cliente(instancia, {"cpf": cpf, "senha": senha}, tribunal=trib)
        _clientes[chave] = cliente
    return cliente


def cliente_pje(instancia: str = "1grau", numero: str | None = None) -> MNIClient:
    """Cliente MNI usando a credencial vigente (sessão/env), no PJe do tribunal do
    `numero` (sem número, o principal). Levanta CredencialPJeAusente se o usuário
    ainda não se conectou ao PJe."""
    trib = nucleo_carteira.tribunal_de(numero)
    cred = _credencial_atual(trib.sigla)
    if cred is None:
        raise CredencialPJeAusente(
            "Conecte-se ao PJe (informe CPF e senha) para consultar o tribunal.")
    # O principal vai sem `tribunal=` (como antes); os demais, com o tribunal deles.
    extras = {} if trib == nucleo_tribunal.atual() else {"tribunal": trib}
    return obter_cliente(instancia, cred["cpf"],
                         credenciais.senha_da_instancia(cred, instancia), **extras)


def _cliente_do_processo(numero: str, instancia: str = "1grau") -> MNIClient:
    """`cliente_pje` no tribunal do processo; o principal vai sem o número (como antes)."""
    if nucleo_carteira.tribunal_de(numero) == nucleo_tribunal.atual():
        return cliente_pje(instancia)
    return cliente_pje(instancia, numero)


def _siglas() -> str:
    """Siglas dos tribunais do escritório ("TJMT" ou "TJMT/TJMG"), para os avisos."""
    return "/".join(t.sigla for t in nucleo_tribunal.configurados())


def _ctx_tribunal_do(numero: str) -> dict:
    """Sigla e instâncias do tribunal DO processo (sobrepõem as do principal na tela)."""
    trib = nucleo_tribunal.do_numero(numero)
    if trib is None:
        return {}
    return {"tribunal_sigla": trib.sigla, "tribunal_tem_2grau": trib.tem_instancia("2grau"),
            "tribunal_instancias": [rotulo for _chave, rotulo, *_ in trib.instancias]}


# Disponibiliza ao worker de segundo plano a fábrica de clientes (recebe a
# credencial do job) e as instâncias.
tarefas.configurar(obter_cliente, INSTANCIAS)


@app.errorhandler(CredencialPJeAusente)
def _sem_credencial_pje(exc):
    import painel_local  # importado no fim deste arquivo (ciclo)
    if painel_local.ativo():  # instalação local: a senha mora na Configuração do escritório
        flash("Para consultar o PJe e baixar os autos, marque o tribunal e guarde a senha "
              "do PJe na Configuração do escritório.", "aviso")
        return redirect(url_for("local.configurar"))
    flash("Conecte-se ao PJe para fazer consultas no tribunal.", "erro")
    # Volta para a página de origem (GET) após conectar — não para a rota POST.
    proximo = url_for("lista")
    ref = request.referrer
    if ref:
        p = urlparse(ref)
        if p.netloc == urlparse(request.host_url).netloc and p.path:
            proximo = p.path
    return redirect(url_for("conectar_pje", proximo=proximo))


@app.context_processor
def _ctx_pje():
    atual = nucleo_tribunal.atual()
    return {"pje_conectado": pje_conectado(), "pje_modo_sessao": _modo_sessao(),
            "painel_exige_login": _login_exigido(), "tribunal_sigla": atual.sigla,
            "tribunais_siglas": _siglas(),
            "tribunal_tem_2grau": atual.tem_instancia("2grau"),
            "tribunal_instancias": [rotulo for _chave, rotulo, *_ in atual.instancias]}


def _credencial_valida_no_pje(cpf: str, senha: str) -> bool:
    """Confere a credencial listando os avisos pendentes no 1º grau (não abre
    teor nem registra ciência). Só rejeita se o login for recusado; falha de
    serviço não prova senha errada. Assim 'Conectado' quer dizer autenticado."""
    try:
        obter_cliente("1grau", cpf, senha).consultar_avisos_pendentes()
    except CredencialInvalidaError:
        return False
    except MNIError:
        return True   # o login não foi recusado; a falha foi do serviço
    return True


@app.route("/conectar-pje", methods=["GET", "POST"])
def conectar_pje():
    """Modo caminho 2: usuário informa CPF+senha do PJe; guardamos só em memória
    (com TTL), nunca em disco/cookie. O cofre do navegador preenche o formulário."""
    if not _modo_sessao():
        # Instância em modo armazenado (.env): não há o que conectar.
        return redirect(url_for("lista"))
    if request.method == "POST":
        cpf = "".join(c for c in (request.form.get("cpf") or "") if c.isdigit())
        senha = request.form.get("senha") or ""
        if len(cpf) != 11 or not senha:
            flash("Informe um CPF (11 dígitos) e a senha do PJe.", "erro")
        elif not _credencial_valida_no_pje(cpf, senha):
            flash("O PJe recusou essas credenciais — confira CPF e senha "
                  "(e se a senha do PJe não expirou).", "erro")
        else:
            token = session.get("pje_token") or credenciais.novo_token()
            credenciais.guardar(token, cpf, senha)
            session["pje_token"] = token
            destino = _destino_seguro(request.args.get("proximo"), url_for("lista"))
            flash("Conectado ao PJe nesta sessão (credenciais verificadas).", "ok")
            return redirect(destino)
    return render_template("conectar_pje.html")


@app.route("/desconectar-pje", methods=["POST"])
def desconectar_pje():
    credenciais.limpar(session.pop("pje_token", None))
    flash("Desconectado do PJe.", "ok")
    return redirect(url_for("lista"))


@app.route("/")
def lista():
    """Cockpit: números da carteira, atividade recente e distribuição por tribunal."""
    agora = painel_dados.agora_cuiaba()
    conn = banco.conectar()
    try:
        contexto = {
            "kpis": painel_dados.kpis(conn, agora.date()),
            "atividade": painel_dados.atividade_recente(conn),
            "por_tribunal": painel_dados.por_tribunal(conn),
            "por_situacao": painel_dados.por_situacao(conn),
            "ultima_varredura": painel_dados.ultima_varredura(conn),
            "quadro": painel_dados.quadro(conn, agora.date()),
            "atividade_agora": painel_dados.atividade_agora(conn),
            "faixa_voltaram": painel_dados.faixa_voltaram(conn, agora.date()),
        }
    finally:
        conn.close()
    return render_template(
        "cockpit.html", saudacao=painel_dados.saudacao(agora),
        primeiro_nome=painel_dados.primeiro_nome(os.getenv("CARTEIRA_ADVOGADO_NOME", "")),
        **contexto)


_MESES_CURTOS = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
_PERIODOS_RESULTADOS = (("30d", "30 dias"), ("12m", "12 meses"), ("tudo", "Tudo"))


def _barras(pares: list[tuple[str, int]]) -> list[dict]:
    """Linhas de barra: a maior quantidade ocupa 100% da largura."""
    maior = max((q for _, q in pares), default=0)
    return [{"rotulo": r, "quantidade": q, "percentual": round(100 * q / maior) if maior else 0}
            for r, q in pares]


def _rotulo_mes(aaaa_mm: str) -> str:
    ano, _, mes = aaaa_mm.partition("-")
    try:
        return f"{_MESES_CURTOS[int(mes) - 1]}/{ano}"
    except (ValueError, IndexError):
        return aaaa_mm


@app.route("/resultados")
def resultados_tela():
    """Resultados do Lex: tempo até a peça ficar pronta, peças por mês e por tipo."""
    periodo = request.args.get("periodo", "12m")
    if periodo not in painel_dados.PERIODOS:
        periodo = "12m"
    conn = banco.conectar()
    try:
        r = painel_dados.resultados(conn, periodo)
    finally:
        conn.close()
    por_mes = _barras([(_rotulo_mes(m), q) for m, q in r["pecas_por_mes"]])
    return render_template(
        "resultados.html", r=r, por_mes=por_mes, por_tipo=_barras(r["pecas_por_tipo"]),
        periodos=_PERIODOS_RESULTADOS)


def _quer_json() -> bool:
    return "application/json" in (request.headers.get("Accept") or "")


def _resposta_quadro(ok: bool, mensagem: str, desfazer: str | None = None):
    if _quer_json():
        if ok:
            return jsonify({"ok": True, "mensagem": mensagem, "desfazer": desfazer})
        return jsonify({"ok": False, "erro": mensagem}), 409
    flash(mensagem, "ok" if ok else "erro")
    # Formulários do cartão aberto mandam `voltar`; só caminhos internos valem.
    return redirect(_destino_seguro(request.form.get("voltar"), url_for("lista")))


@app.route("/quadro/fragmento")
def quadro_fragmento():
    conn = banco.conectar()
    try:
        atual = painel_dados.versao(conn)
        if request.args.get("versao", type=int) == atual:
            return Response(status=204)
        contexto = {"quadro": painel_dados.quadro(conn, painel_dados.hoje_cuiaba()),
                    "atividade_agora": painel_dados.atividade_agora(conn)}
    finally:
        conn.close()
    resposta = Response(render_template("_quadro.html", **contexto))
    resposta.headers["X-Versao"] = str(contexto["quadro"]["versao"])
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


app.jinja_env.globals["selo_citacao"] = painel_dados.selo_citacao
app.jinja_env.filters["nota_md"] = painel_dados.markdown_minimo


@app.route("/demanda/<int:demanda_id>")
def demanda_tela(demanda_id: int):
    conn = banco.conectar()
    try:
        d = painel_dados.demanda_detalhe(conn, demanda_id, painel_dados.hoje_cuiaba(),
                                         _pasta_das_pecas())
    finally:
        conn.close()
    if d is None:
        abort(404)
    return render_template("demanda.html", d=d)


def _arquivo_da_peca(demanda_id: int, n: int, nome: str):
    """(caminho do arquivo, número do processo) do pacote da execução `n`. 404 se o cartão
    ou a execução não existem, ou se o arquivo não está dentro da pasta das peças (o
    caminho é montado a partir de números; nada vem do usuário)."""
    conn = banco.conectar()
    try:
        linha = conn.execute(
            "SELECT d.numero FROM demanda d JOIN execucao_lex e ON e.demanda_id = d.id "
            "WHERE d.id = ? AND e.n = ?", (demanda_id, n)).fetchone()
    finally:
        conn.close()
    if linha is None:
        abort(404)
    base = os.path.realpath(_pasta_das_pecas())
    caminho = os.path.realpath(os.path.join(base, str(demanda_id), str(n), nome))
    if not caminho.startswith(base + os.sep) or not os.path.isfile(caminho):
        abort(404)
    return caminho, linha["numero"]


@app.route("/demanda/<int:demanda_id>/peca/<int:n>.pdf")
def demanda_peca_pdf(demanda_id: int, n: int):
    caminho, _ = _arquivo_da_peca(demanda_id, n, "peca.pdf")
    resposta = send_file(caminho, mimetype="application/pdf", as_attachment=False,
                         download_name="peca.pdf", conditional=True)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Content-Security-Policy"] = (
        "default-src 'none'; object-src 'self'; frame-ancestors 'self'")
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.route("/demanda/<int:demanda_id>/peca/<int:n>.docx")
def demanda_peca_docx(demanda_id: int, n: int):
    caminho, numero = _arquivo_da_peca(demanda_id, n, "peca.docx")
    resposta = send_file(
        caminho, as_attachment=True, download_name=f"peca-{_sanitizar(numero)}-v{n}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        conditional=True)
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.route("/demanda/<int:demanda_id>/mover", methods=["POST"])
def demanda_mover(demanda_id: int):
    para = request.form.get("para") or ""
    conn = banco.conectar()
    try:
        with conn:
            r = nucleo_quadro.mover(conn, demanda_id, para, "advogado",
                                    orientacao=(request.form.get("orientacao") or "")[:600],
                                    ajuste=(request.form.get("ajuste") or "")[:2000],
                                    agora=painel_dados.agora_cuiaba())
    except nucleo_quadro.MovimentoInvalido as exc:
        return _resposta_quadro(False, str(exc))
    except nucleo_prazos_banco.TransacaoPerdida as exc:
        # A transação inteira se foi (o movimento também): nada mudou.
        logging.getLogger(__name__).warning("mover: transação perdida (%s)", type(exc).__name__)
        return _resposta_quadro(False, nucleo_quadro.MSG_PRAZOS_NAO_VOLTARAM)
    except sqlite3.IntegrityError:
        # Corrida rara: o índice "um cartão novo por processo" recusou no meio do caminho.
        # (Erro durante o desfazer dos prazos chega como PrazosNaoVoltaram, acima.)
        return _resposta_quadro(False, "Já há um cartão novo deste processo esperando triagem.")
    except sqlite3.OperationalError as exc:  # trava de escrita além do tempo: nada foi feito
        logging.getLogger(__name__).warning("mover: banco (%s)", type(exc).__name__)
        return _resposta_quadro(False, MSG_BANCO_OCUPADO)
    finally:
        conn.close()
    mensagem = f"Movido para “{nucleo_quadro.COLUNAS[r['para']]}”."
    if r.get("prazos_falha"):
        mensagem = ("O cartão foi movido, mas os prazos dele não foram marcados. "
                    "Confira na aba Prazos.")
    else:
        n, sem_data = r.get("prazos_cumpridos", 0), r.get("prazos_nao_lidos", 0)
        if n:
            mensagem += " 1 prazo marcado como cumprido." if n == 1 else f" {n} prazos marcados como cumpridos."
        if sem_data:
            mensagem += (" 1 ainda sem data continua aberto." if sem_data == 1
                         else f" {sem_data} ainda sem data continuam abertos.")
    return _resposta_quadro(True, mensagem, r["desfazer"])


@app.route("/demanda/<int:demanda_id>/pasta/abrir", methods=["POST"])
def demanda_pasta_abrir(demanda_id: int):
    """Abre a pasta `autos` do caso no explorador de arquivos. Só no modo local (o
    computador do mentorado é o que tem a pasta); na VPS não existe."""
    import painel_local  # importado no fim deste arquivo (ciclo)
    if not painel_local.ativo():
        abort(404)
    destino = _destino_seguro(request.form.get("voltar"), url_for("demanda_tela",
                                                                  demanda_id=demanda_id))
    conn = banco.conectar()
    try:
        d = conn.execute("SELECT pasta_nome FROM demanda WHERE id = ?", (demanda_id,)).fetchone()
    finally:
        conn.close()
    if d is None:
        abort(404)
    if not d["pasta_nome"]:
        flash("Este cartão ainda não tem pasta.", "erro")
        return redirect(destino)
    from ponte_mac import local as ponte_local
    casa_texto = ponte_local.casa_da_banca()
    if not casa_texto:
        flash("A casa da Banca ainda não foi escolhida em Configurações.", "erro")
        return redirect(destino)
    casa = os.path.realpath(casa_texto)
    pasta = os.path.realpath(os.path.join(casa, d["pasta_nome"], "autos"))
    if not pasta.startswith(casa.rstrip(os.sep) + os.sep):
        flash("Não abri a pasta: ela fica fora da casa da Banca.", "erro")
        return redirect(destino)
    if not os.path.isdir(os.path.dirname(pasta)):
        # Só o Lex cria a pasta do processo (com o nome dele); aqui, no máximo, `autos/`.
        flash("A pasta ainda não foi criada pelo Lex.", "erro")
        return redirect(destino)
    try:
        os.makedirs(pasta, exist_ok=True)
        subprocess.Popen(["explorer" if sys.platform == "win32" else "open", pasta])
    except OSError:
        flash("Não consegui abrir a pasta. Copie o caminho e abra à mão.", "erro")
    return redirect(destino)


@app.route("/demanda/<int:demanda_id>/autos/tentar", methods=["POST"])
def demanda_autos_tentar(demanda_id: int):
    conn = banco.conectar()
    try:
        with conn:
            nucleo_quadro.tentar_autos_de_novo(conn, demanda_id)
    except nucleo_quadro.MovimentoInvalido as exc:
        return _resposta_quadro(False, str(exc))
    finally:
        conn.close()
    return _resposta_quadro(True, "Download dos autos pedido de novo.")


@app.route("/demanda/<int:demanda_id>/lex/tentar", methods=["POST"])
def demanda_lex_tentar(demanda_id: int):
    conn = banco.conectar()
    try:
        with conn:
            nucleo_quadro.tentar_lex_de_novo(conn, demanda_id)
    except nucleo_quadro.MovimentoInvalido as exc:
        return _resposta_quadro(False, str(exc))
    finally:
        conn.close()
    return _resposta_quadro(True, "Pedido ao Lex de novo.")


@app.route("/lex/automatico", methods=["POST"])
def lex_automatico_alternar():
    ligado = request.form.get("ligado") == "1"
    conn = banco.conectar()
    try:
        with conn:
            n = nucleo_config.definir_lex_automatico(conn, ligado)
    finally:
        conn.close()
    if not ligado:
        return _resposta_quadro(True, "Lex automático desligado.")
    return _resposta_quadro(
        True, f"Lex automático ligado: {n} cartão(ões) foram para o Lex.")


# --- configurações do Lex (chave automática, estado do Mac, chave da ponte) ----------

def _estado_do_mac(ultimo: str | None, sem_plano: bool = False) -> dict:
    """Texto e tom do estado da ponte: contato nos últimos
    `nucleo_ponte.CONTATO_RECENTE_MINUTOS` minutos = conectado. Na instalação local o Lex
    roda no próprio computador (Windows ou Mac): o texto não fala em "Mac"."""
    local = os.getenv("CONTROLADORIA_LOCAL", "").strip() == "1"
    if not ultimo:
        return {"texto": "Ainda não começou (aguarde 1 minuto)" if local
                else "Nunca conectado", "tom": "neutro"}
    try:
        quando = datetime.fromisoformat(ultimo)
    except ValueError:
        return {"texto": "Nunca conectado", "tom": "neutro"}
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=painel_dados.FUSO)
    quando = quando.astimezone(painel_dados.FUSO)
    passado = painel_dados.agora_cuiaba() - quando
    minutos = max(0, int(passado.total_seconds() // 60))
    if passado <= timedelta(minutes=nucleo_ponte.CONTATO_RECENTE_MINUTOS):
        if sem_plano:
            return {"texto": "Lex rodando, mas falta conectar ao plano do Claude" if local
                    else "Mac conectado, sem acesso ao plano do Lex", "tom": "alerta"}
        return {"texto": "Conectado agora" if minutos == 0 else f"Conectado há {minutos} min",
                "tom": "ok"}
    return {"texto": f"Sem contato desde {quando.strftime('%d/%m %H:%M')}", "tom": "alerta"}


def _hora_curta(iso: str | None) -> str:
    """"às HH:MM" se foi hoje; senão "em dd/mm às HH:MM"."""
    try:
        quando = datetime.fromisoformat(iso or "")
    except ValueError:
        return ""
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=painel_dados.FUSO)
    quando = quando.astimezone(painel_dados.FUSO)
    if quando.date() == painel_dados.hoje_cuiaba():
        return quando.strftime("às %H:%M")
    return quando.strftime("em %d/%m às %H:%M")


def _data_da_chave(criada_em: str | None) -> str:
    try:
        quando = datetime.fromisoformat(criada_em or "")
    except ValueError:
        return ""
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=painel_dados.FUSO)
    return quando.astimezone(painel_dados.FUSO).strftime("%d/%m/%Y %H:%M")


@app.route("/configuracoes/lex")
def configuracoes_lex():
    conn = banco.conectar()
    try:
        automatico = nucleo_config.lex_automatico(conn)
        ultimo = nucleo_ponte.ultimo_contato(conn)
        criada = nucleo_config.ler(conn, nucleo_ponte.CHAVE_CRIADA_EM)
        tem_chave = bool(nucleo_config.ler(conn, nucleo_ponte.CHAVE_HASH))
        sem_plano = nucleo_ponte.mac_sem_plano(conn, painel_dados.agora_cuiaba())
        invalidas, ultima_invalida = nucleo_ponte.chaves_invalidas(conn)
    finally:
        conn.close()
    return render_template("configuracoes_lex.html", automatico=automatico,
                           mac=_estado_do_mac(ultimo, sem_plano), tem_chave=tem_chave,
                           chave_criada_em=_data_da_chave(criada) if tem_chave else "",
                           chave=None, chaves_invalidas=invalidas,
                           ultima_invalida=_hora_curta(ultima_invalida))


@app.route("/configuracoes/lex/chave", methods=["POST"])
def configuracoes_lex_chave():
    """Gera a chave e a mostra UMA vez, direto na resposta (nunca em flash, log, redirect
    ou GET); `no-store` para o navegador não guardá-la no cache."""
    conn = banco.conectar()
    try:
        with conn:
            chave = nucleo_ponte.gerar_chave(conn)
        criada = nucleo_config.ler(conn, nucleo_ponte.CHAVE_CRIADA_EM)
    finally:
        conn.close()
    resposta = make_response(render_template(
        "configuracoes_lex.html", automatico=None, mac=None, tem_chave=True,
        chave_criada_em=_data_da_chave(criada), chave=chave))
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.route("/configuracoes/lex/revogar", methods=["POST"])
def configuracoes_lex_revogar():
    conn = banco.conectar()
    try:
        with conn:
            nucleo_ponte.revogar_chave(conn)
    finally:
        conn.close()
    flash("Chave revogada. O Mac perdeu o acesso até você gerar uma nova.", "ok")
    return redirect(url_for("configuracoes_lex"))


@app.context_processor
def _ctx_conexao():
    """Avisos de conexão com o PJe (senha recusada, PJe fora do ar) para a faixa do topo."""
    if _modo_apresentacao() or (_login_exigido() and not session.get("logado")):
        return {"avisos_conexao": []}
    try:
        conn = banco.conectar()
        try:
            return {"avisos_conexao": nucleo_conexao.avisos(conn)}
        finally:
            conn.close()
    except Exception:  # a faixa nunca derruba a página (banco, cofre ou ambiente)
        return {"avisos_conexao": []}


# ---------------------------------------------------------------------------
# Configurações › Tribunais: CPF e senha do PJe de cada instância, com teste de login.
# ---------------------------------------------------------------------------

MENSAGEM_COFRE_INDISPONIVEL = "O cofre de senhas não está disponível neste servidor."
MENSAGEM_COFRE_LOCAL_GUARDAR = "O cofre deste computador não aceitou guardar. Nada foi guardado."
MENSAGEM_COFRE_LOCAL_APAGAR = "O cofre deste computador não aceitou apagar."
MENSAGEM_COFRE_LOCAL_LER = "O cofre deste computador não respondeu."
_ROTULO_SELO = {
    "ok": ("ok", "Conectado"),
    "recusada": ("alerta", "Senha recusada"),
    "instavel": ("aviso", "Instável"),
    "fora": ("aviso", "Fora do ar"),
    "nao_verificado": ("neutro", "Guardada · ainda não conferida"),
    "nao_cadastrado": ("neutro", "Não cadastrado"),
    "redigitar": ("alerta", "Digite a senha de novo"),
}
MENSAGEM_SENHA_NAO_ABRE = ("A senha guardada do {sigla} ({rotulo}) não abre com a chave atual "
                           "do cofre. Digite-a de novo.")


def _instancia_do_formulario():
    """(sigla, instancia, rótulo) se a combinação é homologada; senão None (e flash)."""
    sigla = (request.form.get("sigla") or "").strip().upper()
    instancia = (request.form.get("instancia") or "").strip()
    for s, i, rotulo in nucleo_credenciais.INSTANCIAS_HOMOLOGADAS:
        if (s, i) == (sigla, instancia):
            return s, i, rotulo
    flash("Tribunal ou instância desconhecidos.", "erro")
    return None


def _voltar_aos_tribunais():
    return redirect(url_for("configuracoes_tribunais"))


@app.route("/configuracoes/tribunais")
def configuracoes_tribunais():
    conn = banco.conectar()
    try:
        # "sem_chave": nada pode ser guardado (botões desabilitados). "nao_abre": a chave
        # atual serve, mas há senha (ou o CPF) cifrada com outra: libera regravar.
        estado_cofre = nucleo_credenciais.estado_do_cofre(conn)
        cofre_ok = estado_cofre != "sem_chave"
        try:
            cpf = nucleo_credenciais.cpf_guardado(conn)
        except cofre_servidor.CofreServidorError:
            cpf = ""
        linhas = []
        for s in nucleo_conexao.situacao(conn):
            tom, texto = _ROTULO_SELO[s["selo"]]
            if s["selo"] == "ok":
                texto = f"Conectado · conferido {_hora_curta(s['verificado_em'])}".rstrip()
            linhas.append({**s, "selo_tom": tom, "selo_texto": texto})
    finally:
        conn.close()
    return render_template("configuracoes_tribunais.html", linhas=linhas, cofre_ok=cofre_ok,
                           cofre_nao_abre=estado_cofre == "nao_abre",
                           cpf_final=cpf[-2:] if cpf else "", tem_cpf=bool(cpf))


@app.route("/configuracoes/tribunais/cpf", methods=["POST"])
def configuracoes_tribunais_cpf():
    digitos = "".join(c for c in (request.form.get("cpf") or "") if c.isdigit())
    if len(digitos) != 11:
        flash("CPF inválido: use os 11 números.", "erro")
        return _voltar_aos_tribunais()
    conn = banco.conectar()
    try:
        with conn:
            nucleo_credenciais.gravar_cpf(conn, digitos)
        nucleo_credenciais.carregar_no_ambiente(conn)
    except cofre_servidor.CofreServidorError:
        flash(f"{MENSAGEM_COFRE_INDISPONIVEL} Nada foi guardado.", "erro")
        return _voltar_aos_tribunais()
    except nucleo_cofre.CofreError:
        flash(MENSAGEM_COFRE_LOCAL_GUARDAR, "erro")
        return _voltar_aos_tribunais()
    finally:
        conn.close()
    flash("CPF guardado.", "ok")
    return _voltar_aos_tribunais()


@app.route("/configuracoes/tribunais/salvar", methods=["POST"])
def configuracoes_tribunais_salvar():
    alvo = _instancia_do_formulario()
    if alvo is None:
        return _voltar_aos_tribunais()
    sigla, instancia, rotulo = alvo
    senha = request.form.get("senha") or ""
    mesmo_assim = request.form.get("mesmo_assim") == "1"
    conn = banco.conectar()
    try:
        cpf = nucleo_credenciais.cpf_guardado(conn)
        if not cpf:
            flash("Guarde primeiro o seu CPF.", "erro")
        elif not senha:
            flash("Digite a senha.", "erro")
        else:
            estado, detalhe = nucleo_conexao.testar(sigla, instancia, cpf, senha)
            detalhe = (detalhe or "")[:300]
            if estado == "ok":
                with conn:
                    nucleo_credenciais.gravar(conn, sigla, instancia, senha)
                    nucleo_conexao.registrar(conn, sigla, instancia, "ok", detalhe)
                nucleo_credenciais.carregar_no_ambiente(conn)
                flash(f"Senha do {sigla} ({rotulo}) guardada. Login aceito pelo PJe.", "ok")
            elif estado == "recusada":
                flash(f"O PJe recusou a senha do {sigla} ({rotulo}). Nada foi guardado.", "erro")
            elif mesmo_assim:
                with conn:
                    nucleo_credenciais.gravar(conn, sigla, instancia, senha)
                    nucleo_conexao.registrar(conn, sigla, instancia, "fora", detalhe)
                nucleo_credenciais.carregar_no_ambiente(conn)
                flash(f"Senha guardada sem teste: o PJe do {sigla} não respondeu. "
                      "A próxima varredura confere.", "aviso")
            else:
                flash(f"O PJe do {sigla} não respondeu agora. Para guardar sem testar, digite "
                      "a senha de novo e marque “Guardar mesmo assim”.", "aviso")
    except cofre_servidor.CofreServidorError:
        flash(f"{MENSAGEM_COFRE_INDISPONIVEL} Nada foi guardado.", "erro")
    except nucleo_cofre.CofreError:
        flash(MENSAGEM_COFRE_LOCAL_GUARDAR, "erro")
    finally:
        conn.close()
    return _voltar_aos_tribunais()


@app.route("/configuracoes/tribunais/testar", methods=["POST"])
def configuracoes_tribunais_testar():
    alvo = _instancia_do_formulario()
    if alvo is None:
        return _voltar_aos_tribunais()
    sigla, instancia, rotulo = alvo
    conn = banco.conectar()
    try:
        cred = nucleo_credenciais.ler(conn, sigla, instancia)
        if cred is None:
            flash("Nada guardado para testar.", "erro")
            return _voltar_aos_tribunais()
        estado, detalhe = nucleo_conexao.testar(sigla, instancia, cred["cpf"], cred["senha"])
        with conn:
            nucleo_conexao.registrar(conn, sigla, instancia, estado, (detalhe or "")[:300])
        if estado == "ok":
            flash(f"Login do {sigla} ({rotulo}) aceito pelo PJe.", "ok")
        elif estado == "recusada":
            flash(f"O PJe recusou a senha do {sigla} ({rotulo}). Troque a senha abaixo.", "erro")
        else:
            flash(f"O PJe do {sigla} não respondeu agora. A próxima varredura tenta de novo.",
                  "aviso")
    except cofre_servidor.CofreServidorError:
        if nucleo_credenciais.estado_do_cofre(conn) == "nao_abre":
            flash(MENSAGEM_SENHA_NAO_ABRE.format(sigla=sigla, rotulo=rotulo), "erro")
        else:
            flash(MENSAGEM_COFRE_INDISPONIVEL, "erro")
    except nucleo_cofre.CofreError:
        flash(MENSAGEM_COFRE_LOCAL_LER, "erro")
    finally:
        conn.close()
    return _voltar_aos_tribunais()


@app.route("/configuracoes/tribunais/remover", methods=["POST"])
def configuracoes_tribunais_remover():
    alvo = _instancia_do_formulario()
    if alvo is None:
        return _voltar_aos_tribunais()
    sigla, instancia, rotulo = alvo
    conn = banco.conectar()
    try:
        with conn:
            nucleo_credenciais.apagar(conn, sigla, instancia)
            nucleo_conexao.registrar(conn, sigla, instancia, "nao_cadastrado")
        nucleo_credenciais.carregar_no_ambiente(conn)
    except cofre_servidor.CofreServidorError:
        flash(MENSAGEM_COFRE_INDISPONIVEL, "erro")
        return _voltar_aos_tribunais()
    except nucleo_cofre.CofreError:
        flash(MENSAGEM_COFRE_LOCAL_APAGAR, "erro")
        return _voltar_aos_tribunais()
    finally:
        conn.close()
    flash(f"Senha do {sigla} ({rotulo}) removida.", "ok")
    return _voltar_aos_tribunais()


@app.route("/varrer", methods=["POST"])
def varrer():
    conn = banco.conectar()
    try:
        with conn:
            criada = tarefas_fila.pedir(conn, "varredura", por="advogado")
    finally:
        conn.close()
    if not criada:
        return _resposta_quadro(False, "Já há uma varredura na fila ou em andamento.")
    return _resposta_quadro(True, "Varredura pedida. O quadro se atualiza sozinho.")


@app.context_processor
def _ctx_quadro():
    if _login_exigido() and not session.get("logado"):
        return {"na_triagem": 0}
    try:
        conn = banco.conectar()
        try:
            return {"na_triagem": nucleo_quadro.contar_na_triagem(conn)}
        finally:
            conn.close()
    except sqlite3.Error:
        return {"na_triagem": 0}


@app.route("/processo/<numero>")
def processo(numero: str):
    """Processo: cabeçalho e linha do tempo do banco; peças e resumo do cache."""
    conn = banco.conectar()
    try:
        detalhe = painel_dados.processo_detalhe(conn, numero, painel_dados.hoje_cuiaba())
    finally:
        conn.close()
    dados = cache.ler(numero)
    if detalhe is None and dados is not None and _modo_apresentacao():
        return _indisponivel()  # só no cache: as partes de lá não passam pela máscara
    numero_formatado = ((detalhe or {}).get("numero_formatado")
                        or (dados or {}).get("numero_formatado")
                        or formatar_numero_cnj(numero))
    digitos = "".join(c for c in numero if c.isdigit())
    return render_template("processo.html", numero=numero, dados=dados, detalhe=detalhe,
                           numero_formatado=numero_formatado,
                           e_tjmt=nucleo_carteira.e_tjmt(digitos), **_ctx_tribunal_do(digitos))


@app.route("/documento/<numero>/<id_doc>")
def documento(numero: str, id_doc: str):
    """Baixa a peça ao vivo no PJe do tribunal e entrega ao navegador."""
    cliente = _cliente_do_processo(numero)  # CredencialPJeAusente → redireciona p/ conectar
    try:
        baixados = cliente.baixar_documentos(numero, [id_doc])
    except MNIError as exc:
        abort(502, description=str(exc))

    doc = next((d for d in baixados if str(getattr(d, "idDocumento", "")) == id_doc), None)
    if doc is None and baixados:
        doc = baixados[0]
    if doc is None:
        abort(404, description="Documento não retornado pelo tribunal.")

    conteudo = _conteudo_em_bytes(getattr(doc, "conteudo", None))
    if not conteudo:
        abort(404, description="Conteúdo vazio.")

    mimetype = getattr(doc, "mimetype", None) or "application/octet-stream"
    descricao = getattr(doc, "descricao", None) or "documento"
    # Completa metadados pelo cache, se necessário.
    dados = cache.ler(numero)
    if dados:
        meta = next((x for x in dados["documentos"] if x["id"] == id_doc), None)
        if meta:
            descricao = meta.get("descricao") or descricao
            mimetype = meta.get("mimetype") or mimetype

    nome = f"{_sanitizar(descricao)}{_extensao(mimetype)}"
    disposicao = "attachment" if request.args.get("download") else "inline"
    return Response(
        conteudo,
        mimetype=mimetype,
        headers={"Content-Disposition": f'{disposicao}; filename="{nome}"'},
    )


def _numero_conhecido(digitos: str) -> bool:
    """O número já está no banco ou no cache (abrir não precisa do PJe)."""
    conn = banco.conectar()
    try:
        no_banco = bool(painel_dados.numeros_na_carteira(conn, [digitos]))
    finally:
        conn.close()
    return no_banco or cache.ler(digitos) is not None


def _consultar_numero(digitos: str) -> Response:
    """Abre o processo pelo número CNJ (20 dígitos). Se ele já está no banco ou
    no cache, vai direto; senão consulta o PJe e guarda antes de abrir."""
    if not _numero_conhecido(digitos):
        cliente = _cliente_do_processo(digitos)  # CredencialPJeAusente → redireciona p/ conectar
        try:
            sincronizar_um(cliente, digitos)
        except (MNIError, ValueError) as exc:
            flash(f"Falha ao consultar: {exc}", "erro")
            return redirect(url_for("carteira_tela"))
    return redirect(url_for("processo", numero=digitos))


@app.route("/consultar", methods=["POST"])
def consultar():
    """Consulta avulsa: aceita um número CNJ, sincroniza se necessário,
    e redireciona para a página do processo."""
    numero_bruto = (request.form.get("numero") or "").strip()
    digitos = "".join(c for c in numero_bruto if c.isdigit())
    if len(digitos) != 20:
        flash("Número inválido — informe os 20 dígitos do CNJ.", "erro")
        return redirect(url_for("carteira_tela"))
    return _consultar_numero(digitos)


_TRIBUNAIS_BUSCA = ("TJMT", "TRF1", "TRF3", "TJMS", "TJAM")


def _tribunais_busca() -> tuple[str, ...]:
    """Tribunais do filtro de publicações: os do escritório primeiro (o principal é o
    padrão)."""
    siglas = [t.sigla for t in nucleo_tribunal.configurados()]
    return (*siglas, *(t for t in _TRIBUNAIS_BUSCA if t not in siglas))
_PERIODOS_BUSCA = (3, 12, 24)
_MINIMO_BUSCA = 4


@app.route("/buscar")
def buscar():
    """Campo único do topo: 20 dígitos abrem o processo (como a consulta avulsa);
    um nome procura na carteira e nas publicações do DJEN."""
    termo = (request.args.get("q") or "").strip()
    tribunais = _tribunais_busca()
    tribunal = request.args.get("tribunal")
    tribunal = tribunal if tribunal in ("", *tribunais) else tribunais[0]
    meses = request.args.get("meses", type=int)
    meses = meses if meses in _PERIODOS_BUSCA else 12
    contexto = {"q": termo, "tribunal": tribunal, "meses": meses,
                "tribunais": tribunais, "periodos": _PERIODOS_BUSCA,
                "estado": "vazio", "carteira": [], "djen": [], "truncado": False,
                "djen_erro": False}
    if not termo:
        return render_template("busca.html", **contexto)
    digitos = "".join(c for c in termo if c.isdigit())
    if len(digitos) == 20 and not any(c.isalpha() for c in termo):
        # GET não tem efeito colateral: número conhecido abre; desconhecido só
        # consulta o PJe depois de um clique (POST em /consultar).
        if _numero_conhecido(digitos):
            return redirect(url_for("processo", numero=digitos))
        return render_template("busca.html", **{**contexto, "estado": "numero_novo",
                                                "numero": digitos,
                                                "numero_formatado": formatar_numero_cnj(digitos),
                                                "e_tjmt": nucleo_carteira.e_tjmt(digitos),
                                                **_ctx_tribunal_do(digitos)})
    if sum(c.isalnum() for c in termo) < _MINIMO_BUSCA:
        return render_template("busca.html", **{**contexto, "estado": "curto"})

    hoje = painel_dados.hoje_cuiaba()
    conn = banco.conectar()
    try:
        contexto["carteira"] = painel_dados.buscar_na_carteira(conn, termo, hoje)
        try:
            resultado = djen.buscar_por_parte(
                termo, tribunal, hoje - timedelta(days=meses * 30), hoje,
                obter=djen.obter_interativo, pausa=0.2)
        except djen.DJENError:
            contexto["djen_erro"] = True
        else:
            grupos = djen.agrupar_por_processo(resultado["publicacoes"])
            ja_tem = painel_dados.numeros_na_carteira(conn, [g["numero"] for g in grupos])
            contexto["djen"] = [{**g, "na_carteira": g["numero"] in ja_tem,
                                 "e_tjmt": nucleo_tribunal.do_numero(g["numero"]) is not None}
                                for g in grupos]
            contexto["truncado"] = resultado["truncado"]
    finally:
        conn.close()
    return render_template("busca.html", **{**contexto, "estado": "resultado",
                                            "limite": djen.LIMITE_BUSCA_NOME})


_ORIGENS_ADICIONAR = ("manual", "busca")
MENSAGEM_NUMERO_INVALIDO = ("Número inválido — informe os 20 dígitos do número CNJ "
                            "(com ou sem pontos e traço).")


@app.route("/carteira/adicionar", methods=["POST"])
def carteira_adicionar():
    """"Adicionar e acompanhar": o processo entra na carteira já confirmado (aba Ativa),
    venha da busca, da página do processo ou do número digitado na carteira; desfaz um
    descarte anterior. Não há tarefa de fila para um processo só: a próxima varredura
    passa a consultá-lo no DJEN pelo número (e no PJe, se for de um tribunal do
    escritório)."""
    numero = _resolver_apelido(request.form.get("numero"))
    numero = "".join(c for c in numero if c.isdigit())
    destino = _destino_seguro(request.form.get("voltar"), url_for("carteira_tela"))
    if len(numero) != 20:
        flash(MENSAGEM_NUMERO_INVALIDO, "erro")
        return redirect(destino)
    origem = request.form.get("origem")
    origem = origem if origem in _ORIGENS_ADICIONAR else "manual"
    conn = banco.conectar()
    try:
        with conn:
            linha = conn.execute("SELECT * FROM processo WHERE numero = ?", (numero,)).fetchone()
            guardado = (linha is not None
                        and nucleo_carteira.estado(linha) in ("arquivado_voce", "lixeira"))
            ja_estava = False
            if not guardado:
                ja_estava = conn.execute(
                    f"SELECT 1 FROM processo WHERE numero = ? AND ({nucleo_carteira.FILTRO_ATIVA})",
                    (numero,)).fetchone() is not None
                nucleo_carteira.registrar_candidato(
                    conn, numero, nucleo_carteira.tribunal_do_numero(numero), origem)
                if nucleo_carteira.confirmar(conn, numero):
                    nucleo_quadro.devolver_ao_quadro(conn, numero)  # se tinha sido "Não é meu"
    finally:
        conn.close()
    if guardado:
        flash("Este processo está arquivado ou na lixeira. Use «Desarquivar» ou «Restaurar» "
              "na página dele.", "erro")
    elif ja_estava:
        flash("Este processo já está na sua carteira.", "ok")
    else:
        flash("Processo adicionado à sua carteira. A Controladoria passa a acompanhá-lo "
              "nas próximas conferências.", "ok")
    return redirect(url_for("processo", numero=numero))


class _ClientesPreguicosos(dict):
    """Clientes do PJe por instância (no tribunal do `numero`), criados só quando
    alguém os pede: uma instância fora do ar não atrapalha a outra."""

    def __init__(self, numero: str | None = None):
        super().__init__()
        self.numero = numero

    def __missing__(self, instancia):
        cliente = self[instancia] = _cliente_do_processo(self.numero, instancia)
        return cliente


class _ConsultaUnica:
    """Repassa ao cliente do PJe, mas repete a resposta de consultar_processo
    para o mesmo número: o botão Atualizar grava cache e banco com uma consulta só."""

    def __init__(self, cliente):
        self._cliente = cliente
        self._ultima = None

    def consultar_processo(self, numero, incluir_movimentos=False):
        chave = ("".join(c for c in str(numero) if c.isdigit()), incluir_movimentos)
        if self._ultima and self._ultima[0] == chave:
            return self._ultima[1]
        resposta = self._cliente.consultar_processo(numero, incluir_movimentos=incluir_movimentos)
        self._ultima = (chave, resposta)
        return resposta

    def __getattr__(self, nome):
        return getattr(self._cliente, nome)


def _primeira_frase(exc: Exception, limite: int = 160) -> str:
    """Primeira frase do erro, sem detalhes técnicos longos, para mostrar na tela."""
    texto = " ".join(str(exc).split())
    frase = re.split(r"(?<=[.!?])\s", texto, maxsplit=1)[0]
    if len(frase) > limite:
        frase = frase[:limite].rstrip() + "…"
    return frase or "erro desconhecido"


@app.route("/atualizar/<numero>", methods=["POST"])
def atualizar(numero: str):
    """Atualiza do PJe: peças/andamentos no cache (1º grau) e a carteira no banco.
    Só o tribunal do escritório vai ao PJe; os demais chegam pelo DJEN."""
    digitos = "".join(c for c in numero if c.isdigit())
    if not nucleo_carteira.e_do_tribunal(digitos):
        flash(f"Este processo não é do {_siglas()}; "
              "os movimentos chegam pelo DJEN.", "erro")
        return redirect(url_for("processo", numero=numero))

    primeiro = None
    try:
        primeiro = _ConsultaUnica(_cliente_do_processo(digitos))
        dados = sincronizar_um(primeiro, digitos)
        flash(f"Atualizado: {len(dados['andamentos'])} andamentos, "
              f"{len(dados['documentos'])} documentos.", "ok")
    except CredencialPJeAusente:
        raise  # redireciona para conectar
    except (MNIError, ValueError) as exc:
        # Os processos só do 2º grau não têm peças no cache do 1º: segue para o banco.
        flash(f"Peças do 1º grau não atualizadas: {_primeira_frase(exc)}", "erro")

    clientes = _ClientesPreguicosos(digitos)
    if primeiro is not None:
        clientes["1grau"] = primeiro
    conn = banco.conectar()
    try:
        with conn:
            nucleo_carteira.registrar_candidato(
                conn, digitos, nucleo_carteira.tribunal_de(digitos).sigla, "painel")
            ok = nucleo_carteira.sincronizar_processo(
                conn, digitos, clientes, nucleo_carteira.advogado_do_env())
        if not ok:
            flash("A carteira não foi atualizada: veja o aviso no cabeçalho do processo.",
                  "erro")
    except CredencialPJeAusente:
        raise
    except (MNIError, ValueError) as exc:
        flash(f"Carteira não atualizada: {_primeira_frase(exc)}", "erro")
    finally:
        conn.close()
    return redirect(url_for("processo", numero=numero))


@app.route("/resumir/<numero>", methods=["POST"])
def resumir(numero: str):
    dados = cache.ler(numero)
    if dados is None:
        flash("Sincronize o processo antes de gerar o resumo.", "erro")
        return redirect(url_for("processo", numero=numero))

    api_key = os.getenv("LLM_API_KEY", "").strip()
    modelo = os.getenv("LLM_MODEL", "").strip() or "claude-opus-4-7"
    try:
        texto = resumir_processo(dados, api_key=api_key, modelo=modelo)
        cache.gravar_resumo(numero, texto)
        flash("Resumo gerado com sucesso.", "ok")
    except IAError as exc:
        flash(f"Não foi possível gerar o resumo: {exc}", "erro")
    return redirect(url_for("processo", numero=numero))


@app.route("/saude")
def saude():
    """Verificação de saúde (pública): o app responde e o banco abre. Usada pelo
    deploy/publicar.sh. Não expõe contagens nem dados."""
    try:
        conn = banco.conectar()
        try:
            conn.execute("SELECT 1 FROM processo LIMIT 1").fetchall()
            ultima = conn.execute("SELECT MAX(quando) FROM evento "
                                  "WHERE tipo = 'varredura_concluida'").fetchone()[0]
            # Chave do cofre ausente ou que não abre as senhas não derruba o painel:
            # `ok` só depende do banco.
            try:
                cofre = nucleo_credenciais.estado_do_cofre(conn)
            except Exception:  # noqa: BLE001 — o estado do cofre nunca derruba a saúde
                cofre = "sem_chave"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — verificação de saúde: qualquer falha é "não saudável"
        return jsonify({"ok": False, "erro": type(exc).__name__}), 503
    horas = None
    if ultima:
        horas = round((painel_dados.agora_cuiaba()
                       - datetime.fromisoformat(ultima)).total_seconds() / 3600, 1)
    return jsonify({"ok": True, "versao": nucleo_versao.atual(),
                    "trabalhador": nucleo_trabalhador.estado_da_batida(),
                    "cofre": cofre,
                    "horas_desde_varredura": horas})


# --- ponte com o Lex (o Mac do advogado chama com a chave dedicada) -----------------

_ARQUIVOS_RESULTADO = (  # campo enviado -> nome fixo gravado (o nome do upload nunca vale)
    ("peca_docx", "peca.docx", True),
    ("peca_pdf", "peca.pdf", True),
    ("citation_gate", "citation-gate.json", True),
    ("nota", "nota-ao-revisor.md", False),
    ("termo", "termo-de-conferencia.docx", False),
    ("manifesto", "manifesto.json", False),
)


def _erro_ponte(mensagem: str, status: int):
    return jsonify({"ok": False, "erro": mensagem}), status


def _pasta_das_pecas() -> str:
    return os.getenv("CONTROLADORIA_PECAS", "dados/pecas")


def _com_chave_da_ponte(rota, registrar):
    """Exige `Authorization: Bearer <chave>`; abre o banco, registra o contato com `registrar` e
    passa a conexão à rota (fechada ao fim). Chave ausente ou errada: 401 (e conta a
    tentativa nas Configurações). Registra a rota como da ponte (sem login nem CSRF)."""
    _ENDPOINTS_PONTE.add(rota.__name__)

    @functools.wraps(rota)
    def envolta(*args, **kwargs):
        cabecalho = request.headers.get("Authorization") or ""
        enviada = cabecalho[7:].strip() if cabecalho[:7].lower() == "bearer " else ""
        conn = banco.conectar()
        try:
            if not nucleo_ponte.chave_valida(conn, enviada):
                with conn:
                    nucleo_ponte.registrar_chave_invalida(conn)
                return _erro_ponte("chave inválida", 401)
            with conn:
                registrar(conn)
            return rota(conn, *args, **kwargs)
        finally:
            conn.close()
    return envolta


def _exige_chave_ponte(rota):
    """A rota do Mac do squad: o pedido conta como contato do Mac."""
    return _com_chave_da_ponte(rota, nucleo_ponte.registrar_contato)


def _exige_chave_do_leitor(rota):
    """A rota do leitor de prazos: o pedido conta só como contato do leitor, nunca do Mac do
    squad. Banco ocupado responde 503, e o leitor espera e tenta de novo."""
    com_chave = _com_chave_da_ponte(rota, nucleo_ponte.registrar_contato_do_leitor)

    @functools.wraps(rota)
    def envolta(*args, **kwargs):
        try:
            return com_chave(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            logging.getLogger(__name__).warning("leitura de prazo: banco (%s)", type(exc).__name__)
            return _erro_ponte("banco ocupado", 503)
    return envolta


def _status_da_recusa(exc: ValueError) -> int:
    """Estado incoerente (cartão saiu do Lex, execução fechada, movimento recusado): 409;
    campo mal formado: 400."""
    if isinstance(exc, (nucleo_ponte.PonteInvalida, nucleo_quadro.MovimentoInvalido)):
        return 409
    return 400


def _inteiro(valor, nome: str) -> int:
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Campo “{nome}” inválido.") from None


@app.errorhandler(413)
def _arquivo_grande_demais(exc):
    if _na_ponte():
        return _erro_ponte("arquivo grande demais", 413)
    return exc


@app.route("/ponte/proximo", methods=["POST"])
@_exige_chave_ponte
def ponte_proximo(conn):
    with conn:  # reserva o cartão e abre a execução numa transação só
        pacote = nucleo_ponte.proximo(conn, painel_dados.agora_cuiaba())
    if pacote is None:
        return "", 204
    return jsonify(pacote)


_LEITURA_MAX_CORPO = 64 * 1024


@app.route("/ponte/leituras/proxima")
@_exige_chave_do_leitor
def ponte_leitura_proxima(conn):
    """A próxima publicação para o Lex ler. Fila e fio próprios: não espera peça nenhuma."""
    with conn:  # reserva numa transação só
        pacote = nucleo_prazos_banco.proxima_leitura(conn, painel_dados.agora_cuiaba())
    if pacote is None:
        return "", 204
    return jsonify(pacote)


@app.route("/ponte/leituras/<int:prazo_id>", methods=["POST"])
@_exige_chave_do_leitor
def ponte_leitura_resposta(conn, prazo_id: int):
    """A resposta crua do Lex, conferida aqui no servidor, ou o aviso de falha. Resposta ou
    falha tardia (o prazo já foi lido por outro leitor ou preenchido à mão) é um 409 limpo."""
    if (request.content_length or 0) > _LEITURA_MAX_CORPO:
        return _erro_ponte("Pedido inválido.", 400)
    if len(request.get_data(cache=True)) > _LEITURA_MAX_CORPO:  # o tamanho lido, não o declarado
        return _erro_ponte("Pedido inválido.", 400)
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        return _erro_ponte("Pedido inválido.", 400)
    resposta, falha, reserva = corpo.get("resposta"), corpo.get("falha"), corpo.get("reserva")
    if falha is not None and falha not in nucleo_prazos_banco.MOTIVOS_DE_FALHA:
        return _erro_ponte("Campo “falha” inválido.", 400)
    if falha is None and not isinstance(resposta, str):
        return _erro_ponte("Campo “resposta” inválido.", 400)
    if reserva is not None and not isinstance(reserva, str):
        return _erro_ponte("Campo “reserva” inválido.", 400)
    try:
        with conn:
            if falha is not None:
                nucleo_prazos_banco.falhar_leitura(conn, prazo_id, falha, reserva)
                return jsonify({"ok": True})
            estado = nucleo_prazos_banco.aplicar_leitura(conn, prazo_id, resposta, reserva)
    except nucleo_prazos_banco.PrazoInvalido as exc:
        return _erro_ponte(str(exc), 409)
    return jsonify({"ok": True, "estado": estado})


@app.route("/ponte/contato", methods=["POST"])
@_exige_chave_ponte
def ponte_contato(conn):
    """O Mac avisa que está vivo e se tem acesso ao plano do Lex (`claude_ok`: 0 ou 1).
    Nenhum efeito além de anotar o contato."""
    corpo = request.get_json(silent=True)
    valor = corpo.get("claude_ok") if isinstance(corpo, dict) else None
    if valor not in (0, 1, "0", "1"):  # True/False contam como 1/0
        return _erro_ponte("Campo “claude_ok” inválido (use 0 ou 1).", 400)
    with conn:
        nucleo_ponte.registrar_contato(conn, claude_ok=valor in (1, "1"))
    return jsonify({"ok": True})


@app.route("/ponte/pastas", methods=["POST"])
@_exige_chave_ponte
def ponte_pastas(conn):
    """O Mac pergunta quais casos esperam pasta e, no mesmo pedido, avisa as que criou
    (`criadas`: demanda + nome da pasta). Demanda que já não espera é ignorada."""
    corpo = request.get_json(silent=True)
    corpo = corpo if isinstance(corpo, dict) else {}
    criadas = corpo.get("criadas") or []
    try:
        if not isinstance(criadas, list):
            raise ValueError("Campo “criadas” inválido.")
        validas = []
        for item in criadas:
            if not isinstance(item, dict) or not isinstance(item.get("pasta_nome"), str):
                raise ValueError("Cada pasta criada precisa de “demanda” e “pasta_nome”.")
            nome = item["pasta_nome"]  # exatamente como o Mac criou (sem juntar espaços)
            if (not nome.strip() or len(nome) > 200 or "/" in nome or "\\" in nome
                    or ".." in nome or any(ord(c) < 32 for c in nome)):
                raise ValueError("Campo “pasta_nome” inválido.")
            validas.append((_inteiro(item.get("demanda"), "demanda"), nome))
    except ValueError as exc:
        return _erro_ponte(str(exc), 400)
    with conn:
        for demanda_id, nome in validas:
            d = conn.execute("SELECT 1 FROM demanda WHERE id = ? AND coluna = 'autos' "
                             "AND pasta_estado = 'faltam'", (demanda_id,)).fetchone()
            if d is not None:
                nucleo_quadro.registrar_pasta(conn, demanda_id, nome)
    return jsonify({"pendentes": nucleo_ponte.pastas_pendentes(conn)})


_PONTE_PASTA_MAX_CORPO = 64 * 1024


@app.route("/ponte/autos-na-pasta", methods=["POST"])
@_exige_chave_ponte
def ponte_autos_na_pasta(conn):
    """O Mac avisa que o PDF dos autos apareceu na pasta do caso. Só nome e tamanho dos
    arquivos: o conteúdo nunca sobe."""
    if (request.content_length or 0) > _PONTE_PASTA_MAX_CORPO:
        return _erro_ponte("arquivo grande demais", 413)
    bruto = request.get_data()
    if len(bruto) > _PONTE_PASTA_MAX_CORPO:
        return _erro_ponte("arquivo grande demais", 413)
    corpo = request.get_json(silent=True)
    corpo = corpo if isinstance(corpo, dict) else {}
    arquivos = corpo.get("arquivos")
    try:
        demanda_id = _inteiro(corpo.get("demanda"), "demanda")
        if not isinstance(arquivos, list) or not arquivos:
            raise ValueError("Campo “arquivos” inválido: envie nome e tamanho de cada arquivo.")
        lista = []
        for a in arquivos:
            tamanho = a.get("tamanho") if isinstance(a, dict) else None
            if (not isinstance(a, dict) or not isinstance(a.get("nome"), str)
                    or isinstance(tamanho, bool) or not isinstance(tamanho, int) or tamanho < 0):
                raise ValueError("Cada arquivo precisa de “nome” e “tamanho”.")
            lista.append({"nome": a["nome"], "tamanho": tamanho})  # nenhum outro campo passa
        with conn:
            na_fila = nucleo_quadro.autos_na_pasta(conn, demanda_id, lista)
    except ValueError as exc:  # MovimentoInvalido também é ValueError
        return _erro_ponte(str(exc), _status_da_recusa(exc))
    return jsonify({"ok": True, "na_fila": na_fila})


@app.route("/ponte/demanda/<int:demanda_id>/autos")
@_exige_chave_ponte
def ponte_autos(conn, demanda_id: int):
    d = conn.execute("SELECT numero, coluna, lex_estado FROM demanda WHERE id = ?",
                     (demanda_id,)).fetchone()
    if d is None:
        return _erro_ponte("cartão não encontrado", 404)
    if d["coluna"] != "lex" or d["lex_estado"] not in ("reservado", "trabalhando"):
        return _erro_ponte("Este cartão não está reservado para o Lex.", 409)
    tmp, tamanho = _zip_de_numeros([d["numero"]])
    if tmp is None:
        return "", 204
    return _resposta_zip(tmp, tamanho, f"autos-{d['numero']}.zip")


@app.route("/ponte/demanda/<int:demanda_id>/batida", methods=["POST"])
@_exige_chave_ponte
def ponte_batida(conn, demanda_id: int):
    corpo = request.get_json(silent=True)
    corpo = corpo if isinstance(corpo, dict) else {}
    try:
        n = _inteiro(corpo.get("n"), "n")
        with conn:
            nucleo_ponte.batida(conn, demanda_id, n, squad=str(corpo.get("squad") or ""),
                                etapa=str(corpo.get("etapa") or ""),
                                squad_nome=str(corpo.get("squad_nome") or ""),
                                agora=painel_dados.agora_cuiaba())
    except ValueError as exc:  # PonteInvalida e MovimentoInvalido também são ValueError
        return _erro_ponte(str(exc), _status_da_recusa(exc))
    return jsonify({"ok": True})


@app.route("/ponte/demanda/<int:demanda_id>/resultado", methods=["POST"])
@_exige_chave_ponte
def ponte_resultado(conn, demanda_id: int):
    try:
        n = _inteiro(request.form.get("n"), "n")
        total = _inteiro(request.form.get("citacoes_total"), "citacoes_total")
        falhas = _inteiro(request.form.get("citacoes_falhas"), "citacoes_falhas")
    except ValueError as exc:
        return _erro_ponte(str(exc), 400)
    gate_status = (request.form.get("gate_status") or "").strip()
    if not gate_status or n < 1 or total < 0 or falhas < 0:
        return _erro_ponte("Campos do resultado inválidos.", 400)
    conteudos = {}
    for campo, nome, obrigatorio in _ARQUIVOS_RESULTADO:
        arq = request.files.get(campo)
        dados = arq.read() if arq else b""
        if obrigatorio and not dados:
            return _erro_ponte(f"Arquivo obrigatório ausente ou vazio: {campo}.", 400)
        if dados:
            conteudos[nome] = dados
    try:
        gate = json.loads(conteudos["citation-gate.json"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        gate = None
    if not isinstance(gate, dict):
        return _erro_ponte("O arquivo citation_gate não é um JSON válido.", 400)
    # O arquivo prevalece sobre o resumo do formulário (mesma regra de contagem do Mac).
    campos = nucleo_ponte.campos_do_gate(gate)
    enviados = {"gate_status": nucleo_ponte.uma_linha(gate_status, 40),
                "citacoes_total": total, "citacoes_falhas": falhas}
    detalhe = ""
    if enviados != campos:
        detalhe = ("o resumo enviado divergiu do citation-gate.json "
                   f"(enviado: {enviados['gate_status']}, {total} citações, {falhas} falhas; "
                   f"arquivo: {campos['gate_status']}, {campos['citacoes_total']} citações, "
                   f"{campos['citacoes_falhas']} falhas); valeu o arquivo")

    base = os.path.abspath(os.path.join(_pasta_das_pecas(), str(demanda_id)))
    destino = os.path.join(base, str(n))
    os.makedirs(base, exist_ok=True)
    preparo = tempfile.mkdtemp(prefix=".recebendo-", dir=base)
    try:
        for nome, dados in conteudos.items():
            with open(os.path.join(preparo, nome), "wb") as f:
                f.write(dados)
        with conn:  # se a troca de pasta falhar, a conclusão é desfeita
            nucleo_ponte.concluir(
                conn, demanda_id, n, squad=(request.form.get("squad") or "").strip(),
                squad_nome=request.form.get("squad_nome") or "",
                run_id=(request.form.get("run_id") or "").strip(), pasta=destino,
                agora=painel_dados.agora_cuiaba(), detalhe=detalhe, **campos)
            shutil.rmtree(destino, ignore_errors=True)  # sobra de tentativa interrompida
            os.replace(preparo, destino)
    except (nucleo_ponte.PonteInvalida, nucleo_quadro.MovimentoInvalido) as exc:
        return _erro_ponte(str(exc), 409)
    finally:
        shutil.rmtree(preparo, ignore_errors=True)
        try:
            os.rmdir(base)  # só some se ficou vazia (nada foi guardado)
        except OSError:
            pass
    return jsonify({"ok": True})


@app.route("/ponte/demanda/<int:demanda_id>/falha", methods=["POST"])
@_exige_chave_ponte
def ponte_falha(conn, demanda_id: int):
    corpo = request.get_json(silent=True)
    corpo = corpo if isinstance(corpo, dict) else {}
    try:
        n = _inteiro(corpo.get("n"), "n")
        with conn:
            nucleo_ponte.falhar(conn, demanda_id, n, str(corpo.get("motivo") or ""),
                                str(corpo.get("detalhe") or ""), painel_dados.agora_cuiaba())
    except ValueError as exc:
        return _erro_ponte(str(exc), _status_da_recusa(exc))
    return jsonify({"ok": True})


ABAS_DA_CARTEIRA = ("ativa", "a_conferir", "arquivados", "lixeira", "descartados")


@app.route("/carteira")
def carteira_tela():
    """Carteira: Ativa, A conferir, Arquivados, Lixeira e Descartados."""
    aba = request.args.get("aba", "ativa")
    if aba not in ABAS_DA_CARTEIRA:
        aba = "ativa"
    busca = (request.args.get("q") or "").strip()
    tribunal = (request.args.get("tribunal") or "").strip()
    hoje = painel_dados.hoje_cuiaba()
    conn = banco.conectar()
    try:
        todas = painel_dados.linhas_da_carteira(conn, aba, hoje)
        contagens = painel_dados.contagens(conn, hoje)
    finally:
        conn.close()
    return render_template(
        "carteira.html", aba=aba, busca=busca, tribunal=tribunal, contagens=contagens,
        tribunais=painel_dados.tribunais(todas),
        linhas=painel_dados.filtrar(todas, busca, tribunal))


def _descarte(acao, mensagem_ok: str, mensagem_nada: str, no_quadro=None,
              mensagem_quadro: str = ""):
    """Decisão do advogado sobre o processo (descartar, restaurar, confirmar). Com
    `no_quadro`, ajusta também os cartões do processo (tira ou devolve) quando a
    decisão mudou algo, e acrescenta `mensagem_quadro` se algum cartão se moveu."""
    numero = _resolver_apelido(request.form.get("numero"))
    numero = "".join(c for c in numero if c.isdigit())
    destino = _destino_seguro(request.form.get("voltar"), url_for("carteira_tela"))
    if len(numero) != 20:
        flash("Número inválido.", "erro")
        return redirect(destino)
    conn = banco.conectar()
    try:
        with conn:
            mudou = acao(conn, numero)
            cartoes = no_quadro(conn, numero) if mudou and no_quadro else []
    finally:
        conn.close()
    mensagem = mensagem_ok if mudou else mensagem_nada
    if cartoes and mensagem_quadro:
        mensagem = f"{mensagem} {mensagem_quadro}"
    flash(mensagem, "ok")
    return redirect(destino)


@app.route("/carteira/descartar", methods=["POST"])
def carteira_descartar():
    """Descartar / "Não é meu": o processo vai para Descartados e o cartão sai do quadro."""
    return _descarte(nucleo_carteira.descartar,
                     'Descartado. Ele fica em "Descartados" e pode ser restaurado.',
                     "Este processo já estava descartado.",
                     nucleo_quadro.tirar_do_quadro,
                     "O cartão saiu do quadro (foi para “Resolvido”).")


@app.route("/carteira/restaurar", methods=["POST"])
def carteira_restaurar():
    return _descarte(nucleo_carteira.restaurar, 'Restaurado para "A conferir".',
                     "Este processo não estava descartado.",
                     nucleo_quadro.devolver_ao_quadro, "O cartão voltou ao quadro.")


@app.route("/carteira/confirmar", methods=["POST"])
def carteira_confirmar():
    """"É meu, acompanhar": o processo entra na carteira (aba Ativa)."""
    return _descarte(nucleo_carteira.confirmar,
                     "Confirmado: o processo está na sua carteira e segue monitorado.",
                     "Este processo já estava confirmado na sua carteira.",
                     nucleo_quadro.devolver_ao_quadro, "O cartão voltou ao quadro.")


def _resposta_acao(ok: bool, mensagem: str):
    """Resposta das ações de arquivo: JSON para o cartão do quadro; para formulário, o
    aviso na tela e a volta à página de origem (padrão: a carteira)."""
    if _quer_json():
        return _resposta_quadro(ok, mensagem)
    flash(mensagem, "ok" if ok else "erro")
    return redirect(_destino_seguro(request.form.get("voltar"), url_for("carteira_tela")))


def _acao_de_arquivo(acao: str):
    """Arquivar, desarquivar, excluir ou restaurar da lixeira: um processo (página do
    processo, cartão do quadro) ou um lote (carteira), pelo campo `numero` repetido. Até
    acoes_processo.LIMITE_LOTE de uma vez; acima disso, nada muda. Se o quadro mudar no
    meio (movimento concorrente de um cartão), a transação é desfeita por inteiro."""
    numeros = []
    for valor in request.form.getlist("numero"):
        digitos = "".join(c for c in _resolver_apelido(valor) if c.isdigit())
        if len(digitos) == 20:
            numeros.append(digitos)
    if not numeros:
        return _resposta_acao(False, "Escolha pelo menos um processo.")
    conn = banco.conectar()
    try:
        with conn:
            r = acoes_processo.aplicar(conn, acao, numeros)
        # Prazo não some sozinho: quem arquiva ou exclui fica sabendo que ele continua correndo.
        # O processo já foi arquivado: se o aviso falhar, o arquivamento não vira erro.
        extra = ""
        if acao in ("arquivar", "excluir") and r.feitos:
            try:
                extra = prazos_tela.aviso_de_arquivar(conn, r.feitos, painel_dados.hoje_cuiaba())
            except Exception as exc:  # noqa: BLE001 - o aviso é um extra
                logging.getLogger(__name__).warning("arquivar: aviso de prazos falhou (%s)",
                                                    type(exc).__name__)
    except acoes_processo.LoteGrande as exc:
        return _resposta_acao(False, str(exc))
    except nucleo_quadro.MovimentoInvalido:
        return _resposta_acao(False, "O quadro mudou enquanto você agia. Tente de novo.")
    finally:
        conn.close()
    texto = acoes_processo.mensagem(acao, r)
    return _resposta_acao(acoes_processo.deu_certo(r), f"{texto} {extra}".strip())


@app.route("/carteira/arquivar", methods=["POST"])
def carteira_arquivar():
    """«Arquivar»: sai da carteira ativa e do quadro; volta sozinho com publicação nova."""
    return _acao_de_arquivo("arquivar")


@app.route("/carteira/desarquivar", methods=["POST"])
def carteira_desarquivar():
    """«Desarquivar» (por você ou pelo tribunal): volta à carteira, com os cartões."""
    return _acao_de_arquivo("desarquivar")


@app.route("/carteira/excluir", methods=["POST"])
def carteira_excluir():
    """«Excluir»: lixeira por 30 dias. Nada é apagado; os autos e a pasta ficam."""
    return _acao_de_arquivo("excluir")


@app.route("/carteira/lixeira/restaurar", methods=["POST"])
def carteira_restaurar_da_lixeira():
    """«Restaurar» (lixeira): o processo volta para onde estava, com os cartões."""
    return _acao_de_arquivo("restaurar")


# ---------------------------------------------------------------------------
# Prazos (1.6.0): telas e ações. A conta e os estados ficam em nucleo/prazos_banco.py.
# ---------------------------------------------------------------------------

_VISTAS_DE_PRAZO = ("lista", "semana", "mes")

# A confirmação de arquivar ou excluir diz o que ainda corre no processo (cartão e página).


@app.context_processor
def _ctx_prazos():
    """O contador do menu e a faixa do topo. A faixa nunca derruba a página."""
    vazio = {"prazos_nav": 0, "faixa_prazos": None}
    if _login_exigido() and not session.get("logado"):
        return vazio
    try:
        conn = banco.conectar()
        try:
            hoje = painel_dados.hoje_cuiaba()
            return {"prazos_nav": prazos_tela.contador_nav(conn, hoje),
                    "faixa_prazos": prazos_tela.faixa(conn, hoje)}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a faixa é um extra: a página abre sem ela
        logging.getLogger(__name__).warning("prazos: faixa do topo falhou (%s)", type(exc).__name__)
        return vazio


def _data_da_consulta(valor: str | None, padrao):
    """O dia pedido na aba. Dia que não existe ou que está fora do alcance dos prazos (do começo
    do calendário até alguns anos adiante) volta ao padrão: a grade não anda até o fim do tempo."""
    try:
        dia = date.fromisoformat(valor or "")
    except ValueError:
        return padrao
    ultimo = date(padrao.year + nucleo_prazos_banco.ANOS_A_FRENTE, 12, 31)
    return dia if nucleo_prazos_banco.DATA_MINIMA <= dia <= ultimo else padrao


def _lex_conectado(conn) -> bool:
    """O Lex que lê os prazos está de pé: o leitor consultou a fila de leitura há pouco, e o Mac
    (se está de pé) não avisou que perdeu o acesso ao plano."""
    agora = painel_dados.agora_cuiaba()
    return (nucleo_ponte.leitor_conectado(conn, agora)
            and not nucleo_ponte.mac_sem_plano(conn, agora))


def _alcance_dos_cartoes(conn) -> None:
    """Ao abrir a aba, os cartões abertos que ainda não têm prazo ganham o seu (a mesma rotina
    que roda no fim de toda varredura; idempotente). É a única escrita de uma tela de leitura,
    e por isso nunca derruba a página nem roda no modo apresentação, que só mostra."""
    if _modo_apresentacao():
        return
    try:
        # Já feito uma vez: a abertura da aba só lê, não pede a trava de escrita nem regrava a marca
        # (cartão que chega depois ganha o prazo em `quadro.adicionar_item`, e a varredura recupera).
        if nucleo_config.ler(conn, nucleo_prazos_banco.CHAVE_ALCANCE):
            return
        with conn:
            nucleo_prazos_banco.criar_dos_cartoes_abertos(conn)
    except Exception as exc:  # noqa: BLE001 - a aba abre mesmo assim; a varredura tenta de novo
        logging.getLogger(__name__).warning("prazos: alcance dos cartões falhou (%s)",
                                            type(exc).__name__)


@app.route("/prazos")
def prazos_lista():
    vista = request.args.get("vista")
    vista = vista if vista in _VISTAS_DE_PRAZO else "lista"
    hoje = painel_dados.hoje_cuiaba()
    ref = _data_da_consulta(request.args.get("ref"), hoje)
    conn = banco.conectar()
    try:
        _alcance_dos_cartoes(conn)
        grupos = prazos_tela.lista(conn, hoje) if vista == "lista" else None
        grade = None if vista == "lista" else prazos_tela.grade(conn, ref, hoje, vista)
        lex_conectado = _lex_conectado(conn)
    finally:
        conn.close()
    if _modo_apresentacao():  # num lugar só: o que a apresentação mostra sai desta função
        if grupos is not None:
            grupos = {chave: [prazos_tela.para_apresentacao(l) for l in linhas]
                      for chave, linhas in grupos.items()}
        if grade is not None:
            grade["dias"] = [{**d, "itens": [prazos_tela.para_apresentacao(i) for i in d["itens"]]}
                             for d in grade["dias"]]
    return render_template("prazos.html", vista=vista, grupos=grupos, grade=grade,
                           lex_conectado=lex_conectado)


@app.route("/prazos/<int:prazo_id>")
def prazo_tela(prazo_id: int):
    conn = banco.conectar()
    try:
        try:
            prazo = prazos_tela.detalhe(conn, prazo_id, painel_dados.hoje_cuiaba())
        except (nucleo_prazos_banco.PrazoInvalido, OverflowError):  # id que não cabe no banco
            abort(404)
        lex_conectado = _lex_conectado(conn)
    finally:
        conn.close()
    if _modo_apresentacao():
        prazo = prazos_tela.para_apresentacao(prazo)
    return render_template("prazo.html", prazo=prazo, lex_conectado=lex_conectado,
                           max_dias=nucleo_prazos_banco.MAX_DIAS,
                           limite_do_ato=nucleo_prazos_banco.LIMITE_ATO)


def _contexto_das_configuracoes_de_prazos(conn) -> dict:
    """O que a tela de Configurações de prazos mostra. A agenda assinada entra só como «existe um
    endereço ou não»: o endereço em si aparece uma vez, na resposta que o gera (`agenda_url`)."""
    hoje = painel_dados.hoje_cuiaba()
    siglas = [t.sigla for t in nucleo_tribunal.configurados()]
    filtro_tribunal = (request.args.get("tribunal") or "").strip().upper()
    if filtro_tribunal not in siglas:
        filtro_tribunal = siglas[0]
    try:
        ano = int(request.args.get("ano") or hoje.year)
    except ValueError:
        ano = hoje.year
    primeiro, ultimo = (nucleo_prazos_banco.DATA_MINIMA.year,
                        hoje.year + nucleo_prazos_banco.ANOS_A_FRENTE)
    if not primeiro <= ano <= ultimo:
        ano = hoje.year
    dias = prazos_tela.dias_do_calendario(conn, filtro_tribunal, ano)
    return {"dias": dias, "linhas_do_calendario": prazos_tela.agrupar_dias(dias),
            "lembrete": prazos_tela.lembrete_calendario(conn, hoje, siglas),
            "d_padrao": nucleo_prazos_banco.d_padrao(conn),
            "versao": nucleo_prazos_banco.versao_calendario(conn),
            "filtro_tribunal": filtro_tribunal, "ano": ano, "siglas": siglas,
            "tipos": prazos_tela.ROTULOS_DE_DIA, "d_validos": nucleo_prazos_banco.D_VALIDOS,
            "agenda_ligada": nucleo_agenda.tem_token(conn),
            "agenda_criada_em": nucleo_agenda.criado_em(conn)[:10], "agenda_url": None,
            "agenda_janela_dias": nucleo_agenda.JANELA_DA_AGENDA_DIAS}


@app.route("/configuracoes/prazos")
def configuracoes_prazos():
    conn = banco.conectar()
    try:
        contexto = _contexto_das_configuracoes_de_prazos(conn)
    finally:
        conn.close()
    return render_template("configuracoes_prazos.html", **contexto)


MSG_PRAZO_FALHOU = "Não foi possível concluir agora. Nada foi alterado; tente de novo."
MSG_BANCO_OCUPADO = "O banco está ocupado agora. Nada foi alterado; tente de novo."
MSG_DATAS_NAO_REFEITAS = ("Não foi possível refazer as datas dos prazos. Nada foi alterado; "
                          "tente de novo.")
MSG_NAO_E_MEU = ("Este processo está marcado como «Não é meu». Confirme que ele é seu antes de "
                 "pedir uma nova leitura.")
MSG_NADA_MUDOU = "Nada foi alterado: os campos são iguais aos que já estão."
MSG_PRAZO_MUDOU = ("Este prazo mudou depois que você abriu a tela. Veja a data nova e confira "
                   "de novo.")


def _por_quem() -> str:
    return PAINEL_USUARIO or "advogado"


class _AcaoFalhou(Exception):
    """A ação não valeu: o texto (em português) vai à tela, e a transação foi desfeita."""

    def __init__(self, texto: str, ausente: bool = False, destino: str | None = None):
        super().__init__(texto)
        self.texto, self.ausente, self.destino = texto, ausente, destino


def _barreira_de_acao():
    """Defesa da própria ação, além dos filtros de pedido: nada de prazo roda no modo
    apresentação nem sem login (quando ele está ligado)."""
    if _modo_apresentacao():
        return _indisponivel()
    if _login_exigido() and not session.get("logado"):
        return redirect(url_for("login", proximo=request.path))
    return None


def _rodar_acao(acao, falha: str = MSG_PRAZO_FALHOU):
    """Roda `acao(conn)` numa transação só. Dado ruim vira a mensagem do próprio erro; qualquer
    outra falha, uma frase fixa (o que o erro diz pode trazer texto da publicação) e o tipo vai
    ao registro técnico. Em qualquer falha a transação é desfeita por inteiro."""
    conn = None
    try:
        conn = banco.conectar()
        with conn:
            if not conn.in_transaction:
                # a trava de escrita vem antes da primeira leitura: o que a ação lê é o que ela
                # grava, e uma escrita de outro no meio não se perde na gravação da linha inteira
                conn.execute("BEGIN IMMEDIATE")
            return acao(conn)
    except _AcaoFalhou:
        raise
    except nucleo_prazos_banco.PrazoInvalido as exc:
        raise _AcaoFalhou(str(exc)) from None
    except (nucleo_prazos_banco.TransacaoPerdida, sqlite3.Error) as exc:
        logging.getLogger(__name__).warning("prazos: ação falhou (%s)", type(exc).__name__)
        raise _AcaoFalhou(MSG_BANCO_OCUPADO if isinstance(exc, sqlite3.OperationalError)
                          else falha) from None
    except Exception as exc:  # noqa: BLE001 - a tela diz que falhou, nunca devolve 500
        logging.getLogger(__name__).warning("prazos: ação falhou (%s)", type(exc).__name__)
        raise _AcaoFalhou(falha) from None
    finally:
        if conn is not None:
            conn.close()


def _versao_confere(conn, prazo_id: int) -> None:
    """A ação age sobre o que o advogado viu: o formulário traz a versão da tela (`fatal` e o
    momento da última mudança), e ela tem de ser a de agora, lida na mesma transação da ação.
    Sem ela, ou com outra, nada muda."""
    enviada = request.form.get("versao") or ""
    atual = prazos_tela.versao_do_prazo(nucleo_prazos_banco.obter(conn, prazo_id))
    if not enviada or not hmac.compare_digest(enviada.encode(), atual.encode()):
        raise _AcaoFalhou(MSG_PRAZO_MUDOU, destino=f"/prazos/{prazo_id}")


def _acao_de_prazo(prazo_id: int, acao, mensagem: str, com_versao: bool = False,
                   sem_mudanca: str = "Nada mudou: o prazo já estava assim.",
                   com_link_do_prazo: bool = False):
    """Uma ação sobre um prazo: numa transação só, com o aviso na tela e a volta à página de
    origem. Dado ruim ou estado incoerente desfaz tudo e diz por quê. `com_versao`: exige a
    versão da tela (ações sobre o que se está vendo). Se `acao` devolve False, nada mudou."""
    if (barreira := _barreira_de_acao()) is not None:
        return barreira

    def dentro(conn):
        if conn.execute("SELECT 1 FROM prazo WHERE id = ?", (prazo_id,)).fetchone() is None:
            raise _AcaoFalhou("Prazo não encontrado.", ausente=True)
        if com_versao:
            _versao_confere(conn, prazo_id)
        return acao(conn)

    padrao = f"/prazos/{prazo_id}"
    try:
        mudou = _rodar_acao(dentro)
    except _AcaoFalhou as exc:
        flash(exc.texto, "erro")
        if exc.destino:
            return redirect(exc.destino)  # a tela velha não manda o advogado de volta a ela
        if exc.ausente:
            padrao = "/prazos"
    else:
        destino = _destino_seguro(request.form.get("voltar"), padrao)
        if com_link_do_prazo and mudou is not False and destino != padrao:
            # a linha sai da lista: a mensagem leva ao prazo, onde está o «Desfazer»
            flash(Markup('{} <a href="{}">Abrir o prazo para desfazer</a>.').format(
                mensagem, padrao), "ok")
        else:
            flash(sem_mudanca if mudou is False else mensagem, "ok")
        return redirect(destino)
    return redirect(_destino_seguro(request.form.get("voltar"), padrao))


def _campos_da_correcao(p: dict) -> dict:
    """Só o que o advogado mudou: campo igual ao que já está não vai (o prazo com data fatal
    informada recusa dias, início, ciência e dobro). Data vazia limpa a data. Sem nada novo,
    nada se corrige: o prazo conferido não volta a pedir conferência à toa."""
    form = request.form
    campos: dict = {}
    if form.get("ato") is not None:
        # em branco também vai: quem mandou o campo vazio é recusado (a não ser que escolha da lista)
        ato = " ".join(form["ato"].split())[:nucleo_prazos_banco.LIMITE_ATO]
        # ato em branco só passa com um ato da lista: sem ele, vai e é recusado, mesmo que o
        # gravado também esteja em branco (linha que o Lex ainda não leu)
        if ato != p["ato"] or (not ato and not form.get("codigo")):
            campos["ato"] = ato
    if form.get("dias"):
        try:
            dias = int(form["dias"])
        except ValueError:
            raise nucleo_prazos_banco.PrazoInvalido("Informe os dias, de 1 a 365.") from None
        if dias != p["dias"]:
            campos["dias"] = dias
    if form.get("unidade") and form["unidade"] != p["unidade"]:
        campos["unidade"] = form["unidade"]
    if form.get("codigo") and form["codigo"] != p["codigo_ato"]:
        campos["codigo"] = form["codigo"]
    if "dobro" in form and (form["dobro"] == "1") != bool(p["dobro"]):
        campos["dobro"] = form["dobro"] == "1"
    for nome, atual in (("inicio", p["inicio_informado"]), ("fatal_informada", p["fatal_informada"]),
                        ("ciencia", p["ciencia"])):
        if form.get(nome) is not None and form[nome].strip() != (atual or ""):
            campos[nome] = form[nome].strip()
    if not campos:
        raise nucleo_prazos_banco.PrazoInvalido(MSG_NADA_MUDOU)
    return campos


@app.route("/prazos/<int:prazo_id>/conferir", methods=["POST"])
def prazo_conferir(prazo_id: int):
    """«Conferir»: o clique do advogado, o único caminho até `conferido`. Só por POST, com
    login, fora do modo apresentação, e o registro diz quem clicou."""
    return _acao_de_prazo(prazo_id, lambda c: nucleo_prazos_banco.conferir(
        c, prazo_id, por=_por_quem()), "Prazo conferido.", com_versao=True)


@app.route("/prazos/<int:prazo_id>/corrigir", methods=["POST"])
def prazo_corrigir(prazo_id: int):
    def acao(c):
        campos = _campos_da_correcao(nucleo_prazos_banco.obter(c, prazo_id))
        nucleo_prazos_banco.corrigir(c, prazo_id, por=_por_quem(),
                                     hoje=painel_dados.hoje_cuiaba(), **campos)
    return _acao_de_prazo(prazo_id, acao, "Prazo corrigido. Confira a conta.", com_versao=True)


@app.route("/prazos/<int:prazo_id>/descartar", methods=["POST"])
def prazo_descartar(prazo_id: int):
    """«Não há prazo aqui». Nada é apagado."""
    return _acao_de_prazo(prazo_id, lambda c: nucleo_prazos_banco.descartar(
        c, prazo_id, por=_por_quem()), "Marcado: não há prazo aqui.", com_versao=True,
        sem_mudanca="Nada mudou: o prazo já estava marcado como sem prazo aqui.")


@app.route("/prazos/<int:prazo_id>/trabalho", methods=["POST"])
def prazo_trabalho(prazo_id: int):
    def acao(c):
        data = request.form.get("data") or None
        d = None
        if not data:
            try:
                d = int(request.form.get("d", ""))
            except ValueError:
                raise nucleo_prazos_banco.PrazoInvalido("Escolha o dia de trabalho.") from None
        nucleo_prazos_banco.escolher_trabalho(c, prazo_id, d=d, data=data, por=_por_quem())
    return _acao_de_prazo(prazo_id, acao, "Data de trabalho marcada.", com_versao=True)


@app.route("/prazos/<int:prazo_id>/dobro", methods=["POST"])
def prazo_dobro(prazo_id: int):
    """Dobro nunca é automático: um clique liga ou desliga, e a conta refeita aparece na hora."""
    ligado = request.form.get("ligado") == "1"
    return _acao_de_prazo(prazo_id, lambda c: nucleo_prazos_banco.alternar_dobro(
        c, prazo_id, ligado, por=_por_quem()),
        "Prazo em dobro ligado. Confira a conta." if ligado else "Prazo em dobro desligado.",
        com_versao=True, sem_mudanca=("Nada mudou: o prazo já estava em dobro." if ligado
                                      else "Nada mudou: o prazo já estava sem dobro."))


@app.route("/prazos/<int:prazo_id>/cumprido", methods=["POST"])
def prazo_cumprido(prazo_id: int):
    return _acao_de_prazo(prazo_id, lambda c: nucleo_prazos_banco.cumprir(
        c, prazo_id, por=_por_quem()), "Prazo cumprido.", com_versao=True, com_link_do_prazo=True)


@app.route("/prazos/<int:prazo_id>/desfazer-cumprido", methods=["POST"])
def prazo_desfazer_cumprido(prazo_id: int):
    return _acao_de_prazo(prazo_id, lambda c: nucleo_prazos_banco.desfazer_cumprido(
        c, prazo_id, por=_por_quem()), "Desfeito: o prazo voltou a correr.", com_versao=True)


@app.route("/prazos/<int:prazo_id>/fatal", methods=["POST"])
def prazo_fatal(prazo_id: int):
    maior = request.form.get("maior") == "1"
    return _acao_de_prazo(prazo_id, lambda c: nucleo_prazos_banco.escolher_fatal(
        c, prazo_id, maior, por=_por_quem()), "Data fatal trocada. Confira a conta.",
        com_versao=True, sem_mudanca="Nada mudou: o prazo já usa esta data.")


@app.route("/prazos/<int:prazo_id>/ciencia", methods=["POST"])
def prazo_ciencia(prazo_id: int):
    """O advogado abriu a intimação no PJe e diz o dia. O sistema nunca abre por conta própria."""
    return _acao_de_prazo(prazo_id, lambda c: nucleo_prazos_banco.informar_ciencia(
        c, prazo_id, request.form.get("data") or "", por=_por_quem(),
        hoje=painel_dados.hoje_cuiaba()), "Data da ciência guardada.", com_versao=True)


@app.route("/prazos/<int:prazo_id>/ler-de-novo", methods=["POST"])
def prazo_ler_de_novo(prazo_id: int):
    def acao(c):
        p = nucleo_prazos_banco.obter(c, prazo_id)
        if nucleo_prazos_banco.processo_descartado(c, p["numero"]):
            raise nucleo_prazos_banco.PrazoInvalido(MSG_NAO_E_MEU)
        nucleo_prazos_banco.ler_de_novo(c, prazo_id, por=_por_quem())
    return _acao_de_prazo(prazo_id, acao, "O Lex vai ler de novo.", com_versao=True)


def _acao_no_calendario(acao, mensagem: str, refaz_datas: bool = True):
    """Mudou o calendário: os prazos em aberto são refeitos na mesma transação (`acao` devolve
    quantos pedem atenção), e o aviso diz quantos são. Se a conta não sai, o dia também não
    fica, e a tela diz isso: as datas ficariam velhas com o calendário novo."""
    if (barreira := _barreira_de_acao()) is not None:
        return barreira
    try:
        resultado = _rodar_acao(acao, MSG_DATAS_NAO_REFEITAS if refaz_datas else MSG_PRAZO_FALHOU)
    except _AcaoFalhou as exc:
        flash(exc.texto, "erro")
    else:
        atencao, extra = resultado if isinstance(resultado, tuple) else (resultado, "")
        if atencao:
            mensagem += (" 1 prazo pede atenção: confira a data." if atencao == 1
                         else f" {atencao} prazos pedem atenção: confira as datas.")
        flash(f"{mensagem} {extra}".strip(), "ok")
    return redirect("/configuracoes/prazos")


@app.route("/configuracoes/prazos/padrao", methods=["POST"])
def configuracoes_prazos_padrao():
    def acao(c):
        try:
            d = int(request.form.get("d", ""))
        except ValueError:
            raise nucleo_prazos_banco.PrazoInvalido("Escolha D-0, D-1, D-2 ou D-3.") from None
        nucleo_prazos_banco.definir_d_padrao(c, d, por=_por_quem())
        return 0
    return _acao_no_calendario(acao, "Padrão da data de trabalho guardado.", refaz_datas=False)


@app.route("/configuracoes/prazos/dia", methods=["POST"])
def configuracoes_prazos_incluir():
    f = request.form

    def acao(c):
        _, atencao = nucleo_prazos_banco.incluir_dia(
            c, f.get("data", ""), f.get("tribunal", ""), f.get("comarca", ""), f.get("tipo", ""),
            f.get("descricao", ""), f.get("fonte", ""), origem=f.get("origem") or "manual",
            por=_por_quem())
        comarca = " ".join(f.get("comarca", "").split())
        extra = ""
        if comarca and not nucleo_prazos_banco.comarca_casa_com_algum(
                c, f.get("tribunal", ""), comarca):
            extra = (f"A comarca «{comarca}» só vale onde o órgão julgador do processo traz "
                     "exatamente esse nome, e nenhum processo seu casa com ela agora.")
        return atencao, extra
    return _acao_no_calendario(acao, "Dia incluído.")


@app.route("/configuracoes/prazos/dia/<int:dia_id>/corrigir", methods=["POST"])
def configuracoes_prazos_corrigir_dia(dia_id: int):
    f = request.form
    return _acao_no_calendario(lambda c: nucleo_prazos_banco.corrigir_dia(
        c, dia_id, tipo=f.get("tipo"), descricao=f.get("descricao"), fonte=f.get("fonte"),
        por=_por_quem()), "Dia corrigido.")


@app.route("/configuracoes/prazos/dia/<int:dia_id>/desativar", methods=["POST"])
def configuracoes_prazos_desativar(dia_id: int):
    return _acao_no_calendario(lambda c: nucleo_prazos_banco.desativar_dia(
        c, dia_id, por=_por_quem()), "Dia retirado da conta. A linha fica guardada.")


@app.route("/configuracoes/prazos/agenda/gerar", methods=["POST"])
def configuracoes_prazos_agenda_gerar():
    """Gera o endereço da agenda (o anterior deixa de valer) e o mostra UMA vez, na própria
    resposta: nunca em aviso de sessão (o cookie é lido pelo navegador), nunca em redirecionamento,
    nunca em registro. A página é montada dentro da mesma transação que grava o endereço: se
    qualquer parte falhar, nada é gravado e o endereço antigo continua valendo. Recarregar a
    página gera outro endereço, e o que se perdeu não volta."""
    if (barreira := _barreira_de_acao()) is not None:
        return barreira

    def acao(conn):
        token = nucleo_agenda.gerar_token(conn)
        contexto = _contexto_das_configuracoes_de_prazos(conn)
        # Atrás do nginx o Flask vê http: na produção (com login) o endereço é sempre https.
        contexto["agenda_url"] = url_for("agenda_ics", t=token, _external=True,
                                         _scheme="https" if _login_exigido() else "http")
        contexto["agenda_ligada"] = True
        contexto["agenda_criada_em"] = nucleo_agenda.criado_em(conn)[:10]
        return render_template("configuracoes_prazos.html", **contexto)

    try:
        pagina = _rodar_acao(acao)
    except _AcaoFalhou as exc:
        flash(exc.texto, "erro")
        return redirect(url_for("configuracoes_prazos"))
    resposta = make_response(pagina)
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.route("/configuracoes/prazos/agenda/revogar", methods=["POST"])
def configuracoes_prazos_agenda_revogar():
    if (barreira := _barreira_de_acao()) is not None:
        return barreira
    try:
        _rodar_acao(nucleo_agenda.revogar_token)
    except _AcaoFalhou as exc:
        flash(exc.texto, "erro")
    else:
        flash("Endereço revogado. A agenda assinada para de atualizar.", "ok")
    return redirect(url_for("configuracoes_prazos"))


@app.route("/agenda/baixar")
def agenda_baixar():
    """O arquivo da agenda, para importar. Pede login (o filtro de pedidos) e some no modo
    apresentação (403). É o caminho da instalação local, cujo endereço 127.0.0.1 o celular não
    alcança. O conteúdo vem do mesmo `montar_ics` da assinatura: número do processo e ato da
    lista, sem nome. Falha nossa: frase simples, sem detalhe, sem cache; só o tipo vai ao
    registro."""
    conn = None
    try:
        conn = banco.conectar()
        resposta = Response(nucleo_agenda.montar_ics(conn), mimetype="text/calendar")
        resposta.headers["Content-Disposition"] = "attachment; filename=prazos.ics"
    except Exception as exc:  # noqa: BLE001 - a frase é fixa; o que o erro diz não vai à tela
        logging.getLogger(__name__).warning("agenda: o arquivo falhou (%s)", type(exc).__name__)
        resposta = Response("Não foi possível montar o arquivo da agenda agora. Tente de novo.",
                            status=503, mimetype="text/plain")
    finally:
        if conn is not None:
            conn.close()
    resposta.headers["Cache-Control"] = "no-store"
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta


@app.route("/agenda.ics")
def agenda_ics():
    """A agenda assinada, sem login e com o token próprio. O token vai na consulta, não no
    caminho. O que este código garante: ele não registra a consulta (nem o token, nem o erro, só o
    tipo da falha), e o `access_log` do nginx da produção também não a guarda (`$uri`, sem
    consulta nem referer). O que NÃO está garantido aqui: o `error_log` do nginx escreve a
    requisição com a consulta quando o painel está fora do ar (502), e o bloco `:80` que o certbot
    cria pode usar o registro padrão. Isso se confere no servidor. Sempre sem cache. Token ausente,
    errado ou revogado: 403 sem motivo; qualquer falha nossa: 503 sem detalhe."""
    conn = None
    try:
        # Um navegador com o modo apresentação ligado não baixa números reais. A resposta é texto
        # puro, não a página do painel (ela repete o endereço pedido, com o token, num campo).
        # O serviço de calendário não tem a sessão do navegador, então não é afetado.
        if _modo_apresentacao():
            resposta = Response(MENSAGEM_APRESENTACAO, status=403, mimetype="text/plain")
        else:
            conn = banco.conectar()
            if nucleo_agenda.token_valido(conn, request.args.get("t")):
                resposta = Response(nucleo_agenda.montar_ics(conn), mimetype="text/calendar")
            else:
                resposta = Response("Agenda indisponível.", status=403, mimetype="text/plain")
    except Exception as exc:  # noqa: BLE001 - o serviço de calendário tenta de novo e mantém o que tem
        logging.getLogger(__name__).warning("agenda: falhou (%s)", type(exc).__name__)
        resposta = Response("Agenda indisponível.", status=503, mimetype="text/plain")
        resposta.headers["Retry-After"] = "300"
    finally:
        if conn is not None:
            conn.close()
    resposta.headers["Cache-Control"] = "no-store"
    resposta.headers["Referrer-Policy"] = "no-referrer"
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta


@app.route("/carteira/exportar")
def carteira_exportar():
    """Carteira.xlsx a partir do banco (abas Carteira, A conferir e Arquivados e lixeira)."""
    conn = banco.conectar()
    try:
        conteudo = gerar_xlsx(nucleo_carteira.listar(conn, "ativa"),
                              nucleo_carteira.listar(conn, "a_conferir"),
                              arquivados=(nucleo_carteira.listar(conn, "arquivados")
                                          + nucleo_carteira.listar(conn, "lixeira")))
    finally:
        conn.close()
    return Response(
        conteudo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="Carteira.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Grupos de processos (coleções nomeadas, baixadas em segundo plano).
# ---------------------------------------------------------------------------

def _tem_pecas(numero: str) -> bool:
    pasta = os.path.join("peticoes", numero)
    if not os.path.isdir(pasta):
        return False
    for _raiz, _dirs, arquivos in os.walk(pasta):
        if arquivos:
            return True
    return False


def _processos_do_grupo(grupo: dict) -> list[dict]:
    return [{
        "numero": n,
        "formatado": formatar_numero_cnj(n),
        "dados": cache.ler(n),
        "baixado": _tem_pecas(n),
    } for n in grupo.get("numeros", [])]


@app.route("/grupos")
def grupos_lista():
    return render_template("grupos.html", grupos=grupos.listar(),
                           status=tarefas.ler_status)


@app.route("/grupos/novo", methods=["GET", "POST"])
def grupo_novo():
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        descricao = request.form.get("descricao") or ""
        texto = request.form.get("numeros", "")
        numeros = numeros_da_lista(texto)
        ctx = {"nome": nome, "descricao": descricao, "texto": texto}

        if not nome:
            flash("Dê um nome ao grupo.", "erro")
            return render_template("grupo_novo.html", **ctx)
        if not numeros:
            flash("Cole ao menos um número de processo (20 dígitos).", "erro")
            return render_template("grupo_novo.html", **ctx)

        classif = classificar_lista(numeros)
        revisado = request.form.get("revisado") == "1"
        incluir_todos = request.form.get("incluir_todos") == "1"

        # Há números problemáticos e o usuário ainda não viu o aviso → revisar.
        if classif["problemas"] and not revisado:
            return render_template("grupo_novo.html", classif=classif, **ctx)

        finais = numeros if incluir_todos else classif["validos"]
        if not finais:
            flash(f"Nenhum processo válido do {_siglas()} na lista.", "erro")
            return render_template("grupo_novo.html", classif=classif, **ctx)

        g = grupos.criar(nome, descricao, finais)
        excluidos = len(numeros) - len(finais)
        msg = f"Grupo “{g['nome']}” criado com {len(finais)} processo(s)."
        if excluidos:
            msg += f" {excluidos} com problema foram deixados de fora."
        flash(msg, "ok")
        return redirect(url_for("grupo_detalhe", slug=g["slug"]))
    return render_template("grupo_novo.html")


@app.route("/grupos/<slug>")
def grupo_detalhe(slug: str):
    grupo = grupos.ler(slug)
    if grupo is None:
        abort(404, description="Grupo não encontrado.")
    processos = _processos_do_grupo(grupo)
    conn = banco.conectar()
    try:
        selos = painel_dados.selos_de_estado(conn, [p["numero"] for p in processos])
    finally:
        conn.close()
    for p in processos:
        p["estado"] = selos.get(p["numero"], "")  # arquivado ou na lixeira segue no grupo
    return render_template("grupo.html", grupo=grupo, processos=processos,
                           status=tarefas.ler_status(slug))


# Quantos números de fora a mensagem lista um a um (o resto vira "e mais N").
_FORA_NA_MENSAGEM = 10


def _conferir_para_o_grupo(texto: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Cada trecho colado (um por linha, ou separados por vírgula/ponto e vírgula) passa
    pela mesma conferência da tela de criar grupo. Devolve os números válidos (sem
    repetição, na ordem) e os que ficaram de fora, com o motivo."""
    validos: list[str] = []
    fora: list[tuple[str, str]] = []
    for bruto in re.split(r"[\n,;]+", texto or ""):
        trecho = " ".join(bruto.split())
        if not any(c.isdigit() for c in trecho):
            continue
        v = validar_cnj(trecho)
        if v["ok"]:
            if v["digitos"] not in validos:
                validos.append(v["digitos"])
        else:
            fora.append((v["formatado"][:40], v["motivo"]))
    return validos, fora


@app.route("/grupos/<slug>/incluir", methods=["POST"])
def grupo_incluir(slug: str):
    if grupos.ler(slug) is None:
        abort(404)
    validos, fora = _conferir_para_o_grupo(request.form.get("numeros", ""))
    if not validos and not fora:
        flash("Cole ao menos um número de processo (20 dígitos).", "erro")
        return redirect(url_for("grupo_detalhe", slug=slug))
    resultado = grupos.incluir(slug, validos)
    if resultado is None:
        abort(404)
    fora = [(formatar_numero_cnj(n), "já estava no grupo") for n in resultado["ja_estavam"]] + fora
    entraram = len(resultado["entraram"])
    if entraram:
        flash(f"{entraram} processo(s) entraram no grupo. Use “Baixar todos” para baixar "
              "os autos deles.", "ok")
    else:
        flash("Nenhum processo novo entrou no grupo.", "aviso")
    if fora:
        itens = [f"{numero} ({motivo})" for numero, motivo in fora[:_FORA_NA_MENSAGEM]]
        resto = len(fora) - _FORA_NA_MENSAGEM
        flash(f"Ficaram de fora {len(fora)}: " + "; ".join(itens)
              + (f"; e mais {resto}." if resto > 0 else "."), "aviso")
    return redirect(url_for("grupo_detalhe", slug=slug))


@app.route("/grupos/<slug>/retirar", methods=["POST"])
def grupo_retirar(slug: str):
    if grupos.ler(slug) is None:
        abort(404)
    numero = "".join(c for c in (request.form.get("numero") or "") if c.isdigit())
    if grupos.retirar(slug, numero):
        flash(f"Processo {formatar_numero_cnj(numero)} retirado do grupo. Os autos já "
              "baixados continuam guardados.", "ok")
    else:
        flash("Esse processo não está no grupo.", "erro")
    return redirect(url_for("grupo_detalhe", slug=slug))


@app.route("/grupos/<slug>/baixar", methods=["POST"])
def grupo_baixar(slug: str):
    if grupos.ler(slug) is None:
        abort(404)
    cred = _credencial_atual()
    if cred is None:
        raise CredencialPJeAusente("Conecte-se ao PJe para baixar o grupo.")
    if tarefas.esta_ocupado(slug):
        flash("Já há um processamento em andamento para este grupo.", "erro")
    else:
        tarefas.enfileirar(slug, "baixar", cred)
        flash("Download iniciado em segundo plano. Pode fechar a aba — "
              "o servidor continua baixando.", "ok")
    return redirect(url_for("grupo_detalhe", slug=slug))


@app.route("/grupos/<slug>/atualizar", methods=["POST"])
def grupo_atualizar(slug: str):
    if grupos.ler(slug) is None:
        abort(404)
    cred = _credencial_atual()
    if cred is None:
        raise CredencialPJeAusente("Conecte-se ao PJe para atualizar o grupo.")
    if tarefas.esta_ocupado(slug):
        flash("Já há um processamento em andamento para este grupo.", "erro")
    else:
        tarefas.enfileirar(slug, "sincronizar", cred)
        flash("Atualização de andamentos iniciada em segundo plano.", "ok")
    return redirect(url_for("grupo_detalhe", slug=slug))


@app.route("/grupos/<slug>/status")
def grupo_status(slug: str):
    return jsonify(tarefas.ler_status(slug) or {"estado": "nunca"})


@app.route("/grupos/<slug>/exportar")
def grupo_exportar(slug: str):
    grupo = grupos.ler(slug)
    if grupo is None:
        abort(404)
    registros = [({"numero_digitos": n, "numero": n}, cache.ler(n))
                 for n in grupo.get("numeros", [])]
    conteudo = gerar_planilha_xlsx(registros)
    nome = f"grupo_{slug}.xlsx"
    return Response(
        conteudo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.route("/grupos/<slug>/zip")
def grupo_zip(slug: str):
    grupo = grupos.ler(slug)
    if grupo is None:
        abort(404)
    tmp, tamanho = _zip_de_numeros(grupo.get("numeros", []))
    if tmp is None:
        flash("Nada baixado ainda neste grupo. Use “Baixar todos” primeiro.", "erro")
        return redirect(url_for("grupo_detalhe", slug=slug))
    return _resposta_zip(tmp, tamanho, f"grupo_{slug}.zip")


@app.route("/grupos/<slug>/excluir", methods=["POST"])
def grupo_excluir(slug: str):
    grupo = grupos.ler(slug)
    if grupo is None:
        abort(404)
    grupos.excluir(slug)
    flash(f"Grupo “{grupo['nome']}” excluído. As peças e o cache foram mantidos.", "ok")
    return redirect(url_for("grupos_lista"))


@app.route("/baixar-lote")
def baixar_lote_form():
    return render_template("baixar_lote.html")


def _stream_download(numeros: list[str], cred: dict | None):
    """Gerador de texto: baixa cada processo da lista nas duas instâncias,
    emitindo o progresso linha a linha para a página acompanhar ao vivo."""
    if cred is None:
        yield "ERRO: conecte-se ao PJe (informe CPF e senha) antes de baixar.\n"
        return
    if not numeros:
        yield "ERRO: nenhum número CNJ válido encontrado (esperado: 20 dígitos).\n"
        return
    # Prepara um cliente por instância (com a credencial da sessão).
    clientes = []
    for chave, rotulo, _env, _padrao in INSTANCIAS:
        try:
            clientes.append((chave, rotulo, obter_cliente(
                chave, cred["cpf"], credenciais.senha_da_instancia(cred, chave))))
        except MNIError as exc:
            yield f"ERRO ao preparar {rotulo}: {exc}\n"
            return

    rotulos = " e ".join(rotulo for _chave, rotulo, _cliente in clientes)
    yield (f"== {len(numeros)} processo(s) × {len(clientes)} instância(s) "
           f"({rotulos}) ==\n\n")
    total_ok = total_falhas = 0
    outros: dict[str, list | str] = {}  # demais tribunais: clientes ou o erro ao preparar
    for i, numero in enumerate(numeros, 1):
        yield f"[{i}/{len(numeros)}] Processo {formatar_numero_cnj(numero)}\n"
        clientes_do_numero = clientes
        trib = nucleo_tribunal.do_numero(numero)
        if trib is not None and trib != nucleo_tribunal.atual():
            if trib.sigla not in outros:
                cred_t = credenciais.credencial_do_env(trib.sigla) or cred
                try:
                    outros[trib.sigla] = [
                        (chave, f"{rotulo} do {trib.sigla}", obter_cliente(
                            chave, cred_t["cpf"], credenciais.senha_da_instancia(cred_t, chave),
                            tribunal=trib))
                        for chave, rotulo, *_ in trib.instancias]
                except MNIError as exc:
                    outros[trib.sigla] = f"ERRO ao preparar o {trib.sigla}: {exc}"
            if isinstance(outros[trib.sigla], str):
                yield f"  ✗ {outros[trib.sigla]}\n\n"
                continue
            clientes_do_numero = outros[trib.sigla]
        for chave, rotulo, cliente in clientes_do_numero:
            yield f"  • {rotulo}:\n"
            for ev in baixar_processo_completo(cliente, numero, subpasta=chave):
                tipo = ev["evento"]
                if tipo == "processo_ausente":
                    yield "      (não consta nesta instância)\n"
                elif tipo == "erro_processo":
                    yield f"      ✗ Falhou: {ev['msg']}\n"
                elif tipo == "dossie":
                    yield f"      📄 dossiê salvo ({ev['andamentos']} andamento(s))\n"
                elif tipo == "dossie_erro":
                    yield f"      ⚠ dossiê não salvo: {ev['msg']}\n"
                elif tipo == "processo_inicio":
                    if ev["total"] == 0:
                        yield "      (sem peças)\n"
                    else:
                        yield f"      {ev['total']} peça(s) — baixando…\n"
                elif tipo == "peca":
                    if ev.get("erro"):
                        yield (f"      [{ev['indice']}/{ev['total']}] {ev['nome']} "
                               f"— FALHOU ({ev['erro']})\n")
                    else:
                        yield (f"      [{ev['indice']}/{ev['total']}] {ev['nome']} "
                               f"({formatar_tamanho(ev['tamanho'])})\n")
                elif tipo == "processo_fim":
                    total_ok += ev["ok"]
                    total_falhas += ev["falhas"]
                    if ev["ok"] or ev["falhas"]:
                        yield (f"      ✓ {ev['ok']} salva(s), {ev['falhas']} falha(s), "
                               f"{formatar_tamanho(ev['bytes'])} → {ev['pasta']}/\n")
        yield "\n"
    yield ("=" * 50 + "\n")
    yield (f"Concluído: {total_ok} peça(s) salvas, "
           f"{total_falhas} falha(s) em {len(numeros)} processo(s).\n")


def _resposta_stream(numeros: list[str]) -> Response:
    # Captura a credencial aqui (com contexto de sessão) e passa ao gerador.
    cred = _credencial_atual()
    return Response(_stream_download(numeros, cred), mimetype="text/plain; charset=utf-8",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.route("/baixar-lote/stream", methods=["POST"])
def baixar_lote_stream():
    return _resposta_stream(numeros_da_lista(request.form.get("numeros", "")))


@app.route("/baixar-processo/<numero>/stream", methods=["POST"])
def baixar_processo_stream(numero: str):
    """Baixa um único processo (nas duas instâncias) a partir da página dele."""
    digitos = "".join(c for c in numero if c.isdigit())
    return _resposta_stream([digitos] if len(digitos) == 20 else [])


def _zip_de_numeros(numeros: list[str]):
    """Compacta as peças+dossiês já baixados dos números informados.
    Retorna (caminho_tmp, tamanho) ou (None, 0) se não houver nada em disco."""
    arquivos = []
    for digitos in numeros:
        pasta = os.path.join("peticoes", digitos)
        if os.path.isdir(pasta):
            for raiz, _dirs, nomes in os.walk(pasta):
                arquivos.extend(os.path.join(raiz, n) for n in nomes)
    if not arquivos:
        return None, 0
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for caminho in arquivos:
            # nome dentro do zip: <numero>/<instancia>/<arquivo>
            z.write(caminho, os.path.relpath(caminho, "peticoes"))
    return tmp, os.path.getsize(tmp)


def _resposta_zip(tmp: str, tamanho: int, nome_arquivo: str) -> Response:
    def stream_e_apaga():
        try:
            with open(tmp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            os.remove(tmp)

    return Response(
        stream_e_apaga(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "Content-Length": str(tamanho),
        },
    )


@app.route("/baixar-zip/<numero>")
def baixar_zip(numero: str):
    """Compacta as peças já baixadas do processo (1grau + 2grau + dossiê) e
    entrega o .zip para o computador do usuário."""
    digitos = "".join(c for c in numero if c.isdigit())
    tmp, tamanho = _zip_de_numeros([digitos])
    if tmp is None:
        flash("Nada baixado ainda para este processo. Clique em "
              f"“Baixar do {nucleo_carteira.tribunal_de(digitos).sigla}” primeiro.", "erro")
        return redirect(url_for("processo", numero=digitos))
    return _resposta_zip(tmp, tamanho, f"processo_{digitos}.zip")


# Instalação local (CONTROLADORIA_LOCAL=1): tela /configurar e a barreira até configurar.
from painel_local import bp as _bp_local  # noqa: E402
app.register_blueprint(_bp_local)


if __name__ == "__main__":
    porta = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=porta, debug=False)
