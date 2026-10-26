"""
Gera um "dossiê" HTML autocontido de um processo, para salvar junto com as
peças baixadas (peticoes/<numero>/<instancia>/000_DOSSIE.html).

Contém: número, classe, órgão, valor, partes com advogados (e aviso de
intimação pendente) e a linha do tempo completa de andamentos com datas — para
o usuário avaliar prazos offline. Não faz rede; recebe o dict já parseado por
processo_parser.processo_para_dict.
"""

from __future__ import annotations

import html
from datetime import datetime

_ROTULO_INSTANCIA = {"1grau": "1ª instância", "2grau": "2ª instância"}


def _e(texto) -> str:
    return html.escape(str(texto or ""))


def gerar_dossie_html(dados: dict, instancia: str | None = None) -> str:
    inst = _ROTULO_INSTANCIA.get(instancia or "", "")
    gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M")

    partes_html = []
    for polo in dados.get("partes", []):
        integrantes = polo.get("integrantes")
        linhas = []
        if integrantes:
            for p in integrantes:
                badge = (' <span class="alerta">intimação pendente</span>'
                         if p.get("intimacao_pendente") else "")
                advs = "; ".join(
                    f'{_e(a["nome"])}' + (f' ({_e(a["oab"])})' if a.get("oab") else "")
                    for a in p.get("advogados", [])
                )
                adv_html = f'<div class="adv">adv.: {advs}</div>' if advs else ""
                linhas.append(f'<li>{_e(p["nome"])}{badge}{adv_html}</li>')
        else:
            for nome in polo.get("nomes", []):
                linhas.append(f"<li>{_e(nome)}</li>")
        partes_html.append(
            f'<div class="polo"><h3>{_e(polo.get("polo",""))}</h3>'
            f'<ul>{"".join(linhas) or "<li>—</li>"}</ul></div>'
        )

    andamentos = dados.get("andamentos", [])
    linhas_and = "".join(
        f'<tr><td class="data">{_e(a.get("data"))}</td>'
        f'<td>{_e(a.get("texto"))}</td></tr>'
        for a in andamentos
    ) or '<tr><td colspan="2">Nenhum andamento.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="pt-br"><head><meta charset="utf-8">
<title>Dossiê {_e(dados.get("numero_formatado"))}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    color:#0f172a; max-width:900px; margin:24px auto; padding:0 20px; line-height:1.5; }}
  h1 {{ font-size:22px; margin:0; }} h2 {{ font-size:16px; border-bottom:1px solid #e2e8f0;
    padding-bottom:6px; margin-top:28px; }} h3 {{ font-size:14px; margin:12px 0 4px; }}
  .meta {{ color:#64748b; font-size:13px; margin:6px 0 0; }}
  .polo ul {{ margin:4px 0 0; }} .adv {{ color:#64748b; font-size:13px; margin-left:4px; }}
  .alerta {{ background:#fee2e2; color:#b91c1c; font-size:11px; font-weight:600;
    padding:1px 7px; border-radius:999px; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; margin-top:8px; }}
  td {{ border-bottom:1px solid #eef2f7; padding:7px 8px; vertical-align:top; }}
  td.data {{ white-space:nowrap; color:#475569; width:140px; }}
  .rodape {{ color:#94a3b8; font-size:12px; margin-top:28px; }}
</style></head><body>
<h1>{_e(dados.get("numero_formatado"))}</h1>
<p class="meta">
  {f"{_e(inst)} · " if inst else ""}{_e(dados.get("classe")) or "Classe —"}
  {f' · {_e(dados.get("orgao_julgador"))}' if dados.get("orgao_julgador") else ""}
  {f' · {_e(dados.get("valor_causa"))}' if dados.get("valor_causa") else ""}
</p>

<h2>Partes e advogados</h2>
{"".join(partes_html) or "<p>—</p>"}

<h2>Andamentos ({len(andamentos)})</h2>
<table><tbody>{linhas_and}</tbody></table>

<p class="rodape">Dossiê gerado em {gerado_em} pela Controladoria Processual.
Avalie os prazos a partir das datas acima.</p>
</body></html>"""
