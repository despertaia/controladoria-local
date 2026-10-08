#!/usr/bin/env bash
# Para o serviço do Mac da ponte e remove o LaunchAgent. Não apaga configuração nem Chaves.
# DESTINO e LAUNCHCTL existem só para teste.
set -euo pipefail

DESTINO=${DESTINO:-$HOME/Library/LaunchAgents}
LAUNCHCTL=${LAUNCHCTL:-launchctl}
ROTULO=br.com.despertaia.controladoria-ponte
PLIST="$DESTINO/$ROTULO.plist"

"$LAUNCHCTL" bootout "gui/$(id -u)/$ROTULO" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "Serviço removido: $ROTULO (a configuração e as Chaves foram mantidas)."
