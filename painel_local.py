"""Configuração do escritório na instalação local (só com CONTROLADORIA_LOCAL=1).

O próprio advogado informa, no navegador, o nome como sai no DJEN e a OAB (o que a busca
de publicações precisa) e, se quiser baixar os autos, marca os tribunais do PJe (TJMT,
TJMG ou os dois, cada um com a sua senha) e o CPF. O PJe é opcional: sem ele, a carteira
se monta só pelo DJEN. As senhas vão para o cofre do computador (nunca para arquivo, log
ou tela). Antes de gravar, o login é testado em cada instância marcada com a mesma sonda
da varredura. Enquanto nome e OAB não estiverem guardados, todo o painel leva a esta tela.

Fora do modo local (produção na VPS) a tela não existe (404) e nada é redirecionado.
O CSRF é o do painel: o `before_request` dele confere todo POST, este incluído."""

from __future__ import annotations

import os

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from nucleo import ajustes_locais, banco, cofre, tarefas_fila
from nucleo import tribunal as nucleo_tribunal
from nucleo.advogado import UFS
from nucleo.mni_fabrica import INDISPONIVEL, RECUSADA, sondar_login

bp = Blueprint("local", __name__)

# Abertas mesmo sem configuração: a própria tela, os arquivos estáticos e a saúde.
_LIVRES = {"local.configurar", "static", "saude"}
# Lembra o "sim" para não consultar o cofre a cada página (volta a False ao reiniciar).
_configurado = False

AVISO_PJE_FORA = ("A carteira pelo DJEN já funciona; o download dos autos pelo PJe "
                  "será tentado na próxima varredura.")


def ativo() -> bool:
    return os.getenv("CONTROLADORIA_LOCAL", "").strip() == "1"


def _ja_configurado() -> bool:
    global _configurado
    if not _configurado:
        _configurado = ajustes_locais.configurado()
    return _configurado


@bp.before_app_request
def _exigir_configuracao():
    # A ponte do Lex fala com o painel por chave própria (/ponte/*): não passa pela barreira.
    if not ativo() or request.endpoint in _LIVRES or request.path.startswith("/ponte/"):
        return None
    if not _ja_configurado():
        return redirect(url_for("local.configurar"))
    return None


@bp.app_context_processor
def _ctx_local():
    return {"modo_local": ativo()}


def _guardado() -> dict:
    a = ajustes_locais.ler()
    siglas = ajustes_locais.tribunais_guardados(a)
    principal = siglas[0] if siglas else None
    return {
        "tribunais": siglas,  # nenhum marcado = sem PJe (só o DJEN)
        "advogado_nome": a.get("advogado_nome", ""),
        "oab_numero": a.get("oab_numero", ""),
        "oab_uf": a.get("oab_uf", ""),
        "cpf": cofre.ler("pje_cpf") or "",
        # Só os tribunais marcados contam: senha guardada é a de um tribunal em uso.
        "tem_senha": {t.sigla: t.sigla in siglas and bool(
            ajustes_locais.senha_guardada(t.sigla, principal=principal))
            for t in nucleo_tribunal.disponiveis()},
        "tem_senha_2grau": {t.sigla: t.sigla in siglas and bool(
            ajustes_locais.senha_guardada(t.sigla, True, principal))
            for t in nucleo_tribunal.disponiveis()},
    }


def _tela(valores: dict, guardado: dict):
    return render_template(
        "configurar.html", v=valores, guardado=guardado, primeira_vez=not _ja_configurado(),
        tribunais=nucleo_tribunal.disponiveis(), ufs=sorted(UFS))


def _so_digitos(texto: str) -> str:
    return "".join(c for c in texto if c.isdigit())


def _rotulos(trib: nucleo_tribunal.Tribunal, chaves: list[str]) -> str:
    nomes = [rotulo for chave, rotulo, _env, _wsdl in trib.instancias if chave in chaves]
    return " e ".join(nomes)


def _marcados(f) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Tribunais marcados (na ordem da lista: o primeiro é o principal) e as senhas
    digitadas de cada um. Aceita também o formulário antigo, de um tribunal só
    (`tribunal`, `senha`, `senha_2grau`)."""
    pedidos = [s.strip().upper() for s in f.getlist("tribunais")]
    if not pedidos and f.get("tribunal"):
        sigla = f.get("tribunal").strip().upper()
        return ([sigla] if sigla in nucleo_tribunal.TRIBUNAIS else [],
                {sigla: f.get("senha") or ""}, {sigla: f.get("senha_2grau") or ""})
    siglas = [t.sigla for t in nucleo_tribunal.disponiveis() if t.sigla in pedidos]
    return (siglas, {s: f.get(f"senha_{s}") or "" for s in siglas},
            {s: f.get(f"senha_2grau_{s}") or "" for s in siglas})


@bp.route("/configurar", methods=["GET", "POST"])
def configurar():
    if not ativo():
        abort(404)
    guardado = _guardado()
    if request.method == "GET":
        return _tela(guardado, guardado)

    f = request.form
    siglas, senhas_digitadas, senhas2_digitadas = _marcados(f)
    valores = {
        "tribunais": siglas,
        "advogado_nome": " ".join((f.get("advogado_nome") or "").split()),
        "oab_numero": _so_digitos(f.get("oab_numero") or ""),
        "oab_uf": (f.get("oab_uf") or "").strip().upper(),
        "cpf": _so_digitos(f.get("cpf") or ""),
    }
    cpf_mudou = valores["cpf"] != guardado["cpf"]
    principal_antes = (ajustes_locais.tribunais_guardados() or [None])[0]

    def guardada(sigla: str, segundo_grau: bool = False) -> str:
        # Campo de senha vazio mantém a guardada (de um tribunal já marcado), salvo se
        # o CPF mudou.
        if cpf_mudou or not guardado["tem_senha"].get(sigla):
            return ""
        return ajustes_locais.senha_guardada(sigla, segundo_grau, principal_antes) or ""

    senhas = {s: senhas_digitadas[s] or guardada(s) for s in siglas}
    sem_senha = [s for s in siglas if not senhas[s]]
    erro = None
    # Primeiro o advogado (é o que a busca no DJEN precisa); o PJe é opcional e só é
    # conferido quando há tribunal marcado.
    if not valores["advogado_nome"]:
        erro = "Informe o nome exatamente como sai no DJEN."
    elif not valores["oab_numero"]:
        erro = "Informe o número da OAB (só os números)."
    elif valores["oab_uf"] not in UFS:
        erro = "Escolha a UF da OAB."
    elif siglas and len(valores["cpf"]) != 11:
        erro = ("Informe o CPF do PJe com 11 dígitos, ou desmarque os tribunais: as "
                "publicações do Diário funcionam sem o PJe.")
    elif sem_senha:
        if cpf_mudou and any(guardado["tem_senha"].get(s) for s in sem_senha):
            erro = "Ao trocar o CPF, digite a senha do PJe de novo."
        else:
            erro = (f"Informe a senha do PJe do {' e do '.join(sem_senha)}, ou desmarque "
                    "o tribunal: as publicações do Diário funcionam sem a senha.")
    if erro:
        flash(erro, "erro")
        return _tela(valores, guardado)

    tribs = [nucleo_tribunal.TRIBUNAIS[s] for s in siglas]
    senhas2 = {t.sigla: (senhas2_digitadas[t.sigla] or guardada(t.sigla, True))
               if t.tem_instancia("2grau") else "" for t in tribs}
    fora: list[tuple[nucleo_tribunal.Tribunal, list[str]]] = []
    recusas: list[str] = []
    for trib in tribs:  # cada tribunal com a SUA senha e o SEU endereço
        cred = {"cpf": valores["cpf"], "senha": senhas[trib.sigla]}
        if senhas2[trib.sigla]:
            cred["senha_2grau"] = senhas2[trib.sigla]
        resultado = sondar_login(cred, trib.instancias, forcar_endereco=trib.forcar_endereco)
        recusadas = [c for c, (estado, _m) in resultado.items() if estado == RECUSADA]
        indisponiveis = [c for c, (estado, _m) in resultado.items() if estado == INDISPONIVEL]
        if recusadas:
            recusas.append(f"O PJe do {trib.sigla} recusou o login na "
                           f"{_rotulos(trib, recusadas)}.")
        if indisponiveis:
            fora.append((trib, indisponiveis))
    if recusas:
        flash(" ".join(recusas) + " Confira o CPF e a senha (o PJe pede troca periódica "
              "da senha). Nada foi gravado.", "erro")
        return _tela(valores, guardado)

    try:
        ajustes_locais.salvar(
            tribunais=siglas, advogado_nome=valores["advogado_nome"],
            oab_numero=valores["oab_numero"], oab_uf=valores["oab_uf"],
            pje_cpf=valores["cpf"],
            senhas={s: (senhas[s], senhas2[s]) for s in siglas})
    except cofre.CofreError as exc:
        flash(f"Não foi possível guardar no cofre deste computador: {exc}", "erro")
        return _tela(valores, guardado)
    global _configurado
    _configurado = True

    conn = banco.conectar()
    try:
        with conn:
            pedida = tarefas_fila.pedir(conn, "varredura", por="configuracao")
    finally:
        conn.close()
    flash("Configuração guardada. " + (
        "A primeira varredura já está na fila; o Cockpit se atualiza sozinho." if pedida
        else "Já havia uma varredura na fila."), "ok")
    if not siglas:
        flash("As publicações do Diário (DJEN) já são buscadas. Para a Controladoria baixar "
              "os autos do PJe, volte à Configuração do escritório quando tiver a senha.",
              "aviso")
    for trib, indisponiveis in fora:
        flash(f"O PJe do {trib.sigla} não respondeu agora ({_rotulos(trib, indisponiveis)}). "
              + AVISO_PJE_FORA, "aviso")
    return redirect(url_for("lista"))
