#!/usr/bin/env bash
# Sobe o painel local com o banco de demonstração (dados fictícios).
# Uso: scripts/painel_demo.sh   → http://127.0.0.1:5055
set -euo pipefail
cd "$(dirname "$0")/.."
BANCO=${BANCO:-dados/demo.db}
# Peças fictícias do Lex (PDF, nota e relatório de citações) que a demo mostra.
export CONTROLADORIA_PECAS=dados/demo-pecas
# Demonstração de versão antiga (sem o Lex), com a pasta das peças sumida, gerada em outro
# dia ou há mais de 1 h (o "agora" dela ficou para trás): recria. Só recria o que o script
# reconhece como demonstração (código 3 / "atual"); qualquer outro banco nunca é apagado.
if [ -f "$BANCO" ]; then
  situacao=0
  .venv/bin/python scripts/banco_demo.py --situacao "$BANCO" || situacao=$?
  if [ "$situacao" -eq 0 ]; then
    .venv/bin/python scripts/banco_demo.py --desatualizada "$BANCO" || situacao=$?
  fi
  if [ "$situacao" -eq 3 ]; then
    echo "Demonstração desatualizada: recriando $BANCO"
    rm -f "$BANCO" "$BANCO-wal" "$BANCO-shm"
    rm -rf "$CONTROLADORIA_PECAS"
  fi
fi
[ -f "$BANCO" ] || .venv/bin/python scripts/banco_demo.py "$BANCO"
export CONTROLADORIA_BANCO="$BANCO"
export PORT=${PORT:-5055}
export PAINEL_SENHA=""
export TJMT_SENHA=""
# python-dotenv não sobrescreve variáveis já definidas: o .env real nunca vaza para a demo.
export TJMT_CPF=""
export TJMT_SENHA_2GRAU=""
export LLM_API_KEY=""
export CARTEIRA_ADVOGADO_NOME="${CARTEIRA_ADVOGADO_NOME:-ADVOGADA DEMONSTRAÇÃO}"
exec .venv/bin/python painel.py
