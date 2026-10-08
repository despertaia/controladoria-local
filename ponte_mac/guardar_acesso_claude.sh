#!/usr/bin/env bash
# Gera o acesso de longa duração do Claude Code (claude setup-token) e o guarda nas
# Chaves do macOS, sem mostrar o código e sem copiar/colar.
# Uso: rode este script e autorize no navegador quando ele abrir.
set -euo pipefail
SERVICO=controladoria-lex-claude
CLAUDE_BIN=${CLAUDE_BIN:-$HOME/.local/bin/claude}
registro=$(mktemp -t lexacesso)
trap 'rm -f "$registro"' EXIT
echo "Vai abrir o navegador para autorizar. Volte aqui quando terminar."
echo "(O código aparece na tela do Claude; não precisa copiar nada.)"
# `script` mantém o terminal interativo e grava a saída num arquivo temporário.
script -q "$registro" /usr/bin/env -i HOME="$HOME" USER="$USER" TERM="${TERM:-xterm-256color}" \
  COLUMNS=400 PATH="$HOME/.local/bin:/usr/bin:/bin" "$CLAUDE_BIN" setup-token
codigo=$(sed -E $'s/\x1b\\[[0-9;?]*[A-Za-z]//g; s/\x1b\\][^\x07]*\x07//g' "$registro" \
  | grep -oE 'sk-ant-oat01-[A-Za-z0-9_-]{20,}' | tail -1 || true)
rm -f "$registro"
if [ -z "$codigo" ]; then
  echo "Não consegui ler o código. Nada foi guardado. Rode de novo ou me avise."
  exit 1
fi
printf 'add-generic-password -U -a %s -s %s -w %s\n' "$USER" "$SERVICO" "$codigo" | security -i >/dev/null
unset codigo
clear
echo "Pronto: acesso guardado nas Chaves do macOS (${SERVICO}). Pode fechar esta janela."
