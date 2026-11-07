#!/usr/bin/env bash
# Triggers each homelab detection on demand; see docs/demo.md for the 10-minute walkthrough.
set -euo pipefail
cd "$(dirname "$0")/.."

NS=shop
PIDS=()

# The proxy dies with the script, so a Ctrl-C never leaves it behind.
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

# proxy_up: one local kubectl proxy; unlike port-forward it raises no AuditExecByUser alert and survives pod restarts.
proxy_up() {
  [ ${#PIDS[@]} -gt 0 ] && return 0
  kubectl proxy --port=18001 >/dev/null 2>&1 &
  PIDS+=($!)
  for _ in $(seq 1 40); do
    (echo >/dev/tcp/127.0.0.1/18001) 2>/dev/null && return 0
    sleep 0.5
  done
  echo "kubectl proxy did not come up" >&2
  return 1
}

# svc_url <svc> <port>: proxy URL of shop-web (its NetworkPolicy admits any source; the backends only admit shop-web).
svc_url() { echo "http://127.0.0.1:18001/api/v1/namespaces/$NS/services/$1:$2/proxy"; }

# pf <pod> <local-port>: port-forward to a backend pod; kubelet traffic is the only path its NetworkPolicy lets through.
pf() {
  kubectl -n "$NS" port-forward "pod/$1" "$2:8080" >/dev/null 2>&1 &
  PF_PID=$!
  PIDS+=($PF_PID)
  for _ in $(seq 1 40); do
    (echo >"/dev/tcp/127.0.0.1/$2") 2>/dev/null && return 0
    sleep 0.5
  done
  echo "port-forward to $1 did not come up" >&2
  return 1
}

# drive <seconds> <curl args...>: repeat a request twice a second; non-2xx is expected and ignored.
drive() {
  local end=$((SECONDS + $1)); shift
  while [ $SECONDS -lt $end ]; do
    curl -s -o /dev/null -m 5 "$@" || true
    sleep 0.5
  done
}

# pod_of <app>: first pod name of a shop Deployment (fault state is per pod, so we pin one).
pod_of() { kubectl -n "$NS" get pod -l "app.kubernetes.io/name=$1" -o jsonpath='{.items[0].metadata.name}'; }

# netpol_blocked: the shipped policies with orders-api removed from the postgres allow-lists.
netpol_blocked() { sed 's/values: \[orders-api, catalog-api, invoice-api\]/values: [catalog-api, invoice-api]/' deploy/network-policies.yaml; }

case "${1:-}" in
  5xx)
    echo "Drives GET /api/products/4 (deliberate 500) through shop-web for 3 min."
    echo "Expect in Telegram: Http5xxRate (catalog-api, shop-web) after ~2-3 min, AppErrorLogs right after."
    proxy_up
    drive 180 "$(svc_url shop-web 3000)/api/products/4"
    ;;
  error-rate)
    echo "Makes orders-api fail 50% of POST /api/orders for 5 min and sends orders."
    echo "Expect in Telegram: Http5xxRate (orders-api) after ~3 min, AppErrorLogs shortly after."
    pf "$(pod_of orders-api)" 18080
    curl -s -m 5 -X POST "http://127.0.0.1:18080/fault/error-rate?rate=0.5&status=500&seconds=300" >/dev/null
    drive 240 -X POST -H 'Content-Type: application/json' -d '{"product_id":1,"qty":1}' http://127.0.0.1:18080/api/orders
    ;;
  crash)
    echo "Crashes one invoice-api pod 4 times so it restarts 3+ times within 10 min."
    echo "Expect in Telegram: PodRestartingFrequently (warning) about 1-2 min after the last crash."
    pod=$(pod_of invoice-api)
    for i in 1 2 3 4; do
      pf "$pod" 18081
      curl -s -m 5 -X POST http://127.0.0.1:18081/fault/crash >/dev/null || true
      kill "$PF_PID" 2>/dev/null || true
      echo "crash $i sent to $pod"
      sleep 5
      kubectl -n "$NS" wait --for=condition=Ready "pod/$pod" --timeout=90s >/dev/null
    done
    ;;
  oom)
    echo "Allocates memory in one catalog-api pod until it is OOMKilled."
    echo "Expect in Telegram: PodOOMKilled (warning) about 1-2 min later."
    pod=$(pod_of catalog-api)
    pf "$pod" 18082
    curl -s -m 60 -X POST http://127.0.0.1:18082/fault/oom >/dev/null || true
    echo "oom sent to $pod"
    ;;
  netpol)
    echo "Blocks orders-api -> postgres:5432 (as a non-admin identity) and keeps it; undo with: demo.sh netpol-undo."
    echo "Expect: HubblePolicyDrops after ~3 min, Http5xxRate on orders-api once orders are sent, AuditNetworkPolicyChange within ~1 min."
    # Impersonating a non-admin user makes the audit rule apply; system:admin changes are deliberately silent.
    netpol_blocked | kubectl apply --as=demo-operator --as-group=system:masters -f -
    proxy_up
    drive 200 -X POST -H 'Content-Type: application/json' -d '{"product_id":1,"qty":1}' "$(svc_url shop-web 3000)/api/orders"
    ;;
  netpol-undo)
    echo "Restores deploy/network-policies.yaml (orders-api can reach postgres again)."
    kubectl apply -f deploy/network-policies.yaml
    ;;
  rollout)
    echo "Restarts catalog-api while 5xx traffic flows; Http5xxRate must be inhibited by ServiceRollingOut."
    echo "Expect: no Telegram message for catalog-api 5xx. See it with: scripts/demo.sh status (state suppressed, inhibitedBy set)."
    kubectl -n "$NS" rollout restart deploy/catalog-api
    proxy_up
    drive 150 "$(svc_url shop-web 3000)/api/products/4"
    ;;
  shell)
    echo "Runs a command in a running orders-api pod with a TTY (exec, not run); needs a real terminal."
    echo "Expect in Telegram: AuditExecByUser (warning) and FalcoTerminalShell (warning) within ~1-2 min."
    pod=$(pod_of orders-api)
    # Falco only flags a shell with a TTY (proc.tty != 0); Git Bash needs winpty to hand kubectl a real console.
    run=(kubectl)
    flags=-t
    if [ -t 0 ]; then
      flags=-it
      command -v winpty >/dev/null 2>&1 && run=(winpty kubectl)
    else
      echo "warning: stdin is not a terminal, so no TTY is allocated and Falco stays quiet (the audit alert still fires)" >&2
    fi
    "${run[@]}" -n "$NS" exec $flags "$pod" -- sh -c 'echo "shell on $(hostname) as uid $(id -u)"; ls /; sleep 3'
    ;;
  secret)
    echo "Lists Secrets as a throwaway ServiceAccount in namespace demo-siem, then deletes the namespace."
    echo "Expect in Telegram: AuditSecretsListByServiceAccount (critical) within ~1 min; the Role and RoleBinding setup also raises AuditRBACChangeByAdmin warnings (system:admin writes RBAC)."
    kubectl create namespace demo-siem
    kubectl -n demo-siem create serviceaccount probe
    kubectl -n demo-siem create role secret-lister --verb=list --resource=secrets
    kubectl -n demo-siem create rolebinding probe-secrets --role=secret-lister --serviceaccount=demo-siem:probe
    kubectl -n demo-siem get secrets --as=system:serviceaccount:demo-siem:probe
    kubectl delete namespace demo-siem --wait=false
    ;;
  status)
    echo "ALERTNAME | SERVICE | SEVERITY | STATE | INHIBITED BY | SUMMARY"
    # API-server proxy instead of port-forward, so checking status raises no audit alert itself.
    kubectl get --raw /api/v1/namespaces/observability/services/kps-alertmanager:9093/proxy/api/v2/alerts | sed 's/},{"annotations"/}\n{"annotations"/g' | while read -r a; do
      f() { printf '%s' "$a" | grep -o "\"$1\":\"[^\"]*\"" | head -1 | cut -d'"' -f4; }
      inh=$(printf '%s' "$a" | grep -o '"inhibitedBy":\[[^]]*\]' | head -1 | sed 's/.*\[\(.*\)\]/\1/')
      printf '%s | %s | %s | %s | %s | %.90s\n' "$(f alertname)" "$(f service)" "$(f severity)" "$(f state)" "${inh:--}" "$(f summary)"
    done | sort
    ;;
  reset)
    echo "Undoing every demo change: network policy, demo-siem namespace, per-pod fault state."
    kubectl apply -f deploy/network-policies.yaml
    kubectl delete namespace demo-siem --ignore-not-found
    # Fault mode lives in process memory, so a restart clears it.
    for d in shop-web orders-api catalog-api invoice-api; do kubectl -n "$NS" rollout restart "deploy/$d"; done
    for d in shop-web orders-api catalog-api invoice-api; do kubectl -n "$NS" rollout status "deploy/$d" --timeout=180s; done
    ;;
  *)
    echo "usage: scripts/demo.sh {5xx|error-rate|crash|oom|netpol|netpol-undo|rollout|shell|secret|status|reset}" >&2
    exit 2
    ;;
esac
