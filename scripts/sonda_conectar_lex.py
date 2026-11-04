"""Sonda do botão "Conectar o Lex" com o claude real (CI do Windows e do Mac).

Roda a conexão de verdade: instala o Claude Code oficial se faltar e abre o `claude
setup-token` no pseudoterminal. Quando o link aparece, cola um código FALSO e exige que
a Controladoria perceba a recusa da Anthropic e abra uma autorização nova (link novo e
mensagem de recusa). Não autoriza nada. Sai com 0 se tudo isso aconteceu."""

import sys
import time

sys.path.insert(0, ".")

from ponte_mac import conectar  # noqa: E402

LIMITE_S = 420
CODIGO_FALSO = "abcdefghijklmnopqrstuvwxyz0123456789#estadoinventado123"


def main() -> int:
    cx = conectar.Conexao()
    cx.iniciar()
    vistas, inicio, primeiro = [], time.monotonic(), None
    try:
        while time.monotonic() - inicio < LIMITE_S:
            e = cx.estado()
            marca = (e["etapa"], e["conferindo"], e["recusas"])
            if not vistas or vistas[-1] != marca:
                vistas.append(marca)
                print(f"[{time.monotonic() - inicio:5.1f}s] {e['etapa']}: {e['mensagem']}",
                      flush=True)
            if e["etapa"] == conectar.AUTORIZAR and e["link"]:
                if primeiro is None:
                    primeiro = e["link"]
                    print("link de autorização:", primeiro.split("?")[0], flush=True)
                    if cx.colar_codigo(CODIGO_FALSO) != conectar.COLADO:
                        print("a conexão não aceitou o código colado", flush=True)
                        return 1
                elif e["link"] != primeiro and e["recusas"] == 1:
                    ok = e["mensagem"] == conectar.MENSAGEM_RECUSADO
                    print("recusa percebida; link novo aberto;", "mensagem certa" if ok
                          else f"mensagem inesperada: {e['mensagem']}", flush=True)
                    return 0 if ok else 1
            if not e["andamento"]:
                return 1
            time.sleep(1)
        print("tempo esgotado", flush=True)
        return 1
    finally:
        print("diagnóstico:\n  " + "\n  ".join(cx.diagnostico()), flush=True)
        cx.cancelar()


if __name__ == "__main__":
    sys.exit(main())
