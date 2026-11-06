#!/usr/bin/env bash
# Builds the sample apps, loads them into k3d and deploys them with the shared chart. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=homelab
TAG=0.1.0
SERVICES=(shop-web orders-api catalog-api invoice-api)

kubectl apply -f deploy/postgres/namespace.yaml

# Passwords are generated once; later runs keep the existing Secrets.
if ! kubectl -n shop get secret postgres-auth >/dev/null 2>&1 || ! kubectl -n shop get secret postgres-app >/dev/null 2>&1; then
  admin_pw=$(openssl rand -hex 24)
  app_pw=$(openssl rand -hex 24)
  kubectl -n shop create secret generic postgres-auth \
    --from-literal=POSTGRES_PASSWORD="$admin_pw" --from-literal=APP_PASSWORD="$app_pw" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n shop create secret generic postgres-app \
    --from-literal=DATABASE_URL="postgres://shop:${app_pw}@postgres:5432/shop" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  unset admin_pw app_pw
fi

images=()
for svc in "${SERVICES[@]}"; do
  docker build -t "homelab/${svc}:${TAG}" "apps/${svc}"
  images+=("homelab/${svc}:${TAG}")
done
k3d image import -c "$CLUSTER" "${images[@]}"

kubectl apply -f deploy/postgres/init-configmap.yaml -f deploy/postgres/statefulset.yaml
kubectl -n shop rollout status statefulset/postgres --timeout=300s

for svc in "${SERVICES[@]}"; do
  helm upgrade --install "$svc" deploy/charts/app -n shop -f "deploy/apps/${svc}.yaml"
done

kubectl apply -f deploy/network-policies.yaml

for svc in "${SERVICES[@]}"; do
  kubectl -n shop rollout status "deployment/${svc}" --timeout=300s
done

echo "Done. Open the shop with:"
echo "  kubectl -n shop port-forward svc/shop-web 3000:3000"
echo "  then browse http://localhost:3000"
