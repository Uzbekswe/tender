#!/usr/bin/env bash
# Share the local app through a Cloudflare quick tunnel: prints a public https://*.trycloudflare.com link.
# The link works while this Mac, this script and the app are running. Stop with Ctrl+C.
# Usage (from backend/):  bash scripts/share.sh
set -uo pipefail

PORT=8010
LOCAL_URL="http://localhost:${PORT}"
BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RESET=""
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Install it once with:"
  echo "  brew install cloudflared"
  exit 1
fi

if ! curl -fsS --max-time 5 -o /dev/null "${LOCAL_URL}/health"; then
  echo "The app is not answering on ${LOCAL_URL}. Start it first, in another terminal:"
  echo "  cd backend && uv run uvicorn app.main:app --port ${PORT}"
  exit 1
fi

password_set=0
if [[ -n "${ACCESS_PASSWORD:-}" ]]; then
  password_set=1
elif [[ -f "${BACKEND_DIR}/.env" ]] && grep -qE '^[[:space:]]*ACCESS_PASSWORD[[:space:]]*=[[:space:]]*[^[:space:]]' "${BACKEND_DIR}/.env"; then
  password_set=1
elif [[ "${ACCOUNTS:-}" =~ ^(1|true|yes)$ ]] || { [[ -f "${BACKEND_DIR}/.env" ]] && grep -qiE '^[[:space:]]*ACCOUNTS[[:space:]]*=[[:space:]]*(1|true|yes)' "${BACKEND_DIR}/.env"; }; then
  password_set=1  # account mode: sign-in required (with SIGNUP_CODE, only invited people can sign up)
fi
if [[ "${password_set}" -eq 0 ]]; then
  echo "${YELLOW}WARNING:${RESET} neither ACCOUNTS=1 nor ACCESS_PASSWORD is set in backend/.env."
  echo "  Anyone with the link can create projects and use your AI quota."
  echo "  Set ACCOUNTS=1, SIGNUP_CODE=<invite code> and DEMO_PUBLIC=1 in backend/.env, restart the app, then run this script again."
  echo
fi

echo "Starting the tunnel to ${LOCAL_URL} ..."
echo "The public link appears below. Stop with Ctrl+C."
echo

announced=""
cloudflared tunnel --url "${LOCAL_URL}" 2>&1 | while IFS= read -r line; do
  printf '%s\n' "$line"
  if [[ "$line" =~ https://[a-zA-Z0-9-]+\.trycloudflare\.com ]]; then
    url="${BASH_REMATCH[0]}"
    if [[ "$url" != "$announced" ]]; then
      announced="$url"
      echo
      echo "${GREEN}==> Public link: ${BOLD}${url}${RESET}"
      echo
    fi
  fi
done
