#!/usr/bin/env bash
# simulate-spot-failure.sh
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "[dry-run] No nodes will be deleted."
fi

RAY_NAMESPACE="ray"
WORKER_POOL_LABEL="cloud.google.com/gke-nodepool=worker-pool"

echo ""
echo "=== Step 1: Checking for active RayJob ==="
JOB_STATUS=$(kubectl get rayjob ray-pipeline-job -n "$RAY_NAMESPACE" \
  -o jsonpath='{.status.jobStatus}' 2>/dev/null || echo "NOT_FOUND")

if [[ "$JOB_STATUS" == "NOT_FOUND" ]]; then
  echo "ERROR: No RayJob found in namespace."
  exit 1
fi

echo "RayJob status: $JOB_STATUS"
if [[ "$JOB_STATUS" != "RUNNING" ]]; then
  echo "Job is not in RUNNING state. Proceeding anyway."
fi

echo ""
echo "=== Step 2: Listing worker-pool nodes ==="
WORKER_NODES=$(kubectl get nodes -l "$WORKER_POOL_LABEL" \
  --no-headers -o custom-columns="NAME:.metadata.name,STATUS:.status.conditions[-1].type")
echo "$WORKER_NODES"
echo ""

TARGET_NODE=$(kubectl get nodes -l "$WORKER_POOL_LABEL" \
  --no-headers -o custom-columns="NAME:.metadata.name" | head -1)

if [[ -z "$TARGET_NODE" ]]; then
  echo "ERROR: No worker-pool nodes found."
  exit 1
fi

echo "Target node for deletion: $TARGET_NODE"

echo ""
echo "=== Step 3: Ray cluster state BEFORE node deletion ==="
echo ""

echo "Ray worker pods on target node:"
kubectl get pods -n "$RAY_NAMESPACE" \
  --field-selector "spec.nodeName=$TARGET_NODE" \
  --no-headers 2>/dev/null || echo "  (none found)"

echo ""
read -rp "Press ENTER to delete node '$TARGET_NODE'"

echo ""
echo "=== Step 4: Deleting node (simulating Spot preemption) ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[dry-run] Would run: kubectl delete node $TARGET_NODE"
else
  kubectl cordon "$TARGET_NODE"
  echo "Node cordoned."

  kubectl delete node "$TARGET_NODE" --grace-period=0
  echo "Node '$TARGET_NODE' deleted."
fi

if [[ "$DRY_RUN" == "false" ]]; then
  kubectl get pods -n "$RAY_NAMESPACE" -w &
  WATCH_PID=$!

  echo ""
  echo "--- RayJob status updates ---"
  for i in $(seq 1 12); do
    sleep 10
    STATUS=$(kubectl get rayjob ray-pipeline-job -n "$RAY_NAMESPACE" \
      -o jsonpath='{.status.jobStatus}' 2>/dev/null || echo "UNKNOWN")
    WORKERS=$(kubectl get pods -n "$RAY_NAMESPACE" -l ray.io/node-type=worker \
      --no-headers 2>/dev/null | wc -l | tr -d ' ')
    echo "[$(date +%H:%M:%S)] RayJob=$STATUS  Workers running=$WORKERS"
    if [[ "$STATUS" == "SUCCEEDED" ]]; then
      echo "Pipeline completed successfully after node failure. Task retry worked."
      break
    fi
    if [[ "$STATUS" == "FAILED" ]]; then
      echo "Pipeline FAILED. Check logs: kubectl logs -n $RAY_NAMESPACE -l ray.io/node-type=head"
      break
    fi
  done

  kill $WATCH_PID 2>/dev/null || true
fi
