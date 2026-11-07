#!/usr/bin/env bash
# Builds the alert agent, loads it into k3d and applies deploy/alerting/. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=homelab/alert-agent:0.1.0

[ -f .env ] || { echo "missing .env (copy .env.example)" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. <(tr -d '\r' < .env)
set +a
for v in OPENAI_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID; do
  [ -n "${!v:-}" ] || { echo "$v is empty in .env" >&2; exit 1; }
done

docker build -t "$IMAGE" agent
k3d image import -c homelab "$IMAGE"

kubectl apply -f deploy/alerting/namespace.yaml
# Values reach kubectl only through argv on this machine, never a file in the repo.
kubectl -n alerting create secret generic alert-agent \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
  --from-literal=TELEGRAM_BOT_TOKEN="${TELEGRAM_AGENT_BOT_TOKEN:-$TELEGRAM_BOT_TOKEN}" \
  --from-literal=TELEGRAM_ALERT_CHAT_ID="$TELEGRAM_ALERT_CHAT_ID" \
  --from-literal=TELEGRAM_ALLOWED_USER_IDS="${TELEGRAM_ALLOWED_USER_IDS:-}" \
  --from-literal=HOLMES_API_KEY="${HOLMES_API_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset HOLMES_API_KEY OPENAI_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_AGENT_BOT_TOKEN TELEGRAM_ALERT_CHAT_ID

kubectl apply -f deploy/alerting/
# A rebuilt image keeps the same tag, so force new pods to pick it up.
kubectl -n alerting rollout restart deploy/alert-agent >/dev/null
kubectl -n alerting rollout status deploy/alert-agent --timeout=300s
echo "Logs: kubectl -n alerting logs deploy/alert-agent -f"
