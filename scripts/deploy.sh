#!/bin/bash
set -euo pipefail

# ============================================================
# RAG PoC — Master Deployment Script
# Run from your local workstation after SNO is installed.
# Prerequisite: export KUBECONFIG=~/rag-sno/auth/kubeconfig
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$PROJECT_ROOT/deploy"
SERVICES_DIR="$PROJECT_ROOT/services"

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WAIT]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

wait_for_pods() {
    local namespace=$1
    local label=$2
    local timeout=${3:-300}
    warn "Waiting for pods with label '$label' in namespace '$namespace' (timeout: ${timeout}s)..."
    oc wait --for=condition=Ready pod -l "$label" -n "$namespace" --timeout="${timeout}s" 2>/dev/null || true
}

wait_for_operator() {
    local namespace=$1
    local name=$2
    local timeout=${3:-180}
    warn "Waiting for operator '$name' in '$namespace' (timeout: ${timeout}s)..."
    local end=$((SECONDS + timeout))
    while [ $SECONDS -lt $end ]; do
        local phase=$(oc get csv -n "$namespace" -o jsonpath='{.items[?(@.spec.displayName=="'"$name"'")].status.phase}' 2>/dev/null || echo "")
        if [ "$phase" = "Succeeded" ]; then
            info "$name operator is ready."
            return 0
        fi
        sleep 10
    done
    warn "$name operator not ready after ${timeout}s — continuing (may need manual check)."
}

build_image() {
    local name=$1
    local source_dir=$2
    local namespace=${3:-rag-app}

    if oc get bc/"$name" -n "$namespace" &>/dev/null; then
        info "Build config '$name' exists — starting build."
    else
        info "Creating build config for $name."
        oc new-build --binary --name="$name" -n "$namespace" --strategy=docker
        oc patch bc/"$name" -n "$namespace" -p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Containerfile"}}}}'
    fi
    oc start-build "$name" --from-dir="$source_dir" -n "$namespace" --follow
}

# ============================================================
# Pre-flight checks
# ============================================================
info "=== Pre-flight checks ==="

if ! command -v oc &>/dev/null; then
    error "oc CLI not found. Install: brew install openshift-cli"
fi

if ! oc whoami &>/dev/null; then
    error "Not logged in to OpenShift. Set KUBECONFIG or run: oc login"
fi

CLUSTER_USER=$(oc whoami)
NODE_COUNT=$(oc get nodes --no-headers | wc -l | tr -d ' ')
info "Logged in as: $CLUSTER_USER"
info "Cluster nodes: $NODE_COUNT"

if [ "$NODE_COUNT" -ne 1 ]; then
    warn "Expected 1 node (SNO) but found $NODE_COUNT"
fi

GPU_COUNT=$(oc describe nodes | grep -c "nvidia.com/gpu" 2>/dev/null || true)
GPU_COUNT=${GPU_COUNT:-0}
if [ "$GPU_COUNT" -eq 0 ]; then
    warn "No nvidia.com/gpu found on nodes — GPU Operator may need to be installed first."
fi

echo ""
info "=== Step 1: Node Feature Discovery ==="
oc apply -f "$DEPLOY_DIR/infrastructure/gpu-operator/nfd-subscription.yaml"
sleep 30
wait_for_operator "openshift-nfd" "Node Feature Discovery Operator" 120
oc apply -f "$DEPLOY_DIR/infrastructure/gpu-operator/nfd-instance.yaml"
sleep 15

echo ""
info "=== Step 2: NVIDIA GPU Operator ==="
oc apply -f "$DEPLOY_DIR/infrastructure/gpu-operator/namespace.yaml"
oc apply -f "$DEPLOY_DIR/infrastructure/gpu-operator/operatorgroup.yaml"
oc apply -f "$DEPLOY_DIR/infrastructure/gpu-operator/subscription.yaml"
wait_for_operator "nvidia-gpu-operator" "NVIDIA GPU Operator" 180
oc apply -f "$DEPLOY_DIR/infrastructure/gpu-operator/device-plugin-config.yaml"
oc apply -f "$DEPLOY_DIR/infrastructure/gpu-operator/clusterpolicy.yaml"
warn "GPU drivers installing (~5-10 min). Continuing with non-GPU steps..."

echo ""
info "=== Step 3: Service Mesh + Serverless (RHOAI prerequisites) ==="
oc apply -f "$DEPLOY_DIR/infrastructure/rhoai/servicemesh-subscription.yaml"
oc apply -f "$DEPLOY_DIR/infrastructure/rhoai/serverless-subscription.yaml"
warn "Waiting 90s for Mesh + Serverless operators to install..."
sleep 90

echo ""
info "=== Step 4: Red Hat OpenShift AI ==="
oc apply -f "$DEPLOY_DIR/infrastructure/rhoai/rhoai-subscription.yaml"
wait_for_operator "redhat-ods-operator" "Red Hat OpenShift AI" 300
oc apply -f "$DEPLOY_DIR/infrastructure/rhoai/datasciencecluster.yaml"
warn "Waiting 180s for RHOAI components to start..."
sleep 180

echo ""
info "=== Step 5: Create application namespaces ==="
oc apply -f "$DEPLOY_DIR/infrastructure/rhoai/namespaces.yaml"

echo ""
info "=== Step 6: Crunchy Postgres ==="
oc apply -f "$DEPLOY_DIR/data/postgres/subscription.yaml"
wait_for_operator "openshift-operators" "Crunchy Postgres for Kubernetes" 180
oc apply -f "$DEPLOY_DIR/data/postgres/postgrescluster.yaml"
warn "Waiting for Postgres cluster to be ready..."
sleep 60
oc wait --for=condition=Ready pod -l postgres-operator.crunchydata.com/cluster=rag-db -n rag-app --timeout=300s 2>/dev/null || warn "Postgres pods may still be starting."

echo ""
info "=== Step 7: Apply database schema (init Job) ==="
oc apply -f "$DEPLOY_DIR/data/schema/001-init-job.yaml"
warn "Waiting for schema init Job to complete..."
oc wait --for=condition=Complete job/init-schema -n rag-app --timeout=300s 2>/dev/null || warn "Schema init Job may still be running — check: oc logs job/init-schema -n rag-app"
info "Database schema applied."

echo ""
info "=== Step 8: MinIO (object storage) ==="
info "Building MinIO image from official binary (bypasses registry auth)..."
build_image "minio" "$DEPLOY_DIR/data/minio" "rag-app"
oc apply -f "$DEPLOY_DIR/data/minio/minio.yaml"
wait_for_pods "rag-app" "app=minio" 120

echo ""
info "=== Step 9: Model storage config (MinIO) ==="
oc apply -f "$DEPLOY_DIR/models/storage-config.yaml"
info "KServe storage-config and MinIO credentials applied to rag-models."

echo ""
info "=== Step 10: Download models to MinIO (runs in background) ==="
oc apply -f "$DEPLOY_DIR/models/downloads/download-qwen-job.yaml"
oc apply -f "$DEPLOY_DIR/models/downloads/download-guardian-job.yaml"
oc apply -f "$DEPLOY_DIR/models/downloads/download-bge-m3-job.yaml"
oc apply -f "$DEPLOY_DIR/models/downloads/download-bge-reranker-job.yaml"
warn "Model downloads started — all 4 models downloading from HuggingFace to MinIO."
warn "Qwen 7B GGUF (~4.7GB) ~5 min, Guardian (~5GB) ~10 min, BGE-M3 (~2GB) ~3 min, BGE-reranker (~1.5GB) ~2 min."

echo ""
info "=== Step 11: Apply model ServingRuntimes ==="
oc apply -f "$DEPLOY_DIR/models/servingruntimes/"
info "ServingRuntimes registered: llamacpp-gpu, text-embeddings-inference"

echo ""
info "=== Step 12: Wait for model downloads to complete ==="
warn "Waiting for BGE-M3 download (timeout: 10 min)..."
oc wait --for=condition=Complete job/download-bge-m3 -n rag-models --timeout=600s 2>/dev/null || warn "BGE-M3 download may still be running."
warn "Waiting for BGE-reranker download (timeout: 10 min)..."
oc wait --for=condition=Complete job/download-bge-reranker -n rag-models --timeout=600s 2>/dev/null || warn "BGE-reranker download may still be running."
warn "Waiting for Guardian download (timeout: 20 min)..."
oc wait --for=condition=Complete job/download-guardian -n rag-models --timeout=1200s 2>/dev/null || warn "Guardian download may still be running."
warn "Waiting for Qwen download (timeout: 15 min)..."
oc wait --for=condition=Complete job/download-qwen -n rag-models --timeout=900s 2>/dev/null || warn "Qwen download may still be running."

echo ""
info "=== Step 13: Deploy BGE-M3 embedding InferenceService ==="
oc apply -f "$DEPLOY_DIR/models/embedding/bge-m3.yaml"
warn "KServe storage initializer will pull BGE-M3 from MinIO..."
oc wait --for=condition=Ready inferenceservice/bge-m3 -n rag-models --timeout=300s 2>/dev/null || warn "BGE-M3 InferenceService may still be starting."

echo ""
info "=== Step 14: Deploy BGE reranker InferenceService ==="
oc apply -f "$DEPLOY_DIR/models/reranker/bge-reranker.yaml"
warn "KServe storage initializer will pull BGE-reranker from MinIO..."
oc wait --for=condition=Ready inferenceservice/bge-reranker -n rag-models --timeout=300s 2>/dev/null || warn "BGE-reranker InferenceService may still be starting."

echo ""
info "=== Step 15: Deploy Qwen 7B InferenceService (GPU, llama.cpp CUDA) ==="
oc apply -f "$DEPLOY_DIR/models/vllm/vllm-qwen.yaml"
warn "KServe storage initializer will pull Qwen 7B GGUF from MinIO..."
oc wait --for=condition=Ready inferenceservice/qwen-7b -n rag-models --timeout=600s 2>/dev/null || warn "Qwen InferenceService may still be starting."

echo ""
info "=== Step 16: Deploy Guardian InferenceService (GPU, llama.cpp CUDA) ==="
oc apply -f "$DEPLOY_DIR/models/guardian/guardian.yaml"
warn "KServe storage initializer will pull Guardian from MinIO..."
oc wait --for=condition=Ready inferenceservice/granite-guardian -n rag-models --timeout=300s 2>/dev/null || warn "Guardian InferenceService may still be starting."

echo ""
info "=== Step 17: Build & deploy RAG Orchestrator ==="
build_image "rag-orchestrator" "$SERVICES_DIR/orchestrator"
oc apply -f "$DEPLOY_DIR/apps/orchestrator/rag-orchestrator.yaml"
wait_for_pods "rag-app" "app=rag-orchestrator" 120

echo ""
info "=== Step 18: Deploy Open WebUI ==="
oc apply -f "$DEPLOY_DIR/apps/webui/open-webui.yaml"
wait_for_pods "rag-app" "app=open-webui" 120

echo ""
# info "=== Step 19: Monitoring (vLLM metrics) ==="
# oc apply -f "$DEPLOY_DIR/monitoring/servicemonitor-vllm.yaml"
info "Step 19 (vLLM monitoring) skipped — using llama.cpp, no vLLM metrics."

echo ""
info "=== Step 20: Build ingestion pipeline image ==="
build_image "rag-ingestion" "$SERVICES_DIR/ingestion"
info "Ingestion image built. Run the ingestion job after uploading PDFs to MinIO:"
echo "  oc apply -f $DEPLOY_DIR/apps/ingestion/ingestion-job.yaml"

echo ""
info "=== Step 21: Red Hat Quay (container registry) ==="
oc apply -f "$DEPLOY_DIR/infrastructure/quay/namespace.yaml"
oc apply -f "$DEPLOY_DIR/infrastructure/quay/subscription.yaml"
warn "Waiting for Quay operator to install..."
sleep 120
wait_for_operator "openshift-operators" "Red Hat Quay" 180
info "Creating quay-registry bucket in MinIO for Quay object storage..."
oc exec -n rag-app deploy/minio -- sh -c 'curl -s -o /dev/null http://localhost:9000/quay-registry' 2>/dev/null || \
  oc exec -n rag-app deploy/rag-orchestrator -- python3 -c "
import urllib.request,hashlib,hmac,datetime
h='minio.rag-app.svc:9000';ak='rag-minio-admin';sk='IJK4gek3P93mMjCzanmAnBX2rJEAOU';b='quay-registry';r='us-east-1'
now=datetime.datetime.now(datetime.UTC);ds=now.strftime('%Y%m%d');ad=now.strftime('%Y%m%dT%H%M%SZ')
def s(k,m):return hmac.new(k,m.encode(),hashlib.sha256).digest()
sk2=s(s(s(s(('AWS4'+sk).encode(),ds),r),'s3'),'aws4_request');ph=hashlib.sha256(b'').hexdigest()
c=f'PUT\n/{b}/\n\nhost:{h}\nx-amz-content-sha256:{ph}\nx-amz-date:{ad}\n\nhost;x-amz-content-sha256;x-amz-date\n{ph}'
sc=f'{ds}/{r}/s3/aws4_request';sts=f'AWS4-HMAC-SHA256\n{ad}\n{sc}\n'+hashlib.sha256(c.encode()).hexdigest()
sig=hmac.new(sk2,sts.encode(),hashlib.sha256).hexdigest()
rq=urllib.request.Request(f'http://{h}/{b}/',method='PUT');rq.add_header('Host',h);rq.add_header('x-amz-date',ad)
rq.add_header('x-amz-content-sha256',ph);rq.add_header('Authorization',f'AWS4-HMAC-SHA256 Credential={ak}/{sc}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={sig}')
try:urllib.request.urlopen(rq);print('Bucket created')
except Exception as e:print(f'Bucket exists or error: {e}')
" 2>/dev/null
oc apply -f "$DEPLOY_DIR/infrastructure/quay/quay-config-bundle.yaml"
oc apply -f "$DEPLOY_DIR/infrastructure/quay/quay-registry.yaml"
warn "Quay registry deploying (~3-5 min for all components to start)."

echo ""
info "=== Step 22: CI/CD Pipeline Infrastructure ==="
oc apply -f "$DEPLOY_DIR/infrastructure/pipelines/subscription.yaml"
warn "Waiting for OpenShift Pipelines operator to install..."
sleep 60
wait_for_operator "openshift-operators" "Red Hat OpenShift Pipelines" 180
oc apply -f "$PROJECT_ROOT/pipelines/namespace.yaml"
oc apply -f "$PROJECT_ROOT/pipelines/rbac.yaml"
oc apply -f "$PROJECT_ROOT/pipelines/webhook-secret.yaml"
oc apply -f "$PROJECT_ROOT/pipelines/pipeline.yaml"
warn "Waiting for Tekton Triggers CRDs to be established..."
oc wait --for=condition=Established crd/eventlisteners.triggers.tekton.dev --timeout=120s
oc apply -f "$PROJECT_ROOT/pipelines/triggers.yaml"
info "Tekton pipeline infrastructure deployed."

echo ""
info "============================================"
info "  DEPLOYMENT COMPLETE"
info "============================================"
echo ""
info "Endpoints:"

WEBUI_URL=$(oc get route open-webui -n rag-app -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
MINIO_URL=$(oc get route minio-console -n rag-app -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
RHOAI_URL=$(oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
QUAY_URL=$(oc get route rag-registry-quay -n quay -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
WEBHOOK_URL=$(oc get route rag-webhook -n rag-pipelines -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
OCP_URL=$(oc whoami --show-console 2>/dev/null || echo "pending")

echo "  Open WebUI:        https://$WEBUI_URL"
echo "  MinIO Console:     https://$MINIO_URL"
echo "  Quay Registry:     https://$QUAY_URL"
echo "  RHOAI Dashboard:   https://$RHOAI_URL"
echo "  Webhook (CI/CD):   https://$WEBHOOK_URL"
echo "  OpenShift Console: $OCP_URL"
echo ""
info "Next: Run scripts/validate.sh to verify all components."
echo ""
info "Optional — enable GitOps (Argo CD):"
echo "  oc apply -f $DEPLOY_DIR/infrastructure/gitops/subscription.yaml"
echo "  # Wait ~2 min, then:"
echo "  oc apply -f $DEPLOY_DIR/infrastructure/gitops/argocd-application.yaml"
echo ""
info "CI/CD — configure GitHub webhook:"
echo "  1. Go to your GitHub repo → Settings → Webhooks → Add webhook"
echo "  2. Payload URL: https://$WEBHOOK_URL"
echo "  3. Content type: application/json"
echo "  4. Secret: (match the value in pipelines/webhook-secret.yaml)"
echo "  5. Events: Just the push event"
echo ""
info "Manual build triggers:"
echo "  oc create -f pipelines/pipelinerun-orchestrator.yaml -n rag-pipelines"
echo "  oc create -f pipelines/pipelinerun-ingestion.yaml -n rag-pipelines"
