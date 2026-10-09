#!/bin/bash
set -uo pipefail

# ============================================================
# RAG PoC — Validation Script
# Run after deploy.sh to verify all components are working.
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0

check() {
    local name=$1
    shift
    if "$@" &>/dev/null; then
        echo -e "  ${GREEN}PASS${NC}  $name"
        ((PASS++))
    else
        echo -e "  ${RED}FAIL${NC}  $name"
        ((FAIL++))
    fi
}

echo ""
echo "============================================"
echo "  RAG PoC — Component Validation"
echo "============================================"
echo ""

# --- Cluster ---
echo "--- Cluster ---"
check "OpenShift reachable" oc get nodes
check "Single node (SNO)" test "$(oc get nodes --no-headers | wc -l | tr -d ' ')" -eq 1
check "Node is Ready" oc wait --for=condition=Ready node --all --timeout=5s

# --- GPU ---
echo ""
echo "--- GPU ---"
check "GPU Operator pods running" oc get pods -n nvidia-gpu-operator --field-selector=status.phase=Running -o name
check "nvidia.com/gpu on node" bash -c "oc describe nodes | grep -q 'nvidia.com/gpu'"
check "Device plugin config" oc get configmap device-plugin-config -n nvidia-gpu-operator

# --- RHOAI ---
echo ""
echo "--- RHOAI ---"
check "RHOAI operator running" oc get pods -n redhat-ods-operator --field-selector=status.phase=Running -o name
check "DataScienceCluster ready" oc get datascienceclusters default-dsc
check "RHOAI dashboard route" oc get route rhods-dashboard -n redhat-ods-applications

# --- Postgres ---
echo ""
echo "--- PostgreSQL ---"
PG_POD=$(oc get pods -n rag-app -l postgres-operator.crunchydata.com/role=master -o name 2>/dev/null | head -1)
check "Postgres master running" test -n "$PG_POD"
check "pgvector extension" oc exec -n rag-app "$PG_POD" -- psql -U postgres -d rag-db -c "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null
check "Chunks table exists" oc exec -n rag-app "$PG_POD" -- psql -U postgres -d rag-db -c "SELECT count(*) FROM chunks;" 2>/dev/null

# --- MinIO ---
echo ""
echo "--- MinIO ---"
check "MinIO pod running" oc get pods -n rag-app -l app=minio --field-selector=status.phase=Running -o name
check "MinIO route" oc get route minio-console -n rag-app

# --- Model ServingRuntimes ---
echo ""
echo "--- KServe ServingRuntimes ---"
# check "vLLM GPU runtime" oc get servingruntime vllm-gpu -n rag-models
# check "llama.cpp CPU runtime" oc get servingruntime llamacpp-cpu -n rag-models
check "llama.cpp GPU runtime" oc get servingruntime llamacpp-gpu -n rag-models
check "TEI runtime" oc get servingruntime text-embeddings-inference -n rag-models

# --- Embedding ---
echo ""
echo "--- BGE-M3 Embedding (InferenceService) ---"
check "Embedding InferenceService exists" oc get inferenceservice bge-m3 -n rag-models
check "Embedding pod running" oc get pods -n rag-models -l serving.kserve.io/inferenceservice=bge-m3 --field-selector=status.phase=Running -o name
EMB_POD=$(oc get pods -n rag-models -l serving.kserve.io/inferenceservice=bge-m3 --field-selector=status.phase=Running -o name 2>/dev/null | head -1)
if [ -n "$EMB_POD" ]; then
    EMB_RESULT=$(oc exec -n rag-models "$EMB_POD" -- curl -s http://localhost:8080/embed \
      -H "Content-Type: application/json" \
      -d '{"inputs": "test query"}' 2>/dev/null || echo "")
    check "Embedding returns vectors" bash -c "echo '$EMB_RESULT' | python3 -c 'import sys,json; d=json.load(sys.stdin); assert len(d[0])==1024' 2>/dev/null"
fi

# --- Reranker ---
echo ""
echo "--- BGE Reranker (InferenceService) ---"
check "Reranker InferenceService exists" oc get inferenceservice bge-reranker -n rag-models
check "Reranker pod running" oc get pods -n rag-models -l serving.kserve.io/inferenceservice=bge-reranker --field-selector=status.phase=Running -o name
RERANK_POD=$(oc get pods -n rag-models -l serving.kserve.io/inferenceservice=bge-reranker --field-selector=status.phase=Running -o name 2>/dev/null | head -1)
if [ -n "$RERANK_POD" ]; then
    RERANK_RESULT=$(oc exec -n rag-models "$RERANK_POD" -- curl -s http://localhost:8080/rerank \
      -H "Content-Type: application/json" \
      -d '{"query":"pump torque","texts":["Bolt torque is 45Nm","Change oil monthly"]}' 2>/dev/null || echo "")
    check "Reranker returns scores" bash -c "echo '$RERANK_RESULT' | python3 -c 'import sys,json; d=json.load(sys.stdin); assert len(d)>0' 2>/dev/null"
fi

# --- Qwen 7B (llama.cpp CPU) ---
echo ""
echo "--- Qwen 7B (InferenceService, llama.cpp GPU) ---"
check "Qwen InferenceService exists" oc get inferenceservice qwen-7b -n rag-models
check "Qwen pod running" oc get pods -n rag-models -l serving.kserve.io/inferenceservice=qwen-7b --field-selector=status.phase=Running -o name
LLM_POD=$(oc get pods -n rag-models -l serving.kserve.io/inferenceservice=qwen-7b --field-selector=status.phase=Running -o name 2>/dev/null | head -1)
if [ -n "$LLM_POD" ]; then
    LLM_RESULT=$(oc exec -n rag-models "$LLM_POD" -- curl -s http://localhost:8080/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"model":"qwen-7b","messages":[{"role":"user","content":"Say hello in one word."}],"max_tokens":5}' 2>/dev/null || echo "")
    check "LLM inference works" bash -c "echo '$LLM_RESULT' | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d[\"choices\"][0][\"message\"][\"content\"]' 2>/dev/null"
fi

# --- Guardian ---
echo ""
echo "--- Guardian Granite 3.3 8B (InferenceService) ---"
check "Guardian InferenceService exists" oc get inferenceservice granite-guardian -n rag-models
check "Guardian pod running" oc get pods -n rag-models -l serving.kserve.io/inferenceservice=granite-guardian --field-selector=status.phase=Running -o name
GUARD_POD=$(oc get pods -n rag-models -l serving.kserve.io/inferenceservice=granite-guardian --field-selector=status.phase=Running -o name 2>/dev/null | head -1)
if [ -n "$GUARD_POD" ]; then
    GUARD_RESULT=$(oc exec -n rag-models "$GUARD_POD" -- curl -s http://localhost:8080/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"messages":[{"role":"user","content":"Is this grounded? Context: Torque is 95Nm. Answer: Torque is 95Nm."}],"max_tokens":10}' 2>/dev/null || echo "")
    check "Guardian inference works" bash -c "test -n '$GUARD_RESULT'"
fi

# --- RAG Orchestrator ---
echo ""
echo "--- RAG Orchestrator ---"
check "Orchestrator pod running" oc get pods -n rag-app -l app=rag-orchestrator --field-selector=status.phase=Running -o name
ORCH_HEALTH=$(oc exec -n rag-app deploy/rag-orchestrator -- curl -s http://localhost:8000/health 2>/dev/null || echo "")
check "Orchestrator health endpoint" bash -c "echo '$ORCH_HEALTH' | grep -q 'ok'"
ORCH_MODELS=$(oc exec -n rag-app deploy/rag-orchestrator -- curl -s http://localhost:8000/v1/models 2>/dev/null || echo "")
check "Orchestrator serves model list" bash -c "echo '$ORCH_MODELS' | grep -q 'qwen-7b'"

# --- Open WebUI ---
echo ""
echo "--- Open WebUI ---"
check "Open WebUI pod running" oc get pods -n rag-app -l app=open-webui --field-selector=status.phase=Running -o name
check "Open WebUI route" oc get route open-webui -n rag-app
WEBUI_HOST=$(oc get route open-webui -n rag-app -o jsonpath='{.spec.host}' 2>/dev/null || echo "")
if [ -n "$WEBUI_HOST" ]; then
    check "Open WebUI responds" curl -sk -o /dev/null -w "%{http_code}" "https://$WEBUI_HOST" | grep -q "200"
fi

# --- Quay ---
echo ""
echo "--- Red Hat Quay ---"
QUAY_PODS=$(oc get pods -n quay --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')
check "Quay pods running" test "$QUAY_PODS" -gt 0
check "Quay route" oc get route rag-registry-quay -n quay

# --- GitOps (optional — only checked if installed) ---
if oc get namespace openshift-gitops &>/dev/null; then
    echo ""
    echo "--- OpenShift GitOps ---"
    GITOPS_PODS=$(oc get pods -n openshift-gitops --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')
    check "GitOps pods running" test "$GITOPS_PODS" -gt 0
    ARGOCD_APP=$(oc get application rag-poc -n openshift-gitops -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "")
    if [ -n "$ARGOCD_APP" ]; then
        check "Argo CD app synced" test "$ARGOCD_APP" = "Synced"
    fi
else
    echo ""
    echo "--- OpenShift GitOps (skipped — not installed) ---"
fi

# --- Tekton Pipelines ---
echo ""
echo "--- Tekton Pipelines ---"
check "Pipeline namespace exists" oc get namespace rag-pipelines
check "Pipeline rag-build exists" oc get pipelines.tekton.dev rag-build -n rag-pipelines
check "EventListener rag-webhook exists" oc get eventlistener rag-webhook -n rag-pipelines
check "EventListener pod running" oc get pods -n rag-pipelines -l eventlistener=rag-webhook --field-selector=status.phase=Running -o name
check "Webhook route exists" oc get route rag-webhook -n rag-pipelines

# --- Summary ---
echo ""
echo "============================================"
TOTAL=$((PASS + FAIL))
echo -e "  Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC} out of $TOTAL checks"
echo "============================================"

if [ $FAIL -gt 0 ]; then
    echo ""
    echo "Failed checks may be due to components still starting."
    echo "Wait a few minutes and re-run: ./validate.sh"
    exit 1
fi
