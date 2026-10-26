"""Interface única das credenciais do PJe (CPF e senha de cada tribunal/instância).

Dois modos, decididos por `CONTROLADORIA_LOCAL=1` (a mesma regra do painel local):

- Local (computador do advogado): delega a `nucleo.cofre` (Chaves do macOS, Gerenciador
  de Credenciais do Windows) e a `nucleo.ajustes_locais` (lista de tribunais). As chaves
  do cofre são `pje_cpf`, `pje_senha_<SIGLA>` e `pje_senha_2grau_<SIGLA>`. A tabela
  `credencial` não é usada.
- VPS: tabela `credencial` (senha cifrada por sigla e instância) e `config`
  (`pje_cpf_cifrado`), tudo cifrado com `nucleo.cofre_servidor`. Sem a chave do cofre,
  gravar levanta `CofreServidorError` e carregar no ambiente cai no `.env` de sempre.

Contrato com `nucleo.tribunal`: `carregar_no_ambiente` define `PJE_CPF`,
`PJE_SENHA_<SIGLA>` (1ª instância), `PJE_SENHA_2GRAU_<SIGLA>` (2ª instância) e
`CONTROLADORIA_TRIBUNAIS` (siglas com alguma senha, o TJMT primeiro; sem nenhuma, a
variável não é mexida). Variável de credencial apagada sai do ambiente.

Segredos nunca vão a log nem a mensagem de erro. Nada aqui faz commit: quem chama usa
`with conn:`."""

from __future__ import annotations

import logging
import os
import sqlite3

from nucleo import ajustes_locais, cofre, cofre_servidor, config, eventos, tribunal

log = logging.getLogger(__name__)

CHAVE_CPF = "pje_cpf_cifrado"
INSTANCIAS = ("1grau", "2grau")

INSTANCIAS_HOMOLOGADAS: list[tuple[str, str, str]] = [
    (sigla, chave, rotulo)
    for sigla, trib in tribunal.TRIBUNAIS.items()
    for chave, rotulo, _var, _wsdl in trib.instancias
]

# Variáveis de ambiente que este módulo definiu: só estas saem quando a credencial é
# apagada (o que veio do .env nunca é tocado).
_definidas: set[str] = set()
# Valor que a variável tinha (do .env/sistema) antes de este módulo a definir: volta a
# valer quando o cofre esvazia (None = não existia).
_originais: dict[str, str | None] = {}


def _local() -> bool:
    return os.getenv("CONTROLADORIA_LOCAL", "").strip() == "1"


def _validar(sigla: str, instancia: str) -> tuple[str, str]:
    sigla = str(sigla).strip().upper()
    if (sigla, instancia) not in {(s, i) for s, i, _ in INSTANCIAS_HOMOLOGADAS}:
        raise ValueError(f"Tribunal ou instância desconhecidos: {sigla} {instancia}")
    return sigla, instancia


def _chave_local(sigla: str, instancia: str) -> str:
    return f"pje_senha_2grau_{sigla}" if instancia == "2grau" else f"pje_senha_{sigla}"


def _var_ambiente(sigla: str, instancia: str) -> str:
    return f"PJE_SENHA_2GRAU_{sigla}" if instancia == "2grau" else f"PJE_SENHA_{sigla}"


def _so_digitos(cpf: str) -> str:
    digitos = "".join(c for c in str(cpf) if c.isdigit())
    if len(digitos) != 11:
        raise ValueError("O CPF deve ter 11 dígitos.")
    return digitos


# --- CPF -------------------------------------------------------------------------

def cpf_guardado(conn: sqlite3.Connection) -> str:
    """CPF guardado ("" se não houver). A tela só mostra os 2 últimos dígitos."""
    if _local():
        return cofre.ler("pje_cpf") or ""
    token = config.ler(conn, CHAVE_CPF)
    return cofre_servidor.decifrar(token) if token else ""


def gravar_cpf(conn: sqlite3.Connection, cpf: str) -> None:
    digitos = _so_digitos(cpf)
    if _local():
        cofre.gravar("pje_cpf", digitos)
        return
    config.gravar(conn, CHAVE_CPF, cofre_servidor.cifrar(digitos))


def _migrar_chaves_antigas(principal: str | None) -> None:
    """Instalação de antes: a senha do principal estava só em `pje_senha` e
    `pje_senha_2grau`. Passa para as chaves por sigla antes de mexer, senão apagar a
    por sigla deixaria a antiga valendo (`senha_guardada` recorre a ela)."""
    if not principal:
        return
    for instancia in INSTANCIAS:
        if cofre.ler(_chave_local(principal, instancia)):
            continue
        antiga = cofre.ler("pje_senha_2grau" if instancia == "2grau" else "pje_senha")
        if antiga:
            cofre.gravar(_chave_local(principal, instancia), antiga)


def _espelhar_principal(principal: str | None) -> None:
    """As chaves antigas espelham o principal atual (versão anterior do programa
    continua lendo); sem principal ou sem a senha, ficam vazias (apagadas)."""
    for instancia in INSTANCIAS:
        valor = (cofre.ler(_chave_local(principal, instancia)) if principal else "") or ""
        cofre.gravar("pje_senha_2grau" if instancia == "2grau" else "pje_senha", valor)


# --- senhas ----------------------------------------------------------------------

def _senha(conn: sqlite3.Connection, sigla: str, instancia: str) -> str | None:
    if _local():
        return ajustes_locais.senha_guardada(sigla, instancia == "2grau") or None
    linha = conn.execute("SELECT senha_cifrada FROM credencial WHERE sigla = ? AND "
                         "instancia = ?", (sigla, instancia)).fetchone()
    return cofre_servidor.decifrar(linha["senha_cifrada"]) if linha else None


def ler(conn: sqlite3.Connection, sigla: str, instancia: str) -> dict | None:
    """{"cpf", "senha"} da instância, ou None se não há senha guardada."""
    sigla, instancia = _validar(sigla, instancia)
    senha = _senha(conn, sigla, instancia)
    if not senha:
        return None
    return {"cpf": cpf_guardado(conn), "senha": senha}


def gravar(conn: sqlite3.Connection, sigla: str, instancia: str, senha: str) -> None:
    sigla, instancia = _validar(sigla, instancia)
    if not senha:
        raise ValueError("Informe a senha.")
    if not cpf_guardado(conn):
        raise ValueError("Guarde primeiro o seu CPF.")
    if _local():
        siglas = ajustes_locais.tribunais_guardados()
        _migrar_chaves_antigas(siglas[0] if siglas else None)
        cofre.gravar(_chave_local(sigla, instancia), senha)
        if sigla not in siglas:
            siglas = siglas + [sigla]
            ajustes_locais.guardar_tribunais(siglas)
        _espelhar_principal(siglas[0])
        return
    conn.execute(
        "INSERT INTO credencial (sigla, instancia, senha_cifrada, atualizado_em) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (sigla, instancia) DO UPDATE SET "
        "senha_cifrada = excluded.senha_cifrada, atualizado_em = excluded.atualizado_em",
        (sigla, instancia, cofre_servidor.cifrar(senha), eventos.agora()))


def apagar(conn: sqlite3.Connection, sigla: str, instancia: str) -> None:
    sigla, instancia = _validar(sigla, instancia)
    if not _local():
        conn.execute("DELETE FROM credencial WHERE sigla = ? AND instancia = ?",
                     (sigla, instancia))
        return
    siglas = ajustes_locais.tribunais_guardados()
    _migrar_chaves_antigas(siglas[0] if siglas else None)
    cofre.apagar(_chave_local(sigla, instancia))
    restante = any(cofre.ler(_chave_local(sigla, i)) for i in INSTANCIAS
                   if i != instancia and tribunal.TRIBUNAIS[sigla].tem_instancia(i))
    if not restante and sigla in siglas:
        siglas = [s for s in siglas if s != sigla]
        ajustes_locais.guardar_tribunais(siglas)
    _espelhar_principal(siglas[0] if siglas else None)


def cadastradas(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Instâncias que têm senha guardada."""
    if _local():
        return {(s, i) for s, i, _ in INSTANCIAS_HOMOLOGADAS if _senha(conn, s, i)}
    return {(r["sigla"], r["instancia"])
            for r in conn.execute("SELECT sigla, instancia FROM credencial")}


# --- ambiente --------------------------------------------------------------------

def _definir(var: str, valor: str) -> None:
    if valor:
        os.environ[var] = valor
        _definidas.add(var)
    elif var in _definidas:
        os.environ.pop(var, None)
        _definidas.discard(var)


def _variaveis_de_senha() -> list[str]:
    return [_var_ambiente(s, i) for s, i, _ in INSTANCIAS_HOMOLOGADAS]


def _variaveis_locais() -> tuple[str, ...]:
    return ("PJE_CPF", "PJE_SENHA", "PJE_SENHA_2GRAU", "CONTROLADORIA_TRIBUNAIS",
            "CONTROLADORIA_TRIBUNAL", *_variaveis_de_senha())


def registrar_configuracao_local() -> None:
    """Depois de `ajustes_locais.salvar` (a Configuração do modo local), que põe no
    ambiente, por cima de tudo, o CPF, as senhas e os tribunais que guardou: daqui em
    diante essas variáveis são do cofre, e a tela de tribunais pode trocá-las ou
    removê-las sem reiniciar."""
    _definidas.update(v for v in _variaveis_locais() if os.environ.get(v, "").strip())


def _carregar_local() -> None:
    """Delega a `ajustes_locais` (que não sobrescreve nada) e sincroniza com o cofre o
    que ele não faz: valor trocado entra, credencial apagada sai. Só se mexe no que o
    próprio módulo (ou o `ajustes_locais`, via esta chamada) pôs no ambiente; variável
    que veio do `.env` ou do sistema continua mandando."""
    _definidas.update(ajustes_locais.carregar_no_ambiente())
    desejado = {v: "" for v in _variaveis_locais()}
    desejado.update({v: x for v, x in ajustes_locais._ambiente(ajustes_locais.ler()).items()
                     if v in desejado})
    for var, valor in desejado.items():
        if valor and (var in _definidas or not os.environ.get(var, "").strip()):
            _definir(var, valor)
        elif not valor:
            _definir(var, "")


def carregar_local() -> None:
    """Primeira carga do lançador do modo local (`local/iniciar.py`): como
    `carregar_no_ambiente`, registrando os nomes definidos para que trocar ou remover
    uma senha na tela valha sem reiniciar."""
    _carregar_local()


def _siglas_do_ambiente() -> list[str]:
    """Tribunais que o ambiente (.env/sistema) já tem com senha, sem contar o que este
    módulo mesmo pôs lá numa chamada anterior. Calcula sobre uma CÓPIA do ambiente
    (sem as variáveis do módulo e com a lista de tribunais original do .env): o
    `os.environ` não é tocado (outras threads do painel o leem ao mesmo tempo)."""
    amb = {k: v for k, v in os.environ.items() if k not in _definidas}
    original = _originais.get("CONTROLADORIA_TRIBUNAIS")
    if "CONTROLADORIA_TRIBUNAIS" in _definidas and original is not None:
        amb["CONTROLADORIA_TRIBUNAIS"] = original
    return [t.sigla for t in tribunal.configurados(amb)
            if tribunal.senha(t.sigla, amb) or tribunal.senha_2grau(t.sigla, amb)]


def _restaurar(var: str) -> None:
    """Devolve a variável ao valor de antes de este módulo a definir."""
    if var not in _definidas:
        return
    original = _originais.get(var)
    if original is None:
        os.environ.pop(var, None)
    else:
        os.environ[var] = original
    _definidas.discard(var)


def _aplicar_do_cofre(conn: sqlite3.Connection) -> bool:
    """VPS: lê tudo do cofre e só então mexe no ambiente (nunca fica pela metade).
    Decifra linha a linha: a senha que a chave atual não abre fica de fora (o log diz
    qual, sem segredo) e as legíveis valem. Sem o CPF não há login: se ele não abre
    (ou a chave é ilegível), nada é aplicado e devolve False (o ambiente fica como
    estava)."""
    try:
        cpf = cpf_guardado(conn)
    except cofre_servidor.CofreServidorError:
        log.warning("cofre do servidor: a chave não abre o CPF guardado; nenhuma senha "
                    "do cofre foi usada")
        return False
    senhas = {}
    for sigla, instancia, _rotulo in INSTANCIAS_HOMOLOGADAS:
        try:
            senhas[(sigla, instancia)] = _senha(conn, sigla, instancia) or ""
        except cofre_servidor.CofreServidorError:
            log.warning("cofre do servidor: a senha guardada do %s (%s) não abre com a "
                        "chave atual; ficou de fora", sigla, instancia)
            senhas[(sigla, instancia)] = ""
    if "CONTROLADORIA_TRIBUNAIS" not in _definidas:  # ainda é a do .env (ou nenhuma)
        _originais["CONTROLADORIA_TRIBUNAIS"] = os.environ.get("CONTROLADORIA_TRIBUNAIS")
    no_cofre = [s for s in dict.fromkeys(s for (s, _i), v in senhas.items() if v)]
    siglas = list(dict.fromkeys(no_cofre + _siglas_do_ambiente())) if no_cofre else []
    siglas.sort(key=lambda s: s != "TJMT")  # estável: o TJMT primeiro
    _definir("PJE_CPF", cpf)
    for (sigla, instancia), valor in senhas.items():
        _definir(_var_ambiente(sigla, instancia), valor)
    if siglas:
        _definir("CONTROLADORIA_TRIBUNAIS", ",".join(siglas))
    else:
        _restaurar("CONTROLADORIA_TRIBUNAIS")  # cofre vazio: volta a do .env
    return True


# --- estado do cofre (VPS) -------------------------------------------------------

def tem_credenciais_cifradas(conn: sqlite3.Connection) -> bool:
    """O banco guarda alguma senha ou o CPF cifrados (banco sem as tabelas: False)."""
    try:
        if conn.execute("SELECT 1 FROM credencial LIMIT 1").fetchone():
            return True
        return bool(config.ler(conn, CHAVE_CPF))
    except sqlite3.Error:
        return False


def _chave_utilizavel() -> bool:
    if not cofre_servidor.pronto():
        return False
    try:
        cofre_servidor._fernet()
    except cofre_servidor.CofreServidorError:
        return False
    return True


def _abre(token: str) -> bool:
    try:
        cofre_servidor.decifrar(token)
    except cofre_servidor.CofreServidorError:
        return False
    return True


def ilegiveis(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """VPS com chave utilizável: instâncias com senha guardada que a chave atual não
    decifra (a tela pede para digitar de novo). Local ou sem chave: vazio."""
    if _local() or not _chave_utilizavel():
        return set()
    try:
        linhas = conn.execute("SELECT sigla, instancia, senha_cifrada FROM credencial"
                              ).fetchall()
    except sqlite3.Error:
        return set()
    return {(r["sigla"], r["instancia"]) for r in linhas if not _abre(r["senha_cifrada"])}


def estado_do_cofre(conn: sqlite3.Connection) -> str:
    """"local" (modo local: cofre do computador), "sem_chave" (VPS sem a chave ou com
    um arquivo de chave ilegível), "nao_abre" (existe senha ou CPF guardado que a chave
    atual não decifra) ou "ok"."""
    if _local():
        return "local"
    if not _chave_utilizavel():
        return "sem_chave"
    if ilegiveis(conn):
        return "nao_abre"
    try:
        token = config.ler(conn, CHAVE_CPF)
    except sqlite3.Error:
        token = ""
    if token and not _abre(token):
        return "nao_abre"
    return "ok"


def carregar_no_ambiente(conn: sqlite3.Connection) -> None:
    """Põe as credenciais guardadas em `os.environ` (contrato no docstring do módulo).

    VPS: `CONTROLADORIA_TRIBUNAIS` = siglas com senha no cofre mais as que o `.env` já
    atende, o TJMT primeiro (guardar só o TJMG não tira o TJMT do `.env`). Sem nenhuma
    senha no cofre, a variável que este módulo definiu antes volta ao valor do `.env`
    (ou sai, se o `.env` não a tinha)."""
    if _local():
        _carregar_local()
        return
    if not cofre_servidor.pronto():
        log.warning("cofre do servidor sem chave: usando o .env")
        return
    _aplicar_do_cofre(conn)


def _abrir_somente_leitura(caminho: str) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def problema_do_cofre_no_banco(caminho: str) -> str:
    """Para o verificador de publicação: se o banco (aberto só para leitura, sem migrar)
    guarda senhas cifradas e a chave do cofre não as abre (ausente, ilegível ou outra),
    devolve o motivo em português, sem segredo; senão "" (banco sem credenciais, sem as
    tabelas ou inexistente não é problema do cofre)."""
    if not caminho or _local():
        return ""
    conn = _abrir_somente_leitura(caminho)
    if conn is None:
        return ""
    try:
        if not tem_credenciais_cifradas(conn):
            return ""
        estado = estado_do_cofre(conn)
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    chave = cofre_servidor.caminho_da_chave()
    if estado == "sem_chave":
        return (f"O banco de produção tem senhas cifradas, mas a chave do cofre ({chave}) "
                "não está disponível neste servidor: restaure a chave ou recadastre as "
                "senhas em Configurações › Tribunais.")
    if estado == "nao_abre":
        return (f"Há senhas guardadas no banco de produção que a chave do cofre ({chave}) "
                "não abre: restaure a chave certa ou digite essas senhas de novo em "
                "Configurações › Tribunais.")
    return ""


def carregar_do_banco_somente_leitura(caminho: str) -> bool:
    """Como `carregar_no_ambiente` na VPS, mas abrindo o banco só para leitura e sem
    migrar (o verificador de publicação lê o banco de produção sem tocá-lo). Devolve
    True se carregou; False se não há banco, chave ou tabela `credencial` (banco de
    antes do cofre) ou se a chave não abre as credenciais."""
    if not caminho or _local() or not cofre_servidor.pronto():
        return False
    conn = _abrir_somente_leitura(caminho)
    if conn is None:
        return False
    try:
        tabela = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                              "name = 'credencial'").fetchone()
        if tabela is None:
            return False
        return _aplicar_do_cofre(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.close()
