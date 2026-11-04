"""A conta dos prazos: datas fixas, sem banco nem rede (spec, seção 3 e 8)."""

import ast
import pathlib
from datetime import date, datetime, timedelta

import pytest

from nucleo import prazos


def _intervalo(de, ate, descricao="suspensão"):
    return {de + timedelta(days=i): descricao for i in range((ate - de).days + 1)}


def _cal(sem=None, red=(), sus=None, tribunal="TJMT", anos=(2026, 2027), nacional=(2026, 2027)):
    base = {date(2026, 11, 2): "feriado", date(2026, 11, 15): "feriado",
            date(2026, 11, 20): "feriado", date(2026, 12, 25): "feriado",
            date(2027, 1, 1): "feriado"}
    base.update(sem or {})
    return prazos.Calendario(tribunal=tribunal, sem_expediente=base,
                             reduzido={d: "expediente reduzido" for d in red},
                             suspensao=sus or {}, anos_tribunal=frozenset(anos),
                             anos_nacional=frozenset(nacional), versao=7)


FIM_DE_ANO = _intervalo(date(2026, 12, 20), date(2027, 1, 20))
RECESSO = _intervalo(date(2026, 12, 20), date(2027, 1, 6), "recesso")


def test_exemplo_da_fonte_a14():
    c = prazos.contar(date(2026, 11, 5), 15, prazos.UTEIS, _cal())
    assert c.publicacao == date(2026, 11, 6)
    assert c.inicio == date(2026, 11, 9)
    assert c.fatal == date(2026, 11, 30)
    assert c.versao == 7
    texto = "\n".join(c.passos)
    assert "Disponibilização: qui 05/11/2026" in texto
    assert "Publicação: sex 06/11/2026" in texto
    assert "seg 09/11/2026: dia 1" in texto
    assert "sex 20/11/2026: não conta (feriado)" in texto
    assert "seg 30/11/2026: dia 15" in texto


def test_mesma_entrada_mesma_data():
    a = prazos.contar(date(2026, 11, 5), 15, prazos.UTEIS, _cal())
    b = prazos.contar(date(2026, 11, 5), 15, prazos.UTEIS, _cal())
    assert a == b and a.como_dict() == b.como_dict()
    assert a.como_dict()["fatal"] == "2026-11-30"


def test_dia_reduzido_no_comeco_passa_ao_dia_util_seguinte():
    c = prazos.contar(date(2026, 11, 12), 3, prazos.UTEIS, _cal(red=[date(2026, 11, 16)]))
    assert c.inicio == date(2026, 11, 17)
    assert c.fatal == date(2026, 11, 19)
    assert any("reduzido" in p for p in c.passos)


def test_dia_reduzido_no_vencimento_passa_ao_dia_util_seguinte():
    c = prazos.contar(date(2026, 11, 5), 5, prazos.UTEIS, _cal(red=[date(2026, 11, 13)]))
    assert c.fatal == date(2026, 11, 16)


def test_dia_reduzido_no_meio_conta():
    c = prazos.contar(date(2026, 11, 5), 5, prazos.UTEIS, _cal(red=[date(2026, 11, 11)]))
    assert c.fatal == date(2026, 11, 13)


def test_publicacao_em_dia_reduzido_nao_muda_nada():
    c = prazos.contar(date(2026, 11, 5), 1, prazos.UTEIS, _cal(red=[date(2026, 11, 6)]))
    assert c.publicacao == date(2026, 11, 6) and c.inicio == date(2026, 11, 9)


def test_virada_de_ano_com_suspensao():
    cal = _cal(sem=RECESSO, sus=FIM_DE_ANO)
    c = prazos.contar(date(2026, 12, 16), 5, prazos.UTEIS, cal)
    assert c.inicio == date(2026, 12, 18)
    assert c.fatal == date(2027, 1, 26)
    assert any("suspenso" in a for a in c.avisos)
    assert any("alimentos" in a for a in c.avisos)


def test_disponibilizado_no_recesso_comeca_a_contar_em_21_de_janeiro():
    cal = _cal(sem=RECESSO, sus=FIM_DE_ANO)
    c = prazos.contar(date(2027, 1, 5), 1, prazos.UTEIS, cal)
    assert c.publicacao == date(2027, 1, 7)
    assert c.inicio == date(2027, 1, 21)
    assert c.fatal == date(2027, 1, 21)


def test_suspensao_com_expediente_nao_conta_mas_nao_e_feriado():
    cal = _cal(sus=FIM_DE_ANO)
    assert cal.dia_util(date(2027, 1, 12)) is True
    assert cal.corre(date(2027, 1, 12)) is False


@pytest.mark.parametrize("dias, fatal", [(5, date(2026, 11, 11)), (2, date(2026, 11, 9))])
def test_corridos_contam_fim_de_semana_e_domingo_prorroga(dias, fatal):
    c = prazos.contar(date(2026, 11, 5), dias, prazos.CORRIDOS, _cal())
    assert c.inicio == date(2026, 11, 7)
    assert c.fatal == fatal


def test_corridos_que_terminam_em_feriado_passam_ao_dia_util():
    c = prazos.contar(date(2026, 11, 17), 2, prazos.CORRIDOS, _cal())
    assert c.fatal == date(2026, 11, 23)


def test_corridos_que_terminam_no_sabado_ficam_no_sabado_com_aviso():
    c = prazos.contar(date(2026, 11, 5), 1, prazos.CORRIDOS, _cal())
    assert c.fatal == date(2026, 11, 7)
    assert any("sábado" in a for a in c.avisos)


def test_corridos_param_na_suspensao():
    c = prazos.contar(date(2026, 12, 16), 3, prazos.CORRIDOS, _cal(sem=RECESSO, sus=FIM_DE_ANO))
    assert c.fatal == date(2027, 1, 21)


def test_dobro_conta_o_dobro_e_diz_no_calculo():
    c = prazos.contar(date(2026, 11, 5), 2, prazos.UTEIS, _cal(), dobro=True)
    assert c.fatal == date(2026, 11, 12) and c.dobro is True and c.dias == 2
    assert any("dobro" in p for p in c.passos)


def test_inicio_informado_vale_e_anda_se_nao_corre():
    c = prazos.contar(None, 2, prazos.UTEIS, _cal(), inicio=date(2026, 11, 14))
    assert c.publicacao is None
    assert c.inicio == date(2026, 11, 16) and c.fatal == date(2026, 11, 17)


@pytest.mark.parametrize("d, esperado", [(0, date(2026, 11, 23)), (1, date(2026, 11, 19)),
                                         (2, date(2026, 11, 18)), (3, date(2026, 11, 17))])
def test_d_x_em_dias_uteis(d, esperado):
    assert prazos.data_de_trabalho(date(2026, 11, 23), d, _cal()) == esperado


def test_uteis_ate():
    cal = _cal()
    assert prazos.uteis_ate(date(2026, 11, 19), date(2026, 11, 23), cal) == 1
    assert prazos.uteis_ate(date(2026, 11, 23), date(2026, 11, 23), cal) == 0
    assert prazos.uteis_ate(date(2026, 11, 24), date(2026, 11, 23), cal) < 0


def test_ano_sem_calendario_avisa_e_conta_com_o_que_ha():
    cal = _cal(anos=(2026,), nacional=(2026,))
    c = prazos.contar(date(2027, 2, 1), 5, prazos.UTEIS, cal)
    assert c.fatal == date(2027, 2, 9)
    assert "calendário de 2027 do TJMT ainda não cadastrado: confira" in c.avisos
    assert "feriados nacionais de 2027 não cadastrados: confira" in c.avisos


def test_tribunal_sem_tabela_avisa():
    cal = _cal(tribunal="TJSP", anos=())
    c = prazos.contar(date(2026, 11, 5), 15, prazos.UTEIS, cal)
    assert c.fatal == date(2026, 11, 30)
    assert "calendário de 2026 do TJSP ainda não cadastrado: confira" in c.avisos


def test_calculo_diz_quais_feriados_municipais_entraram():
    c = prazos.contar(date(2026, 11, 5), 1, prazos.UTEIS, _cal())
    assert "feriados municipais considerados: os cadastrados" in c.avisos


@pytest.mark.parametrize("unidade", ["horas", "meses", ""])
def test_horas_e_meses_ficam_fora_da_conta(unidade):
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(date(2026, 11, 5), 5, unidade, _cal())


def test_dias_invalidos_ficam_fora_da_conta():
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(date(2026, 11, 5), 0, prazos.UTEIS, _cal())


def test_ciencia_informada_usa_a_leitura_mais_curta():
    c = prazos.contar_da_ciencia(date(2026, 11, 10), 5, prazos.UTEIS, _cal())
    assert c.inicio == date(2026, 11, 11) and c.fatal == date(2026, 11, 17)


def test_ciencia_em_dia_nao_util_vale_no_dia_util_seguinte():
    c = prazos.contar_da_ciencia(date(2026, 11, 14), 1, prazos.UTEIS, _cal())
    assert c.inicio == date(2026, 11, 17)


def test_ciencia_tacita_nao_anda_e_conta_do_dia_util_seguinte():
    assert prazos.ciencia_tacita(date(2026, 11, 4), 10) == date(2026, 11, 14)
    c = prazos.contar_da_ciencia(date(2026, 11, 14), 1, prazos.UTEIS, _cal(), tacita=True)
    assert c.inicio == date(2026, 11, 16)


def test_ciencia_em_corridos_conta_do_dia_seguinte():
    c = prazos.contar_da_ciencia(date(2026, 11, 10), 2, prazos.CORRIDOS, _cal())
    assert c.inicio == date(2026, 11, 11) and c.fatal == date(2026, 11, 12)


@pytest.mark.parametrize("numero, tribunal, rito, trecho", [
    ("00012345620248110041", "TJMT", "", ""),
    ("00012345620243000000", "STJ", "", "tribunais superiores"),
    ("00012345620241000000", "", "", "tribunais superiores"),
    ("00012345620245000000", "", "", "tribunais superiores"),
    ("00012345620246110001", "TRE-MT", "", "eleitoral"),
    ("00012345620248110041", "TJMT", "eleitoral", "eleitoral"),
])
def test_fora_da_conta(numero, tribunal, rito, trecho):
    motivo = prazos.fora_da_conta(numero, tribunal, rito)
    assert (motivo == "") if not trecho else (trecho in motivo)


def test_prazos_py_nao_tem_numero_de_lei():
    """Nenhum número legal no módulo: dias vêm da lista fechada ou da publicação, e datas,
    da tabela do calendário."""
    arvore = ast.parse((pathlib.Path(prazos.__file__)).read_text(encoding="utf-8"))
    achados = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Assign) and any(getattr(t, "id", "") in ("FIM_DE_SEMANA", "_J_TR", "LIMITE_DIAS")
                                              for t in no.targets):
            for filho in ast.walk(no):
                filho.liberado = True
        if (isinstance(no, ast.Constant) and isinstance(no.value, int)
                and not isinstance(no.value, bool) and no.value not in (0, 1)
                and not getattr(no, "liberado", False)):
            achados.append(no.value)
    assert achados == []


# ---- rodada de correções 1: a rede de segurança ----------------------------------------

def _cal_fim():
    return _cal(sem=RECESSO, sus=FIM_DE_ANO)


@pytest.mark.parametrize("conta, fatal", [
    (lambda cal: prazos.contar_da_ciencia(date(2026, 12, 18), 1, prazos.UTEIS, cal),
     date(2027, 1, 21)),
    (lambda cal: prazos.contar_da_ciencia(date(2026, 12, 28), 5, prazos.UTEIS, cal),
     date(2027, 1, 27)),
    (lambda cal: prazos.contar_da_ciencia(date(2026, 12, 28), 5, prazos.CORRIDOS, cal),
     date(2027, 1, 25)),
    (lambda cal: prazos.contar_da_ciencia(date(2027, 1, 10), 5, prazos.UTEIS, cal, tacita=True),
     date(2027, 1, 27)),
    (lambda cal: prazos.contar(None, 5, prazos.UTEIS, cal, inicio=date(2026, 12, 28)),
     date(2027, 1, 27)),
])
def test_aviso_de_suspensao_em_todos_os_caminhos(conta, fatal):
    c = conta(_cal_fim())
    assert c.fatal == fatal
    assert any("suspenso" in a for a in c.avisos)
    assert any("alimentos" in a for a in c.avisos)
    assert sum(a == prazos.AVISO_SUSPENSAO for a in c.avisos) == 1


def test_limite_de_dias_depois_do_dobro():
    assert prazos.contar(date(2026, 11, 5), prazos.LIMITE_DIAS, prazos.CORRIDOS, _cal())
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(date(2026, 11, 5), prazos.LIMITE_DIAS + 1, prazos.UTEIS, _cal())
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(date(2026, 11, 5), 200, prazos.UTEIS, _cal(), dobro=True)
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar_da_ciencia(date(2026, 11, 10), 366, prazos.CORRIDOS, _cal())


def test_perto_do_fim_do_calendario_vira_fora_da_conta():
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(date(9999, 12, 28), 5, prazos.UTEIS, _cal())
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar_da_ciencia(date(9999, 12, 31), 2, prazos.CORRIDOS, _cal())
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.ciencia_tacita(date(9999, 12, 31), 5)
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.data_de_trabalho(date(1, 1, 5), 10, _cal())


MAS_DATAS = [datetime(2026, 11, 5, 10, 30), "2026-11-05", None, 20261105]


@pytest.mark.parametrize("ruim", MAS_DATAS)
def test_so_data_simples_entra_na_conta(ruim):
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(ruim, 5, prazos.UTEIS, _cal())
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar_da_ciencia(ruim, 5, prazos.UTEIS, _cal())
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.ciencia_tacita(ruim, 10)
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.data_de_trabalho(ruim, 1, _cal())


@pytest.mark.parametrize("ruim", [datetime(2026, 11, 9, 8, 0), "2026-11-09", 20261109])
def test_inicio_informado_tambem_so_data_simples(ruim):
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(None, 5, prazos.UTEIS, _cal(), inicio=ruim)
    with pytest.raises(prazos.PrazoForaDaConta):
        prazos.contar(date(2026, 11, 5), 5, prazos.UTEIS, _cal(), inicio=ruim)


def test_ciencia_tacita_e_data_de_trabalho_recusam_numero_ruim():
    for ruim in (-1, 1.5, "10", None, True):
        with pytest.raises(prazos.PrazoForaDaConta):
            prazos.ciencia_tacita(date(2026, 11, 4), ruim)
    for ruim in (-1, 1.5, "1", None, True):
        with pytest.raises(prazos.PrazoForaDaConta):
            prazos.data_de_trabalho(date(2026, 11, 23), ruim, _cal())
    assert prazos.ciencia_tacita(date(2026, 11, 4), 0) == date(2026, 11, 4)


def test_ciencia_mostra_a_data_digitada_e_o_deslocamento():
    c = prazos.contar_da_ciencia(date(2026, 12, 28), 5, prazos.UTEIS, _cal_fim())
    texto = "\n".join(c.passos)
    assert c.passos[0] == "Ciência no PJe: seg 28/12/2026"
    assert "Início informado" not in texto
    assert any("28/12/2026" in p and "07/01/2027" in p and "recesso" in p for p in c.passos)
    assert c.fatal == date(2027, 1, 27) and "qua 27/01/2027: dia 5" in c.passos


def test_ciencia_em_dia_util_nao_inventa_deslocamento_nem_inicio_informado():
    c = prazos.contar_da_ciencia(date(2026, 11, 10), 2, prazos.UTEIS, _cal())
    texto = "\n".join(c.passos)
    assert c.passos[0] == "Ciência no PJe: ter 10/11/2026"
    assert "Início informado" not in texto
    assert "vale no dia útil seguinte" not in texto


def test_ciencia_em_sabado_diz_que_vale_na_segunda():
    c = prazos.contar_da_ciencia(date(2026, 11, 14), 1, prazos.UTEIS, _cal())
    assert c.passos[0] == "Ciência no PJe: sáb 14/11/2026"
    assert any("sáb 14/11/2026" in p and "fim de semana" in p and "seg 16/11/2026" in p
               for p in c.passos)


def test_inicio_informado_continua_dizendo_que_foi_informado():
    c = prazos.contar(None, 2, prazos.UTEIS, _cal(), inicio=date(2026, 11, 14))
    assert any(p.startswith("Início informado: sáb 14/11/2026") for p in c.passos)
    assert any("sáb 14/11/2026" in p and "não conta" in p for p in c.passos)


def test_disponibilizacao_no_recesso_explica_o_recesso_e_a_suspensao():
    c = prazos.contar(date(2027, 1, 5), 1, prazos.UTEIS, _cal_fim())
    i_pub = next(i for i, p in enumerate(c.passos) if p.startswith("Publicação:"))
    i_dia1 = next(i for i, p in enumerate(c.passos) if ": dia 1" in p)
    recesso = [i for i, p in enumerate(c.passos) if "06/01/2027" in p and "recesso" in p]
    suspensao = [i for i, p in enumerate(c.passos)
                 if "08/01/2027" in p and "20/01/2027" in p and "suspensão" in p]
    assert recesso and recesso[0] < i_pub
    assert suspensao and i_pub < suspensao[0] < i_dia1


def test_publicacao_so_com_fim_de_semana_nao_enche_o_passo_a_passo():
    c = prazos.contar(date(2026, 11, 5), 1, prazos.UTEIS, _cal())
    assert not any("fim de semana" in p and "sem expediente" in p for p in c.passos)


def test_corridos_dizem_suspensao_e_nao_fim_de_semana():
    c = prazos.contar(date(2026, 12, 16), 3, prazos.CORRIDOS, _cal_fim())
    assert c.fatal == date(2027, 1, 21)
    assert "sáb 09/01/2027: não conta (suspensão)" in c.passos
    assert "dom 10/01/2027: não conta (suspensão)" in c.passos
    assert not any("09/01/2027" in p and "fim de semana" in p for p in c.passos)


def test_por_que_nao_conta_em_corridos_olha_a_suspensao_antes():
    cal = _cal_fim()
    assert cal.por_que_nao_conta(date(2027, 1, 9)) == "fim de semana"
    assert cal.por_que_nao_conta(date(2027, 1, 9), suspensao_primeiro=True) == "suspensão"


def test_corridos_no_sabado_que_e_feriado_mantem_a_data_e_diz_o_feriado():
    cal = _cal(sem={date(2026, 11, 7): "Dia do Teste"})
    c = prazos.contar(date(2026, 11, 5), 1, prazos.CORRIDOS, cal)
    assert c.fatal == date(2026, 11, 7)
    assert any("Dia do Teste" in a and "feriado" in a for a in c.avisos)
    assert any("sábado" in a for a in c.avisos)


def test_corridos_em_dia_reduzido_mantem_a_data_e_ressalva():
    c = prazos.contar(date(2026, 11, 5), 3, prazos.CORRIDOS, _cal(red=[date(2026, 11, 9)]))
    assert c.fatal == date(2026, 11, 9)
    assert any("expediente reduzido" in a for a in c.avisos)


def test_corridos_em_dia_comum_nao_ganha_ressalva():
    c = prazos.contar(date(2026, 11, 5), 3, prazos.CORRIDOS, _cal())
    assert not any(a.startswith("Vence") for a in c.avisos)


@pytest.mark.parametrize("rito", ["Eleitoral", "ELEITORAL", " eleitoral ", "Rito Eleitoral"])
def test_rito_eleitoral_em_qualquer_caixa(rito):
    assert "eleitoral" in prazos.fora_da_conta("00012345620248110041", "TJMT", rito)


def test_tribunal_com_acento_e_caixa():
    assert "eleitoral" in prazos.fora_da_conta("00012345620246110001", "Tre-MT")
    assert "superiores" in prazos.fora_da_conta("00012345620248110041", "stj")


@pytest.mark.parametrize("numero", ["", None, "123", "abc", "0001234-56.2024.8.11.004",
                                    "000123456202481100410", 12345])
def test_numero_ilegivel_vai_para_a_definir(numero):
    assert prazos.fora_da_conta(numero, "TJMT") != ""


@pytest.mark.parametrize("numero", ["00012345620248110041", "0001234-56.2024.8.11.0041",
                                    " 0001234-56.2024.8.11.0041 "])
def test_numero_valido_continua_dentro_da_conta(numero):
    assert prazos.fora_da_conta(numero, "TJMT", "") == ""
