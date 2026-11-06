#!/usr/bin/env bash
# Installs metrics, logs, traces, alert rules and Falco. Idempotent: re-running upgrades in place.
# Usage: scripts/platform.sh [--no-telegram]
set -euo pipefail
cd "$(dirname "$0")/.."

# Pinned chart versions: bump here and re-run.
KPS_CHART=92.21.2
LOKI_CHART=7.5.0
TEMPO_CHART=1.24.4
ALLOY_CHART=1.15.0
FALCO_CHART=9.2.2

OBS=observability
NO_TELEGRAM=false
for arg in "$@"; do
  case "$arg" in
    --no-telegram) NO_TELEGRAM=true ;;
    -h|--help) sed -n 2,3p "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

for bin in kubectl helm; do
  command -v "$bin" >/dev/null || { echo "missing $bin, see README Requirements" >&2; exit 1; }
done
kubectl cluster-info >/dev/null 2>&1 || { echo "cluster not reachable, run scripts/up.sh first" >&2; exit 1; }

# Values are loaded, never printed. tr strips CRLF that Windows editors add.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1090
  . <(tr -d '\r' < .env)
  set +a
fi

if [ "$NO_TELEGRAM" = true ]; then
  echo "WARN: --no-telegram, Telegram notifications will not work until you re-run without it" >&2
  TELEGRAM_BOT_TOKEN=disabled
  TELEGRAM_ALERT_CHAT_ID=0
else
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || { echo "TELEGRAM_BOT_TOKEN is empty in .env (or pass --no-telegram)" >&2; exit 1; }
  [ -n "${TELEGRAM_ALERT_CHAT_ID:-}" ] || { echo "TELEGRAM_ALERT_CHAT_ID is empty in .env (or pass --no-telegram)" >&2; exit 1; }
  [[ "$TELEGRAM_ALERT_CHAT_ID" =~ ^-?[0-9]+$ ]] || { echo "TELEGRAM_ALERT_CHAT_ID must be an integer" >&2; exit 1; }
fi

# Namespaces; privileged because node-exporter, Falco and the audit Alloy need host access (documented in docs/siem.md).
for ns in "$OBS" falco; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace "$ns" pod-security.kubernetes.io/enforce=privileged --overwrite >/dev/null
done

# Secrets. The token passes through kubectl's argv on this machine only, never into a file.
kubectl -n "$OBS" create secret generic alertmanager-telegram \
  --from-literal=bot_token="$TELEGRAM_BOT_TOKEN" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

if [ -n "${GRAFANA_ADMIN_PASSWORD:-}" ]; then
  GRAFANA_PW=$GRAFANA_ADMIN_PASSWORD
elif kubectl -n "$OBS" get secret grafana-admin >/dev/null 2>&1; then
  GRAFANA_PW=
else
  GRAFANA_PW=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
fi
if [ -n "$GRAFANA_PW" ]; then
  kubectl -n "$OBS" create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-literal=admin-password="$GRAFANA_PW" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi
unset GRAFANA_PW

# Alertmanager chat_id is an integer in the config, so it is filled into a throwaway values file.
AM_VALUES=$(mktemp)
trap 'rm -f "$AM_VALUES"' EXIT
sed "s/^\( *chat_id:\) 0\$/\1 ${TELEGRAM_ALERT_CHAT_ID}/" platform/observability/kube-prometheus-stack.yaml > "$AM_VALUES"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add falcosecurity https://falcosecurity.github.io/charts >/dev/null 2>&1 || true
helm repo update prometheus-community grafana falcosecurity >/dev/null

# Installs the Prometheus Operator CRDs that the rules below need.
helm upgrade --install kps prometheus-community/kube-prometheus-stack --version "$KPS_CHART" \
  --namespace "$OBS" --values "$AM_VALUES" \
  --set-file 'alertmanager.templateFiles.telegram\.tmpl=platform/observability/telegram.tmpl' \
  --wait --timeout 10m

kubectl apply -f platform/observability/rules/

helm upgrade --install loki grafana/loki --version "$LOKI_CHART" \
  --namespace "$OBS" --values platform/observability/loki.yaml --wait --timeout 10m
helm upgrade --install tempo grafana/tempo --version "$TEMPO_CHART" \
  --namespace "$OBS" --values platform/observability/tempo.yaml --wait --timeout 5m
helm upgrade --install alloy grafana/alloy --version "$ALLOY_CHART" \
  --namespace "$OBS" --values platform/observability/alloy.yaml --wait --timeout 5m
helm upgrade --install alloy-audit grafana/alloy --version "$ALLOY_CHART" \
  --namespace "$OBS" --values platform/observability/alloy-audit.yaml --wait --timeout 5m

# No silent fallback: if the eBPF driver fails on this kernel, stop and read docs/siem.md.
helm upgrade --install falco falcosecurity/falco --version "$FALCO_CHART" \
  --namespace falco --values platform/falco/values.yaml --wait --timeout 5m || {
  echo "Falco failed to start. Check: kubectl -n falco logs ds/falco -c falco ; see docs/siem.md (Falco driver)" >&2
  exit 1
}

kubectl -n "$OBS" rollout status statefulset/prometheus-kps-prometheus --timeout=300s
kubectl -n "$OBS" rollout status statefulset/alertmanager-kps-alertmanager --timeout=300s
kubectl -n "$OBS" rollout status deployment/kps-grafana --timeout=300s
kubectl -n "$OBS" rollout status statefulset/loki --timeout=300s
kubectl -n "$OBS" rollout status daemonset/alloy --timeout=300s
kubectl -n falco rollout status daemonset/falco --timeout=300s

cat <<EOF

Observability layer is up. Open these in separate terminals:
  Grafana       kubectl -n $OBS port-forward svc/kps-grafana 3001:80
  Alertmanager  kubectl -n $OBS port-forward svc/kps-alertmanager 9093:9093
  Hubble UI     kubectl -n kube-system port-forward svc/hubble-ui 12000:80

Grafana login: user admin, password from .env GRAFANA_ADMIN_PASSWORD, or if it was empty:
  kubectl -n $OBS get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d
EOF
