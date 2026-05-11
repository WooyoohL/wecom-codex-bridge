#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${1:-$ROOT_DIR/config.local.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "missing config file: $CONFIG_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a

: "${VPS_HOST:?missing VPS_HOST}"
: "${VPS_USER:?missing VPS_USER}"

VPS_SSH_PORT="${VPS_SSH_PORT:-22}"
VPS_REMOTE_HOST="${VPS_REMOTE_HOST:-127.0.0.1}"
VPS_REMOTE_PORT="${VPS_REMOTE_PORT:-9800}"
LOCAL_FORWARD_HOST="${LOCAL_FORWARD_HOST:-127.0.0.1}"
LOCAL_FORWARD_PORT="${LOCAL_FORWARD_PORT:-8000}"

SSH_ARGS=(
  -p "$VPS_SSH_PORT"
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -N
  -R "${VPS_REMOTE_HOST}:${VPS_REMOTE_PORT}:${LOCAL_FORWARD_HOST}:${LOCAL_FORWARD_PORT}"
)

if [[ -n "${SSH_PROXY_URL:-}" ]]; then
  if [[ "$SSH_PROXY_URL" =~ ^http://([^:/]+):([0-9]+)$ ]]; then
    PROXY_HOST="${BASH_REMATCH[1]}"
    PROXY_PORT="${BASH_REMATCH[2]}"
    SSH_ARGS=(
      -o "ProxyCommand=nc -X connect -x ${PROXY_HOST}:${PROXY_PORT} %h %p"
      "${SSH_ARGS[@]}"
    )
  else
    echo "Unsupported SSH_PROXY_URL: $SSH_PROXY_URL" >&2
    echo "Expected format: http://host:port" >&2
    exit 2
  fi
fi

BRIDGE_PID=""
TUNNEL_PID=""

stop_children() {
  local code=$?
  trap - EXIT INT TERM
  echo
  echo "stopping bridge and tunnel..."
  if [[ -n "$BRIDGE_PID" ]]; then
    kill -TERM -- "-$BRIDGE_PID" 2>/dev/null || true
  fi
  if [[ -n "$TUNNEL_PID" ]]; then
    kill -TERM "$TUNNEL_PID" 2>/dev/null || true
  fi
  sleep 1
  if [[ -n "$BRIDGE_PID" ]]; then
    kill -KILL -- "-$BRIDGE_PID" 2>/dev/null || true
  fi
  if [[ -n "$TUNNEL_PID" ]]; then
    kill -KILL "$TUNNEL_PID" 2>/dev/null || true
  fi
  wait "$BRIDGE_PID" "$TUNNEL_PID" 2>/dev/null || true
  echo "stopped"
  exit "$code"
}

trap stop_children EXIT INT TERM

echo "starting bridge..."
setsid python3 "$ROOT_DIR/wecom_bridge.py" --env-file "$CONFIG_FILE" &
BRIDGE_PID="$!"

echo "starting ssh tunnel..."
ssh "${SSH_ARGS[@]}" "${VPS_USER}@${VPS_HOST}" &
TUNNEL_PID="$!"

echo
echo "running"
echo "bridge pid: $BRIDGE_PID"
echo "tunnel pid: $TUNNEL_PID"
echo "callback: http://${VPS_HOST}/wecom/callback"
echo "press Ctrl+C to stop"
echo

wait -n "$BRIDGE_PID" "$TUNNEL_PID"
