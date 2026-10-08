#!/usr/bin/env bash
# Desinstala a Controladoria do Lex Lab deste Mac: para o serviço, tira o início automático
# e o atalho da Mesa. Os dados (processos, quadro, peças) e as senhas guardadas nas Chaves
# só são apagados com --apagar-dados. A Banca não é tocada.
#
#   bash ~/Controladoria/local/desinstalar.sh [--destino PASTA] [--apagar-dados]
#
# LAUNCHCTL, AGENTES e AREA_DE_TRABALHO existem só para teste; no uso normal, não defina.

main() {
  set -euo pipefail

  local DESTINO="$HOME/Controladoria"
  local APAGAR=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --destino) DESTINO="${2:?falta a pasta depois de --destino}"; shift 2 ;;
      --apagar-dados) APAGAR=1; shift ;;
      *) echo "Opção desconhecida: $1" >&2; return 1 ;;
    esac
  done
  local LAUNCHCTL=${LAUNCHCTL:-launchctl}
  local AGENTES=${AGENTES:-$HOME/Library/LaunchAgents}
  local AREA=${AREA_DE_TRABALHO:-$HOME/Desktop}
  local ROTULO=br.com.despertaia.controladoria
  local PLIST="$AGENTES/$ROTULO.plist"

  echo "== Controladoria do Lex Lab: desinstalação =="
  "$LAUNCHCTL" bootout "gui/$(id -u)/$ROTULO" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  echo "Início automático desligado."
  # Alguma cópia aberta à mão (fora do serviço) também sai.
  pkill -f "$DESTINO/.venv/bin/python -m local.iniciar" >/dev/null 2>&1 || true
  rm -f "$AREA/Controladoria.webloc" "$AREA/Controladoria.command"
  echo "Atalho da Mesa removido."

  if [ "$APAGAR" -eq 1 ]; then
    if [ -x "$DESTINO/.venv/bin/python" ]; then
      # Senhas e chave guardadas nas Chaves do macOS (nada é mostrado).
      (cd "$DESTINO" && .venv/bin/python - <<'PY' || true
import keyring
for nome in ("pje_cpf", "pje_senha", "pje_senha_2grau", "secret_key"):
    try:
        keyring.delete_password("br.com.despertaia.controladoria", nome)
    except Exception:
        pass
PY
      )
    fi
    if [ -f "$DESTINO/local/iniciar.py" ] || [ ! -e "$DESTINO" ]; then
      rm -rf "$DESTINO"
      rm -f "$HOME/Library/Logs/controladoria.log"
      echo "Pasta $DESTINO e dados apagados."
    else
      echo "ERRO: $DESTINO não parece a pasta da Controladoria; não apaguei nada." >&2
      return 1
    fi
  else
    echo "Seus dados continuam em $DESTINO."
    echo "Para apagar tudo (inclusive as senhas guardadas), rode de novo com --apagar-dados."
  fi
  echo "Pronto. A Banca não foi alterada."
}

main "$@"
