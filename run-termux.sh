#!/data/data/com.termux/files/usr/bin/bash
set -eu
cd "$(dirname "$0")"
if ! command -v python >/dev/null 2>&1; then
  echo '请先执行: pkg install python -y'
  exit 1
fi
if [ ! -f .orchestrator_token ]; then
  python -c 'import secrets; print(secrets.token_urlsafe(32))' > .orchestrator_token
  chmod 600 .orchestrator_token
fi
export ORCH_TOKEN="$(cat .orchestrator_token)"
export PORT="${PORT:-8000}"
export MODEL="${MODEL:-openrouter/free}"
export CONTROLLER_MODEL="${CONTROLLER_MODEL:-openrouter/free}"
if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock || true; fi
printf '\nYi 多模型主控已准备就绪\n浏览器地址: http://127.0.0.1:%s\n控制台令牌（请勿公开）: %s\nCtrl+C 结束服务。\n\n' "$PORT" "$ORCH_TOKEN"
exec python app.py
