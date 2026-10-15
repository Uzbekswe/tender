#!/usr/bin/env bash
# Saves an OpenRouter API key into backend/.env without echoing it.
set -euo pipefail
cd "$(dirname "$0")/.."
read -rsp "Paste your OpenRouter key and press Enter: " key
echo
if [[ -z "$key" ]]; then
  echo "No key entered; nothing saved." >&2
  exit 1
fi
umask 077
printf 'OPENROUTER_API_KEY=%s\n' "$key" > .env
unset key
echo "Saved to backend/.env (readable only by you)."
