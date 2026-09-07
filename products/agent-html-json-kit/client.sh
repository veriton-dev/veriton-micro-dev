#!/usr/bin/env bash
# Veriton HTML→JSON prepaid client (kit v1)
set -euo pipefail
HOST="${VERITON_API_HOST:-https://trees-lopez-laws-responded.trycloudflare.com}"
USD="${1:-1}"
echo "quote $USD USDC on $HOST"
curl -sS "$HOST/v1/credits/quote" -H 'content-type: application/json' -d "{"usd":$USD}"
echo
echo "1) Send exact USDC on Base to payTo from quote"
echo "2) export TX=0x..."
echo "3) claim:"
cat <<EOF
TOKEN=\$(curl -sS "$HOST/v1/credits/claim" -H 'content-type: application/json' -d "{\"tx\":\"\$TX\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))')
curl -sS "$HOST/v1/html-to-json" -H "authorization: Bearer \$TOKEN" -H 'content-type: application/json' \
  -d '{"html":"<html><title>Hi</title><h1>X</h1></html>","selector":"h1"}'
EOF
