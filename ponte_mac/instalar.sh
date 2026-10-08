#!/usr/bin/env bash
# Instala o serviço do Mac da ponte com o Lex como LaunchAgent do usuário:
# sobe sozinho ao entrar no Mac e é reiniciado se cair. Pode rodar de novo (atualiza).
# Não lê nem mostra nenhuma credencial; só confere se estão nas Chaves do macOS.
# DESTINO, LAUNCHCTL, SECURITY e ESPERA_BOOTSTRAP existem só para teste; no uso normal,
# não defina.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DESTINO=${DESTINO:-$HOME/Library/LaunchAgents}
LAUNCHCTL=${LAUNCHCTL:-launchctl}
SECURITY=${SECURITY:-security}
ESPERA_BOOTSTRAP=${ESPERA_BOOTSTRAP:-2}
ROTULO=br.com.despertaia.controladoria-ponte
PLIST="$DESTINO/$ROTULO.plist"
PYTHON="$REPO/.venv/bin/python"
CONFIG="$HOME/.config/controladoria-ponte.json"
LOG="$HOME/Library/Logs/controladoria-ponte.log"
CHAVE_PONTE=controladoria-ponte-chave
ACESSO_CLAUDE=controladoria-lex-claude

if [ ! -x "$PYTHON" ]; then
  echo "ERRO: não achei o Python do projeto em $PYTHON."
  echo "Crie o ambiente primeiro: cd \"$REPO\" && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# Escapa o texto para ir dentro do XML do plist.
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

# 1) Configuração: só cria se ainda não existir (nunca sobrescreve a do usuário).
if [ ! -e "$CONFIG" ]; then
  mkdir -p "$(dirname "$CONFIG")"
  cat > "$CONFIG" <<'JSON'
{
  "painel": "https://processos.despertaia.com.br",
  "casa": "~/Desktop/Claude Cowork/Processos pendentes"
}
JSON
  echo "Configuração criada em $CONFIG"
else
  echo "Configuração já existe, mantida: $CONFIG"
fi

# 2) Serviço (LaunchAgent).
mkdir -p "$DESTINO" "$(dirname "$LOG")"
cat > "$PLIST" <<EOF_PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$ROTULO</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml "$PYTHON")</string>
        <string>-m</string>
        <string>ponte_mac</string>
        <string>rodar</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml "$REPO")</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$(xml "$LOG")</string>
    <key>StandardErrorPath</key>
    <string>$(xml "$LOG")</string>
</dict>
</plist>
EOF_PLIST
chmod 644 "$PLIST"

UID_NUM=$(id -u)
"$LAUNCHCTL" bootout "gui/$UID_NUM/$ROTULO" >/dev/null 2>&1 || true
# Logo após o bootout o macOS às vezes ainda não liberou o serviço (erro 5): tenta até 3x.
tentativa=1
until "$LAUNCHCTL" bootstrap "gui/$UID_NUM" "$PLIST"; do
  if [ "$tentativa" -ge 3 ]; then
    echo "ERRO: o macOS não aceitou o serviço ($PLIST)."
    exit 1
  fi
  tentativa=$((tentativa + 1))
  sleep "$ESPERA_BOOTSTRAP"
done
echo "Serviço instalado e iniciado: $ROTULO"
echo "Registro (log): $LOG"

# 3) Chaves do macOS: só confere se existem (sem ler o valor).
falta=0
if ! "$SECURITY" find-generic-password -s "$CHAVE_PONTE" >/dev/null 2>&1; then
  falta=1
  echo "FALTA a chave da ponte nas Chaves ($CHAVE_PONTE)."
  echo "  Gere a chave nas Configurações do Lex, copie e rode: cd \"$REPO\" && .venv/bin/python -m ponte_mac guardar-chave"
fi
if ! "$SECURITY" find-generic-password -s "$ACESSO_CLAUDE" >/dev/null 2>&1; then
  falta=1
  echo "FALTA o acesso do Claude nas Chaves ($ACESSO_CLAUDE)."
  echo "  Rode: bash \"$REPO/ponte_mac/guardar_acesso_claude.sh\" e autorize no navegador."
fi
[ "$falta" -eq 0 ] && echo "Chaves conferidas: chave da ponte e acesso do Claude presentes."

# 4) Permissão da Mesa.
cat <<'AVISO'

Atenção: a casa da Banca fica na Mesa (Desktop). Na primeira execução o macOS pode
perguntar se o Python pode acessar a pasta Mesa: clique em Permitir. Se negou sem querer:
Ajustes do Sistema › Privacidade e Segurança › Arquivos e Pastas, ache o Python e ligue "Mesa".
Depois reinicie o serviço rodando este instalador de novo.
AVISO
