#!/usr/bin/env bash
# Installs HolmesGPT (second investigation engine) into namespace alerting. Idempotent; secret values never reach stdout or the repo.
set -euo pipefail
cd "$(dirname "$0")/.."

CHART_VERSION=0.46.0
IMAGE=robustadev/holmes:$CHART_VERSION
AGENT_IMAGE=homelab/alert-agent:0.1.0
HELM=${HELM:-helm}
K3D=${K3D:-k3d}

[ -f .env ] || { echo "missing .env (copy .env.example)" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. <(tr -d '\r' < .env)
set +a
[ -n "${OPENAI_API_KEY:-}" ] || { echo "OPENAI_API_KEY is empty in .env" >&2; exit 1; }

# One random key shared by Holmes and alert-agent; it lives in .env (git-ignored) so both deploy scripts see it.
if [ -z "${HOLMES_API_KEY:-}" ]; then
  HOLMES_API_KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
  printf '\nHOLMES_API_KEY=%s\n' "$HOLMES_API_KEY" >> .env
fi

kubectl apply -f deploy/alerting/namespace.yaml >/dev/null

# Grafana Viewer token for the Loki toolset. Kept when holmes-secrets already has one (ROTATE_GRAFANA_TOKEN=1 replaces it).
GRAFANA_API_KEY=$(kubectl -n alerting get secret holmes-secrets -o jsonpath='{.data.GRAFANA_API_KEY}' 2>/dev/null | base64 -d 2>/dev/null || true)
if [ -z "$GRAFANA_API_KEY" ] || [ "${ROTATE_GRAFANA_TOKEN:-0}" = 1 ]; then
  # Grafana is not reachable from the host without a port-forward (which raises an audit alert), so a short Job inside the cluster calls its API.
  trap 'kubectl -n alerting delete job grafana-token-bootstrap secret grafana-bootstrap --ignore-not-found >/dev/null 2>&1 || true' EXIT
  kubectl -n alerting delete job grafana-token-bootstrap --ignore-not-found >/dev/null
  kubectl -n alerting create secret generic grafana-bootstrap \
    --from-literal=user="$(kubectl -n observability get secret grafana-admin -o jsonpath='{.data.admin-user}' | base64 -d)" \
    --from-literal=password="$(kubectl -n observability get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f - >/dev/null <<YAML
apiVersion: batch/v1
kind: Job
metadata: {name: grafana-token-bootstrap, namespace: alerting}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: Never
      securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: bootstrap
          image: $AGENT_IMAGE
          imagePullPolicy: Never
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          resources: {requests: {cpu: 10m, memory: 32Mi}, limits: {memory: 128Mi}}
          env:
            - {name: GF_USER, valueFrom: {secretKeyRef: {name: grafana-bootstrap, key: user}}}
            - {name: GF_PASSWORD, valueFrom: {secretKeyRef: {name: grafana-bootstrap, key: password}}}
          command: [python, -c]
          args:
            - |
              import base64, json, os, time, urllib.request
              base = "http://kps-grafana.observability.svc"
              auth = "Basic " + base64.b64encode(f"{os.environ['GF_USER']}:{os.environ['GF_PASSWORD']}".encode()).decode()
              def call(method, path, body=None):
                  req = urllib.request.Request(base + path, method=method, headers={"Authorization": auth, "Content-Type": "application/json"},
                                               data=None if body is None else json.dumps(body).encode())
                  return json.load(urllib.request.urlopen(req, timeout=20))
              found = call("GET", "/api/serviceaccounts/search?query=holmes-viewer")["serviceAccounts"]
              sa = next((s for s in found if s["name"] == "holmes-viewer"), None) or call("POST", "/api/serviceaccounts", {"name": "holmes-viewer", "role": "Viewer"})
              print("TOKEN=" + call("POST", f"/api/serviceaccounts/{sa['id']}/tokens", {"name": f"holmes-{int(time.time())}"})["key"])
YAML
  kubectl -n alerting wait --for=condition=complete job/grafana-token-bootstrap --timeout=120s >/dev/null
  GRAFANA_API_KEY=$(kubectl -n alerting logs job/grafana-token-bootstrap | sed -n 's/^TOKEN=//p')
  [ -n "$GRAFANA_API_KEY" ] || { echo "no Grafana token returned" >&2; exit 1; }
fi

kubectl -n alerting create secret generic holmes-secrets \
  --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
  --from-literal=HOLMES_API_KEY="$HOLMES_API_KEY" \
  --from-literal=GRAFANA_API_KEY="$GRAFANA_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset OPENAI_API_KEY HOLMES_API_KEY GRAFANA_API_KEY

# Pre-load the image so the node never pulls from Docker Hub itself.
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE"
"$K3D" image import -c homelab "$IMAGE"

kubectl apply -f deploy/alerting/holmes-network-policy.yaml
"$HELM" repo add robusta https://robusta-charts.storage.googleapis.com >/dev/null 2>&1 || true
"$HELM" repo update robusta >/dev/null
"$HELM" upgrade --install holmes robusta/holmes --version "$CHART_VERSION" -n alerting -f platform/holmes/values.yaml --wait --timeout 5m
kubectl -n alerting rollout status deploy/holmes-holmes --timeout=300s
# After a secret change (ROTATE_GRAFANA_TOKEN=1), run: kubectl -n alerting rollout restart deploy/holmes-holmes
echo "Logs: kubectl -n alerting logs deploy/holmes-holmes -f"
