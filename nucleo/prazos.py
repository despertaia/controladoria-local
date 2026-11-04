"""A conta dos prazos (1.6.0): módulo puro, sem banco nem rede. Recebe datas, dias, unidade
e calendário e devolve a data fatal com o passo a passo. Mesma entrada, mesma data.

Aqui não há número de lei: os dias vêm da publicação ou de `nucleo/prazos_legais.py`, e os
feriados e as suspensões, da tabela `dia_forense`. Regras e fontes: A1 a A3, A14, C9, C10 e
F15 de docs/fontes/2026-11-01-prazos-fontes.md (só no repositório privado)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Mapping

UTEIS = "uteis"
CORRIDOS = "corridos"
UNIDADES_DA_CONTA = (UTEIS, CORRIDOS)
FIM_DE_SEMANA = (5, 6)  # sábado e domingo em date.weekday()
_SABADO, _ = FIM_DE_SEMANA
_UM_DIA = timedelta(days=1)
_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
_SUPERIORES = ("STF", "STJ", "TST", "TSE", "STM")
_J_TR = slice(13, 16)  # segmento e tribunal no número CNJ, só dígitos
LIMITE_DIAS = 365  # teto do prazo já com o dobro; acima disso a conta é à mão
_NUMERO_CNJ = re.compile(r"\d{20}")  # os 20 dígitos do número único
_FIM_DE_SEMANA_TXT = "fim de semana"
MSG_FORA_DO_ALCANCE = "Data fora do alcance da conta automática."
AVISO_MUNICIPAIS = "feriados municipais considerados: os cadastrados"
AVISO_SUSPENSAO = ("O prazo passa por dias suspensos. No fim de ano há exceções (réu preso, "
                   "Maria da Penha, urgências, alimentos): confira.")
AVISO_SABADO = ("Vence no sábado. Em dias corridos, a regra lida fala em domingo ou feriado; "
                "mostramos a data mais cedo. Confira.")


class PrazoForaDaConta(ValueError):
    """Entrada que a conta automática não trata (unidade, dias, data): vira «a definir»."""


def dia_br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _rotulo(d: date) -> str:
    return f"{_SEMANA[d.weekday()]} {dia_br(d)}"


def _exigir_data(valor, nome: str, opcional: bool = False) -> None:
    """Só `date` simples entra na conta: datetime, texto e número ficam de fora."""
    if valor is None and opcional:
        return
    if type(valor) is not date:
        raise PrazoForaDaConta(f"Data inválida ({nome}): é preciso uma data, sem hora.")


def _exigir_inteiro(valor, nome: str) -> None:
    if not isinstance(valor, int) or isinstance(valor, bool) or valor < 0:
        raise PrazoForaDaConta(f"Número inválido ({nome}): {valor!r}.")


def _sem_acento_maiusculo(texto) -> str:
    base = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in base if not unicodedata.combining(c)).strip().upper()


@dataclass(frozen=True)
class Calendario:
    tribunal: str = ""
    comarca: str = ""
    sem_expediente: Mapping[date, str] = field(default_factory=dict)
    reduzido: Mapping[date, str] = field(default_factory=dict)
    suspensao: Mapping[date, str] = field(default_factory=dict)
    anos_tribunal: frozenset = frozenset()
    anos_nacional: frozenset = frozenset()
    versao: int = 0

    def dia_util(self, d: date) -> bool:
        """Há expediente: não é fim de semana nem dia sem expediente."""
        return d.weekday() not in FIM_DE_SEMANA and d not in self.sem_expediente

    def corre(self, d: date) -> bool:
        """É dia útil e o prazo corre nele (não está em suspensão)."""
        return self.dia_util(d) and d not in self.suspensao

    def proximo_util(self, d: date) -> date:
        d += _UM_DIA
        while not self.dia_util(d):
            d += _UM_DIA
        return d

    def proximo_que_corre(self, d: date) -> date:
        d += _UM_DIA
        while not self.corre(d):
            d += _UM_DIA
        return d

    def por_que_nao_conta(self, d: date, suspensao_primeiro: bool = False) -> str:
        """O motivo de o dia não contar. Em dias corridos (e nos intervalos pulados) a
        suspensão vem antes do fim de semana, porque é ela que tira o dia da conta."""
        if d in self.sem_expediente:
            return self.sem_expediente[d]
        if suspensao_primeiro and d in self.suspensao:
            return self.suspensao[d]
        if d.weekday() in FIM_DE_SEMANA:
            return _FIM_DE_SEMANA_TXT
        return self.suspensao.get(d, "prazos suspensos")


@dataclass(frozen=True)
class Contagem:
    disponibilizacao: date | None
    publicacao: date | None
    inicio: date
    fatal: date
    dias: int
    unidade: str
    dobro: bool
    passos: tuple[str, ...]
    avisos: tuple[str, ...]
    versao: int

    def como_dict(self) -> dict:
        def iso(d):
            return d.isoformat() if d else None
        return {"disponibilizacao": iso(self.disponibilizacao), "publicacao": iso(self.publicacao),
                "inicio": iso(self.inicio), "fatal": iso(self.fatal), "dias": self.dias,
                "unidade": self.unidade, "dobro": self.dobro, "passos": list(self.passos),
                "avisos": list(self.avisos), "versao": self.versao}


def _avisos(desde: date, fatal: date, cal: Calendario, unidade: str) -> tuple[str, ...]:
    """`desde` é o primeiro dia antes de qualquer pulo (a publicação, o início informado ou o
    dia seguinte à ciência): a suspensão que o prazo atravessa tem de aparecer."""
    avisos = [AVISO_MUNICIPAIS + (f" (comarca de {cal.comarca})" if cal.comarca else "")]
    for ano in range(desde.year, fatal.year + 1):
        if ano not in cal.anos_tribunal:
            avisos.append(f"calendário de {ano} do {cal.tribunal or 'tribunal'} "
                          "ainda não cadastrado: confira")
        if ano not in cal.anos_nacional:
            avisos.append(f"feriados nacionais de {ano} não cadastrados: confira")
    if any(desde + timedelta(days=i) in cal.suspensao
           for i in range((fatal - desde).days + 1)):
        avisos.append(AVISO_SUSPENSAO)
    if unidade == CORRIDOS and fatal.weekday() == _SABADO:
        avisos.append(AVISO_SABADO)
        if fatal in cal.sem_expediente:
            avisos.append(f"O sábado do vencimento é feriado ou dia sem expediente "
                          f"({cal.sem_expediente[fatal]}): a data fica nele, mas pode passar "
                          "ao dia útil seguinte. Confira.")
    if fatal in cal.reduzido:
        avisos.append(f"Vence em dia de expediente reduzido ({cal.reduzido[fatal]}): "
                      "confira o horário do protocolo.")
    return tuple(avisos)


def _pular(cal: Calendario, de: date, conta, motivo, texto: str, sempre: bool):
    """Primeiro dia a partir de `de` em que `conta(d)` vale, e as linhas que explicam os dias
    pulados (agrupados por motivo). Só fim de semana não vira linha, salvo `sempre`."""
    grupos: list[tuple[date, date, str]] = []
    d = de
    while not conta(d):
        m = motivo(d)
        if grupos and grupos[-1][-1] == m and grupos[-1][1] + _UM_DIA == d:
            grupos[-1] = (grupos[-1][0], d, m)
        else:
            grupos.append((d, d, m))
        d += _UM_DIA
    if not sempre and all(m == _FIM_DE_SEMANA_TXT for _, _, m in grupos):
        grupos = []
    linhas = []
    for primeiro, ultimo, m in grupos:
        quando = (_rotulo(primeiro) if primeiro == ultimo
                  else f"de {_rotulo(primeiro)} a {_rotulo(ultimo)}")
        linhas.append(f"{quando}: {texto} ({m})")
    return d, linhas


def _contar(disponibilizacao, dias, unidade, cal, dobro, inicio, passos_antes, informado):
    if unidade not in UNIDADES_DA_CONTA:
        raise PrazoForaDaConta(f"Unidade fora da conta automática: {unidade!r}.")
    if not isinstance(dias, int) or isinstance(dias, bool) or dias < 1:
        raise PrazoForaDaConta(f"Número de dias inválido: {dias!r}.")
    _exigir_data(disponibilizacao, "disponibilização", opcional=True)
    _exigir_data(inicio, "início", opcional=True)
    if disponibilizacao is None and inicio is None:
        raise PrazoForaDaConta("Sem data de disponibilização nem de início.")
    total = dias + dias if dobro else dias
    if total > LIMITE_DIAS:
        raise PrazoForaDaConta(f"Prazo de {total} dias passa do limite da conta automática "
                               f"({LIMITE_DIAS} dias). Preencha à mão.")
    passos = list(passos_antes)
    publicacao = None

    def motivo(d):
        return cal.por_que_nao_conta(d, suspensao_primeiro=True)

    if inicio is None:
        passos.append(f"Disponibilização: {_rotulo(disponibilizacao)}")
        publicacao, pulos = _pular(cal, disponibilizacao + _UM_DIA, cal.dia_util,
                                   lambda d: cal.sem_expediente.get(d, _FIM_DE_SEMANA_TXT),
                                   "sem expediente", sempre=False)
        passos.extend(pulos)
        passos.append(f"Publicação: {_rotulo(publicacao)} (primeiro dia útil seguinte)")
        desde = publicacao
        candidato = publicacao + _UM_DIA
        sempre = False
    elif informado:
        passos.append(f"Início informado: {_rotulo(inicio)}")
        candidato = desde = inicio
        sempre = True
    else:
        passos.append(f"A contagem começa no dia seguinte à ciência: {_rotulo(inicio)}")
        candidato = desde = inicio
        sempre = True
    if dobro:
        passos.append(f"Prazo em dobro: {total} dias")
    if unidade == UTEIS:
        conta = cal.corre
    else:
        def conta(d):
            return d not in cal.suspensao
    primeiro, pulos = _pular(cal, candidato, conta, motivo, "não conta", sempre)
    passos.extend(pulos)
    if unidade == UTEIS:
        while primeiro in cal.reduzido:
            passos.append(f"{_rotulo(primeiro)}: expediente reduzido no começo; "
                          "passa ao dia útil seguinte")
            primeiro = cal.proximo_que_corre(primeiro)
    d, n = primeiro, 0
    while True:
        if conta(d):
            n += 1
            passos.append(f"{_rotulo(d)}: dia {n}")
            if n == total:
                break
        else:
            passos.append(f"{_rotulo(d)}: não conta ({motivo(d)})")
        d += _UM_DIA
    fatal = d
    if unidade == UTEIS:
        while fatal in cal.reduzido:
            passos.append(f"{_rotulo(fatal)}: expediente reduzido no vencimento; "
                          "passa ao dia útil seguinte")
            fatal = cal.proximo_que_corre(fatal)
    elif fatal.weekday() != _SABADO and not cal.corre(fatal):
        passos.append(f"{_rotulo(fatal)}: não há expediente; passa ao dia útil seguinte")
        fatal = cal.proximo_que_corre(fatal)
    passos.append(f"Vencimento: {_rotulo(fatal)}")
    return Contagem(disponibilizacao, publicacao, primeiro, fatal, dias, unidade, dobro,
                    tuple(passos), _avisos(desde, fatal, cal, unidade), cal.versao)


def contar(disponibilizacao: date | None, dias: int, unidade: str, cal: Calendario, *,
           dobro: bool = False, inicio: date | None = None,
           passos_antes: tuple[str, ...] = ()) -> Contagem:
    """A conta. Sem `inicio`: disponibilização → publicação (primeiro dia útil seguinte) →
    início (primeiro dia seguinte em que o prazo corre). Com `inicio` (informado pelo
    advogado): ele é o primeiro dia contado, ou o próximo em que o prazo corre."""
    try:
        return _contar(disponibilizacao, dias, unidade, cal, dobro, inicio, passos_antes, True)
    except OverflowError:
        raise PrazoForaDaConta(MSG_FORA_DO_ALCANCE) from None


def contar_da_ciencia(ciencia: date, dias: int, unidade: str, cal: Calendario, *,
                      dobro: bool = False, tacita: bool = False) -> Contagem:
    """Conta a partir da ciência no PJe pela leitura mais curta (F15): o dia da ciência é o
    dia zero. Consulta em dia não útil vale no dia útil seguinte (B2); a ciência que
    acontece sozinha (`tacita`) não anda, o que dá a data mais cedo."""
    _exigir_data(ciencia, "ciência")
    try:
        zero = ciencia if tacita or cal.dia_util(ciencia) else cal.proximo_util(ciencia)
        como = "Ciência sozinha no PJe" if tacita else "Ciência no PJe"
        antes = [f"{como}: {_rotulo(ciencia)}"]
        if zero != ciencia:
            antes.append(f"{_rotulo(ciencia)}: sem expediente ({cal.por_que_nao_conta(ciencia)}); "
                         f"a ciência vale no dia útil seguinte, {_rotulo(zero)}")
        return _contar(None, dias, unidade, cal, dobro, zero + _UM_DIA, tuple(antes), False)
    except OverflowError:
        raise PrazoForaDaConta(MSG_FORA_DO_ALCANCE) from None


def ciencia_tacita(envio: date, dias_da_lei: int) -> date:
    """O dia em que a ciência acontece sozinha: `dias_da_lei` dias corridos depois do envio."""
    _exigir_data(envio, "envio")
    _exigir_inteiro(dias_da_lei, "dias da lei")
    try:
        return envio + timedelta(days=dias_da_lei)
    except OverflowError:
        raise PrazoForaDaConta(MSG_FORA_DO_ALCANCE) from None


def data_de_trabalho(fatal: date, d: int, cal: Calendario) -> date:
    """D-x: `d` dias úteis antes da fatal (D-0 é a própria fatal)."""
    _exigir_data(fatal, "fatal")
    _exigir_inteiro(d, "dias antes")
    if d > LIMITE_DIAS:
        raise PrazoForaDaConta(f"Dias antes da fatal acima do limite ({LIMITE_DIAS}).")
    dia = fatal
    try:
        while d > 0:
            dia -= _UM_DIA
            while not cal.dia_util(dia):
                dia -= _UM_DIA
            d -= 1
    except OverflowError:
        raise PrazoForaDaConta(MSG_FORA_DO_ALCANCE) from None
    return dia


def uteis_ate(hoje: date, alvo: date, cal: Calendario) -> int:
    """Quantos dias úteis faltam até `alvo` (0 se é hoje; -1 se já passou)."""
    _exigir_data(hoje, "hoje")
    _exigir_data(alvo, "alvo")
    if alvo < hoje:
        return -1
    n, d = 0, hoje
    while d < alvo:
        d += _UM_DIA
        if cal.dia_util(d):
            n += 1
    return n


def fora_da_conta(numero: str, tribunal: str = "", rito: str = "") -> str:
    """O motivo de este processo não entrar na conta automática (F17, F18), ou ""."""
    digitos = "".join(c for c in str(numero or "") if c.isdigit())
    legivel = _NUMERO_CNJ.fullmatch(digitos) is not None
    jtr = digitos[_J_TR] if legivel else ""
    segmento, corte = jtr[:1], jtr[1:]
    sigla = _sem_acento_maiusculo(tribunal)
    if ("ELEITORAL" in _sem_acento_maiusculo(rito) or segmento == "6"
            or sigla.startswith("TRE") or sigla == "TSE"):
        return "Rito eleitoral: a contagem muda a cada eleição. Preencha à mão."
    if sigla in _SUPERIORES or segmento in ("1", "3") or (segmento in ("5", "7")
                                                          and corte == "00"):
        return "Nos tribunais superiores o calendário é próprio. Preencha à mão."
    if not legivel:
        return "Número do processo ilegível: não dá para saber o tribunal. Preencha à mão."
    return ""
