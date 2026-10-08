"""Banco de demonstração com dados FICTÍCIOS (nomes, números e valores
inventados), para ver, revisar e apresentar o painel sem expor clientes reais.

Uso:
    python scripts/banco_demo.py dados/demo.db
    scripts/painel_demo.sh          # painel em http://127.0.0.1:5055
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nucleo import banco, config, eventos, ponte, quadro  # noqa: E402

ADVOGADO = ("ADVOGADA DEMONSTRAÇÃO", "MT0012345A")
CLIENTES = [
    ("MARIA APARECIDA SOUZA", "BANCO ALFA S.A."),
    ("JOÃO PEDRO LIMA", "CONSTRUTORA HORIZONTE LTDA"),
    ("ANA CAROLINA REIS", "SEGURADORA PRUDÊNCIA S.A."),
    ("CARLOS EDUARDO NUNES", "MUNICÍPIO DE VÁRZEA ALTA"),
    ("FERNANDA COSTA MELO", "TELEFONIA CONECTA S.A."),
    ("RICARDO ALVES PINTO", "ESTADO DE MATO GROSSO"),
    ("PATRÍCIA GOMES DIAS", "VAREJO BOA COMPRA LTDA"),
    ("LUCAS MARTINS ROCHA", "COOPERATIVA AGRO NORTE"),
    ("JULIANA FERREIRA BRITO", "PLANO DE SAÚDE VIDA PLENA S.A."),
    ("ROBERTO SILVA CAMPOS", "TRANSPORTADORA RODOVIA LTDA"),
    ("CAMILA RIBEIRO SANTOS", "ENERGIA DO CERRADO S.A."),
    ("EDUARDO LOPES TEIXEIRA", "BANCO ALFA S.A."),
]
DIAS_ULTIMO = [0, 1, 2, 4, 6, 9, 15, 30, 45, 95, 120, 210]
VALORES = [85000, 120000, 45000, 300000, 15000, 62000, 980000, 210000,
           33000, 150000, 72000, 1250000]
TEXTOS = ["Juntada de Petição de manifestação", "Decisão proferida",
          "Audiência de conciliação designada", "Expedição de alvará",
          "Conclusos para sentença", "Ato ordinatório praticado"]
ORGAOS = ["1ª Vara Cível de Cuiabá", "3ª Vara Cível de Cuiabá",
          "Juizado Especial Cível de Cuiabá", "2ª Vara da Fazenda Pública de Cuiabá"]
ARQUIVADOS = ["HELENA PRADO", "OTÁVIO BRANDÃO", "SÍLVIA MATOS"]
VARREDURA = "T06:00:00-04:00"
# Sobe quando a demonstração ganha dado novo: o painel_demo.sh recria a demo antiga.
DEMO_VERSAO = 5
CHAVE_DEMO = "demo_versao"
CHAVE_GERADA_EM = "demo_gerada_em"
VALIDADE_DEMO = timedelta(hours=1)  # além disso (ou em outro dia) o painel_demo.sh regenera
PECAS_PADRAO = str(Path(__file__).resolve().parents[1] / "dados" / "demo-pecas")
# Clientes fictícios dos processos do TRF1 que passam pelo Lex (o DJEN não traz partes).
# Nome legível de cada squad (o `name` do squad.yaml que o Mac manda ao painel).
NOMES_SQUAD = {
    "contestacao-civel": "Contestação cível",
    "apelacao-civel": "Apelação cível",
    "embargos-de-declaracao": "Embargos de declaração",
    "replica-a-contestacao": "Réplica à contestação",
    "manifestacao-sobre-laudo": "Manifestação sobre o laudo pericial",
}
CLIENTES_TRF1 = {
    13: ("VALDEMAR TORRES NETO", "UNIÃO FEDERAL"),
    14: ("SÔNIA REGINA ALMEIDA", "UNIÃO FEDERAL"),
    15: ("MARCOS VINÍCIUS DUARTE", "UNIÃO FEDERAL"),
    16: ("CLÁUDIA REGINA FONSECA", "UNIÃO FEDERAL"),
}


def _tjmt(n: int) -> str:
    return f"{n:07d}0220248110041"


def _trf1(n: int) -> str:
    return f"{n:07d}0220244013600"


def _reais(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _partes(cliente: str, contraria: str) -> list[dict]:
    return [
        {"polo": "Polo Ativo", "nomes": [cliente], "integrantes": [
            {"nome": cliente, "intimacao_pendente": False,
             "advogados": [{"nome": ADVOGADO[0], "oab": ADVOGADO[1]}]}]},
        {"polo": "Polo Passivo", "nomes": [contraria], "integrantes": [
            {"nome": contraria, "intimacao_pendente": False,
             "advogados": [{"nome": "ADVOGADO FICTÍCIO", "oab": "MT0099999A"}]}]},
    ]


# Processo do TRF1 "a conferir" (a OAB não veio na publicação): íntegra longa e partes
# publicadas, para a leitura no painel e o "Este processo é seu?". Tudo fictício.
TRF1_A_CONFERIR = 24
PARTES_TRF1_A_CONFERIR = [
    {"nome": "ROBERTO FICTÍCIO DA SILVA", "polo": "ativo"},
    {"nome": "LUZIA FICTÍCIA DA SILVA", "polo": "ativo"},
    {"nome": "AUTARQUIA FEDERAL FICTÍCIA", "polo": "passivo"},
]
INTEGRA_TRF1 = (
    "<p><b>PODER JUDICIÁRIO</b><br>JUSTIÇA FEDERAL DE PRIMEIRO GRAU<br>"
    "Seção Judiciária Fictícia · 1ª Vara Federal Cível</p>"
    "<p>PROCEDIMENTO COMUM CÍVEL. AUTORES: ROBERTO FICTÍCIO DA SILVA e LUZIA FICTÍCIA "
    "DA SILVA. RÉ: AUTARQUIA FEDERAL FICTÍCIA.</p>"
    "<p><b>DECISÃO</b></p>"
    "<p>Trata-se de ação de procedimento comum em que a parte autora pede o "
    "restabelecimento de benefício cessado administrativamente, com o pagamento das "
    "parcelas vencidas desde a cessação, acrescidas de correção monetária e juros.</p>"
    "<p>Alega, em síntese, que permanece incapacitada para o trabalho e que a perícia "
    "administrativa desconsiderou os laudos particulares juntados com o requerimento. "
    "Pede tutela de urgência para o restabelecimento imediato.</p>"
    "<p>É o relatório. Decido.</p>"
    "<p>Os documentos que acompanham a inicial não bastam, neste momento, para afastar a "
    "presunção de legitimidade do ato administrativo. A questão depende de prova técnica, "
    "razão pela qual <b>indefiro</b>, por ora, a tutela de urgência, sem prejuízo de nova "
    "análise após a perícia.</p>"
    "<p>Determino a realização de perícia médica. Nomeio perito o profissional cadastrado "
    "no sistema da Seção, que deverá responder aos quesitos do juízo e aos que as partes "
    "apresentarem.</p>"
    "<ul><li>Intimem-se as partes para, no prazo de 15 (quinze) dias, apresentar quesitos "
    "e indicar assistentes técnicos;</li>"
    "<li>Cite-se a ré para contestar no prazo legal;</li>"
    "<li>Com o laudo, vista às partes por 15 (quinze) dias.</li></ul>"
    "<p>Intimem-se. Cumpra-se.</p>"
    "<p>Documento assinado eletronicamente pelo(a) Juiz(a) Federal Fictício(a).</p>"
)


def _inserir_processo(conn, **campos) -> None:
    colunas = ", ".join(campos)
    marcas = ", ".join(f":{c}" for c in campos)
    conn.execute(f"INSERT INTO processo ({colunas}) VALUES ({marcas})", campos)


def _inserir_publicacao(conn, id_, numero, tribunal, dia: date, orgao, texto, oab) -> None:
    conn.execute(
        "INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, tipo, orgao, "
        "classe, texto, link, oab_confirmada) VALUES (?, ?, ?, ?, 'Intimação', ?, "
        "'PROCEDIMENTO COMUM CÍVEL', ?, '', ?)",
        (id_, numero, tribunal, dia.isoformat(), orgao, texto, oab))


# --- o Lex na demonstração ------------------------------------------------------------

def _pdf_minimo(texto: str) -> bytes:
    """PDF de uma página, escrito à mão (sem biblioteca), com o texto dado em
    Helvetica/WinAnsi. Os deslocamentos da tabela xref são calculados, então abre em
    qualquer leitor. Determinístico."""
    def esc(t: str) -> bytes:
        return (t.encode("cp1252").replace(b"\\", b"\\\\")
                .replace(b"(", b"\\(").replace(b")", b"\\)"))

    fluxo = (b"BT /F1 20 Tf 72 740 Td 24 TL (" + esc(texto) + b") Tj T* /F1 11 Tf ("
             + esc("Documento fictício do banco de demonstração; nenhum dado real.")
             + b") Tj ET")
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(fluxo)).encode() + b" >>\nstream\n" + fluxo + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    saida = bytearray(b"%PDF-1.4\n")
    deslocamentos = []
    for i, corpo in enumerate(objetos, start=1):
        deslocamentos.append(len(saida))
        saida += f"{i} 0 obj\n".encode() + corpo + b"\nendobj\n"
    xref = len(saida)
    saida += f"xref\n0 {len(objetos) + 1}\n".encode() + b"0000000000 65535 f \n"
    for d in deslocamentos:
        saida += f"{d:010d} 00000 n \n".encode()
    saida += (f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n"
              "%%EOF\n").encode()
    return bytes(saida)


class _Relogio:
    """Troca `eventos.agora` por um relógio simulado enquanto a demonstração é montada:
    todos os momentos gravados (cartões, eventos, tarefas) saem fictícios e iguais a
    cada geração do mesmo dia. Sempre restaura o relógio de verdade."""

    def __init__(self, inicio: datetime):
        self.t = inicio

    def __enter__(self):
        self._original = eventos.agora
        eventos.agora = lambda: self.t.isoformat(timespec="seconds")
        return self

    def __exit__(self, *exc):
        eventos.agora = self._original


def _quando(dia: date, hora: int, minuto: int = 0) -> datetime:
    return datetime.combine(dia, time(hora, minuto), tzinfo=eventos.FUSO)


def _publicacao(conn, numero: str, k: int = 0) -> int:
    return conn.execute("SELECT id FROM publicacao WHERE numero = ? ORDER BY id LIMIT 1 OFFSET ?",
                        (numero, k)).fetchone()["id"]


def _gravar_pacote(pecas_dir: str, demanda_id: int, n: int, citacoes: list,
                   pendencias: list, gate_status: str, nota: str) -> str:
    pasta = os.path.join(pecas_dir, str(demanda_id), str(n))
    shutil.rmtree(pasta, ignore_errors=True)
    os.makedirs(pasta)
    with open(os.path.join(pasta, "peca.pdf"), "wb") as f:
        f.write(_pdf_minimo("Peça de demonstração — caso fictício"))
    gate = {"gate_status": gate_status, "citations": citacoes,
            "pendencias_do_profissional": pendencias}
    with open(os.path.join(pasta, "citation-gate.json"), "w", encoding="utf-8") as f:
        json.dump(gate, f, ensure_ascii=False, indent=2)
    with open(os.path.join(pasta, "nota-ao-revisor.md"), "w", encoding="utf-8") as f:
        f.write(nota)
    return pasta


_PLANALTO = "https://www.planalto.gov.br/ccivil_03/"
_CPC = _PLANALTO + "_ato2015-2018/2015/lei/l13105.htm"
CITACOES_REPLICA = [
    {"title": "Código de Processo Civil, art. 350", "status": "verificada", "source_url": _CPC},
    {"title": "Código de Defesa do Consumidor, art. 6º, VIII", "status": "verificada",
     "source_url": _PLANALTO + "leis/l8078compilado.htm"},
    {"title": "Código de Processo Civil, art. 373, § 1º", "status": "verificada",
     "source_url": _CPC},
]
PENDENCIAS_REPLICA = [
    {"marcador": "[CONFERIR DATA DA INTIMAÇÃO]", "onde": "Item II, 1º parágrafo",
     "diligencia": "Conferir nos autos a data em que a parte foi intimada da contestação."},
    {"marcador": "[ANEXAR COMPROVANTE]", "onde": "Item IV",
     "diligencia": "Juntar o comprovante de pagamento citado na réplica."},
]
NOTA_REPLICA = """# Nota ao revisor (demonstração)

Caso fictício. A réplica rebate a preliminar e a impugnação ao valor da causa e pede a
inversão do ônus da prova.

- Confira a data da intimação (marcador no item II).
- Falta anexar o comprovante de pagamento (item IV).
- As 25 citações foram verificadas pelo Citation Gate, sem falhas.
"""
CITACOES_EMBARGOS = [
    {"title": "Código de Processo Civil, art. 1.022", "status": "verificada", "source_url": _CPC},
    {"title": "Código de Processo Civil, art. 1.023", "status": "verificada", "source_url": _CPC},
    {"title": "Súmula 999 do TRF1", "status": "não encontrada", "source_url": ""},
]
NOTA_EMBARGOS = """# Nota ao revisor (demonstração)

Caso fictício. Embargos de declaração por omissão quanto ao pedido sucessivo.

- Uma citação não foi encontrada: confira a súmula do TRF1 antes de protocolar.
- As demais citações foram verificadas.
"""
NOTA_SIMPLES = "# Nota ao revisor (demonstração)\n\nCaso fictício; peça já protocolada.\n"


def _levar_ate_autos(conn, relogio, numero: str, pub: int, chegada: datetime) -> int:
    """Faz o caminho do cartão até "Autos baixados": chegada, triagem e autos."""
    relogio.t = chegada
    demanda_id, _ = quadro.adicionar_item(conn, numero, "publicacao", str(pub),
                                          chegada.date().isoformat())
    relogio.t = chegada + timedelta(minutes=25)
    quadro.mover(conn, demanda_id, "acao", "advogado")
    relogio.t = chegada + timedelta(minutes=47)
    quadro.marcar_autos(conn, demanda_id, "ok", "")
    quadro.mover(conn, demanda_id, "autos", "trabalhador")
    return demanda_id


def _mandar_ao_lex(conn, relogio, demanda_id: int, quando: datetime,
                   orientacao: str = "") -> None:
    relogio.t = quando
    quadro.mover(conn, demanda_id, "lex", "advogado", orientacao=orientacao)


def _reservar(conn, relogio, demanda_id: int, quando: datetime) -> int:
    relogio.t = quando
    pacote = ponte.proximo(conn, quando)
    if not (pacote and pacote["demanda"] == demanda_id):
        raise RuntimeError(f"a demonstração esperava reservar a demanda {demanda_id}")
    return pacote["n"]


def _bater(conn, relogio, demanda_id: int, n: int, quando: datetime, squad: str,
           etapa: str) -> None:
    relogio.t = quando
    ponte.batida(conn, demanda_id, n, squad=squad, etapa=etapa,
                 squad_nome=NOMES_SQUAD.get(squad, ""), agora=quando)


def _entregar(conn, relogio, pecas_dir: str, demanda_id: int, n: int, quando: datetime, *,
              squad: str, run_id: str, citacoes: list, pendencias: list, nota: str,
              total: int, falhas: int, gate_status: str) -> None:
    relogio.t = quando
    pasta = _gravar_pacote(pecas_dir, demanda_id, n, citacoes, pendencias, gate_status, nota)
    ponte.concluir(conn, demanda_id, n, squad=squad, run_id=run_id, gate_status=gate_status,
                   citacoes_total=total, citacoes_falhas=falhas, pasta=pasta, agora=quando,
                   squad_nome=NOMES_SQUAD.get(squad, ""))


def _protocolada(conn, relogio, pecas_dir: str, numero: str, pub: int, chegada: datetime,
                 squad: str, run_id: str, hora_lex: int, minutos_lex: int,
                 protocolo: timedelta) -> None:
    demanda_id = _levar_ate_autos(conn, relogio, numero, pub, chegada)
    inicio = _quando(chegada.date(), hora_lex, 5)
    _mandar_ao_lex(conn, relogio, demanda_id, inicio - timedelta(minutes=4))
    n = _reservar(conn, relogio, demanda_id, inicio)
    _bater(conn, relogio, demanda_id, n, inicio + timedelta(minutes=minutos_lex // 2),
           squad, "revisão final (13/13)")
    fim = inicio + timedelta(minutes=minutos_lex)
    _entregar(conn, relogio, pecas_dir, demanda_id, n, fim, squad=squad, run_id=run_id,
              citacoes=CITACOES_REPLICA, pendencias=[], nota=NOTA_SIMPLES, total=21,
              falhas=0, gate_status="aprovado")
    relogio.t = fim + protocolo
    quadro.mover(conn, demanda_id, "protocolado", "advogado")


def _demonstrar_o_lex(conn, relogio, hoje: date, agora: datetime, pecas_dir: str) -> None:
    """Cartões do Lex em todos os estados, pelas mesmas funções que o painel e a ponte
    usam (histórico, cronômetro e Resultados saem do caminho de verdade)."""
    for k, (cliente, contraria) in CLIENTES_TRF1.items():
        conn.execute("UPDATE processo SET cliente = ?, parte_contraria = ?, polo_cliente = "
                     "'Polo Ativo', orgao_julgador = '1ª Vara Federal Cível da SJMT' "
                     "WHERE numero = ?", (cliente, contraria, _trf1(k)))

    # Duas peças já protocoladas, de meses anteriores (alimentam os Resultados).
    trf16 = _trf1(16)
    _protocolada(conn, relogio, pecas_dir, trf16, _publicacao(conn, trf16, 0),
                 _quando(hoje - timedelta(days=76), 6, 4), "contestacao-civel",
                 "demo-run-0003", 10, 58, timedelta(days=1, hours=5))
    _protocolada(conn, relogio, pecas_dir, trf16, _publicacao(conn, trf16, 1),
                 _quando(hoje - timedelta(days=41), 6, 10), "apelacao-civel",
                 "demo-run-0004", 8, 44, timedelta(days=2, hours=3))

    # "Sua revisão" com citação a conferir: o Lex bateu no limite do plano e retomou.
    trf14 = _trf1(14)
    dia = hoje - timedelta(days=2)
    b = _levar_ate_autos(conn, relogio, trf14, _publicacao(conn, trf14, 0), _quando(dia, 6, 4))
    _mandar_ao_lex(conn, relogio, b, _quando(dia, 9, 10),
                   "Priorizar a omissão quanto ao pedido sucessivo.")
    squad_b = "embargos-de-declaracao"
    n = _reservar(conn, relogio, b, _quando(dia, 9, 15))
    _bater(conn, relogio, b, n, _quando(dia, 9, 22), squad_b, "pesquisa jurídica (8/13)")
    relogio.t = _quando(dia, 9, 31)
    ponte.falhar(conn, b, n, "limite", "pausado: limite do plano", relogio.t)  # frase fixa do Mac
    n = _reservar(conn, relogio, b, _quando(dia, 10, 4))
    _bater(conn, relogio, b, n, _quando(dia, 10, 20), squad_b, "redação (11/13)")
    _entregar(conn, relogio, pecas_dir, b, n, _quando(dia, 10, 42), squad=squad_b,
              run_id="demo-run-0002", citacoes=CITACOES_EMBARGOS, pendencias=[],
              nota=NOTA_EMBARGOS, total=18, falhas=1, gate_status="a conferir")

    # "Sua revisão" com peça limpa: 25 citações, 0 falhas, 47 minutos de Lex.
    tjmt7 = _tjmt(7)
    dia = hoje - timedelta(days=1)
    a = _levar_ate_autos(conn, relogio, tjmt7, _publicacao(conn, tjmt7), _quando(dia, 6, 5))
    _mandar_ao_lex(conn, relogio, a, _quando(dia, 7, 28))
    squad_a = "replica-a-contestacao"
    n = _reservar(conn, relogio, a, _quando(dia, 7, 32))
    _bater(conn, relogio, a, n, _quando(dia, 7, 55), squad_a, "pesquisa jurídica (8/13)")
    _bater(conn, relogio, a, n, _quando(dia, 8, 10), squad_a, "verificação de citações (12/13)")
    _entregar(conn, relogio, pecas_dir, a, n, _quando(dia, 8, 19), squad=squad_a,
              run_id="demo-run-0001", citacoes=CITACOES_REPLICA,
              pendencias=PENDENCIAS_REPLICA, nota=NOTA_REPLICA, total=25, falhas=0,
              gate_status="aprovado")

    # "Lex minutando": um trabalhando agora (etapa viva) e um na fila com orientação.
    tjmt8 = _tjmt(8)
    t = _levar_ate_autos(conn, relogio, tjmt8, _publicacao(conn, tjmt8),
                         _quando(hoje - timedelta(days=1), 15, 20))
    _mandar_ao_lex(conn, relogio, t, agora - timedelta(minutes=24))
    n = _reservar(conn, relogio, t, agora - timedelta(minutes=21))
    _bater(conn, relogio, t, n, agora - timedelta(minutes=1), "manifestacao-sobre-laudo",
           "pesquisa jurídica (8/13)")
    trf15 = _trf1(15)
    f = _levar_ate_autos(conn, relogio, trf15, _publicacao(conn, trf15, 0),
                         _quando(hoje - timedelta(days=1), 16, 40))
    _mandar_ao_lex(conn, relogio, f, agora - timedelta(minutes=6),
                   "Tom firme, sem proposta de acordo. Pedir a juntada do contrato original.")

    config.gravar(conn, config.LEX_AUTOMATICO, "0")
    relogio.t = agora - timedelta(minutes=1)
    ponte.registrar_contato(conn)


def situacao(caminho: str) -> str:
    """Lê (sem migrar nem alterar) um banco: "atual" se é a demonstração desta versão,
    "antiga" se é uma demonstração de versão anterior e "outro" se não é demonstração
    (ou não existe). Só "antiga" pode ser recriada com segurança."""
    if not os.path.isfile(caminho):
        return "outro"
    try:
        conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
        try:
            demo = conn.execute("SELECT 1 FROM processo WHERE numero = ? AND cliente = ?",
                                (_tjmt(1), CLIENTES[0][0])).fetchone()
            if not demo:
                return "outro"
            try:
                versao = conn.execute("SELECT valor FROM config WHERE chave = ?",
                                      (CHAVE_DEMO,)).fetchone()
            except sqlite3.OperationalError:  # banco anterior à tabela `config`
                versao = None
            # Pasta das peças sumiu (apagada à mão): o painel mostraria PDFs que não existem.
            try:
                pastas = [r[0] for r in conn.execute(
                    "SELECT DISTINCT pasta FROM execucao_lex WHERE pasta IS NOT NULL "
                    "AND pasta != ''")]
            except sqlite3.OperationalError:
                pastas = []
            if any(not os.path.isdir(pasta) for pasta in pastas):
                return "antiga"
        finally:
            conn.close()
    except sqlite3.Error:
        return "outro"
    return "atual" if versao and versao[0] == str(DEMO_VERSAO) else "antiga"


def desatualizada(caminho: str, agora: datetime | None = None) -> bool:
    """True se a demonstração foi gerada em outro dia ou há mais de 1 h (ou não registra
    quando foi gerada): o "agora" dela já ficou para trás (contato do Mac, etapa viva).
    Só vale para um banco que `situacao` reconhece como demonstração."""
    agora = agora or datetime.now(eventos.FUSO)
    try:
        conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
        try:
            r = conn.execute("SELECT valor FROM config WHERE chave = ?",
                             (CHAVE_GERADA_EM,)).fetchone()
        finally:
            conn.close()
        gerada = datetime.fromisoformat(r[0]).astimezone(agora.tzinfo)
    except (sqlite3.Error, TypeError, ValueError):
        return True
    return gerada.date() != agora.date() or abs(agora - gerada) > VALIDADE_DEMO


def gerar(conn, hoje: date, pecas_dir: str | None = None,
          agora: datetime | None = None) -> None:
    """Preenche um banco vazio com 24 processos fictícios: 12 ativos no TJMT,
    4 no TRF1 só pelo DJEN, 3 arquivados, 3 de terceiros, 1 sem acesso e 1 do TRF1 a
    conferir (publicação longa, com as partes); mais o quadro
    com o Lex em todos os estados. Os arquivos das peças fictícias vão para `pecas_dir`
    (padrão: CONTROLADORIA_PECAS ou dados/demo-pecas). `agora` ancora o que está
    "acontecendo agora" (padrão: `hoje` às 10h): para o mesmo par, a saída é idêntica."""
    pecas_dir = pecas_dir or os.getenv("CONTROLADORIA_PECAS", "").strip() or PECAS_PADRAO
    agora = agora or _quando(hoje, 10)
    def momento(dias: int, hora: str = "1000") -> str:
        return (hoje - timedelta(days=dias)).strftime("%Y%m%d") + hora + "00"

    varredura = hoje.isoformat() + VARREDURA
    with conn:
        pub = 1
        for i, (cliente, contraria) in enumerate(CLIENTES):
            numero = _tjmt(i + 1)
            ultimo = momento(DIAS_ULTIMO[i])
            ultima_pub = None
            if i < 8:
                dia_pub = hoje - timedelta(days=DIAS_ULTIMO[i] + 1)
                _inserir_publicacao(conn, pub, numero, "TJMT", dia_pub, ORGAOS[i % 4],
                                    "Fica a parte intimada para manifestação no prazo legal.", 0)
                ultima_pub = dia_pub.strftime("%Y%m%d")
                pub += 1
            _inserir_processo(
                conn, numero=numero, tribunal="TJMT", instancia="1grau", classe="7",
                orgao_julgador=ORGAOS[i % 4], valor_causa=_reais(VALORES[i]),
                polo_cliente="Polo Ativo", cliente=cliente, parte_contraria=contraria,
                advogado_atua=1, arquivado=0, ultimo_andamento_data=ultimo[:8],
                ultimo_andamento_texto=TEXTOS[i % 6], ultima_publicacao_data=ultima_pub,
                partes_json=json.dumps(_partes(cliente, contraria), ensure_ascii=False),
                fontes='["djen"]', sincronizado_em=varredura)
            conn.executemany(
                "INSERT INTO andamento (numero, data_ordenavel, codigo, texto) VALUES (?, ?, ?, ?)",
                [(numero, ultimo, 85, TEXTOS[i % 6]),
                 (numero, momento(DIAS_ULTIMO[i] + 20, "0900"), 51, "Conclusos para decisão")])
            eventos.registrar(conn, "processo_sincronizado", numero,
                              {"instancia": "1grau", "andamentos": 2}, quando=varredura)

        for j in range(4):
            numero = _trf1(13 + j)
            dias = [hoje - timedelta(days=3 + 50 * j), hoje - timedelta(days=20 + 50 * j)]
            for dia in dias:
                _inserir_publicacao(conn, pub, numero, "TRF1", dia,
                                    "1ª Vara Federal Cível da SJMT",
                                    "Intime-se o patrono da parte autora.", 1)
                pub += 1
            _inserir_processo(
                conn, numero=numero, tribunal="TRF1", advogado_atua=1, arquivado=0,
                ultima_publicacao_data=max(dias).strftime("%Y%m%d"), fontes='["djen"]')

        for k, cliente in enumerate(ARQUIVADOS):
            numero = _tjmt(17 + k)
            arquivamento = momento(60 + 30 * k)
            _inserir_processo(
                conn, numero=numero, tribunal="TJMT", instancia="1grau", classe="7",
                orgao_julgador=ORGAOS[k], cliente=cliente, parte_contraria="BANCO ALFA S.A.",
                polo_cliente="Polo Ativo", advogado_atua=1, arquivado=1,
                data_arquivamento=arquivamento[:8], ultimo_andamento_data=arquivamento[:8],
                ultimo_andamento_texto="Arquivado Definitivamente",
                partes_json=json.dumps(_partes(cliente, "BANCO ALFA S.A."), ensure_ascii=False),
                fontes='["sistema:cache"]', sincronizado_em=varredura)
            conn.execute("INSERT INTO andamento (numero, data_ordenavel, codigo, texto) "
                         "VALUES (?, ?, 246, 'Arquivado Definitivamente')", (numero, arquivamento))

        for k in range(3):
            numero = _tjmt(20 + k)
            dia = momento(12 + 5 * k)
            _inserir_processo(
                conn, numero=numero, tribunal="TJMT", instancia="1grau", classe="7",
                orgao_julgador=ORGAOS[k], advogado_atua=0, arquivado=0,
                ultimo_andamento_data=dia[:8], ultimo_andamento_texto="Juntada de Certidão",
                fontes='["sistema:grupo"]', sincronizado_em=varredura)
            conn.execute("INSERT INTO andamento (numero, data_ordenavel, codigo, texto) "
                         "VALUES (?, ?, 581, 'Juntada de Certidão')", (numero, dia))

        _inserir_processo(
            conn, numero=_tjmt(23), tribunal="TJMT", arquivado=0, fontes='["sistema:cache"]',
            erro_sincronizacao="Processo em segredo de justiça", sincronizado_em=varredura)

        numero = _trf1(TRF1_A_CONFERIR)
        publicadas = ((hoje - timedelta(days=9), "<p>Cite-se a ré. Intime-se a parte autora "
                       "para emendar a inicial em 15 dias.</p>", PARTES_TRF1_A_CONFERIR[:2]),
                      (hoje - timedelta(days=2), INTEGRA_TRF1, PARTES_TRF1_A_CONFERIR))
        for dia, texto, partes in publicadas:
            conn.execute(
                "INSERT INTO publicacao (id, numero, tribunal, data_disponibilizacao, tipo, "
                "orgao, classe, texto, link, oab_confirmada, partes_json) VALUES (?, ?, 'TRF1', "
                "?, 'Intimação', '1ª Vara Federal Cível da SJMT', 'PROCEDIMENTO COMUM CÍVEL', ?, "
                "'https://exemplo.invalido/publicacao-demo', 0, ?)",
                (pub, numero, dia.isoformat(), texto, json.dumps(partes, ensure_ascii=False)))
            pub += 1
        _inserir_processo(
            conn, numero=numero, tribunal="TRF1", arquivado=0, fontes='["djen"]',
            ultima_publicacao_data=publicadas[-1][0].strftime("%Y%m%d"))

        with _Relogio(_quando(hoje, 6, 0)) as relogio:
            # Quadro fictício: cartões em todas as colunas, uma falha de autos e a
            # varredura das 06h. Nada fica na fila (a demo não tem trabalhador).
            manha = hoje.isoformat() + "T06:00:00-04:00"
            conn.execute(
                "INSERT INTO aviso (instancia, id, numero, tipo_comunicacao, data_disponibilizacao, "
                "orgao, visto_em, pendente) VALUES ('1grau', 'demo-1', ?, 'INT', ?, ?, ?, 1)",
                (_tjmt(1), (hoje - timedelta(days=1)).isoformat(), ORGAOS[0], manha))
            quadro.ligar(conn, hoje)
            cartoes = {r["numero"]: r["id"] for r in conn.execute("SELECT id, numero FROM demanda")}
            planos = [(_tjmt(3), "acao", "baixando", ""),
                      (_tjmt(4), "acao", "falhou", "O PJe não respondeu (tempo esgotado)."),
                      (_tjmt(5), "autos", "ok", ""),
                      (_tjmt(6), "autos", "ok", "faltaram 2 peça(s)")]
            for numero, coluna, estado, detalhe in planos:
                demanda_id = cartoes.get(numero)
                if demanda_id is None:
                    continue
                quadro.mover(conn, demanda_id, "acao", "advogado")
                if coluna == "autos":
                    quadro.marcar_autos(conn, demanda_id, "ok", detalhe)
                    quadro.mover(conn, demanda_id, "autos", "trabalhador")
                else:
                    quadro.marcar_autos(conn, demanda_id, estado, detalhe)
            _demonstrar_o_lex(conn, relogio, hoje, agora, pecas_dir)
            conn.execute("UPDATE tarefa SET estado = 'ok', terminada_em = MIN(pedida_em, ?) "
                         "WHERE estado IN ('na_fila', 'rodando')", (manha,))
            config.gravar(conn, CHAVE_DEMO, str(DEMO_VERSAO))
            config.gravar(conn, CHAVE_GERADA_EM, agora.isoformat(timespec="seconds"))
            eventos.registrar(conn, "varredura_concluida", None,
                              {"publicacoes_novas": 3, "avisos_novos": 1, "sincronizados": 12,
                               "falhas_pje": 0, "cartoes_novos": len(cartoes), "djen_ok": True,
                               "pje_ok": True, "erros": []}, quando=manha)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--situacao":
        # Para o painel_demo.sh: 0 = demonstração atual, 3 = demonstração antiga (pode
        # ser recriada), 4 = não é demonstração (nunca apagar) ou não existe.
        return {"atual": 0, "antiga": 3}.get(situacao(argv[1]), 4)
    if len(argv) == 2 and argv[0] == "--desatualizada":
        # 3 = gerada em outro dia ou há mais de 1 h (regerar); 0 = ainda fresca.
        return 3 if desatualizada(argv[1]) else 0
    if len(argv) != 1:
        print("Uso: python scripts/banco_demo.py <caminho.db>\n"
              "     python scripts/banco_demo.py --situacao <caminho.db>\n"
              "     python scripts/banco_demo.py --desatualizada <caminho.db>", file=sys.stderr)
        return 2
    caminho = argv[0]
    if os.path.exists(caminho):
        print(f"ERRO: {caminho} já existe; escolha outro caminho.", file=sys.stderr)
        return 1
    conn = banco.conectar(caminho)
    try:
        agora = datetime.now(eventos.FUSO).replace(microsecond=0)
        gerar(conn, agora.date(), agora=agora)
    finally:
        conn.close()
    print(f"Banco de demonstração criado: {caminho}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
