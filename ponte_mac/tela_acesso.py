"""Tela "Conexão com o Lex" (só no modo local, `CONTROLADORIA_LOCAL=1`).

O caminho principal é o botão "Conectar o Lex" (`ponte_mac.conectar`): a Controladoria
instala o Claude Code se faltar, roda o `claude setup-token` sem terminal e guarda o
acesso sozinha; a tela acompanha pelo `/configuracoes/lex/conectar/estado`. A outra
forma (o mentorado cola o código do `claude setup-token` que ele mesmo rodou) continua
aqui. O acesso vai direto para o cofre do sistema e nunca volta à tela; o teste usa a
sonda da ponte, que separa recusa, falta de login e rede (`executor.classificar_acesso`).

Os formulários usam o CSRF e o login do painel (os `before_request` do app valem para
este blueprint). Fora do modo local as rotas respondem 404."""

from __future__ import annotations

import logging
import platform
import re
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, request, session, url_for

from nucleo import versao
from ponte_mac import chaves, conectar, executor
from ponte_mac import local as ponte_local

bp = Blueprint("acesso_lex", __name__)
log = logging.getLogger("ponte_mac.tela_acesso")

# o código do `claude setup-token`: sk-ant-oat01-… (só letras, números, _ e -)
_CODIGO = re.compile(r"sk-ant-oat[A-Za-z0-9_-]{20,500}")
TEMPO_TESTE_S = 120

MENSAGEM_FORMATO = ("Isto não parece o código do “claude setup-token”: ele começa com "
                    "sk-ant-oat e não tem espaços. Copie de novo a linha inteira.")

MENSAGEM_COFRE = ("O cofre de senhas deste computador não abriu. Reinicie o computador e "
                  "tente de novo; se repetir, clique em Copiar diagnóstico e cole no grupo "
                  "da mentoria.")
RESPOSTAS_CODIGO = {
    conectar.FORMATO: (400, "Isto não parece o código da página da Anthropic. Copie de "
                            "novo, sem espaços."),
    conectar.FORA: (409, "A autorização já terminou. Clique em Conectar o Lex de novo."),
    conectar.JA_CONFERINDO: (409, "Já estamos conferindo um código. Espere a resposta "
                                  "(até 1 minuto)."),
}


@bp.app_context_processor
def _ctx():
    return {"acesso_lex_local": situacao}  # `modo_local` vem do painel_local


def situacao() -> dict:
    """O que a tela mostra: se há `claude` instalado, se há acesso guardado (nunca o
    valor), se a pasta da Banca existe e como vai a conexão em andamento."""
    casa = ponte_local.casa_da_banca()
    return {"claude": bool(executor.localizar_claude(executor.ambiente())),
            "guardado": bool(chaves.ler(chaves.ACESSO_CLAUDE)),
            "casa": bool(casa) and Path(casa).expanduser().is_dir(),
            "conexao": conectar.CONEXAO.estado()}


@bp.before_request
def _so_no_modo_local():
    if not ponte_local.modo_local():
        abort(404)
    if session.get("apresentacao"):
        flash("Indisponível no modo apresentação.", "erro")
        return redirect(url_for("configuracoes_lex"))


def _voltar():
    return redirect(url_for("configuracoes_lex") + "#acesso-lex")


def codigo_valido(bruto: str) -> str | None:
    """O código sem espaços nem quebras de linha (a janela do terminal quebra a linha
    longa ao copiar), ou None se não tiver o formato."""
    codigo = "".join(str(bruto or "").split())
    return codigo if _CODIGO.fullmatch(codigo) else None


@bp.route("/configuracoes/lex/acesso", methods=["POST"])
def salvar():
    codigo = codigo_valido(request.form.get("codigo", ""))
    if not codigo:
        flash(MENSAGEM_FORMATO, "erro")
        return _voltar()
    try:
        chaves.guardar(chaves.ACESSO_CLAUDE, codigo)
    except (ValueError, RuntimeError):
        flash("O cofre do sistema recusou guardar o acesso. Tente de novo.", "erro")
        return _voltar()
    flash("Acesso do Lex guardado no cofre deste computador. Use “Testar acesso” para "
          "conferir.", "ok")
    return _voltar()


@bp.route("/configuracoes/lex/acesso/apagar", methods=["POST"])
def apagar():
    try:
        chaves.apagar(chaves.ACESSO_CLAUDE)
    except (ValueError, RuntimeError):
        flash("O cofre do sistema recusou apagar o acesso. Tente de novo.", "erro")
        return _voltar()
    flash("Acesso apagado. O Lex passa a usar o login normal do Claude Code deste "
          "computador.", "ok")
    return _voltar()


@bp.route("/configuracoes/lex/acesso/testar", methods=["POST"])
def testar():
    amb = executor.ambiente()
    if not executor.localizar_claude(amb):
        flash("O Claude Code não foi encontrado neste computador. Instale-o e tente de novo.",
              "erro")
        return _voltar()
    try:
        token = chaves.ler(chaves.ACESSO_CLAUDE, estrito=True)
    except RuntimeError:
        conectar.CONEXAO.registrar_teste("o cofre não abriu")
        flash(MENSAGEM_COFRE, "erro")
        return _voltar()
    casa = Path(ponte_local.casa_da_banca() or Path.home()).expanduser()
    if not casa.is_dir():
        casa = Path.home()
    resultado = executor.classificar_acesso(token, casa, tempo=TEMPO_TESTE_S)
    conectar.CONEXAO.registrar_teste(resultado if token else f"{resultado} (sem acesso guardado)")
    if resultado == executor.OK:
        flash("Acesso conferido: o Lex consegue usar o plano do Claude." if token else
              "O Lex está usando o login normal do Claude Code deste computador, e ele "
              "funciona.", "ok")
    elif resultado == executor.SEM_LOGIN:
        flash("O Claude não reconheceu o acesso guardado. Clique em Conectar de novo."
              if token else "Ainda não há acesso guardado. Clique em Conectar o Lex.", "erro")
    elif resultado == executor.RECUSADO:
        flash("O Claude recusou o acesso guardado (vencido ou revogado). Clique em Conectar "
              "de novo." if token else
              "O Claude recusou o login deste computador. Clique em Conectar o Lex.", "erro")
    else:
        flash("Não deu para conferir agora (sem internet ou o Claude demorou demais). "
              "Tente de novo em alguns minutos.", "erro")
    return _voltar()


@bp.route("/configuracoes/lex/conectar", methods=["POST"])
def conectar_iniciar():
    if not conectar.CONEXAO.iniciar():
        flash("A conexão do Lex já está em andamento.", "aviso")
    return _voltar()


@bp.route("/configuracoes/lex/conectar/estado", methods=["GET"])
def conectar_estado():
    resposta = jsonify(conectar.CONEXAO.estado())
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@bp.route("/configuracoes/lex/conectar/codigo", methods=["POST"])
def conectar_codigo():
    situacao_codigo = conectar.CONEXAO.colar_codigo(request.form.get("codigo", ""))
    if situacao_codigo == conectar.COLADO:
        return jsonify({"ok": True})
    status, mensagem = RESPOSTAS_CODIGO[situacao_codigo]
    return jsonify({"ok": False, "mensagem": mensagem}), status


@bp.route("/configuracoes/lex/diagnostico", methods=["GET"])
def diagnostico():
    texto = conectar.texto_diagnostico(conectar.CONEXAO, versao=versao.atual(),
                                       claude=conectar.versao_claude(),
                                       sistema=platform.platform(terse=True))
    log.info("diagnóstico da conexão do Lex:\n%s", texto)
    resposta = jsonify({"texto": texto})
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@bp.route("/configuracoes/lex/conectar/cancelar", methods=["POST"])
def conectar_cancelar():
    conectar.CONEXAO.cancelar()
    return _voltar()
