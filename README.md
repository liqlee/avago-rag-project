# Maintenance Knowledge RAG Chatbot

An on-premises AI-powered chatbot that ingests scanned maintenance manuals, understands their technical content, and delivers precise, source-cited answers to natural-language maintenance questions. Built on Red Hat OpenShift and open-weight models — no data leaves the plant network.

A technician asks *"What's the torque spec for the Model 400 pump impeller retaining bolts?"* and receives the exact specification, procedure context, safety warnings, and a citation to the original manual page.

## Problem

Experienced technicians are retiring faster than replacements can be trained. Decades of equipment maintenance knowledge — failure patterns, undocumented workarounds, equipment-specific nuances — exist only as tribal memory and stacks of scanned PDF manuals. New technicians cannot self-serve this knowledge, creating downtime risk and safety hazards when they improvise without correct specifications.

## Architecture

### Ingestion (offline)

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   MinIO ──── Ingestion Job ──── BGE-M3 ──── PostgreSQL          │
│   (manuals/   (Docling OCR        (1024-dim     + pgvector      │
│    bucket)     + chunking)          embedding)   (HNSW index)   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
  Upload PDFs → extract text → classify chunks → embed → store
```

### RAG Query (online)

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   User ─── Open WebUI ─── RAG Orchestrator (LangGraph)          │
│              (chat UI)          │                                │
│                                 ├── BGE-M3 Embedding (CPU)      │
│                                 │     query → dense vector       │
│                                 │                                │
│                                 ├── PostgreSQL + pgvector        │
│                                 │     cosine similarity → top 20 │
│                                 │                                │
│                                 ├── BGE Reranker v2-m3 (CPU)    │
│                                 │     cross-encoder → top 5      │
│                                 │                                │
│                                 ├── Qwen 2.5 14B AWQ (GPU)      │
│                                 │     generate cited answer      │
│                                 │                                │
│                                 └── Granite Guardian 3.3 (CPU)  │
│                                       verify groundedness        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
  Question → embed → retrieve → rerank → generate → verify → answer
```

### Ingestion Pipeline

Triggered as an OpenShift Job after PDFs are uploaded to MinIO. Processes each PDF through an 8-step pipeline that converts scanned pages into searchable, classified, and embedded chunks:

```
MinIO (manuals/ bucket)
    │  List PDFs, skip already-processed (ingestion_logs)
    ▼
1. DOWNLOAD PDF
   Fetch from MinIO to temp directory
    │
    ▼
2. EXTRACT PAGE IMAGES (PyMuPDF)
   Render every page as PNG at 200 DPI
    │
    ▼
3. UPLOAD PAGE IMAGES → MinIO (page-images/ bucket)
   s3://page-images/{manual}/{page_0001.png, ...}
    │
    ▼
4. DOCLING OCR + LAYOUT ANALYSIS
   Scanned PDF → structured document with headings,
   tables, lists, and reading-order text
    │
    ▼
5. HIERARCHICAL CHUNKING
   Split by document structure (Docling HierarchicalChunker)
   Classify each chunk: procedure | table | warning | reference
   Attach metadata: equipment_id, manual_title, section_path,
                     page_range, original_page_images
    │
    ▼
6. BGE-M3 EMBEDDING
   Batch embed (batch size 32) → 1024-dim dense vectors
   Auto-fallback to one-at-a-time on HTTP 413
    │
    ▼
7. STORE IN PGVECTOR
   Upsert chunks + vectors (ON CONFLICT → update)
   HNSW index (m=16, ef_construction=200, cosine)
    │
    ▼
8. TRACK IN INGESTION_LOGS
   Status: processing → completed / failed
   Record: file_name, chunks_created, error_message

Exit code 1 if any file failed
```

**Chunk classification** uses rule-based detection: 3+ numbered steps → `procedure`, 2+ pipe-delimited rows → `table`, WARNING/CAUTION/DANGER keywords → `warning`, everything else → `reference`. Each chunk carries a section path derived from the heading hierarchy (e.g. `Chapter 3 > Maintenance > Lubrication`) and links back to the original page images in MinIO.

### RAG Pipeline

The orchestrator uses a LangGraph StateGraph that streams intermediate step indicators to the user in real time via the OpenAI SSE protocol:

```
User Question
    │
    ▼
1. QUERY REWRITE (Qwen 2.5 14B)
   Expand abbreviations, resolve equipment aliases
    │
    ▼
2. BGE-M3 EMBEDDING
   Query → 1024-dim dense vector
    │
    ▼
3. VECTOR SEARCH (pgvector, cosine similarity)
   Top 20 candidates from the chunks table
    │
    ▼
4. RERANK (BGE-reranker-v2-m3, cross-encoder)
   Score candidates → select top 5
    │
    ▼
5. GENERATE (Qwen 2.5 14B, GPU)
   System prompt + context + query → cited answer
    │
    ▼
6. SAFETY GATE (Granite Guardian 3.3 8B, CPU)
   Verify every claim is grounded in source documents
   Flag ungrounded answers + add disclaimer if needed
    │
    ▼
Verified answer with [Manual, Section, Page] citations
```

## Technology Stack

| Layer | Component | Notes |
|-------|-----------|-------|
| Platform | OpenShift 4.22 (SNO) | Single Node OpenShift on RHEL |
| AI/ML | Red Hat OpenShift AI | KServe InferenceService + ServingRuntime CRs |
| LLM | Qwen 2.5 14B Instruct (AWQ 4-bit) | vLLM GPU ServingRuntime, stored in MinIO |
| Safety Gate | Granite Guardian 3.3 8B (GGUF Q4_K_M) | llama.cpp CPU ServingRuntime, stored in MinIO |
| Embedding | BGE-M3 (1024-dim dense) | TEI ServingRuntime, stored in MinIO |
| Reranker | BGE-reranker-v2-m3 | TEI ServingRuntime, stored in MinIO |
| Document Processing | Docling | IBM, scanned PDF to structured text with built-in OCR |
| Database | Crunchy Postgres for Kubernetes + pgvector | Vector search + metadata + chat history |
| Object Storage | MinIO | S3-compatible, stores raw PDFs and page images |
| Frontend | Open WebUI | Chat interface with OpenAI-compatible API |
| GPU Management | NVIDIA GPU Operator | Driver lifecycle + device plugin |
| Registry | Red Hat Quay | Private container image registry |
| CI/CD | Red Hat OpenShift Pipelines (Tekton) | Automated image builds on git push |
| GitOps | OpenShift GitOps (Argo CD) | Declarative deployment (optional) |
| Orchestration | LangGraph | StateGraph with real-time SSE streaming |

## Repository Structure

```
.
├── services/
│   ├── orchestrator/          # RAG query pipeline (FastAPI + LangGraph)
│   │   ├── app/
│   │   │   ├── main.py        # OpenAI-compatible API with SSE streaming
│   │   │   ├── graph.py       # LangGraph StateGraph definition
│   │   │   ├── generator.py   # Query rewrite + answer generation (vLLM)
│   │   │   ├── guardian.py    # Groundedness verification (Granite Guardian)
│   │   │   ├── retriever.py   # Embedding, vector search, reranking
│   │   │   ├── models.py      # Pydantic models (OpenAI-compatible)
│   │   │   └── config.py      # Environment-based configuration
│   │   ├── Containerfile
│   │   └── requirements.txt
│   └── ingestion/             # PDF processing pipeline
│       ├── app/
│       │   ├── main.py        # Entry point — scans MinIO, processes PDFs
│       │   ├── ingest.py      # Docling document processing with OCR
│       │   ├── chunker.py     # Section-aware chunking with metadata
│       │   ├── embedder.py    # BGE-M3 batch embedding with 413-fallback
│       │   └── config.py      # MinIO, Postgres, embedding settings
│       ├── Containerfile
│       └── requirements.txt
├── pipelines/
│   ├── pipeline.yaml          # Tekton Pipeline (git-clone → buildah → rollout-restart)
│   ├── triggers.yaml          # EventListener + TriggerBinding + TriggerTemplates + Route
│   ├── pipelinerun-orchestrator.yaml  # Manual build trigger for orchestrator
│   ├── pipelinerun-ingestion.yaml     # Manual build trigger for ingestion
│   ├── rbac.yaml              # Pipeline ServiceAccount + cross-namespace RBAC
│   ├── webhook-secret.yaml    # GitHub HMAC webhook secret
│   └── namespace.yaml         # rag-pipelines namespace
├── deploy/
│   ├── infrastructure/
│   │   ├── gpu-operator/      # NFD + NVIDIA GPU Operator
│   │   ├── rhoai/             # OpenShift AI + Service Mesh + Serverless
│   │   ├── pipelines/         # OpenShift Pipelines operator subscription
│   │   ├── quay/              # Red Hat Quay container registry
│   │   └── gitops/            # Argo CD (optional)
│   ├── data/
│   │   ├── postgres/          # Crunchy Postgres operator + cluster
│   │   ├── schema/            # Database schema init Job (pgvector, chunks, feedback)
│   │   └── minio/             # MinIO deployment
│   ├── models/
│   │   ├── storage-config.yaml # KServe S3 storage config + MinIO credentials
│   │   ├── servingruntimes/   # KServe ServingRuntime CRs (vllm-gpu, llamacpp-cpu, TEI)
│   │   ├── downloads/         # HuggingFace → MinIO download jobs (4 models)
│   │   ├── vllm/              # Qwen 2.5 14B AWQ InferenceService + bypass Service
│   │   ├── guardian/          # Granite Guardian 3.3 InferenceService + bypass Service
│   │   ├── embedding/         # BGE-M3 InferenceService + bypass Service
│   │   └── reranker/          # BGE-reranker InferenceService + bypass Service
│   ├── apps/
│   │   ├── orchestrator/      # RAG orchestrator deployment
│   │   ├── webui/             # Open WebUI deployment
│   │   └── ingestion/         # Ingestion job definition
│   └── monitoring/            # ServiceMonitors (vLLM metrics)
├── scripts/
│   ├── deploy.sh              # Full deployment (22 steps)
│   ├── teardown.sh            # Remove all resources + destroy cluster
│   └── validate.sh            # Component health checks
├── cluster/
│   └── install-config.yaml    # OpenShift install config (gitignored, contains credentials)
├── manuals/                   # Scanned PDF manuals (gitignored)
├── DEPLOY_AWS.md              # AWS SNO deployment guide
└── README.md
```

## Deployment

### Prerequisites

- OpenShift 4.22 cluster (SNO) with a GPU node
- `oc` CLI authenticated to the cluster
- For AWS PoC: g5.16xlarge instance (NVIDIA A10G 24GB GPU)

### Quick Start

```bash
# 1. Set up the cluster (see DEPLOY_AWS.md for AWS-specific steps)
export KUBECONFIG=~/rag-sno/auth/kubeconfig

# 2. Deploy all components (~45 min for model downloads)
./scripts/deploy.sh

# 3. Validate everything is running
./scripts/validate.sh

# 4. Upload PDFs to MinIO via the console
#    (MinIO console URL printed by deploy.sh)

# 5. Run the ingestion pipeline
oc apply -f deploy/apps/ingestion/ingestion-job.yaml

# 6. Open the WebUI and start asking questions
#    (Open WebUI URL printed by deploy.sh)
```

The deploy script handles 22 steps in order: GPU operator, OpenShift AI, namespaces, PostgreSQL + schema init, MinIO, model storage config, model downloads (HuggingFace → MinIO), ServingRuntime registration, wait for downloads, InferenceService deployments (BGE-M3, reranker, vLLM, Guardian — each pulls from MinIO via KServe storage initializer), orchestrator build, Open WebUI, monitoring, ingestion image build, Quay registry, and Tekton CI/CD pipeline infrastructure.

### CI/CD

After initial deployment, a Tekton pipeline automates image builds on git push. The deploy script sets up the pipeline infrastructure in Step 22.

**GitHub webhook setup:**

1. Get the webhook URL: `oc get route rag-webhook -n rag-pipelines -o jsonpath='{.spec.host}'`
2. In your GitHub repo: Settings → Webhooks → Add webhook
3. Set Payload URL to `https://<webhook-host>`, Content type to `application/json`
4. Set the Secret to match `pipelines/webhook-secret.yaml`
5. Select "Just the push event"

On every push to `main`, the pipeline clones the repo, builds both service images with buildah, pushes to the internal registry, and restarts the orchestrator deployment.

**Manual build triggers:**

```bash
oc create -f pipelines/pipelinerun-orchestrator.yaml -n rag-pipelines
oc create -f pipelines/pipelinerun-ingestion.yaml -n rag-pipelines
```

### Teardown

```bash
./scripts/teardown.sh
```

Deletes all application resources, then prints the `openshift-install destroy cluster` command to remove the underlying infrastructure.

## PoC vs. Production

The current implementation is a proof of concept. Key differences from a production deployment:

| Aspect | PoC (Current) | Production |
|--------|---------------|------------|
| Infrastructure | AWS g5.16xlarge (A10G 24GB) | On-prem Dell PowerEdge R760xa (A100 80GB) |
| Quantization | AWQ 4-bit (~9GB VRAM) | FP16 (~28GB VRAM) for higher quality |
| Authentication | Open WebUI built-in | Keycloak SSO with Active Directory/LDAP |
| Object Storage | MinIO | OpenShift Data Foundation (NooBaa) |
| HA | Single Node OpenShift | 3+ node cluster with rolling updates |
| Model Upgrade | Qwen 2.5 14B | Llama 3.3 70B (add GPUs, tensor parallelism) |
| Domain Tuning | None | InstructLab for plant-specific terminology |

### Production Hardware Recommendation

| Component | Specification |
|-----------|---------------|
| Server | Dell PowerEdge R760xa (2U, 4 PCIe GPU slots) |
| CPU | 2x AMD EPYC 9354 (64 cores total) |
| RAM | 256 GB DDR5 |
| GPU | 1x NVIDIA A100 80GB PCIe (expandable to 4x) |
| Storage | 2x 480GB SSD (RAID-1 boot) + 2x 3.84TB NVMe (data) |

## Safety

Every answer passes through Granite Guardian before delivery. The guardian verifies that each claim in the response is grounded in the retrieved source documents. Unverified claims are stripped and replaced with a disclaimer directing the technician to consult the original manual.

The generation model operates under a system prompt that prohibits guessing numerical specifications (torque values, pressures, clearances) and requires inline citations for every claim.

## Success Criteria

| Metric | Target |
|--------|--------|
| Retrieval precision (correct chunk in top 5) | > 85% |
| Answer factual accuracy | > 80% |
| Groundedness pass rate (Guardian) | > 90% |
| End-to-end latency | < 10 seconds |
| Technician satisfaction (thumbs up/down) | > 70% positive |

## License

All components are open-source or open-weight with permissive licensing (Apache 2.0, MIT). No proprietary dependencies.
