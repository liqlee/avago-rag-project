#!/bin/bash
set -uo pipefail

# ============================================================
# RAG PoC — Teardown Script
# Deletes all application resources, then destroys the cluster.
# ============================================================

RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$PROJECT_ROOT/deploy"

echo -e "${RED}WARNING: This will delete ALL deployed resources and destroy the SNO cluster.${NC}"
echo ""
read -p "Type 'DELETE' to confirm: " CONFIRM
if [ "$CONFIRM" != "DELETE" ]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "--- Deleting GitOps + Quay ---"
oc delete application rag-poc -n openshift-gitops --ignore-not-found 2>/dev/null || true
oc delete quayregistry rag-registry -n quay --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/infrastructure/quay/subscription.yaml" --ignore-not-found 2>/dev/null || true
oc delete namespace quay --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/infrastructure/gitops/subscription.yaml" --ignore-not-found 2>/dev/null || true

echo ""
echo "--- Deleting application resources ---"
oc delete -f "$DEPLOY_DIR/apps/orchestrator/rag-orchestrator.yaml" --ignore-not-found 2>/dev/null || true
oc delete job init-schema -n rag-app --ignore-not-found 2>/dev/null || true
oc delete job ingest-manuals -n rag-app --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/apps/webui/open-webui.yaml" --ignore-not-found 2>/dev/null || true

echo ""
echo "--- Deleting model InferenceServices ---"
oc delete -f "$DEPLOY_DIR/models/vllm/vllm-qwen.yaml" --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/models/guardian/guardian.yaml" --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/models/reranker/bge-reranker.yaml" --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/models/embedding/bge-m3.yaml" --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/models/servingruntimes/" --ignore-not-found 2>/dev/null || true
oc delete job download-qwen download-guardian download-bge-m3 download-bge-reranker -n rag-models --ignore-not-found 2>/dev/null || true
oc delete -f "$DEPLOY_DIR/models/storage-config.yaml" --ignore-not-found 2>/dev/null || true

echo ""
echo "--- Deleting CI/CD pipeline resources ---"
oc delete -f "$PROJECT_ROOT/pipelines/triggers.yaml" --ignore-not-found 2>/dev/null || true
oc delete -f "$PROJECT_ROOT/pipelines/pipeline.yaml" --ignore-not-found 2>/dev/null || true
oc delete -f "$PROJECT_ROOT/pipelines/webhook-secret.yaml" --ignore-not-found 2>/dev/null || true
oc delete -f "$PROJECT_ROOT/pipelines/rbac.yaml" --ignore-not-found 2>/dev/null || true
oc delete namespace rag-pipelines --ignore-not-found 2>/dev/null || true

echo ""
echo "--- Deleting data resources ---"
oc delete -f "$DEPLOY_DIR/data/minio/minio.yaml" --ignore-not-found 2>/dev/null || true
oc delete postgrescluster rag-db -n rag-app --ignore-not-found 2>/dev/null || true

echo ""
echo "--- Deleting namespaces ---"
oc delete namespace rag-models --ignore-not-found 2>/dev/null || true
oc delete namespace rag-app --ignore-not-found 2>/dev/null || true

echo ""
echo "--- Destroying SNO cluster ---"
echo "Run from the directory containing your install files:"
echo ""
echo "  cd ~/rag-sno"
echo "  openshift-install destroy cluster --dir=. --log-level=info"
echo ""
echo "This will delete the EC2 instance, VPC, DNS, and all AWS resources."
