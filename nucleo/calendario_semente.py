"""O calendário forense que vem pronto (spec, seção 3.2). Só datas marcadas CONFIRMADO nas
seções A, D e E de docs/fontes/2026-11-01-prazos-fontes.md, com a fonte em cada linha. O
Daniel confere esta tabela antes de publicar (spec, seção 8).

Fica de fora, de propósito: as datas móveis de 2027 (nenhum ato oficial localizado, E10) e
os anexos por comarca. Vale o aviso de «ano sem calendário»."""

from __future__ import annotations

from datetime import date, timedelta

_FONTE_D1 = "Lei 662/1949, redação da Lei 10.607/2002 (D1)"
_FONTE_D2 = "Lei 6.802/1980, art. 1º (D2)"
_FONTE_D3 = "Lei 14.759/2023 (D3)"
_NACIONAIS = (
    ("01-01", _FONTE_D1), ("04-21", _FONTE_D1), ("05-01", _FONTE_D1), ("09-07", _FONTE_D1),
    ("10-12", _FONTE_D2), ("11-02", _FONTE_D1), ("11-15", _FONTE_D1), ("11-20", _FONTE_D3),
    ("12-25", _FONTE_D1),
)
_ANOS_NACIONAIS = (2026, 2027)
_FONTE_A5 = "CPC, art. 220 (A5)"
_FONTE_A6 = "Res. CNJ 244/2016, arts. 1º a 3º (A6)"
# (primeiro dia, último dia) de cada ciclo, dentro dos anos cadastrados.
_SUSPENSOES = (("2026-01-01", "2026-01-20"), ("2026-12-20", "2027-01-20"),
               ("2027-12-20", "2028-01-20"))
_RECESSOS = (("2026-01-01", "2026-01-06"), ("2026-12-20", "2027-01-06"),
             ("2027-12-20", "2028-01-06"))

_NOMES = {"2026-04-03": "Sexta-feira da Paixão (D5)", "2026-06-04": "Corpus Christi (D6)"}
_FONTE_TJMT = "Portaria TJMT/PRES 1915/2025, Anexo I (E1)"
_FONTE_TJMG = "Portaria Conjunta TJMG 1798/PR/2026, art. 1º (E5)"
_FONTE_TJMG_BH = "Portaria Conjunta TJMG 1798/PR/2026, art. 2º (E5)"
_FONTE_TRF1 = "Portaria Presi TRF1 5/2026, pelo aviso e notícia oficiais (E7)"
_FONTE_TRF6 = "Portaria Presi TRF6 1/2026, art. 1º (E9)"
# tribunal, comarca, fonte, dias sem expediente, dias de expediente reduzido
# 04/06 (Corpus Christi) sai do TRF1 e do TRF6 de propósito: o TRF1 o trata como ponto
# facultativo e o TRF6 como feriado municipal de BH (D6), e os textos não foram lidos por
# inteiro. Na dúvida, a data mais cedo. O 05/06 de ambos fica. No TJMT e no TJMG o 04/06 fica.
_TRIBUNAIS = (
    ("TJMT", "", _FONTE_TJMT,
     ("01-01", "02-16", "02-17", "04-02", "04-03", "04-20", "04-21", "05-01", "06-04", "06-05",
      "08-10", "08-11", "09-07", "10-12", "11-02", "11-15", "11-20", "12-07", "12-08",
      "12-24", "12-25", "12-31"),
     (("02-18", "expediente a partir das 13h"),)),
    ("TJMG", "", _FONTE_TJMG,
     ("01-01", "01-02", "01-03", "01-04", "01-05", "01-06", "02-16", "02-17", "04-01", "04-02",
      "04-03", "04-20", "04-21", "05-01", "09-07", "10-12", "10-30", "11-02", "11-15", "11-20",
      "12-07", "12-08", "12-20", "12-21", "12-22", "12-23", "12-24", "12-25", "12-26", "12-27",
      "12-28", "12-29", "12-30", "12-31"),
     (("02-18", "o ato lido não diz o horário; tratado como reduzido, o lado seguro: confira"),)),
    ("TJMG", "Belo Horizonte", _FONTE_TJMG_BH, ("06-04", "06-05", "08-15"), ()),
    ("TRF1", "", _FONTE_TRF1,
     ("01-01", "02-16", "02-17", "04-01", "04-02", "04-03", "04-20", "04-21", "05-01",
      "06-05", "08-10", "08-11", "09-07", "10-12", "10-30", "11-01", "11-02", "11-15", "11-20",
      "12-07", "12-08", "12-25"),
     (("02-18", "expediente até as 14h"),)),
    ("TRF6", "", _FONTE_TRF6,
     ("01-01", "01-02", "01-03", "01-04", "01-05", "01-06", "02-16", "02-17", "04-01", "04-02",
      "04-03", "04-20", "04-21", "05-01", "06-05", "08-10", "08-11", "09-07", "10-12",
      "10-30", "11-02", "11-20", "12-07", "12-08", "12-20", "12-21", "12-22", "12-23", "12-24",
      "12-25", "12-26", "12-27", "12-28", "12-29", "12-30", "12-31"),
     (("02-18", "expediente até as 14h"),)),
)
# Atos do ano que mudaram o calendário geral (E2). O Anexo I do TJMT (E1) lista 28/10, mas a
# notícia oficial amarra o plantão ao ponto facultativo do Dia do Servidor em 30/10 (E2), o que
# confirma a troca de E3. Na dúvida, a data mais cedo: com as duas datas na tabela um prazo
# que passasse por elas andaria um dia útil a mais. Fica só o 30/10; o 28/10 não entra.
_ATOS_AVULSOS = (
    ("2026-06-29", "TJMT", "Sem expediente", "Portaria TJMT-PRES 901/2026, notícia oficial (E2)"),
    ("2026-10-30", "TJMT", "Ponto facultativo do Dia do Servidor, com plantão de 30/10 a 02/11",
     "Notícia oficial do TJMT, plantão de 30/10 a 02/11/2026 (E2); troca do 28/10 em E3"),
)


def _dias(de: str, ate: str):
    d, fim = date.fromisoformat(de), date.fromisoformat(ate)
    while d <= fim:
        yield d.isoformat()
        d += timedelta(days=1)


def linhas() -> list[dict]:
    def linha(data, tribunal, comarca, tipo, descricao, fonte, origem):
        return {"data": data, "tribunal": tribunal, "comarca": comarca, "tipo": tipo,
                "descricao": descricao, "fonte": fonte, "origem": origem}

    saida = []
    for ano in _ANOS_NACIONAIS:
        for dia, fonte in _NACIONAIS:
            saida.append(linha(f"{ano}-{dia}", "", "", "sem_expediente", "Feriado nacional",
                               fonte, "lei"))
    for de, ate in _SUSPENSOES:
        for data in _dias(de, ate):
            saida.append(linha(data, "", "", "suspensao", "Prazos suspensos no fim de ano",
                               _FONTE_A5, "lei"))
    for de, ate in _RECESSOS:
        for data in _dias(de, ate):
            saida.append(linha(data, "", "", "sem_expediente", "Recesso de fim de ano",
                               _FONTE_A6, "ato"))
    for tribunal, comarca, fonte, sem, reduzidos in _TRIBUNAIS:
        for dia in sem:
            data = f"2026-{dia}"
            nome = _NOMES.get(data, "Feriado ou ponto facultativo do calendário")
            saida.append(linha(data, tribunal, comarca, "sem_expediente", nome, fonte, "ato"))
        for dia, nota in reduzidos:
            saida.append(linha(f"2026-{dia}", tribunal, comarca, "reduzido",
                               f"Expediente reduzido: {nota}", fonte, "ato"))
    for data, tribunal, descricao, fonte in _ATOS_AVULSOS:
        saida.append(linha(data, tribunal, "", "sem_expediente", descricao, fonte, "ato"))
    return saida


def _texto(valor: str) -> str:
    return "'" + valor.replace("'", "''") + "'"


# Depois de publicada, a semente fica congelada: o banco de quem já atualizou não a recebe de
# novo. Qualquer mudança nos dias vem numa migração nova (nunca edite a 9).
SQL_SEMENTE = "\n".join(
    "INSERT OR IGNORE INTO dia_forense (data, tribunal, comarca, tipo, descricao, fonte, origem) "
    f"VALUES ({_texto(l['data'])}, {_texto(l['tribunal'])}, {_texto(l['comarca'])}, "
    f"{_texto(l['tipo'])}, {_texto(l['descricao'])}, {_texto(l['fonte'])}, {_texto(l['origem'])});"
    for l in linhas())
