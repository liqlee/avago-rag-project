# AWS Deployment Guide

## Maintenance Knowledge RAG Chatbot — Single Node OpenShift on AWS

The proposed architecture runs on a single physical server on-premises. This guide simulates that by deploying everything onto a **single EC2 instance** running Single Node OpenShift (SNO) with Red Hat OpenShift AI — the exact platform stack the customer will use in their datacenter.

Two deployment paths are documented: **scripted** (`deploy.sh` — runs all steps automatically) and **manual step-by-step** (for understanding, troubleshooting, or selective deployment).

---

## Phase 0: Prerequisites (Local Workstation)

### 0.1 Request GPU Quota (Do This First — Takes 1-3 Business Days)

AWS does not grant GPU instance access by default. Request quota before anything else.

```
AWS Console → Service Quotas → Amazon EC2
Search: "Running On-Demand G and VT instances"
Request increase to at least 64 vCPUs (for g5.16xlarge)
```

Verify your current quota:

```bash
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-DB2E81BA \
  --query 'Quota.Value'
```

### 0.2 Choose Your EC2 Instance

Everything runs on **one VM**. It needs enough CPU, RAM, and GPU to host the OpenShift control plane, all AI models, the database, and the application services.

| Instance | GPU | VRAM | vCPU | RAM | $/hr | Notes |
|----------|-----|------|------|-----|------|-------|
| g5.8xlarge | 1x A10G | 24 GB | 32 | 128 GB | ~$2.45 | Minimum for SNO + all workloads |
| **g5.16xlarge** | 1x A10G | 24 GB | 64 | 256 GB | ~$4.10 | **Recommended — matches proposal spec** (64 cores, 256GB, 1 GPU) |
| g5.12xlarge | 4x A10G | 96 GB | 48 | 192 GB | ~$5.67 | Multiple GPUs — can run 14B in FP16 on one, Guardian on another |
| p4d.24xlarge | 8x A100 | 320 GB | 96 | 1.1 TB | ~$32.77 | Exact proposal GPU (A100) — expensive, use for final customer demo only |

**g5.16xlarge is the closest match to the proposal's hardware spec:**
- 64 vCPU ↔ 64 cores (2x EPYC 9354)
- 256 GB RAM ↔ 256 GB DDR5
- 1x A10G 24GB ↔ 1x A100 80GB (smaller VRAM — use AWQ quantized models)

### 0.3 Install CLI Tools

```bash
# AWS CLI
brew install awscli    # macOS
aws configure          # Enter access key, secret key, region (us-east-2)

# OpenShift CLI
brew install openshift-cli

# OpenShift Installer
# Download from https://console.redhat.com/openshift/downloads
# Select "OpenShift Container Platform" → "Installer" for your OS
# Place openshift-install on your PATH

# Verify
oc version --client
openshift-install version
aws sts get-caller-identity
```

### 0.4 Create Route53 Hosted Zone

The OpenShift installer needs a Route53 **public hosted zone** for the base domain. It validates this via the AWS API — the domain does **not** need to be registered or publicly resolvable. You just need the hosted zone to exist in your AWS account.

```bash
# Check if the hosted zone already exists
aws route53 list-hosted-zones \
  --query 'HostedZones[?Name==`poc.liqlee.com.`].[Name,Id]' --output table

# If empty, create it
aws route53 create-hosted-zone \
  --name poc.liqlee.com \
  --caller-reference "rag-poc-$(date +%s)" \
  --query 'HostedZone.Id' --output text
```

> **Note:** Domain registration and NS delegation are NOT required. The installer creates DNS records in this zone and validates them by querying Route53 directly (via AWS API), not via public DNS. After install, you will use `/etc/hosts` entries on your workstation to reach the cluster endpoints.

### 0.5 Create SSH Key Pair

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rag-poc-key -N ""

# Upload public key to AWS (or use an existing key pair)
aws ec2 import-key-pair \
  --key-name rag-poc-key \
  --public-key-material fileb://~/.ssh/rag-poc-key.pub

# Load the key into your SSH agent — the installer needs this
# to collect logs from the bootstrap machine if something fails
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/rag-poc-key
```

### 0.6 Download Pull Secret

Go to https://console.redhat.com/openshift/downloads#tool-pull-secret and save the JSON to `~/pull-secret.json`.

The project includes a ready-to-use install config at `cluster/install-config.yaml`. If you are using a different Red Hat account, replace the `pullSecret` and `sshKey` values in that file with your own.

---

## Phase 1: Install Single Node OpenShift (~40-50 min)

### 1.1 Prepare Install Directory

```bash
mkdir -p ~/rag-sno && cd ~/rag-sno
```

### 1.2 Copy and Edit Install Config

The project provides a config at `cluster/install-config.yaml` with these settings:

| Setting | Value |
|---------|-------|
| Cluster name | `rag-poc` |
| Base domain | `poc.liqlee.com` (must match your Route53 hosted zone from step 0.4) |
| Architecture | `amd64` (explicit — prevents arm64 installer binary mismatch) |
| Instance type | `g5.16xlarge` (64 vCPU, 256 GB RAM, 1x A10G GPU) |
| Region | `us-east-2` |
| Root volume | 500 GB gp3, 6000 IOPS |
| Workers | 0 (SNO — single node is both master and worker) |
| Networking | OVNKubernetes |

```bash
# Copy the install config into your working directory
cp /path/to/avago-rag-project/cluster/install-config.yaml .

# Edit pullSecret and sshKey if using a different Red Hat account
vi install-config.yaml

# IMPORTANT: Back it up — the installer CONSUMES the original
cp install-config.yaml install-config.yaml.bak
```

If you need to create the config from scratch instead:

```bash
cat << 'EOF' > install-config.yaml
apiVersion: v1
metadata:
  name: rag-poc
baseDomain: poc.liqlee.com
networking:
  networkType: OVNKubernetes
  clusterNetwork:
    - cidr: 10.128.0.0/14
      hostPrefix: 23
  serviceNetwork:
    - 172.30.0.0/16
controlPlane:
  name: master
  replicas: 1
  architecture: amd64
  platform:
    aws:
      type: g5.16xlarge
      rootVolume:
        size: 500
        type: gp3
        iops: 6000
compute:
  - name: worker
    replicas: 0
platform:
  aws:
    region: us-east-2
pullSecret: 'PASTE_YOUR_PULL_SECRET_HERE'
sshKey: 'PASTE_YOUR_SSH_PUBLIC_KEY_HERE'
EOF
```

**Before proceeding:** Remove any stale `/etc/hosts` entries from a previous cluster install. Old entries will cause the installer to connect to dead IPs and time out.

```bash
# Remove old /etc/hosts entries for this cluster (safe — only deletes matching lines)
sudo sed -i.bak '/rag-poc\.poc\.liqlee\.com/d' /etc/hosts
grep rag-poc /etc/hosts  # should return nothing

# Verify the Route53 hosted zone exists
aws route53 list-hosted-zones \
  --query 'HostedZones[?Name==`poc.liqlee.com.`].Id' --output text
# Must return a zone ID — if empty, go back to step 0.4
```

### 1.3 Run the Installer

```bash
# Ensure SSH key is loaded (installer uses it for bootstrap log collection)
ssh-add -l | grep -q rag-poc || ssh-add ~/.ssh/rag-poc-key

openshift-install create cluster --dir=. --log-level=info
```

This takes **40-50 minutes**. The installer:
1. Creates VPC, subnet, security group, DNS in Route53
2. Launches ONE g5.16xlarge EC2 instance
3. Installs RHEL CoreOS + OpenShift on it
4. Configures it as both control plane AND worker
5. The single node schedules ALL workloads

### 1.4 Set Up `/etc/hosts` and Verify the Cluster

Since `poc.liqlee.com` is not a publicly registered domain, your workstation cannot resolve the cluster hostnames via DNS. You must add `/etc/hosts` entries **before** any `oc` commands will work.

```bash
# Set KUBECONFIG
export KUBECONFIG=~/rag-sno/auth/kubeconfig

# Persist KUBECONFIG across terminal sessions
echo 'export KUBECONFIG=~/rag-sno/auth/kubeconfig' >> ~/.zshrc  # or ~/.bashrc
```

**Add `/etc/hosts` entries:**

```bash
# Get the Route53 zone ID
ZONE_ID=$(aws route53 list-hosted-zones \
  --query 'HostedZones[?Name==`poc.liqlee.com.`].Id' --output text | sed 's|/hostedzone/||')

# Look up the NLB/ELB DNS names the installer created, then resolve to IPs
API_LB=$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --query 'ResourceRecordSets[?Name==`api.rag-poc.poc.liqlee.com.`].AliasTarget.DNSName' \
  --output text)
API_IP=$(dig +short "$API_LB" | head -1)

APPS_LB=$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  --query 'ResourceRecordSets[?contains(Name, `apps.rag-poc`)].AliasTarget.DNSName' \
  --output text)
APPS_IP=$(dig +short "$APPS_LB" | head -1)

echo "API IP:  $API_IP"
echo "Apps IP: $APPS_IP"

# Add entries to /etc/hosts
sudo tee -a /etc/hosts << EOF

# OpenShift SNO cluster — rag-poc (added $(date +%Y-%m-%d))
${API_IP} api.rag-poc.poc.liqlee.com
${APPS_IP} console-openshift-console.apps.rag-poc.poc.liqlee.com
${APPS_IP} oauth-openshift.apps.rag-poc.poc.liqlee.com
${APPS_IP} open-webui-rag-app.apps.rag-poc.poc.liqlee.com
${APPS_IP} minio-console-rag-app.apps.rag-poc.poc.liqlee.com
${APPS_IP} minio-api-rag-app.apps.rag-poc.poc.liqlee.com
${APPS_IP} rhods-dashboard-redhat-ods-applications.apps.rag-poc.poc.liqlee.com
${APPS_IP} rag-registry-quay-quay.apps.rag-poc.poc.liqlee.com
${APPS_IP} openshift-gitops-server-openshift-gitops.apps.rag-poc.poc.liqlee.com
EOF
```

> **Important:** If you destroy and reinstall the cluster, you MUST remove these entries before reinstalling (step 1.2 covers this). New installs get new load balancer IPs — stale entries cause the installer to time out with `dial tcp ...:6443: i/o timeout`.

**Verify the cluster:**

```bash
# Should show ONE node with roles: control-plane, master, worker
oc get nodes
# NAME                          STATUS   ROLES                         AGE   VERSION
# ip-10-0-xx-xx.ec2.internal    Ready    control-plane,master,worker   5m    v1.29.x

echo "Console: https://console-openshift-console.apps.rag-poc.poc.liqlee.com"
echo "Password: $(cat ~/rag-sno/auth/kubeadmin-password)"
```

**OpenShift Web Console:** Open the URL above, log in with `kubeadmin` and the printed password.

### 1.5 Troubleshooting Install Failures

If the installer times out with `dial tcp ...:6443: i/o timeout`:

**1. Stale `/etc/hosts` entries — most common cause on reinstall**

If you previously installed a cluster and added `/etc/hosts` entries, those old IPs override DNS resolution. The installer resolves `api.rag-poc.poc.liqlee.com` via your system resolver, which checks `/etc/hosts` first — if it points to a dead IP from a destroyed cluster, the installer times out connecting to nothing.

```bash
# Check for stale entries
grep rag-poc /etc/hosts

# If any entries exist, remove them and retry the install
sudo sed -i.bak '/rag-poc\.poc\.liqlee\.com/d' /etc/hosts
```

**2. Architecture mismatch (arm64 installer on amd64 config)**

If the install log shows `amd64 controlPlane can't use arm64 release payload`, you downloaded the wrong installer binary. The install-config specifies `architecture: amd64` but the installer binary is for arm64.

```bash
# Verify your installer binary architecture
file $(which openshift-install)
# Should include: x86_64 or amd64

# The install-config.yaml should have:
#   controlPlane:
#     architecture: amd64
```

Download the correct **linux/amd64** installer from https://console.redhat.com/openshift/downloads.

**3. Security group blocking port 6443**

```bash
# Test connectivity from your machine
curl -vk --connect-timeout 5 https://api.rag-poc.poc.liqlee.com:6443

# If timeout, allow your IP through the LB security group
MY_IP=$(curl -s ifconfig.me)
LB_SG=$(aws ec2 describe-security-groups --region us-east-2 \
  --filters "Name=group-name,Values=*rag-poc*lb*" \
  --query 'SecurityGroups[0].GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id "$LB_SG" --protocol tcp --port 6443 \
  --cidr "${MY_IP}/32" --region us-east-2
```

**4. Can't collect bootstrap logs (SSH auth failure)**

```bash
# Load the SSH key into your agent
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/rag-poc-key
```

**To clean up a failed install and retry:**

```bash
cd ~/rag-sno

# Remove stale /etc/hosts entries FIRST
sudo sed -i.bak '/rag-poc\.poc\.liqlee\.com/d' /etc/hosts

# Ensure SSH key is loaded
ssh-add -l | grep -q rag-poc || ssh-add ~/.ssh/rag-poc-key

# Destroy the failed cluster
openshift-install destroy cluster --dir=. --log-level=info
# Wait for all resources to be deleted, then:
cp install-config.yaml.bak install-config.yaml
openshift-install create cluster --dir=. --log-level=info
```

---

## Phase 2: Deploy All Components (~45-60 min)

### 2.1 Run the Deployment Script

```bash
cd /path/to/avago-rag-project
export KUBECONFIG=~/rag-sno/auth/kubeconfig

./scripts/deploy.sh
```

The script performs pre-flight checks first:
- Verifies `oc` CLI is installed and authenticated
- Confirms cluster connectivity and node count (expects 1 for SNO)
- Checks for GPU visibility on the node

It then deploys all 22 steps sequentially with built-in wait logic between dependent steps. At the end, it prints all endpoint URLs.

The sections below document what the script deploys at each step and how to verify each component.

### 2.2 GPU Stack (Steps 1-2)

**Node Feature Discovery (NFD)** detects hardware features on the node. The **NVIDIA GPU Operator** installs GPU drivers, the device plugin, DCGM exporter (metrics), and the container toolkit.

| Component | Manifest | Namespace |
|-----------|----------|-----------|
| NFD Operator + Instance | `deploy/infrastructure/gpu-operator/nfd-*.yaml` | `openshift-nfd` |
| GPU Operator + ClusterPolicy | `deploy/infrastructure/gpu-operator/*.yaml` | `nvidia-gpu-operator` |

GPU drivers take **5-10 minutes** to build and load after the ClusterPolicy is created.

**Verify:**

```bash
# All GPU pods should be Running or Completed
oc get pods -n nvidia-gpu-operator

# Should show nvidia.com/gpu: 1 under Allocatable
oc describe node $(oc get nodes -o name) | grep -A3 "nvidia.com/gpu"
```

**Console:** Compute → Nodes → click node → Details → scroll to Allocatable → confirm `nvidia.com/gpu: 1`.

### 2.3 RHOAI Stack (Steps 3-4)

Installs Red Hat OpenShift AI and its two operator prerequisites.

> **Note:** The script installs Service Mesh **version 2**, not 3. Version 2 shows a deprecation notice in the console — this is expected. RHOAI's KServe component does not yet support Service Mesh 3.

| Component | Manifest |
|-----------|----------|
| Service Mesh 2 | `deploy/infrastructure/rhoai/servicemesh-subscription.yaml` |
| Serverless | `deploy/infrastructure/rhoai/serverless-subscription.yaml` |
| RHOAI Operator | `deploy/infrastructure/rhoai/rhoai-subscription.yaml` |
| DataScienceCluster | `deploy/infrastructure/rhoai/datasciencecluster.yaml` |

The DataScienceCluster enables Dashboard, KServe, ModelMesh, Data Science Pipelines, and Workbenches. Disabled (not needed for this PoC): CodeFlare, Ray, TrustyAI.

**Verify:**

```bash
oc get csv -n redhat-ods-operator
# Should show "Succeeded"

echo "RHOAI Dashboard: https://$(oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='{.spec.host}')"
```

**Console:** Operators → Installed Operators → filter namespace `redhat-ods-operator` → Red Hat OpenShift AI, Service Mesh 2, Serverless all show "Succeeded".

### 2.4 Data Layer (Steps 5-8)

Creates application namespaces, PostgreSQL database with pgvector, database schema, and MinIO object storage.

**Namespaces** (`deploy/infrastructure/rhoai/namespaces.yaml`):

| Namespace | Purpose |
|-----------|---------|
| `rag-models` | AI model serving pods (vLLM, embedding, reranker, guardian) |
| `rag-app` | Application pods (orchestrator, webui, postgres, minio) |

**Crunchy Postgres** (`deploy/data/postgres/`):

| Setting | Value |
|---------|-------|
| Operator | Crunchy Postgres for Kubernetes (channel `v5`) |
| PostgreSQL version | 16 |
| Replicas | 1 |
| Resources | 4-8 CPU, 8-16 Gi RAM |
| Data volume | 50 Gi |
| Backup volume | 20 Gi |
| Extensions | `pgvector` (`shared_preload_libraries: vector`) |
| Config | `shared_buffers: 2GB`, `effective_cache_size: 6GB`, `work_mem: 64MB` |
| Credentials secret | `rag-db-pguser-postgres` in namespace `rag-app` |

**Database Schema** (`deploy/data/schema/001-init-job.yaml`):

The init-schema job waits for Postgres to be ready, then creates the pgvector extension and all tables:

| Table | Purpose |
|-------|---------|
| `chunks` | Document chunks with `vector(1024)` column, HNSW index (m=16, ef_construction=200), GIN index on JSONB metadata |
| `conversations` | Chat session tracking per user |
| `messages` | Individual messages with `chunks_used`, `guardian_result`, `latency_ms` |
| `feedback` | Thumbs up/down ratings per message |
| `ingestion_logs` | Tracks PDF processing status (pending → processing → completed/failed) |

**MinIO** (`deploy/data/minio/minio.yaml`):

| Setting | Value |
|---------|-------|
| Image | `quay.io/minio/minio:latest` |
| Storage | 100 Gi PVC |
| Console credentials | `rag-minio-admin` / `IJK4gek3P93mMjCzanmAnBX2rJEAOU` |
| API port | 9000 |
| Console port | 9001 |
| Routes | `minio-api` and `minio-console` (TLS edge) |

**Verify:**

```bash
# Postgres running
oc get pods -n rag-app -l postgres-operator.crunchydata.com/cluster=rag-db

# Schema applied
oc get jobs -n rag-app
# init-schema should show Complete

# MinIO running
oc get pods -n rag-app -l app=minio

echo "MinIO Console: https://$(oc get route minio-console -n rag-app -o jsonpath='{.spec.host}')"
# Login: rag-minio-admin / IJK4gek3P93mMjCzanmAnBX2rJEAOU
```

**Console:** Workloads → Pods → namespace `rag-app` → rag-db and minio pods Running. Networking → Routes → `minio-console` → click URL.

### 2.5 AI Models (Steps 9-14)

Downloads all 4 models from Hugging Face to MinIO, registers ServingRuntimes, and deploys all AI serving components via KServe InferenceServices. Models are stored in MinIO and loaded by KServe's storage initializer at pod startup.

**Model Storage Config** (`deploy/models/storage-config.yaml`):

Creates the `storage-config` secret with MinIO S3 credentials for KServe, and the `minio-models` secret used by download jobs.

**Model Downloads** (Kubernetes Jobs, `deploy/models/downloads/`):

| Model | Hugging Face Repo | Format | Size | Job |
|-------|-------------------|--------|------|-----|
| Qwen 2.5 14B Instruct AWQ | `Qwen/Qwen2.5-14B-Instruct-AWQ` | Safetensors (4-bit AWQ) | ~9 GB | `download-qwen` |
| Granite Guardian 3.3 8B | `ibm-granite/granite-guardian-3.3-8b-GGUF` | GGUF Q4_K_M | ~5 GB | `download-guardian` |
| BGE-M3 | `BAAI/bge-m3` | SafeTensors | ~2 GB | `download-bge-m3` |
| BGE-reranker-v2-m3 | `BAAI/bge-reranker-v2-m3` | SafeTensors | ~1.5 GB | `download-bge-reranker` |

Monitor downloads:

```bash
oc logs -n rag-models -f job/download-qwen        # ~15 min
oc logs -n rag-models -f job/download-guardian     # ~10 min
oc logs -n rag-models -f job/download-bge-m3      # ~3 min
oc logs -n rag-models -f job/download-bge-reranker # ~2 min
```

**BGE-M3 Embedding Service** (`deploy/models/embedding/bge-m3.yaml`):

| Setting | Value |
|---------|-------|
| Image | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.6` |
| Model | `BAAI/bge-m3` |
| Output | 1024-dim dense vectors |
| Resources | 4-6 CPU, 4-6 Gi RAM |
| Internal URL | `http://bge-m3-embedding.rag-models.svc:8080` |

Model pre-downloaded to MinIO by the download job; KServe storage initializer loads it at pod startup.

**BGE Reranker Service** (`deploy/models/reranker/bge-reranker.yaml`):

| Setting | Value |
|---------|-------|
| Image | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.6` |
| Model | `BAAI/bge-reranker-v2-m3` |
| Resources | 4-6 CPU, 4-6 Gi RAM |
| Internal URL | `http://bge-reranker.rag-models.svc:8080` |

Cross-encoder model for re-scoring retrieval candidates. Pre-downloaded to MinIO by the download job.

**vLLM — Qwen 14B on GPU** (`deploy/models/vllm/vllm-qwen.yaml`):

| Setting | Value |
|---------|-------|
| Image | `vllm/vllm-openai:latest` |
| Model path | `/mnt/models` (loaded from MinIO by KServe storage initializer) |
| Served model name | `qwen-14b` |
| Quantization | AWQ (4-bit) |
| Max context length | 8192 tokens |
| GPU memory utilization | 90% |
| Resources | 4-8 CPU, 16-32 Gi RAM, 1 GPU |
| Internal URL | `http://vllm-qwen.rag-models.svc:8000` |
| Readiness probe | `/health` (initial delay 120s) |

Takes **2-3 minutes** to load the model into GPU memory.

**Granite Guardian — safety gate** (`deploy/models/guardian/guardian.yaml`):

| Setting | Value |
|---------|-------|
| Image | `ghcr.io/ggerganov/llama.cpp:full-<version>` |
| Server binary | `llama-server` |
| Model file | `granite-guardian-3.3-8b-Q4_K_M.gguf` |
| Context size | 4096 tokens |
| CPU threads | 8 |
| Resources | 8 CPU, 8-10 Gi RAM |
| Internal URL | `http://guardian.rag-models.svc:8080` |

Verifies every answer is grounded in source documentation.

**Verify:**

```bash
# Download jobs complete
oc get jobs -n rag-models
# download-qwen, download-guardian should show Complete

# All model InferenceServices ready
oc get inferenceservice -n rag-models
# bge-m3, bge-reranker, qwen-14b-awq, granite-guardian all show READY=True

# GPU allocated to vLLM
oc describe node $(oc get nodes -o name) | grep nvidia.com/gpu

# Download jobs complete
oc get jobs -n rag-models
# download-qwen, download-guardian, download-bge-m3, download-bge-reranker all Complete
```

**Console:** Workloads → Pods → namespace `rag-models` → all model pods Running. Workloads → Jobs → namespace `rag-models` → all 4 download jobs show Complete.

### 2.6 Application Layer (Steps 15-19)

Deploys the application stack.

**RAG Orchestrator** (`deploy/apps/orchestrator/rag-orchestrator.yaml`):

The custom Python service that ties everything together — query rewriting, hybrid retrieval, re-ranking, generation, and safety verification.

| Setting | Value |
|---------|-------|
| Image | `image-registry.openshift-image-registry.svc:5000/rag-app/rag-orchestrator:latest` |
| Resources | 1-2 CPU, 1-2 Gi RAM |
| API | OpenAI-compatible (`/health`, `/v1/models`, `/v1/chat/completions`) |
| Internal URL | `http://rag-orchestrator.rag-app.svc:8000` |

Connects to all backend services:

| Upstream Service | Environment Variable | Internal URL |
|-----------------|---------------------|-------------|
| vLLM (Qwen 14B) | `VLLM_BASE_URL` | `http://vllm-qwen.rag-models.svc:8000` |
| BGE-M3 Embedding | `EMBEDDING_URL` | `http://bge-m3-embedding.rag-models.svc:8080` |
| BGE Reranker | `RERANKER_URL` | `http://bge-reranker.rag-models.svc:8080` |
| Guardian | `GUARDIAN_URL` | `http://guardian.rag-models.svc:8080` |
| PostgreSQL | `PG_HOST`, `PG_PORT`, etc. | Via Crunchy secret `rag-db-pguser-postgres` |

**Open WebUI** (`deploy/apps/webui/open-webui.yaml`):

| Setting | Value |
|---------|-------|
| Image | `ghcr.io/open-webui/open-webui:main` |
| API backend | `http://rag-orchestrator.rag-app.svc:8000/v1` (routes through orchestrator, NOT directly to vLLM) |
| Auth | Built-in (signup allowed — first user registered becomes admin) |
| Storage | 10 Gi PVC |
| Resources | 1-2 CPU, 2-4 Gi RAM |

**Monitoring** (`deploy/monitoring/servicemonitor-vllm.yaml`):

Prometheus `ServiceMonitor` scraping vLLM's `/metrics` endpoint every 15 seconds. Metrics flow into OpenShift's built-in monitoring stack.

**Console:** Observe → Metrics → query `vllm_*` for GPU utilization, request latency, token throughput.

**Red Hat Quay** (`deploy/infrastructure/quay/`):

Private container registry for custom images (rag-orchestrator, rag-ingestion). Required for air-gapped on-prem deployments. Deployed with Clair scanning, HPA, mirror, and monitoring disabled (lightweight config for SNO).

**Verify:**

```bash
# All application pods running
oc get pods -n rag-app
# rag-orchestrator, open-webui, minio, rag-db should all show Running

# Orchestrator health
oc exec -n rag-app deploy/rag-orchestrator -- curl -s http://localhost:8000/health

# WebUI URL
echo "Open WebUI: https://$(oc get route open-webui -n rag-app -o jsonpath='{.spec.host}')"

# Quay URL
echo "Quay: https://$(oc get route rag-registry-quay -n quay -o jsonpath='{.spec.host}')"
```

**Console:** Networking → Routes → namespace `rag-app` → `open-webui` → click URL. First visit: create an admin account, select `qwen-14b` model, ask a maintenance question.

### 2.7 Troubleshooting Deployment

**ImagePullBackOff — container registry issues**

If a pod fails with `ImagePullBackOff`, check the image source. Docker Hub (`docker.io`) has pull rate limits — the manifests use `quay.io` and `ghcr.io` mirrors to avoid this. Some image tags may also become unavailable over time (e.g., `ghcr.io/ggerganov/llama.cpp:server` was replaced with versioned `full-<hash>` tags). Update the manifest to a current tag and re-apply.

**CrashLoopBackOff — permission denied in containers**

OpenShift runs containers as a random non-root UID by default. Containers that write to `/data`, `/.local`, or `/.cache` will fail with permission errors. The manifests include `emptyDir` volumes and `HOME=/tmp` environment variables to work around this. If you see this error in a new container, add a writable `emptyDir` volume for the directory it needs.

**CreateContainerConfigError — secret not found**

Pods that reference a Kubernetes Secret (e.g., `rag-db-pguser-postgres`) will fail with `CreateContainerConfigError` if the secret doesn't exist yet. This means the upstream dependency (e.g., Crunchy Postgres) hasn't finished deploying. Wait for it to complete, then delete and re-create the failed job.

**Failed Jobs — Kubernetes Jobs are immutable**

Once created, a Job spec cannot be updated. To retry after fixing a manifest:

```bash
oc delete job <job-name> -n <namespace>
oc apply -f <manifest-file>
```

---

## Phase 3: Validate the Deployment

```bash
./scripts/validate.sh
```

This runs **~40 automated checks** across all components:

| Category | Checks |
|----------|--------|
| **Cluster** | OpenShift reachable, single node (SNO), node Ready |
| **GPU** | GPU Operator pods running, `nvidia.com/gpu` on node |
| **RHOAI** | Operator running, DataScienceCluster ready, dashboard route |
| **PostgreSQL** | Master pod running, pgvector extension loaded, chunks table exists |
| **MinIO** | Pod running, console route accessible |
| **Embedding** | Pod running, returns 1024-dim vectors for test input |
| **Reranker** | Pod running, returns relevance scores for test pairs |
| **vLLM** | Pod running, GPU allocated, LLM inference returns response |
| **Guardian** | Pod running, inference returns result |
| **RAG Orchestrator** | Pod running, `/health` returns ok, `/v1/models` lists qwen-14b |
| **Tekton Pipelines** | Pipeline namespace, rag-build pipeline, EventListener, webhook route |
| **Open WebUI** | Pod running, route exists, HTTPS returns 200 |
| **Quay** | Pods running, route exists |
| **GitOps** | GitOps pods running, Argo CD app synced (if installed) |

Output: `PASS/FAIL` per check with a summary count. If checks fail, wait a few minutes for components to finish starting and re-run.

Manual spot checks:

```bash
# Confirm everything runs on ONE node
oc get pods -A -o wide | grep -E "rag-models|rag-app" | awk '{print $1, $2, $4, $8}'
# Every pod should show the SAME node name in the last column

# Node resource usage
oc adm top node

# GPU allocation
oc describe node $(oc get nodes -o name) | grep -A5 "Allocated resources"
oc describe node $(oc get nodes -o name) | grep nvidia
```

---

## Phase 4: Enable GitOps with Argo CD (Optional)

Makes the deployment declarative — Argo CD continuously syncs the cluster state to match the `deploy/` directory in the Git repo.

### 4.1 Install OpenShift GitOps Operator

```bash
oc apply -f deploy/infrastructure/gitops/subscription.yaml
```

Wait ~2 minutes for the operator to install.

### 4.2 Create the Argo CD Application

Then apply:

```bash
oc apply -f argocd-application.yaml
```

The Application spec:
- **Source:** `deploy/` directory (recursive), excluding `*.sh`, `*.sql`
- **Sync policy:** Automated with pruning and self-heal
- **Sync options:** CreateNamespace, ServerSideApply, RespectIgnoreDifferences
- **Retry:** Up to 20 retries with exponential backoff (30s → 10m max)

Sync waves (annotated in each manifest via `argocd.argoproj.io/sync-wave`) control deployment order.

### 4.3 Access Argo CD Console

```bash
# URL
echo "https://$(oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='{.spec.host}')"

# Admin password
oc get secret openshift-gitops-cluster -n openshift-gitops \
  -o jsonpath='{.data.admin\.password}' | base64 -d
```

---

## Phase 5: Document Ingestion (Post-Deploy)

Once the platform is running, ingest scanned maintenance PDFs into the RAG system.

### 5.1 Upload PDFs to MinIO

Open the MinIO Console (see [Endpoints Summary](#endpoints-summary)), create a bucket (e.g., `manuals`), and upload scanned PDF files.

### 5.2 Run the Ingestion Pipeline

```bash
oc apply -f deploy/apps/ingestion/ingestion-job.yaml
```

The ingestion job (`image-registry.openshift-image-registry.svc:5000/rag-app/rag-ingestion:latest`):
1. Reads PDFs from MinIO object storage
2. Processes them with Docling (OCR, layout analysis, table extraction)
3. Chunks by document section (procedures, tables, warnings, references)
4. Embeds each chunk via BGE-M3 → 1024-dim dense vectors
5. Stores chunks with vectors and metadata in PostgreSQL + pgvector
6. Logs ingestion status to the `ingestion_logs` table

Environment variables to customize:

| Variable | Default | Purpose |
|----------|---------|---------|
| `EQUIPMENT_ID` | `""` (all) | Filter to specific equipment ID |
| `EQUIPMENT_NAME` | `""` | Human-readable equipment name for metadata |
| `FORCE_REPROCESS` | `"false"` | Re-process already-ingested files |

Monitor progress:

```bash
oc logs -n rag-app -f job/ingest-manuals
```

---

## Endpoints Summary

> **Prerequisite:** These URLs require `/etc/hosts` entries configured in step 1.4. If you skipped that step or reinstalled the cluster, the hostnames won't resolve.

After deployment, all services are available at these URLs:

```bash
echo "Open WebUI:        https://$(oc get route open-webui -n rag-app -o jsonpath='{.spec.host}')"
echo "MinIO Console:     https://$(oc get route minio-console -n rag-app -o jsonpath='{.spec.host}')"
echo "Quay Registry:     https://$(oc get route rag-registry-quay -n quay -o jsonpath='{.spec.host}')"
echo "RHOAI Dashboard:   https://$(oc get route rhods-dashboard -n redhat-ods-applications -o jsonpath='{.spec.host}')"
echo "OpenShift Console: $(oc whoami --show-console)"
```

| Service | Default Credentials |
|---------|-------------------|
| OpenShift Console | `kubeadmin` / `cat ~/rag-sno/auth/kubeadmin-password` |
| Open WebUI | Create account on first visit (first user = admin) |
| MinIO Console | `rag-minio-admin` / `IJK4gek3P93mMjCzanmAnBX2rJEAOU` |
| RHOAI Dashboard | Same as OpenShift (`kubeadmin`) |
| Argo CD (if installed) | `admin` / decode from secret (see Phase 4.3) |

---

## Cost Management

### Monthly Cost

| Component | Cost |
|-----------|-----:|
| g5.16xlarge (24/7) | ~$2,952/mo |
| ELB + Route53 + EBS | ~$150/mo |
| **Total** | **~$3,100/mo** |

### Stop/Start to Save Money

```bash
# Get the SNO instance ID
SNO_INSTANCE=$(oc get machines -n openshift-machine-api \
  -o jsonpath='{.items[0].status.providerStatus.instanceId}')

# Stop when not in use
aws ec2 stop-instances --instance-ids $SNO_INSTANCE
# Cost while stopped: ~$50/month (EBS storage only)

# Resume later
aws ec2 start-instances --instance-ids $SNO_INSTANCE
# NOTE: OpenShift etcd may need recovery after a stop/start.
# For SNO this usually self-heals on boot, but allow 5-10 min.
```

> **`/etc/hosts` after restart:** The NLB/ELB IPs may change after a stop/start. If cluster endpoints are unreachable after restarting, re-run the `/etc/hosts` setup commands from step 1.4 to pick up the new IPs.

### Full Teardown

```bash
# Delete all application resources first (prompts for confirmation)
./scripts/teardown.sh    # Type 'DELETE' to confirm

# Destroy the cluster and all AWS resources
cd ~/rag-sno
openshift-install destroy cluster --dir=. --log-level=info

# Clean up /etc/hosts — stale entries will break future installs
sudo sed -i.bak '/rag-poc\.poc\.liqlee\.com/d' /etc/hosts
```

The teardown script deletes resources in reverse order (GitOps → Quay → apps → models → namespaces), then instructs you to run `openshift-install destroy` to remove the EC2 instance, VPC, DNS, and all remaining AWS resources. The `/etc/hosts` cleanup prevents stale entries from causing `i/o timeout` errors on the next install.

---

## Architecture Summary

All components on one `g5.16xlarge` node:

```
GPU (A10G 24GB):
└── vLLM → Qwen 2.5 14B AWQ (generation)                   4-8 CPU, 16-32 Gi, 1 GPU

CPU workloads:
├── Granite Guardian 3.3 8b GGUF (safety gate, llama.cpp)     8 CPU, 8-10 Gi
├── BGE-M3 (embedding, 1024-dim dense vectors)                4 CPU, 4 Gi
├── BGE-reranker-v2-m3 (cross-encoder re-ranking)             4 CPU, 4 Gi
├── PostgreSQL + pgvector (Crunchy, HNSW index)               4 CPU, 8 Gi
├── RAG Orchestrator (Python API)                             1-2 CPU, 1-2 Gi
├── Open WebUI (chat frontend)                                1-2 CPU, 2-4 Gi
├── MinIO (S3 object storage)                                 1-2 CPU, 1-2 Gi
└── OpenShift control plane + operators                      ~16 CPU, ~38 Gi
```

---

## Mapping Back to the On-Prem Proposal

When presenting to the customer, bridge the AWS simulation to the on-prem deployment:

| AWS PoC (this guide) | On-Prem Production (proposal) | What Changes |
|---------------------|------------------------------|-------------|
| 1x g5.16xlarge EC2 | 1x Dell R760xa rack server | Physical server instead of VM |
| A10G 24GB (AWQ 4-bit models) | A100 80GB (FP16 full precision) | Better answer quality — no quantization needed |
| RHEL CoreOS | RHEL 9 | Same OS family |
| Single Node OpenShift | Single Node OpenShift | Same platform |
| EBS gp3 storage | NVMe SSD | Faster local storage |
| Public internet access | Air-gapped | Pull images/models once, then disconnect |
| AWS security groups | Plant firewall | Same network isolation principle |
| MinIO (PoC object storage) | OpenShift Data Foundation (NooBaa) | Enterprise S3 with replication |
| Self-registration (Open WebUI) | Keycloak SSO ↔ AD/LDAP | Plant credential integration |

*"Everything you see in this demo — the chat interface, the cited answers, the safety verification — runs identically on a single server in your datacenter. The only difference is the on-prem hardware has a larger GPU, so the models run at full precision with even better answer quality."*

---

## Checklist

```
Phase 0 — Prerequisites
[ ] AWS GPU quota approved for g5.16xlarge (64 vCPUs)
[ ] AWS CLI installed and configured (us-east-2)
[ ] OpenShift CLI (oc) installed
[ ] OpenShift installer downloaded (amd64 binary — not arm64)
[ ] Route53 public hosted zone created for poc.liqlee.com
[ ] SSH key pair created, uploaded to AWS, and loaded into ssh-agent
[ ] Pull secret downloaded from console.redhat.com

Phase 1 — SNO Install
[ ] Stale /etc/hosts entries removed (grep rag-poc /etc/hosts returns nothing)
[ ] install-config.yaml prepared (baseDomain, pullSecret, sshKey, architecture: amd64)
[ ] openshift-install create cluster completed (~40-50 min)
[ ] KUBECONFIG exported (and persisted in shell rc file)
[ ] oc get nodes shows 1 Ready node
[ ] /etc/hosts entries added for api and apps endpoints
[ ] OpenShift web console accessible

Phase 2 — Component Deployment (deploy.sh)
[ ] deploy.sh completed without errors
[ ] GPU Operator installed, nvidia.com/gpu: 1 visible on node
[ ] RHOAI, Service Mesh 2, Serverless operators all "Succeeded"
[ ] PostgreSQL cluster running, schema applied, MinIO accessible
[ ] Model downloads complete (Qwen ~9 GB, Guardian ~5 GB, BGE-M3 ~2 GB, BGE-reranker ~1.5 GB)
[ ] vLLM running with GPU, embedding + reranker + guardian all Ready
[ ] RAG Orchestrator running, /health returns ok
[ ] Open WebUI accessible via Route, first user created
[ ] Quay registry deployed (optional)

Phase 3 — Validation
[ ] ./validate.sh passes all checks
[ ] Chat produces responses through the full pipeline
[ ] RHOAI dashboard accessible
[ ] OpenShift console accessible

Phase 4 — GitOps (Optional)
[ ] OpenShift GitOps operator installed
[ ] argocd-application.yaml repoURL updated and applied
[ ] Argo CD console accessible, app shows Synced

Phase 5 — Ingestion (Post-Deploy)
[ ] Scanned PDFs uploaded to MinIO bucket
[ ] Ingestion job completed successfully
[ ] New chunks visible in pgvector (query via orchestrator)
```
