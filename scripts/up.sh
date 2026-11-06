#!/usr/bin/env bash
# Creates the k3d homelab and installs the CNI. Idempotent: re-running skips what already exists.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=homelab
CILIUM_CHART=1.20.4
MIN_DOCKER_MEM_GB=10

for bin in docker k3d kubectl helm; do
  command -v "$bin" >/dev/null || { echo "missing $bin, see README Requirements" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker Desktop is not running" >&2; exit 1; }
mem_gb=$(( $(docker info --format '{{.MemTotal}}') / 1024 / 1024 / 1024 ))
[ "$mem_gb" -ge "$MIN_DOCKER_MEM_GB" ] || { echo "Docker has ${mem_gb} GB, needs ${MIN_DOCKER_MEM_GB} GB" >&2; exit 1; }

if ! k3d cluster list -o json | grep -q "\"name\":\"$CLUSTER\""; then
  # Windows paths need the drive-letter form for Docker bind mounts.
  host_dir=$(cd platform/k3d && (pwd -W 2>/dev/null || pwd))
  k3d cluster create --config platform/k3d/cluster.yaml \
    --volume "$host_dir/audit-policy.yaml:/etc/homelab/audit-policy.yaml@server:*"
fi

# Cilium needs shared bpffs and cgroup2 mounts that k3s-in-docker nodes lack.
for node in $(k3d node list -o json | grep -o "\"name\":\"k3d-$CLUSTER-[a-z]*-[0-9]*\"" | cut -d'"' -f4); do
  docker exec "$node" sh -c '
    mountpoint -q /sys/fs/bpf || mount bpffs /sys/fs/bpf -t bpf
    mount --make-shared /sys/fs/bpf
    mkdir -p /run/cilium/cgroupv2
    mountpoint -q /run/cilium/cgroupv2 || mount -t cgroup2 none /run/cilium/cgroupv2
    mount --make-shared /run/cilium/cgroupv2'
done

helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm repo update cilium >/dev/null
helm upgrade --install cilium cilium/cilium --version "$CILIUM_CHART" \
  --namespace kube-system --values platform/cilium/values.yaml --wait --timeout 10m

kubectl wait --for=condition=Ready nodes --all --timeout=300s
kubectl get nodes -o wide
