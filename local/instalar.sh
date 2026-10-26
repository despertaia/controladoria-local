#!/usr/bin/env bash
# Instala ou atualiza a Controladoria do Lex Lab (Desperta.IA) neste Mac.
# Pode rodar quantas vezes quiser: na segunda vez, atualiza o código e mantém os dados.
#
#   curl -fsSL https://raw.githubusercontent.com/despertaia/controladoria-local/main/local/instalar.sh | bash
#   bash local/instalar.sh [--destino PASTA] [--url URL_DO_ZIP] [--origem PASTA] [--sem-inicio-automatico]
#
# --origem instala a partir de uma pasta local (CI e testes), sem baixar nada.
# --sem-inicio-automatico não cria o serviço nem inicia (CI).
# LAUNCHCTL, AGENTES, AREA_DE_TRABALHO, UV e ESPERA_PAINEL existem só para teste; no uso
# normal, não defina.
#
# Tudo fica dentro de main(): com "curl | bash" o bash lê o script inteiro antes de rodar.

main() {
  set -Eeuo pipefail

  local DESTINO="$HOME/Controladoria"
  local URL="https://github.com/despertaia/controladoria-local/archive/refs/heads/main.zip"
  local ORIGEM=""
  local INICIO_AUTOMATICO=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --destino) DESTINO="${2:?falta a pasta depois de --destino}"; shift 2 ;;
      --url) URL="${2:?falta o endereço depois de --url}"; shift 2 ;;
      --origem) ORIGEM="${2:?falta a pasta depois de --origem}"; shift 2 ;;
      --sem-inicio-automatico) INICIO_AUTOMATICO=0; shift ;;
      -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true; return 0 ;;
      *) echo "Opção desconhecida: $1" >&2; return 1 ;;
    esac
  done

  local LAUNCHCTL=${LAUNCHCTL:-launchctl}
  local AGENTES=${AGENTES:-$HOME/Library/LaunchAgents}
  local AREA=${AREA_DE_TRABALHO:-$HOME/Desktop}
  local ROTULO=br.com.despertaia.controladoria
  local PLIST="$AGENTES/$ROTULO.plist"
  local LOG_SERVICO="$HOME/Library/Logs/controladoria.log"
  local PRESERVAR=" dados peticoes cache grupos .venv .env .git .pytest_cache __pycache__ "
  PASSO="preparar"
  trap 'echo "" >&2; echo "ERRO: a instalação parou na etapa \"$PASSO\". Nada dos seus dados foi apagado." >&2; echo "Se precisar de ajuda, copie as mensagens acima." >&2' ERR

  echo "== Controladoria do Lex Lab (Desperta.IA): instalação =="
  echo "Pasta: $DESTINO"

  # 1) Código novo numa pasta temporária.
  local TMP
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/controladoria.XXXXXX")
  local FONTE
  if [ -n "$ORIGEM" ]; then
    PASSO="ler a pasta de origem"
    [ -f "$ORIGEM/local/iniciar.py" ] || { echo "ERRO: $ORIGEM não parece a Controladoria." >&2; return 1; }
    FONTE=$(cd "$ORIGEM" && pwd)
  else
    PASSO="baixar a Controladoria"
    echo "Baixando a versão mais recente..."
    curl -fsSL --retry 3 -o "$TMP/controladoria.zip" "$URL"
    PASSO="abrir o arquivo baixado"
    unzip -q "$TMP/controladoria.zip" -d "$TMP/zip"
    FONTE=$(find "$TMP/zip" -mindepth 1 -maxdepth 1 -type d | head -n 1)
    [ -f "$FONTE/local/iniciar.py" ] || { echo "ERRO: o arquivo baixado não tem a Controladoria." >&2; return 1; }
  fi

  # Versão: a que já estava instalada (se houver) e a que vai entrar.
  local VERSAO_ANTIGA="" VERSAO_NOVA=""
  [ -f "$DESTINO/VERSAO" ] && VERSAO_ANTIGA=$(tr -d '[:space:]' < "$DESTINO/VERSAO" || true)
  [ -f "$FONTE/VERSAO" ] && VERSAO_NOVA=$(tr -d '[:space:]' < "$FONTE/VERSAO" || true)
  dizer_versao() {
    [ -n "$VERSAO_NOVA" ] || return 0
    if [ -n "$VERSAO_ANTIGA" ] && [ "$VERSAO_ANTIGA" != "$VERSAO_NOVA" ]; then
      echo "Controladoria atualizada da versão $VERSAO_ANTIGA para a $VERSAO_NOVA."
    else
      echo "Controladoria versão $VERSAO_NOVA instalada."
    fi
  }

  # 2) Para o serviço antes de trocar o código (atualização).
  PASSO="parar a versão em uso"
  if [ "$INICIO_AUTOMATICO" -eq 1 ] && [ -f "$PLIST" ]; then
    "$LAUNCHCTL" bootout "gui/$(id -u)/$ROTULO" >/dev/null 2>&1 || true
  fi

  # 3) Copia o código por cima, sem tocar nos dados.
  PASSO="copiar os arquivos"
  mkdir -p "$DESTINO"
  local DEST_ABS
  DEST_ABS=$(cd "$DESTINO" && pwd)
  if [ "$FONTE" != "$DEST_ABS" ]; then
    local item nome
    for item in "$FONTE"/* "$FONTE"/.[!.]*; do
      [ -e "$item" ] || continue
      nome=$(basename "$item")
      case "$PRESERVAR" in *" $nome "*) continue ;; esac
      rm -rf "${DEST_ABS:?}/$nome"
      cp -R "$item" "$DEST_ABS/$nome"
    done
  fi
  mkdir -p "$DEST_ABS/dados"
  rm -rf "$TMP"
  echo "Arquivos copiados."

  # 4) uv (gerenciador de Python), se faltar.
  PASSO="instalar o uv"
  local UV=${UV:-}
  if [ -z "$UV" ]; then
    if command -v uv >/dev/null 2>&1; then
      UV=$(command -v uv)
    elif [ -x "$HOME/.local/bin/uv" ]; then
      UV="$HOME/.local/bin/uv"
    else
      echo "Instalando o uv (gerenciador de Python)..."
      curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_NO_MODIFY_PATH=1 sh
      UV="$HOME/.local/bin/uv"
    fi
  fi
  [ -x "$UV" ] || { echo "ERRO: não encontrei o uv depois de instalar ($UV)." >&2; return 1; }

  # 5) Python 3.12 e dependências (o uv baixa o Python se precisar).
  PASSO="preparar o Python"
  local PY="$DEST_ABS/.venv/bin/python"
  if [ -x "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; then
    echo "Python 3.12 já preparado."
  else
    rm -rf "$DEST_ABS/.venv"
    echo "Preparando o Python 3.12 (pode levar alguns minutos na primeira vez)..."
    (cd "$DEST_ABS" && "$UV" venv --python 3.12 .venv)
  fi
  PASSO="instalar as dependências"
  echo "Instalando as dependências..."
  (cd "$DEST_ABS" && "$UV" pip install --python "$PY" -r requirements-local.txt)

  # 6) Atalho na Mesa: abre o painel no navegador (o serviço fica sempre ligado).
  PASSO="criar o atalho na Mesa"
  local PORTA
  PORTA=$(cd "$DEST_ABS" && "$PY" -c 'from local import iniciar; print(iniciar.ler_porta(None))' 2>/dev/null || echo 5056)
  local URL_PAINEL="http://127.0.0.1:$PORTA"
  if [ -d "$AREA" ]; then
    cat > "$AREA/Controladoria.webloc" <<EOF_WEBLOC
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>URL</key>
    <string>$URL_PAINEL</string>
</dict>
</plist>
EOF_WEBLOC
    echo "Atalho criado na Mesa: Controladoria"
  fi

  if [ "$INICIO_AUTOMATICO" -eq 0 ]; then
    echo ""
    echo "Instalada sem início automático. Para iniciar:"
    echo "  cd \"$DEST_ABS\" && .venv/bin/python -m local.iniciar --abrir"
    dizer_versao
    trap - ERR
    return 0
  fi

  # 7) Início automático: serviço do usuário (LaunchAgent), sobe ao entrar no Mac.
  PASSO="ligar o início automático"
  xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
  mkdir -p "$AGENTES" "$(dirname "$LOG_SERVICO")"
  cat > "$PLIST" <<EOF_PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$ROTULO</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml "$PY")</string>
        <string>-m</string>
        <string>local.iniciar</string>
        <string>--sem-navegador</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml "$DEST_ABS")</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$(xml "$LOG_SERVICO")</string>
    <key>StandardErrorPath</key>
    <string>$(xml "$LOG_SERVICO")</string>
</dict>
</plist>
EOF_PLIST
  chmod 644 "$PLIST"
  local UID_NUM
  UID_NUM=$(id -u)
  "$LAUNCHCTL" bootout "gui/$UID_NUM/$ROTULO" >/dev/null 2>&1 || true
  # Logo após o bootout o macOS às vezes ainda não liberou o serviço (erro 5): tenta até 3x.
  local tentativa=1
  until "$LAUNCHCTL" bootstrap "gui/$UID_NUM" "$PLIST"; do
    if [ "$tentativa" -ge 3 ]; then
      echo "ERRO: o macOS não aceitou o serviço ($PLIST)." >&2
      return 1
    fi
    tentativa=$((tentativa + 1))
    sleep "${ESPERA_BOOTSTRAP:-2}"
  done
  echo "Início automático ligado."

  # 8) Espera o painel responder.
  PASSO="conferir se o painel subiu"
  local i=0 limite=${ESPERA_PAINEL:-180}
  until [ "$limite" -eq 0 ] || curl -fsS -o /dev/null "$URL_PAINEL/saude" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -eq 30 ] && echo "Ainda iniciando... na primeira vez pode levar até 3 minutos."
    if [ "$i" -ge "$limite" ]; then
      trap - ERR
      echo ""
      echo "AVISO: a Controladoria foi instalada, mas ainda está iniciando."
      echo "Espere 2 minutos e abra o atalho Controladoria. Registro: $LOG_SERVICO"
      dizer_versao
      return 0
    fi
    sleep 1
  done
  trap - ERR
  echo ""
  echo "Pronto! A Controladoria está rodando em: $URL_PAINEL"
  echo "Abra esse endereço no navegador (ou o atalho Controladoria na Mesa)."
  dizer_versao
  [ "$limite" -eq 0 ] || open "$URL_PAINEL" >/dev/null 2>&1 || true
}

main "$@"
