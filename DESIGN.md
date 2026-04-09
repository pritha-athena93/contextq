# Ray AI Platform on GKE — Design Document

> **Interview Reference** — this document is the source of truth for the technical walkthrough.

## Table of Contents

1. [Solution Overview](#1-solution-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Python Pipeline — Function-by-Function](#3-python-pipeline--function-by-function)
4. [Infrastructure (Terraform)](#4-infrastructure-terraform)
5. [Kubernetes Platform (KubeRay)](#5-kubernetes-platform-kuberay)
6. [Node Pool Strategy and Spot Instances](#6-node-pool-strategy-and-spot-instances)
7. [Zero-Trust Security Model](#7-zero-trust-security-model)
8. [Data Layer — Apache Iceberg via Nessie](#8-data-layer--apache-iceberg-via-nessie)
9. [CI/CD Pipeline](#9-cicd-pipeline)
10. [Observability](#10-observability)
11. [Spot Instance Resilience and Failure Simulation](#11-spot-instance-resilience-and-failure-simulation)
12. [Key Tradeoffs and Design Decisions](#12-key-tradeoffs-and-design-decisions)
13. [Runbook Snippets](#13-runbook-snippets)
14. [Training Pipeline Deep Dive](#14-training-pipeline-deep-dive)
15. [Container Image Design](#15-container-image-design)
16. [Operations and Debugging Journal](#16-operations-and-debugging-journal)
    - cert-manager CA secret deleted on re-apply
    - RayJob `ValidationFailed` — TTL on non-shutdown job
    - `ImagePullBackOff` — arm64 image on amd64 cluster
    - Rebuilt image still missing packages — Docker layer cache
    - `PENDING_NODE_ASSIGNMENT` — insufficient `worker_pool` slots
    - `torch.distributed` AllReduce blocked by NetworkPolicy
    - HuggingFace SSL failure due to `REQUESTS_CA_BUNDLE` override
    - `sentence_transformers` v3 API incompatibility (current blocker)
    - Accessing Ray Dashboard without port-forward

---

## 1. Solution Overview

This platform deploys a **production-grade Ray distributed ML pipeline** on **Google Kubernetes Engine (GKE)**, managed by the **KubeRay operator**. The pipeline runs as a scheduled Kubernetes job and does the following end-to-end:

1. **Ingests** corporate entity data in parallel from two sources (GCS Parquet or synthetic via Faker)
2. **Resolves** entity names using a stateful Ray Actor backed by **Vertex AI Gemini 2.5 Flash** — normalises variant company names to canonical legal names
3. **Writes** resolved records atomically to an **Apache Iceberg** table via the **Nessie REST catalog**
4. Optionally **trains** a `SentenceTransformer` embedding model on the resolved entity pairs using **Ray Train** on GPU workers

**Key design philosophy:**
- **Zero secrets** — no API keys, no service account key files anywhere. Authentication is entirely through GCP Workload Identity.
- **Zero-trust networking** — Calico NetworkPolicy default-deny-all on all namespaces; explicit allowlists per service.
- **Spot-first workers** — 60–91% cost reduction. Ray's built-in retry handles preemptions transparently.
- **KubeRay CRDs** — the modern, production-supported way to run Ray on Kubernetes.

**Stack summary:**

| Component | Choice | Reason |
|---|---|---|
| Cloud / Kubernetes | GKE (private, asia-south1) | Managed control plane, native Workload Identity, tight GAR/GCS integration |
| Ray orchestration | KubeRay Operator | Manages Ray cluster lifecycle as K8s CRDs |
| Iceberg catalog | Nessie (in-cluster, Helm) | Self-contained REST catalog, no external dependency |
| Nessie backing store | Postgres (in-cluster) | Helm chart native, lightweight for this scale |
| Object storage | GCS | Iceberg warehouse + raw data landing |
| Pod identity | GCP Workload Identity | Keyless — no static credentials anywhere |
| LLM / entity resolution | Vertex AI Gemini 2.5 Flash via WI | Zero secrets; WI authenticates to `aiplatform.googleapis.com` |
| Container registry | Google Artifact Registry | Co-located with GKE, IAM-native pull auth |
| Bastion access | IAP tunnel to private VM | No open ports, gated by Google identity |
| IaC | Terraform + GCS backend | Remote state, native GCS locking |
| K8s packaging | Helm chart | Typed values, `--atomic` rollback, templated image refs |
| CI/CD | GitHub Actions + WIF | Short-lived OIDC tokens; no long-lived GCP credentials in GitHub |

---

## 2. Architecture Diagram

```mermaid
graph TD
    subgraph GitHub
        repo["GitHub Repository\nray_pipeline.py · Terraform · Helm · Dockerfile"]
        pr["PR opened"]
        merge["Merge to main"]
    end

    subgraph "GitHub Actions (OIDC → GCP token)"
        wf_pr["pr.yaml\nterraform plan + tfsec\n→ PR comment"]
        wf_deploy["deploy.yaml\ndocker build → GAR push\n→ helm upgrade --atomic"]
    end

    subgraph GCP
        gar["Google Artifact Registry\nray-pipeline:<sha>"]

        subgraph "VPC (ray-platform-vpc, 10.0.0.0/20)"
            nat["Cloud NAT\negress for private nodes:\nGAR pulls, PyPI, HuggingFace"]

            subgraph "GKE Cluster (asia-south1, private)"
                kuberay["kuberay-system\nKubeRay Operator"]

                subgraph "ray namespace"
                    raycluster["RayCluster CRD\nray-cluster"]

                    subgraph "head-pool (e2-standard-2, On-Demand)"
                        head["Ray Head Pod\nDashboard :8265 · GCS client · Autoscaler"]
                    end

                    subgraph "worker-pool (e2-standard-2, Spot)"
                        workers["Ray Worker Pods\nresources: worker_pool=1\ningest_data tasks\nEntityResolver actor\nIceberg write"]
                    end

                    subgraph "train-pool (n1-standard-8 + T4 GPU, Spot)"
                        train["Ray Train Worker Pods\nresources: train_worker=1, GPU=1\nTorchTrainer\nSentenceTransformer fine-tune"]
                    end

                    rayjob_pipeline["RayJob CRD\nray-pipeline-job\n--mode pipeline"]
                    submitter_pipeline["Submitter Pod\n--mode pipeline"]

                    rayjob_train["RayJob CRD\nray-train-job\n--mode train"]
                    submitter_train["Submitter Pod\n--mode train"]
                end

                subgraph "nessie-ns"
                    nessie["Nessie REST Catalog\n:19120/iceberg (HTTPS)"]
                    pg["Postgres PVC\n10Gi"]
                end

                subgraph "cert-manager + reflector"
                    certmgr["cert-manager\nself-signed CA + Nessie cert"]
                    refl["reflector\npropagates nessie-root-ca → ray ns"]
                end
            end
        end

        subgraph "GCS"
            raw["raw-data bucket\n(landing zone)\nread by workers via WI"]
            iceberg["iceberg-warehouse bucket\n(Parquet + metadata)\nwritten by workers via WI"]
        end

        vertex["Vertex AI\nGemini 2.5 Flash\n(auth via Workload Identity)"]
        wi["Workload Identity\nray-ksa → ray-sa\nstorage.objectAdmin + aiplatform.user"]
    end

    repo --> pr --> wf_pr
    repo --> merge --> wf_deploy
    wf_deploy -->|"docker push :sha"| gar
    wf_deploy -->|"helm upgrade --atomic"| raycluster
    wf_deploy -->|"helm upgrade --atomic"| rayjob_pipeline
    gar -->|"pull via Cloud NAT"| nat
    nat --> head
    nat --> workers
    nat --> train
    kuberay -->|"reconciles"| raycluster
    raycluster -->|"spawns"| head
    raycluster -->|"spawns (autoscaled 0→10)"| workers
    raycluster -->|"spawns (scale-to-0)"| train
    kuberay -->|"reconciles"| rayjob_pipeline
    kuberay -->|"reconciles"| rayjob_train
    rayjob_pipeline -->|"creates"| submitter_pipeline
    rayjob_train -->|"creates"| submitter_train
    submitter_pipeline -->|"ray job submit :8265"| head
    submitter_train -->|"ray job submit :8265"| head
    head -->|"schedules tasks"| workers
    head -->|"schedules TorchTrainer"| train
    workers --> nessie --> pg
    workers -->|"pyiceberg write (private.googleapis.com)"| iceberg
    workers -->|"read (private.googleapis.com)"| raw
    train -->|"model save (private.googleapis.com)"| iceberg
    workers --> vertex
    certmgr -->|"issues cert"| nessie
    refl -->|"copies CA cert"| workers
    workers -.->|"WI OIDC token"| wi
    train -.->|"WI OIDC token"| wi
    wi -.-> raw
    wi -.-> iceberg
    wi -.-> vertex
```

---

## 3. Python Pipeline — Function-by-Function

The pipeline is in [docker/ray_pipeline.py](docker/ray_pipeline.py). It has two modes invoked from CLI:
- `--mode pipeline` — ingest → resolve → write to Iceberg
- `--mode train` — build training pairs from Iceberg → distributed GPU training

### Module-level configuration (lines 12–20)

All configuration is pulled from environment variables injected by the Helm chart at deploy time:

```python
NESSIE_URI        = os.environ.get("NESSIE_URI",        "https://nessie.nessie-ns:19120/iceberg")
ICEBERG_WAREHOUSE = os.environ.get("ICEBERG_WAREHOUSE", "gs://ray-iceberg-warehouse/warehouse")
GCP_PROJECT       = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
VERTEX_REGION     = os.environ.get("VERTEX_REGION",     "asia-south1")
MODEL_OUTPUT_PATH = os.environ.get("MODEL_OUTPUT_PATH", "")
NUM_WORKERS       = int(os.environ.get("NUM_WORKERS", "2"))
SOURCE_A_PATH     = os.environ.get("SOURCE_A_PATH", "")
SOURCE_B_PATH     = os.environ.get("SOURCE_B_PATH", "")
```

None of these are secrets. `GOOGLE_CLOUD_PROJECT` and `VERTEX_REGION` are injected as plain env vars in `raycluster.yaml`. No `LLM_API_KEY` exists — Vertex AI uses Workload Identity.

---

### `ingest_data(source_name, source_path, n=1000)` — lines 23–36

```python
@ray.remote(resources={"worker_pool": 1})
def ingest_data(source_name: str, source_path: str, n: int = 1000) -> pd.DataFrame:
```

**What it does:** Distributed data ingestion task. Runs as a Ray remote task on a worker node.

**Two modes:**
- **prod** (`source_path` set): reads Parquet files from GCS using `ray.data.read_parquet(source_path)`, converts to pandas. This is the Ray Data integration — Parquet reads are parallelised at block level.
- **dev** (`source_path` empty): generates 1,000 fake company records using the `Faker` library with columns: `corporate_name`, `revenue` (uniform $1M–$10B), `source`.

**Scheduling:** `resources={"worker_pool": 1}` pins this task to worker-pool nodes. The head node does not advertise `worker_pool`, so it can never be scheduled there.

**Parallelism:** Two calls are made simultaneously from `run_pipeline()`:
```python
ds1_ref = ingest_data.remote("dataset_a", a_path)
ds2_ref = ingest_data.remote("dataset_b", b_path)
df1, df2 = ray.get([ds1_ref, ds2_ref])   # blocks until both done
```
Both execute concurrently on separate worker nodes.

---

### `EntityResolver` class — lines 44–124

```python
@ray.remote(resources={"worker_pool": 1})
class EntityResolver:
```

**What it is:** A stateful Ray Actor for LLM-based entity resolution. A Ray Actor is a long-lived process — unlike a remote task, it holds state across calls. All method invocations route to the same Python process.

**Scheduling:** Same `resources={"worker_pool": 1}` as `ingest_data` — ensures the actor lands on a worker-pool Spot node, never on the head. The head must stay free for scheduling; loading it with LLM calls would degrade the whole cluster.

#### `__init__(self)` — lines 46–51

Initialises the Vertex AI client once when the actor is first scheduled:

```python
vertexai.init(project=GCP_PROJECT, location=VERTEX_REGION)
self._model = GenerativeModel("gemini-2.5-flash")
```

Authentication is fully automatic — `vertexai.init()` uses Google Application Default Credentials (ADC). On GKE with Workload Identity, ADC resolves to the `ray-sa` GCP service account which has `roles/aiplatform.user`. **No API key is needed.**

#### `_call_with_retry(self, prompt)` — lines 53–78

Internal method. Wraps a single Gemini call with retry logic:

- **429 ResourceExhausted** (quota exceeded): exponential backoff — waits 1s, 2s, 4s, 8s, 16s before giving up after 5 attempts.
- **503/500 transient errors**: fixed 2-second retry, up to 3 attempts.
- **All other errors** (auth failure, invalid argument): propagate immediately — no point retrying.

This is production-grade resilience. Gemini's quota is per-minute, so the exponential backoff correctly waits out the quota window.

#### `_resolve_chunk(self, names: list[str])` — lines 80–103

Resolves a single batch of up to 50 company names in **one Gemini API call**:

1. Formats them as a numbered list: `1. Acme Corp\n2. IBM Inc\n...`
2. Sends a strict prompt: `"Return only the canonical legal names... one per line in the same order, prefixed with the same number. No explanations."`
3. Parses the numbered response, strips the prefix (`"1. "` or `"1) "`), returns the canonical names.
4. Validates that the response has exactly `len(names)` entries — raises `ValueError` if Gemini added or dropped lines.

Batching 50 names per call (instead of 1) reduces API calls from 2,000 to ~40, dramatically cutting latency and cost.

#### `resolve_batch(self, df: pd.DataFrame)` — lines 105–124

The main public method called by the pipeline driver:

1. Splits `df["corporate_name"]` into chunks of 50 (`_GEMINI_BATCH_SIZE`)
2. Calls `_resolve_chunk()` for each chunk, pausing 1 second between chunks to stay under the RPM quota
3. Adds three new columns to the DataFrame:
   - `canonical_name` — Gemini's normalised output (e.g. `"International Business Machines Corporation"`)
   - `canonical_id` — first 16 hex chars of `SHA256(canonical_name.lower())` — stable, deterministic identifier
   - `is_resolved = True`
4. Returns the enriched DataFrame

**Why SHA256 for the ID?** Stable across runs — the same canonical name always produces the same ID. Lowercase before hashing handles capitalisation variants. 16 hex chars = 64 bits of entropy, sufficient to avoid collisions in a 2,000-row dataset.

---

### `write_to_iceberg(df: pd.DataFrame)` — lines 128–161

**What it does:** Writes the resolved DataFrame to the `default.corporate_registry` Iceberg table via Nessie.

**Steps:**
1. Connects to the Nessie catalog using `pyiceberg.catalog.load_catalog()` with `uri=NESSIE_URI, ref="main", warehouse=ICEBERG_WAREHOUSE`. The `ref="main"` means all operations target Nessie's main branch.
2. Creates the `default` namespace if it doesn't exist.
3. Converts the pandas DataFrame to a PyArrow Table.
4. **If table exists:** calls `table.overwrite(arrow_table)` — ACID overwrite. Nessie atomically swaps the metadata pointer; concurrent writers get a 409 and retry.
5. **If table doesn't exist** (`NoSuchTableError`): creates it with an explicit 6-column schema first, then overwrites.

**Schema:**
```
corporate_name  STRING
revenue         DOUBLE
source          STRING
canonical_name  STRING
canonical_id    STRING
is_resolved     BOOLEAN
```

**Why Iceberg:** plain Parquet has no ACID — two concurrent writers corrupt the dataset. Iceberg's metadata layer (commit via Nessie) serialises writes transactionally.

**Authentication:** `ray-sa` has `roles/storage.objectAdmin` on the iceberg-warehouse bucket via Workload Identity — no credentials in code.

---

### `build_training_pairs()` — lines 165–206

**What it does:** Reads the resolved `corporate_registry` Iceberg table and constructs labelled pairs for training an entity-matching embedding model.

**Steps:**
1. Loads the Iceberg table via Nessie; falls back to synthetic data if the table is empty or unreachable.
2. **Positive pairs** (`label=1.0`): groups rows by `canonical_id` — all company name variants that Gemini resolved to the same canonical entity. Every combination within a group is a matching pair.
3. **Negative pairs** (`label=0.0`): randomly samples two company names with *different* `canonical_id` values. Targets the same count as positive pairs (balanced dataset). Maximum 10× attempts to avoid infinite loop if the dataset is heavily skewed.
4. Shuffles all pairs with a fixed seed (42) for reproducibility, returns a list of `(text_a, text_b, float_label)` tuples.

**Why this structure?** `CosineSimilarityLoss` (used in training) expects pairs with similarity scores — 1.0 for same entity, 0.0 for different entity. The model learns to embed entity name variants close together in vector space.

---

### `train_loop_per_worker(config: dict)` — lines 210–246

**What it does:** The per-worker training loop, executed by Ray Train's `TorchTrainer` on each GPU worker simultaneously.

**Steps:**
1. Loads `SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")` — a 22M parameter text embedding model pre-loaded in the GPU Docker image.
2. Wraps the model with `ray.train.torch.prepare_model(model)` — this distributes the model across workers using `torch.distributed` (NCCL or Gloo backend). Each worker gets a shard of the data; gradients are synchronised via AllReduce.
3. Creates `InputExample` objects from the `(text_a, text_b, label)` pairs.
4. Uses `CosineSimilarityLoss` — trains the model to produce embeddings with cosine similarity matching the label.
5. Uses AdamW optimizer wrapped with `ray.train.torch.prepare_optimizer()` — Ray handles distributed parameter updates.
6. Trains for 3 epochs, reporting `{"epoch": ..., "loss": ...}` after each epoch via `ray.train.report()` — this propagates metrics back to the driver for monitoring.
7. After training, **only rank-0 worker** saves the model to `MODEL_OUTPUT_PATH` on GCS (avoids duplicate writes).

**Resource binding:** Called by `run_training()` with `resources_per_worker={"GPU": 1, "train_worker": 1}` — each worker process requires one GPU and the `train_worker` custom resource, ensuring they only land on `train-pool` nodes.

---

### `run_training()` — lines 249–272

**What it does:** Orchestrates distributed GPU training.

1. Calls `build_training_pairs()` to load pairs from Iceberg.
2. Creates a `TorchTrainer` with:
   - `train_loop_per_worker` as the training function
   - `ScalingConfig(num_workers=NUM_WORKERS, use_gpu=True, resources_per_worker={"GPU": 1, "train_worker": 1})` — launches `NUM_WORKERS` (default 2) GPU workers on the train-pool
3. Calls `trainer.fit()` — Ray Train manages actor lifecycle, data distribution, and metric collection.
4. Prints final metrics from `result.metrics`.

---

### `run_pipeline(env: str)` — lines 276–300

**What it does:** Main pipeline orchestration — ingest → resolve → write.

**Steps:**
1. **prod mode**: validates that `SOURCE_A_PATH` and `SOURCE_B_PATH` env vars are set (GCS paths to Parquet files). **dev mode**: uses empty strings, triggering Faker-based synthetic generation.
2. Fires both `ingest_data.remote()` calls simultaneously, then `ray.get([ds1_ref, ds2_ref])` to collect both DataFrames. Concatenates into one 2,000-row DataFrame.
3. Spawns one `EntityResolver.remote()` actor. Splits the combined DataFrame into batches of 200 rows.
4. Sends all batches to the same actor via `resolver.resolve_batch.remote(batch)` for each batch — all submitted immediately (Ray queues them), then `ray.get([...all futures...])` to collect results.
5. Concatenates resolved batches, calls `write_to_iceberg(resolved_df)`.

**Why batch size 200 in `run_pipeline` vs 50 in `_resolve_chunk`?** They operate at different levels: `run_pipeline` batches DataFrame slices sent to the actor (200 rows at a time to manage object store traffic), while `_resolve_chunk` batches within the actor for Gemini API efficiency (50 names per API call).

---

### `__main__` entrypoint — lines 303–322

```python
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["pipeline", "train"], default="pipeline")
parser.add_argument("--env",  choices=["dev", "prod"],      default="dev")
args = parser.parse_args()

ray.init()

if args.mode == "pipeline":
    run_pipeline(args.env)
else:
    run_training()
```

`ray.init()` without arguments connects to the existing Ray cluster via the `RAY_ADDRESS` environment variable (set in the RayJob submitter pod by KubeRay). In the RayJob, the submitter runs this script — it does not start a new cluster; it attaches to the running `RayCluster` and submits tasks/actors to the existing workers.

---

## 4. Infrastructure (Terraform)

Terraform is in `terraform/` and is structured as 5 modules:

### Module: `network`
- VPC `ray-platform-vpc` with one private subnet `10.0.0.0/20`
- Secondary IP ranges: pods `10.1.0.0/16`, services `10.2.0.0/24`
- Cloud NAT — allows outbound internet access for private nodes (pulling images from GAR, PyPI packages, HuggingFace models) without assigning public IPs
- Private DNS zone routing `*.googleapis.com` through the VPC for low-latency, private GCS/Vertex AI access

### Module: `cluster`
- Private GKE cluster (`enable_private_nodes = true`) in `asia-south1`
- Control plane CIDR: `172.16.0.0/28` (isolated from workload networks)
- Workload Identity enabled on the cluster: `workload_pool = "${var.project_id}.svc.id.goog"`
- Calico NetworkPolicy enforced: `network_policy { enabled = true, provider = "CALICO" }` — **must be set or NetworkPolicy objects are silently ignored**
- **head-pool**: `e2-standard-2`, 1–3 nodes, On-Demand — stable, never Spot
- **worker-pool**: `e2-standard-2`, 0–10 nodes, Spot (`spot = true`) — taint: `cloud.google.com/gke-spot=true:NoSchedule`
- **train-pool**: `n1-standard-8` + 1× `nvidia-tesla-t4`, 0–2 nodes, Spot — taint: `ray-train=true:NoSchedule`
- Shielded nodes on all pools (Secure Boot + integrity monitoring)
- `auto_upgrade = false` on all pools — prevents surprise minor version bumps

### Module: `storage`
- `gen-ai-pritha-ray-raw-data` — raw landing zone; 7-day lifecycle delete
- `gen-ai-pritha-ray-iceberg-warehouse` — Iceberg data; 90-day transition to NEARLINE storage class, 7-day trash cleanup
- Both buckets have `uniform_bucket_level_access = true` (no ACLs; IAM only)

### Module: `iam`
- GCP Service Account `ray-sa` with:
  - `roles/storage.objectAdmin` on both GCS buckets (not project-wide)
  - `roles/aiplatform.user` on the project (Gemini API access)
- Workload Identity binding: `serviceAccount:<project>.svc.id.goog[ray/ray-ksa]` → `roles/iam.workloadIdentityUser` on `ray-sa`

### Module: `bastion`
- `e2-micro` VM in `asia-south1-b`, no public IP
- OS Login enabled — SSH keys are in Google accounts, not `authorized_keys`
- Access only via IAP tunnel (`gcloud compute ssh --tunnel-through-iap`)
- Runs `tinyproxy` on port 8888 for HTTPS tunnelling when running `kubectl`/`helm` from a laptop

### Terraform State
- Remote state in `gs://gen-ai-pritha-tf-state/ray-platform/state/`
- GCS native locking via conditional writes — no DynamoDB equivalent needed
- `backend.hcl` is in `.gitignore` — never committed (contains bucket name)
- The state bucket itself is bootstrapped manually before `terraform init` to avoid a circular dependency

---

## 5. Kubernetes Platform (KubeRay)

### Namespaces
- `ray` — Ray cluster and jobs
- `nessie-ns` — Nessie catalog and Postgres
- `cert-manager` — TLS certificate issuance
- `reflector` — secret propagation across namespaces
- `kuberay-system` — KubeRay operator

### KubeRay Operator
KubeRay extends the Kubernetes API with three CRDs: `RayCluster`, `RayJob`, `RayService`.

reconciles desired state → creates pods, services, head node, workers.

When `RayCluster` is applied, the operator:
1. Creates the head Pod (annotated `ray.io/node-type: head`)
2. Creates a worker ReplicaSet per `workerGroupSpec`
3. Creates headless Services for Ray's internal GCS communication
4. Continuously reconciles — if a pod disappears, it is recreated

### `RayCluster` (`k8s/ray-cluster/templates/raycluster.yaml`)

**Head group:**
- `nodeSelector: cloud.google.com/gke-nodepool: head-pool` — pinned to On-Demand pool
- `object-store-memory: 2000000000` (2 GB plasma store)
- Spill config: `filesystem` to `/ray-spill` (dedicated `emptyDir`, 10Gi limit) — prevents ObjectStoreFullError
- Mounts Nessie CA cert (`nessie-root-ca` secret) so `REQUESTS_CA_BUNDLE` trusts the self-signed TLS cert

**Worker group (`default-worker`):**
- `minReplicas: 0, maxReplicas: 10` — autoscales, idles to zero
- `nodeSelector: worker-pool` + `toleration: gke-spot=true:NoSchedule`
- Custom resource advertised: `resources: '"{\"worker_pool\":1}"'` — this is how Ray knows to place `EntityResolver` and `ingest_data` here
- 4 GB plasma store, 20Gi spill volume
- `GOOGLE_CLOUD_PROJECT` and `VERTEX_REGION` injected as env vars for Vertex AI

**Train worker group:**
- `minReplicas: 0, maxReplicas: 4` — scales to zero when no training job
- `nodeSelector: train-pool` + tolerations for `ray-train` and `nvidia.com/gpu`
- Custom resources: `train_worker: 1`, `num-gpus: 1`
- Uses the GPU Docker image (`ray:2.10.0-py311-gpu` base + sentence-transformers pre-loaded)
- 4 GB plasma store (larger for embedding model weights), 20Gi spill

**GPU time-slicing:** A `ConfigMap` enables NVIDIA GPU time slicing (4 replicas per physical GPU) for the `train-pool` — allows multiple pods to share a single T4 during development/testing.

### `RayJob` (`k8s/ray-cluster/templates/rayjob.yaml`)

Two job definitions:

**`ray-pipeline-job`** (`job.enabled: true` by default):
- Entrypoint: `python /app/ray_pipeline.py --mode pipeline --env dev`
- `clusterSelector: ray.io/cluster-name: ray-cluster` — submits to the running cluster
- `backoffLimit: 3` — submitter pod retried 3× on Spot eviction
- `activeDeadlineSeconds: 1800` — fails if not complete in 30 min
- `ttlSecondsAfterFinished: 3600` — garbage-collected 1h after completion

**`ray-train-job`** (`trainJob.enabled: false` by default):
- Entrypoint: `python /app/ray_pipeline.py --mode train`
- `activeDeadlineSeconds: 7200` — 2h for training
- Enabled by `--set trainJob.enabled=true` when triggering a training run

### Nessie TLS (cert-manager + reflector)
- `cert-manager` issues a self-signed root CA and a server certificate for Nessie
- Nessie is configured to serve HTTPS on port 19120
- The root CA (`nessie-root-ca` secret) is created in `nessie-ns`
- `reflector` propagates that secret to the `ray` namespace so Ray pods can mount it
- `REQUESTS_CA_BUNDLE` env var on all Ray pods points to the mounted CA cert — makes Python's `requests` library (used by pyiceberg) trust the self-signed cert

---

## 6. Node Pool Strategy and Spot Instances

### Why three pools

| Pool | Instance | Pricing | Taint | Ray Role |
|---|---|---|---|---|
| `head-pool` | `e2-standard-2` | On-Demand | none | Head pod only. Head death = cluster restart |
| `worker-pool` | `e2-standard-2` | Spot (60–91% cheaper) | `gke-spot=true:NoSchedule` | `ingest_data`, `EntityResolver` actor, Iceberg writes |
| `train-pool` | `n1-standard-8 + T4` | Spot | `ray-train=true:NoSchedule` + `nvidia.com/gpu:NoSchedule` | GPU training workers |

### How placement is enforced (three independent layers)

1. **`nodeSelector`** in pod spec — hard constraint at Kubernetes scheduler level
2. **Toleration** — worker pods have `cloud.google.com/gke-spot:NoSchedule` toleration; head pod does not → head cannot land on Spot nodes
3. **Ray custom resources** — `@ray.remote(resources={"worker_pool": 1})` — head does not advertise `worker_pool`, so Ray's scheduler never assigns those tasks/actors to the head

### Autoscaling to zero

`minReplicas: 0` on both worker groups means workers scale down after `idleTimeoutSeconds: 60`. You only pay for Spot VMs while a job is actively running.

### What happens during Spot preemption

Spot instances are spare compute capacity that cloud providers sell at a steep discount because it would otherwise sit idle - GCP can preempt (take them back) at any time with no warning, when they need the capacity back for On-Demand customers - ideal for stateless batch jobs, with checkpoints or recovery mechanism

```
T+0s   GCP terminates Spot VM (no warning)
T+10s  Ray head detects heartbeat timeout from evicted worker
T+10s  Tasks on evicted worker marked FAILED → re-queued on surviving workers
T+10s  EntityResolver actor marked dead → Ray recreates it on a surviving worker
T+15s  KubeRay detects pod gone → creates replacement pod
T+90s  GKE cluster autoscaler provisions a new Spot VM
T+120s New node joins the cluster, KubeRay schedules replacement worker pod
```

Pipeline continues on surviving workers during the ~90s gap. No manual intervention needed.

---

## 7. Zero-Trust Security Model

### Layer 1: Workload Identity (keyless IAM)

```
Pod starts (serviceAccountName: ray-ksa)
  │  GKE injects OIDC token for ray-ksa
  ▼
google-auth library calls GKE metadata server (169.254.169.252)
  │  "Can ray-ksa[ray] impersonate ray-sa@project.iam?"
  ▼
Short-lived OAuth2 token for ray-sa returned
  │
  ├── GCS API calls (storage.objectAdmin on two named buckets)
  └── Vertex AI calls (aiplatform.user)
```

No static credentials anywhere. No key files. No secrets.

### Layer 2: Calico NetworkPolicy (`k8s/app/networkpolicy.yaml`)

Default-deny-all applied to `ray`, `nessie-ns`, `kuberay-system` namespaces. Explicit allowlists:

- Workers → Ray head: ports 6379 (GCS), 8265 (dashboard), 10001 (client), 8080 (metrics)
- Workers → Nessie: port 19120 (Iceberg REST)
- Workers → kube-dns: port 53
- Workers → `restricted.googleapis.com` (`199.36.153.4/30`): port 443 (GCS + Vertex AI APIs via private.googleapis.com)
- Workers → GKE metadata server: `169.254.169.252:988`
- **Everything else denied** — compromised worker pod cannot reach the internet, other namespaces, or the K8s API server

### Layer 3: Private GKE + IAP Bastion

- All nodes have private IPs only (`enable_private_nodes = true`)
- Control plane on isolated `172.16.0.0/28` CIDR
- Access path: `Developer laptop → IAP → Bastion (e2-micro, no public IP) → kubectl`
- IAP authenticates via Google identity — no SSH key management, no VPN

### Layer 4: Shielded Nodes

All node pools have `enable_secure_boot = true, enable_integrity_monitoring = true`. Secure Boot prevents loading unsigned kernel modules. Integrity monitoring detects tampered boot images via Cloud Monitoring alerts.

### Layer 5: GitHub Actions — Workload Identity Federation

GitHub OIDC JWT exchanged for short-lived GCP token (1-hour TTL). No long-lived service account keys in GitHub Secrets.

```
GitHub Actions OIDC JWT (signed by github.com)
  │  GCP STS: ExchangeToken
  ▼
Short-lived GCP token (scoped to github-sa)
  ├── docker push to GAR
  └── terraform plan (read-only GCS state access)
```

---

## 8. Data Layer — Apache Iceberg via Nessie

### Why Iceberg over plain Parquet

| Feature | Plain Parquet | Apache Iceberg |
|---|---|---|
| ACID writes | No (last writer wins) | Yes — atomic metadata commit |
| Schema evolution | Manual, error-prone | Built-in (add/drop/rename columns) |
| Time travel | No | Yes (`table.scan(snapshot_id=...)`) |
| Concurrent writers | Race conditions | Serialised via catalog commit (409 on conflict) |

### How Iceberg ACID works on GCS

```
1. Writer writes new Parquet data files to GCS (objects are immutable)
2. Writer creates a manifest file listing the new data files
3. Writer creates a new snapshot JSON
4. Writer calls Nessie REST API to atomically swap the metadata pointer
   → If two writers race, one gets 409 Conflict from Nessie and must retry
```

### Table structure on GCS

```
gs://gen-ai-pritha-ray-iceberg-warehouse/warehouse/
  default/
    corporate_registry/
      metadata/
        00000-<uuid>-metadata.json
        snap-<id>-manifest.avro
      data/
        part-00000-<uuid>.parquet
```

### Nessie Deployment

```
nessie-ns
  ├── postgres Deployment (postgres:15)
  │     PVC: 10Gi standard-rwo
  │     Service: postgres-svc:5432
  └── nessie Helm release
        JDBC: jdbc:postgresql://postgres-svc.nessie-ns:5432/nessie
        Service: nessie-svc:19120 (ClusterIP, HTTPS)
```

Nessie stores all catalog metadata (table locations, snapshot history, branch pointers) in Postgres. The Iceberg data files in GCS are intact even if Postgres is lost, but the catalog state (which snapshot is current) would be lost.

### Postgres credentials

`nessie-postgres-creds` K8s Secret is **not** in any committed manifest. It is created imperatively once before deploying Postgres:

```bash
kubectl create secret generic nessie-postgres-creds \
  --namespace nessie-ns \
  --from-literal=username=nessie \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 32)"
```

Reasoning: even a placeholder in a YAML file creates a temptation to accidentally commit a real password.

---

## 9. CI/CD Pipeline

### PR Workflow (`pr.yaml`)

Triggers on PRs touching `terraform/**`:

1. `terraform init -backend-config=backend.hcl` — connects to GCS remote state (read-only)
2. `terraform fmt -check -recursive` — fails on unformatted HCL
3. `terraform validate`
4. `terraform plan -lock=false` — generates a plan against real deployed infra
5. `tfsec` — static security scanning of all HCL; findings posted to PR comment
6. Posts a collapsible PR comment with plan + tfsec output; deletes previous bot comments to keep the thread clean

### Deploy Workflow (`deploy.yaml`)

Triggers on merge to `main`:

**`build-and-push` job:**
```bash
IMAGE="asia-south1-docker.pkg.dev/gen-ai-pritha/ray-platform/ray-pipeline:${{ github.sha }}"
docker build -t "$IMAGE" ./docker
docker push "$IMAGE"
```
Image tagged with `github.sha` — immutable, traceable to the exact commit.

**Note:** The pipeline does not yet include a `helm upgrade` step to trigger the RayJob after pushing the image. This step would be:
```bash
helm upgrade --install ray-cluster k8s/ray-cluster \
  --namespace ray \
  --set image.tag=${{ github.sha }} \
  --set global.projectId=gen-ai-pritha \
  --atomic --timeout 5m
```

### Helm Chart Design

Image references use a `_helpers.tpl` template function:
```
{{ .Values.global.garRegion }}-docker.pkg.dev/{{ .Values.global.projectId }}/{{ .Values.global.garRepo }}/ray-pipeline:{{ .Values.image.tag }}
```

All environment-specific values (`projectId`, `garRegion`, `vertexRegion`, `nessieUri`) are in `values.yaml` and overridden at deploy time with `--set`. The chart itself is environment-agnostic.

`--atomic` on `helm upgrade` means Helm rolls back automatically if pods don't become ready within the timeout — no manual rollback needed for bad deploys.

---

## 10. Observability

### Cloud Monitoring (configured in Terraform)

```hcl
logging_config    { enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"] }
monitoring_config { enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
                    managed_prometheus { enabled = true } }
```

This provides out-of-the-box:
- CPU/memory/PV usage per pod and node
- Ray pod stdout/stderr in Cloud Logging automatically
- Prometheus metrics scraped by Google-managed Prometheus (no StatefulSet to manage)

### Key Signals

| Signal | Source | Alert Condition |
|---|---|---|
| Ray object store usage | Ray Dashboard / Prometheus | > 80% → risk of ObjectStoreFullError |
| Pending tasks | Ray Dashboard | Sustained > 0 → insufficient workers |
| Task retry count | Ray Task Timeline | Spikes = Spot evictions |
| Spot preemption events | Cloud Logging | `protoPayload.methodName = compute.instances.preempted` |
| Iceberg write success | Pipeline stdout in Cloud Logging | Final print absent = write failed |
| Gemini API latency | Vertex AI metrics | Spikes indicate quota pressure |

### Ray Dashboard

```bash
kubectl port-forward svc/ray-cluster-head-svc 8265:8265 -n ray
open http://localhost:8265
```

Key views:
- **Cluster** tab: node count, CPU/memory per node, object store fill level
- **Jobs** tab: job status, logs, duration
- **Task Timeline**: Gantt chart — eviction gaps show as breaks, retries visible as duplicates
- **Actors** tab: `EntityResolver` actor status, method call counts

---

## 11. Spot Instance Resilience and Failure Simulation

### Why Spot is Safe for Ray Workers (not the head)

- Ray tasks are automatically retried on a surviving worker when their node disappears
- Ray actors are automatically restarted on a surviving node (Ray actor supervision)
- Object references pointing to evicted plasma are re-computed by re-executing the task that produced them
- **Head must be On-Demand** — it runs the Global Control Store (GCS) which tracks all actor locations and object IDs. Head death = full cluster restart.

### Simulate a Spot Preemption

```bash
./scripts/simulate-spot-failure.sh
./scripts/simulate-spot-failure.sh --dry-run   # preview only
```

The script:
1. Checks RayJob is running
2. Selects a worker-pool node
3. `kubectl delete node <node>` — simulates GCP preemption
4. Polls RayJob status every 10 seconds for 2 minutes
5. Prints pass/fail: job should complete successfully on surviving workers

**What to show in the walkthrough:**
- Ray Dashboard Task Timeline shows a gap at deletion time
- Worker pod count drops then recovers (~90–120s)
- RayJob ultimately succeeds — no data loss

---

## 12. Key Tradeoffs and Design Decisions

| Decision | Chosen | Alternative Considered | Reason |
|---|---|---|---|
| LLM auth | Vertex AI via Workload Identity | API key in Secret Manager | Zero secrets, audit trail, no rotation needed; WI handles auth entirely |
| Iceberg catalog | Nessie (in-cluster) | BigLake Metastore, Hive | No external dependency, free, REST interface; BigLake = vendor lock-in |
| Nessie state | Postgres in-cluster | Cloud SQL | Simple, Helm native support; no managed service cost for this scale |
| Worker identity | Workload Identity only | ESO + Secret Manager | Vertex AI migration removed the only secret; ESO is no longer needed |
| Bastion access | IAP tunnel | VPN, public bastion | No open ports, Google identity-gated; VPN requires infra |
| CI image tag | `github.sha` | `latest` | `latest` is mutable and loses traceability; SHA is immutable |
| Head pool autoscaling | Fixed at 1 | Autoscaled | Head death = cluster restart; must be stable |
| K8s auto-upgrade | Disabled on all pools | Enabled | Prevents surprise minor version bumps breaking API compatibility |
| Network policy enforcement | Calico (in Terraform) | No CNI | GKE ignores NetworkPolicy objects without a CNI plugin configured in Terraform |
| Observability | Cloud Monitoring + managed Prometheus | Self-managed Prometheus | No extra StatefulSet or PVC; managed Prometheus is already paid for |
| GPU time-slicing | 4 replicas per GPU | One pod per GPU | Allows dev/test without dedicated GPU per worker; prod would use 1:1 |

---

## 13. Runbook Snippets

### Connect to the cluster via bastion

```bash
# Open IAP SSH tunnel (runs tinyproxy on port 8888 inside bastion)
gcloud compute ssh ray-platform-bastion \
  --tunnel-through-iap \
  --project=gen-ai-pritha \
  --zone=asia-south1-b \
  -- -L 8888:localhost:8888

# In a separate terminal — route kubectl through the tunnel
export HTTPS_PROXY=localhost:8888
gcloud container clusters get-credentials ray-platform \
  --region=asia-south1 --project=gen-ai-pritha
kubectl get nodes
```

### Trigger a pipeline run manually

```bash
helm upgrade ray-cluster k8s/ray-cluster \
  --namespace ray \
  --reuse-values \
  --set job.enabled=true \
  --set image.tag=<git-sha>
```

### Check RayJob status

```bash
kubectl get rayjob -n ray
kubectl get rayjob ray-pipeline-job -n ray -o wide
kubectl logs -n ray -l ray.io/node-type=head --tail=100
```

### Access Ray Dashboard

```bash
kubectl port-forward svc/ray-cluster-head-svc 8265:8265 -n ray
open http://localhost:8265
```

### Query Iceberg table

```bash
# Port-forward Nessie
kubectl port-forward svc/nessie 19120:19120 -n nessie-ns
```

```python
from pyiceberg.catalog import load_catalog

catalog = load_catalog(
    "nessie",
    uri="http://localhost:19120/iceberg",
    ref="main",
    warehouse="gs://gen-ai-pritha-ray-iceberg-warehouse/warehouse",
)
table = catalog.load_table("default.corporate_registry")
df = table.scan().to_pandas()
print(df.head())
```

### Time-travel query

```python
history = table.history()
old_snapshot = history[-2].snapshot_id
df_old = table.scan(snapshot_id=old_snapshot).to_pandas()
```

### Scale workers manually

```bash
kubectl scale raycluster ray-cluster -n ray \
  --patch='{"spec":{"workerGroupSpecs":[{"groupName":"default-worker","replicas":5}]}}'
```

### Simulate Spot failure

```bash
./scripts/simulate-spot-failure.sh          # live
./scripts/simulate-spot-failure.sh --dry-run  # preview
```

---

## 14. Training Pipeline Deep Dive

### Why train a model at all

The pipeline uses Gemini to resolve entity names — expensive (~$0.002/call), slow (1–2s per batch), requires internet. The trained SentenceTransformer embedding model replaces LLM calls for future resolution:

1. **Pipeline phase**: Gemini canonicalises 2,000 names → writes to Iceberg
2. **Training phase**: use Gemini's output as labelled data → train `all-MiniLM-L6-v2` on name pairs
3. **Inference phase**: embed new name → cosine similarity search against known canonical embeddings → no LLM needed

The model is not learning revenue patterns. It is learning that `"IBM"`, `"I.B.M."`, and `"International Business Machines"` should cluster close in vector space.

---

### What `build_training_pairs()` produces

Reads `corporate_registry` from Iceberg. Groups rows by `canonical_id` (SHA256 of Gemini's canonical name):

**Positive pairs (`label = 1.0`)**: every combination of name variants within the same canonical group.
```
canonical_id: a3b9...
  variants: ["IBM", "I.B.M.", "International Business Machines Corp."]
  pairs: [("IBM", "I.B.M."), ("IBM", "International Business Machines Corp."), ...]
```

**Negative pairs (`label = 0.0`)**: two names sampled from *different* `canonical_id` groups.
```
("IBM", "Acme Corp")  →  0.0
("Tata", "Apple Inc") →  0.0
```

Target count: equal number of negative and positive pairs (balanced dataset). `target_neg = len(positive_pairs)`. Up to 10× attempts per pair to find mismatched `canonical_id` — avoids infinite loop on skewed data.

Risk: random negative might coincidentally be the same entity — acceptable noise at 2,000 rows. At scale, explicit negative mining (hard negatives) would be more rigorous.

Final output: list of `(text_a, text_b, float_label)` tuples, shuffled with `seed=42` for reproducibility.

---

### SentenceTransformer and `all-MiniLM-L6-v2`

**SentenceTransformer** is a library that wraps BERT-family models for efficient sentence embedding. It adds a pooling layer on top of BERT's token outputs to produce a single fixed-size vector per sentence.

**`all-MiniLM-L6-v2`**: 22M-parameter distilled version of BERT. 6 transformer layers, 384-dimensional output embeddings. Fast enough for production inference. Pre-trained on 1B+ sentence pairs for general semantic similarity.

**BERT pooler vs mean pooling:**
- BERT was originally trained with a `[CLS]` token pooler head (linear + tanh) for classification tasks
- SentenceTransformer discards the pooler and uses **mean pooling** instead — averages all token embeddings to produce the sentence embedding. This is empirically better for similarity tasks.
- The BERT pooler parameters are still present in the model weights but are never called in the forward pass → requires `find_unused_parameters=True` in DDP (see below)

---

### PyTorch DDP and AllReduce

Each training worker (Ray Train actor) holds a **full copy** of the model. Data is split into shards — worker 0 gets rows 0–N/k, worker 1 gets rows N/k–2N/k, etc.

Training step per worker:
```
1. Forward pass: compute embeddings for text_a and text_b batch
2. Compute CosineEmbeddingLoss(emb_a, emb_b, labels)
3. loss.backward() → compute gradients locally
4. AllReduce (NCCL): average gradients across all workers
   → all workers now have identical averaged gradients
5. optimizer.step() → update model weights
   → all workers remain in sync
```

AllReduce is a collective operation — every worker sends its gradients and receives the average simultaneously. No parameter server needed. This is `torch.distributed` DistributedDataParallel (DDP), not model parallelism (that would be FSDP/ZeRO for models too large to fit on one GPU).

**`find_unused_parameters=True`** in `prepare_model()`: required because the BERT pooler weights are in `model.parameters()` but never used in the forward pass. DDP normally errors if a parameter has no gradient after backward. This flag makes DDP skip those parameters during AllReduce.

---

### Training loop mechanics

```python
# collate_fn: converts list of (text_a, text_b, label) tuples into batched tensors
texts_a = [pair[0] for pair in batch]
texts_b = [pair[1] for pair in batch]
labels  = torch.tensor([pair[2] for pair in batch])

# tokenizer: converts raw text → token IDs + attention masks
enc = tokenizer(texts_a + texts_b, padding=True, truncation=True, return_tensors="pt")
# "padding=True" pads all sequences to same length within batch
# "truncation=True" clips sequences longer than model's max (512 tokens)
# texts_a and texts_b concatenated → one forward pass encodes both halves

# forward: model produces token embeddings → mean pool → 384-dim sentence vectors
embeddings = model(**enc)   # shape: [2*batch_size, 384]
emb_a, emb_b = embeddings.chunk(2)  # split back

# CosineEmbeddingLoss:
# label=1.0  → loss = 1 - cosine_similarity(emb_a, emb_b)
# label=-1.0 → loss = max(0, cosine_similarity - margin)
# NOTE: CosineEmbeddingLoss expects -1/+1 labels, not 0/1
#       build_training_pairs uses 1.0 (match) and -1.0 (no match)
loss = criterion(emb_a, emb_b, labels)

optimizer.zero_grad()
loss.backward()   # → triggers AllReduce across DDP workers
optimizer.step()
```

**AdamW** optimizer: Adam (adaptive learning rates per parameter) + weight decay regularisation. Standard for fine-tuning BERT-family models. Learning rate `2e-5` — small to avoid destroying pre-trained weights.

**`ray.train.report({"epoch": e, "loss": avg_loss})`** after each epoch: propagates metrics to the driver process, visible in Ray Dashboard and Cloud Logging.

**Rank-0 model save**: `ray.train.get_context().get_world_rank() == 0` — only the first worker saves the model to GCS. All workers hold identical models after AllReduce; saving from all workers would produce redundant writes and race conditions.

---

### Ray Train internals

`ray.train.torch` is a Ray submodule that wraps PyTorch distributed. When `TorchTrainer.fit()` is called:

1. Ray launches `num_workers` actor processes on nodes matching `ScalingConfig`
2. Ray sets up `torch.distributed` process group (NCCL for GPU, Gloo for CPU)
3. Each actor runs `train_loop_per_worker(config)` independently
4. `prepare_model(model)` wraps the model in `DistributedDataParallel`
5. `prepare_optimizer(optimizer)` wraps for distributed parameter sync
6. Training proceeds; AllReduce happens automatically in `loss.backward()`
7. `ray.train.report()` collects metrics from all workers and aggregates them

`get_world_rank()` returns the worker's index (0, 1, ..., N-1) within the training run. Equivalent to `torch.distributed.get_rank()` but Ray-managed.

**`ScalingConfig(num_workers=1, use_gpu=False, resources_per_worker={"worker_pool": 1})`**: trains on CPU workers in the default worker pool. Single worker means no DDP (no AllReduce needed). Using `worker_pool` (not `train_worker`) keeps training on the same Spot nodes as ingestion — simpler at the cost of GPU acceleration.

---

## 15. Container Image Design

### Two Dockerfiles: CPU workers and GPU train workers

| Image | Base | Torch | Used by |
|---|---|---|---|
| `ray-pipeline:<sha>` | `rayproject/ray:2.10.0-py311` | `torch==2.3.1+cpu` (pinned) | head, workers, submitter pods |
| `ray-pipeline:<sha>-gpu` | `rayproject/ray:2.10.0-py311-gpu` | Not pinned (CUDA torch from base) | train-pool workers only |

**Why pin CPU torch?** The non-GPU base image has no CUDA; PyPI's default torch wheel includes CUDA binaries (~2GB). Pinning `torch==2.3.1+cpu` (from `https://download.pytorch.org/whl/cpu`) installs the 200MB CPU-only wheel instead.

**Why NOT pin GPU torch?** The GPU base image ships with `torch` compiled against its specific CUDA version. Installing any other torch via pip overwrites it and breaks CUDA. `requirements-gpu.txt` omits the torch pin so `pip install` skips it.

```
requirements.txt (CPU):
  --extra-index-url https://download.pytorch.org/whl/cpu
  torch==2.3.1+cpu          ← explicit CPU wheel
  sentence-transformers==...

requirements-gpu.txt (GPU):
  sentence-transformers==... ← no torch line; keeps base image's CUDA torch
```

### Model weight pre-loading at build time

```dockerfile
RUN python -c "from sentence_transformers import SentenceTransformer; \
               SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

This line **downloads the model weights at Docker build time** and caches them in the image layer at `~/.cache/huggingface/`. At runtime, `HF_HUB_OFFLINE=1` prevents any network call to HuggingFace Hub — the model loads from the baked-in cache.

**Why not use `requirements.txt`?** HuggingFace model weights are not Python packages. They cannot be installed via `pip`. They must be fetched by the HuggingFace `hub` library during model instantiation.

### `HF_HUB_OFFLINE` — two places

Set in `raycluster.yaml` (pod-level env var) so all pods inherit it. Also set inside `train_loop_per_worker()`:

```python
os.environ["HF_HUB_OFFLINE"] = "1"
```

This is defensive redundancy. The pod env var is authoritative. The in-code assignment covers the edge case of someone running the script outside the cluster (e.g., local dev) without setting the env var — prevents an unexpected network call to HuggingFace Hub failing on a machine without internet access.

---

### TLS for Nessie: self-signed certificates

A **self-signed certificate** is one signed by its own private key rather than by a trusted third-party Certificate Authority (CA). Browsers reject them by default ("unknown authority"). For internal cluster services this is fine — both sides of the connection are under our control.

How it works here:
1. `cert-manager` generates a self-signed root CA (a CA that trusts itself)
2. `cert-manager` issues a server certificate for Nessie, signed by that CA
3. Nessie serves HTTPS on `:19120` using this certificate
4. The root CA certificate is stored in the `nessie-root-ca` K8s Secret in `nessie-ns`
5. `reflector` copies that secret to the `ray` namespace
6. Ray pods mount the CA cert; `REQUESTS_CA_BUNDLE` env var points to it
7. Python's `requests` library (used by pyiceberg) loads the CA → trusts Nessie's certificate

Without step 6–7, pyiceberg throws `SSLError: certificate verify failed: unable to get local issuer certificate`.

---

## 16. Operations and Debugging Journal

### Incident: RayJob shows `Failed` despite job succeeding

**Symptom**: `kubectl get rayjob -n ray` shows `JOB STATUS: FAILED`, but Ray Dashboard and logs showed the job completed successfully. Error: `JobDeploymentStatusTransitionGracePeriodExceeded`.

**Root cause**: KubeRay's default grace period for job status propagation is 30 seconds. The submitter pod exits after job submission; the Ray cluster then processes the job and updates job status via the Dashboard API. If the status update takes longer than 30 seconds (cluster warm-up, Iceberg write latency), KubeRay marks the deployment as failed even if the job succeeded.

**Fix**: Added `jobDeploymentStatusTransitionGracePeriodSeconds: 300` to both RayJob specs in [k8s/ray-cluster/templates/rayjob.yaml](k8s/ray-cluster/templates/rayjob.yaml). Controlled via `job.gracePeriodSeconds` and `trainJob.gracePeriodSeconds` in [values.yaml](k8s/ray-cluster/values.yaml).

---

### Incident: Autoscaler sidecar crashlooping

**Symptom**: Head pod had a second container (`autoscaler`) in `CrashLoopBackOff`. Log: `ConnectTimeout: HTTPSConnectionPool(host='kubernetes.default', port=443)`.

**Root cause (1)**: `enableInTreeAutoscaling: true` was missing from the `RayCluster` spec. The autoscaler sidecar container was never being injected. Added to [k8s/ray-cluster/templates/raycluster.yaml](k8s/ray-cluster/templates/raycluster.yaml):
```yaml
spec:
  enableInTreeAutoscaling: true
```

**Root cause (2)**: The `default-deny-all` NetworkPolicy in the `ray` namespace blocked the head pod's egress to the Kubernetes API server. The autoscaler needs to create and delete worker pods via the K8s API.

**Fix**: Added two egress rules to `allow-head-egress` in [k8s/app/networkpolicy.yaml](k8s/app/networkpolicy.yaml):
```yaml
- to:
    - ipBlock:
        cidr: 10.2.0.1/32   # kubernetes.default ClusterIP
  ports:
    - port: 443
- to:
    - ipBlock:
        cidr: 172.16.0.0/28  # GKE master CIDR
  ports:
    - port: 443
```

**Gotcha**: Deleting and recreating the head pod was necessary after applying the new NetworkPolicy. Existing pods do not automatically pick up updated NetworkPolicy rules — the kernel `iptables`/`nftables` rules are refreshed when a pod is (re)created.

---

### Incident: KubeRay operator couldn't poll Ray head for job status

**Symptom**: `kubectl describe rayjob` showed operator unable to fetch job status from Ray. Job appeared stuck.

**Root cause**: `allow-kuberay-operator-egress` in `kuberay-system` only allowed port 443 (K8s API). Ray Dashboard runs on port 8265; Redis/GCS on 6379. The operator polls both to determine job status.

**Fix**: Added to `allow-kuberay-operator-egress` in `kuberay-system`:
```yaml
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: ray
      podSelector:
        matchLabels:
          ray.io/node-type: head
  ports:
    - port: 8265
    - port: 6379
```

Also added `allow-head-from-kuberay-operator` ingress policy in the `ray` namespace to allow the operator to reach the head pod on those ports.

---

### Incident: Workers spinning up and down between pipeline and train job

**Symptom**: Train job waited 2–3 minutes before starting. During the wait, worker pods were being deleted immediately after the pipeline job finished.

**Root cause**: `idleTimeoutSeconds: 60` caused the Ray autoscaler to kill idle workers within 1 minute of them having no tasks. The pipeline job finished, workers went idle, autoscaler removed them. Train job then had to wait for GKE Spot provisioning (~90–120s) to bring fresh workers back.

**Fix**: Raised `idleTimeoutSeconds: 300` in `autoscalerOptions` in [values.yaml](k8s/ray-cluster/values.yaml:41). Workers now stay alive for 5 minutes after going idle — long enough for the train job to start and claim them.

---

### Design note: both jobs use `worker_pool`

`ray-train-job` uses `ScalingConfig(resources_per_worker={"worker_pool": 1})`, not `{"train_worker": 1}`. This means training runs on the same CPU Spot worker nodes as the pipeline, not the GPU train-pool.

Rationale: `numWorkers: 1`, CPU training on a 22M parameter model is fast enough. The `train-pool` (n1-standard-8 + T4) is expensive Spot hardware; reserving it for GPU workloads only. If training is promoted to GPU, change `resources_per_worker={"train_worker": 1, "GPU": 1}` and `use_gpu=True`.

**Contention**: when both jobs run simultaneously, they compete for `worker_pool` resources. With only 2 Spot VMs available and each holding `worker_pool: 1`, one job blocks the other. Pipeline has scheduling priority (submitted first). Train job waits. Workaround: sequence jobs (pipeline first, train after), or provision more Spot VMs.

---

### RayJob lifecycle in production

`kubectl apply` over an existing `RayJob` fails for immutable fields. Pattern:

```bash
kubectl delete rayjob ray-pipeline-job -n ray
helm upgrade --install ray-cluster k8s/ray-cluster \
  --namespace ray \
  --set image.tag=<sha> \
  --set job.enabled=true
```

Alternative for ad-hoc runs: `ray job submit` CLI bypasses RayJob CRDs entirely and submits directly to the running cluster's Dashboard API. RayJob CRDs are for scheduled/GitOps-managed jobs; `ray job submit` is for interactive or CI-triggered runs without CRD lifecycle management.

---

### NetworkPolicy walkthrough: all policies, all namespaces

#### `ray` namespace

| Policy | Direction | Who | What | Why |
|---|---|---|---|---|
| `default-deny-all` | Ingress + Egress | all pods | deny everything | baseline zero-trust |
| `allow-job-submitter-egress` | Egress | `ray.io/component: job-submitter` | → head:8265, → kube-dns:53 | submitter pod needs to reach Dashboard to submit job |
| `allow-head-from-submitter` | Ingress | head | from job-submitter:8265 | head accepts job submission |
| `allow-head-from-workers` | Ingress | head | from workers (all ports) | workers heartbeat, GCS, task result reporting |
| `allow-workers-from-head` | Ingress | workers | from head (all ports) | head dispatches tasks to workers |
| `allow-workers-from-workers` | Ingress | workers | from workers (all ports) | PyTorch DDP AllReduce peer-to-peer gradient sync during distributed training |
| `allow-worker-egress` | Egress | workers | → head, → workers, → nessie:19120, → internet:443, → metadata:988, → kube-dns:53 | task execution, Iceberg writes, GCS/Vertex AI, Workload Identity |
| `allow-head-egress` | Egress | head | → workers, → K8s API:443 (10.2.0.1+172.16.0.0/28), → nessie:19120, → internet:443, → metadata:988, → kube-dns:53 | schedule tasks, autoscaler sidecar K8s API, Iceberg, GCS, Workload Identity |
| `allow-submit-to-ray-head` | Egress | `job-name: ray-pipeline-job` | → head:8265, → kube-dns:53 | legacy/redundant with job-submitter rule |
| `allow-head-from-rayjob` | Ingress | head | from `job-name: ray-pipeline-job`:8265 | head accepts submission from pipeline job pod |
| `allow-head-from-kuberay-operator` | Ingress | head | from kuberay-system operator:8265,6379 | operator polls head for job status and cluster health |
| `allow-kuberay-egress` | Egress | kuberay-operator (ray ns) | → head:8265,6379, → internet:443, → kube-dns:53 | redundant — operator runs in kuberay-system not ray ns. Kept for reference. |

#### `nessie-ns` namespace

| Policy | Direction | Who | What | Why |
|---|---|---|---|---|
| `default-deny-all` | Ingress + Egress | all pods | deny everything | baseline zero-trust |
| `allow-nessie-from-ray` | Ingress | nessie pod | from ray namespace:19120 | Ray workers write/read Iceberg metadata |
| `allow-nessie-egress` | Egress | nessie pod | → postgres:5432, → internet:443, → metadata:988, → kube-dns:53 | catalog persistence, GCS (Iceberg data files), Workload Identity |
| `allow-postgres-from-nessie` | Ingress | postgres pod | from nessie:5432 | Nessie reads/writes catalog state to Postgres |

#### `kuberay-system` namespace

| Policy | Direction | Who | What | Why |
|---|---|---|---|---|
| `default-deny-all` | Ingress + Egress | all pods | deny everything | baseline zero-trust |
| `allow-kuberay-operator-ingress` | Ingress | kuberay-operator | from master CIDR 172.16.0.0/28:9443, from GKE nodes 10.0.0.0/8:8080 | K8s API server webhook calls + health probes |
| `allow-kuberay-operator-egress` | Egress | kuberay-operator | → K8s API (10.2.0.1+172.16.0.0/28):443, → internet:443, → ray head:8265,6379, → kube-dns:53 | reconcile CRDs, pull operator image, poll Ray head status |

#### Why workers and head need internet egress

GCS (`storage.googleapis.com`) and Vertex AI (`aiplatform.googleapis.com`) are public Google APIs with public IP addresses. Traffic to them leaves the VPC even from GKE — there is no private IP for these services unless you configure **Private Google Access** with `restricted.googleapis.com` (`199.36.153.4/30`). The `except` blocks in the CIDR rules (`0.0.0.0/0 except 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16`) prevent workers from reaching any other private network while still allowing Google API traffic over internet IPs.

#### GKE metadata server (`169.254.169.252:988`)

Link-local address on every GKE node. Handles the Workload Identity token exchange:
```
Pod (ray-ksa) → metadata server:988 → GKE WI → short-lived OAuth2 token for ray-sa
```
Port 988 is specific to GKE's metadata server implementation. Without this egress rule, all GCP API calls fail with "unable to get application default credentials".

---

### Incident: cert-manager CA secret deleted on manifest re-apply

**Symptom**: After re-applying `k8s/third-party/nessie-tls.yaml`, Nessie pod failed with TLS errors. `kubectl get secret nessie-root-ca -n nessie-ns` showed the secret was gone. cert-manager logs showed it being deleted immediately after apply.

**Root cause**: `cert-manager-values.yaml` has `--enable-certificate-owner-ref=true`. This flag makes cert-manager set an owner reference on the Secret it generates, pointing to the Certificate CR. When the Certificate CR is deleted (even momentarily during a re-apply of the manifest), cert-manager garbage-collects the associated Secret. On a fresh apply, the Certificate CR is recreated but the Secret is gone — cert-manager issues a new cert and new private key, breaking any pods that mounted the old CA.

**Fix**: Do not re-apply the TLS manifest if the cluster is running. If you must re-apply (schema change, renewal), delete the Certificate CRs first and wait for the secrets to be recreated before restarting Nessie.

**Longer-term fix**: Set `--enable-certificate-owner-ref=false` in `cert-manager-values.yaml` so Secrets outlive Certificate CR deletes.

**Note**: The `cert-manager apicheck` one-off error seen in GCP logs on April 2nd was a separate event (apiserver connectivity probe run once during cert-manager startup), not the cause of this incident.

---

### Incident: RayJob `ValidationFailed` — TTL on non-shutdown job

**Symptom**: `kubectl get rayjob -n ray` showed `ray-train-job` stuck in `ValidationFailed` immediately on creation.

**Root cause**: The RayJob spec had `shutdownAfterJobFinishes: false` (the default when the field is missing) combined with `ttlSecondsAfterFinished` set. KubeRay validation rejects this combination — TTL cleanup only makes sense if the job shuts down after finishing.

**Fix**: Ensure `shutdownAfterJobFinishes: true` is set in the RayJob template when `ttlSecondsAfterFinished` is also set. The Helm template in [k8s/ray-cluster/templates/rayjob.yaml](k8s/ray-cluster/templates/rayjob.yaml) should have both fields present.

---

### Incident: `ImagePullBackOff` — arm64 image pushed to amd64 cluster

**Symptom**: Worker pods stuck in `ImagePullBackOff`. Error: `no match for platform in manifest: not found`.

**Root cause**: Docker image was built on Apple Silicon (arm64) without specifying target platform. GKE nodes are x86-64 (amd64). The arm64 manifest was pushed to the registry; GKE nodes couldn't pull it.

**Fix**: Always build with explicit platform flag:
```bash
docker buildx build --platform linux/amd64 --push \
  -t asia-south1-docker.pkg.dev/gen-ai-pritha/ray-platform/ray-pipeline:2.10.0 .
```

---

### Incident: Rebuilt image still missing packages — Docker layer cache

**Symptom**: After adding `torch` to `requirements.txt` and rebuilding, the running workers still couldn't import torch. Log: `ModuleNotFoundError: No module named 'torch'`.

**Root cause**: Docker layer cache served the old `pip install` layer because `requirements.txt` content hash hadn't changed in Docker's view (the file was sometimes cached from before the edit). Workers used stale pods from before the image push; `imagePullPolicy: Always` only re-pulls on pod creation, not while a pod is running.

**Fix (image)**: Force a clean rebuild to bust the pip cache:
```bash
docker buildx build --platform linux/amd64 --no-cache --push ...
```

**Fix (pods)**: Delete running pods to force KubeRay to recreate them with the new image:
```bash
kubectl delete pod -n ray -l ray.io/node-type=worker
kubectl delete pod -n ray -l ray.io/node-type=head
```
Verify new image digest after restart:
```bash
kubectl get pod -n ray -l ray.io/node-type=worker \
  -o jsonpath='{range .items[*]}{.metadata.name}: {.status.containerStatuses[0].imageID}{"\n"}{end}'
```

---

### Incident: `PENDING_NODE_ASSIGNMENT` — insufficient `worker_pool` slots

**Symptom**: Ray Dashboard showed one task stuck in `Waiting for scheduling: 1`. `TorchTrainer` with `num_workers=2` needed 2 `worker_pool` slots but only 1 worker pod existed (holding the single `worker_pool: 1` resource).

**Root cause**: `worker.initialReplicas` was set to 1. The second training worker had no slot. Ray's in-tree autoscaler could not provision a new node because the head pod was missing K8s API egress in the NetworkPolicy (separate incident — autoscaler sidecar needed K8s API to create pods).

**Fix**: Scale workers up manually:
```bash
helm upgrade ray-cluster ./k8s/ray-cluster \
  --namespace ray \
  --set worker.initialReplicas=2 \
  --reuse-values
```
GKE cluster autoscaler then provisions the second Spot node automatically.

---

### Incident: `torch.distributed` AllReduce blocked by NetworkPolicy

**Symptom**: Training job failed with `RayActorError` in `WorkerGroup.add_workers()`. Error: connection refused between worker pods during DDP process group init.

**Root cause**: PyTorch's `gloo` backend (used for CPU distributed training) requires direct peer-to-peer TCP connections between all worker processes. The `default-deny-all` NetworkPolicy in the `ray` namespace had no worker-to-worker ingress rule. The head→worker and worker→head rules existed but not worker↔worker.

**Fix**: Added two rules to [k8s/app/networkpolicy.yaml](k8s/app/networkpolicy.yaml):

1. New `allow-workers-from-workers` ingress policy:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-workers-from-workers
  namespace: ray
spec:
  podSelector:
    matchLabels:
      ray.io/node-type: worker
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              ray.io/node-type: worker
```

2. Worker→worker egress added to `allow-worker-egress`:
```yaml
- to:
    - podSelector:
        matchLabels:
          ray.io/node-type: worker
```

Applied with `kubectl apply -f k8s/app/networkpolicy.yaml`.

---

### Incident: HuggingFace SSL certificate verification failure on training workers

**Symptom**: Training worker logs showed `SSLCertVerificationError: certificate verify failed: unable to get local issuer certificate` when loading `SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')`.

**Root cause**: `REQUESTS_CA_BUNDLE=/etc/ssl/nessie/ca.crt` is set on all Ray pods to make pyiceberg trust Nessie's self-signed TLS cert. This env var overrides Python's `requests` library system CA bundle **globally** — including HuggingFace Hub's network checks. HuggingFace does a HEAD request to `huggingface.co` to check if a cached model is up-to-date; that request fails SSL verification because the Nessie self-signed CA doesn't cover `huggingface.co`.

**Why the model pre-download didn't fully fix it**: The Dockerfile pre-downloads model weights at build time. However, `SentenceTransformer()` still does a HEAD request by default to check if the local cache is stale, even if all files are present.

**Fix**: Set `HF_HUB_OFFLINE=1` to skip all network calls to HuggingFace Hub and load exclusively from the pre-baked cache:

1. In `raycluster.yaml` worker env (covers normal pod starts):
```yaml
- name: HF_HUB_OFFLINE
  value: "1"
```

2. Inside `train_loop_per_worker()` before the import (covers RayTrainWorker sub-processes which may not inherit pod env):
```python
def train_loop_per_worker(config: dict) -> None:
    import os
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from sentence_transformers import SentenceTransformer, ...
```

**Key insight**: `RayTrainWorker` processes are spawned as separate Ray actors. Pod-level env vars propagate to Ray workers in most cases, but setting it in code is defensive insurance and works regardless of how the process is launched.

---

### Incident: `sentence_transformers` v3 API incompatibility with raw PyTorch DataLoader

**Symptom**: Training job failed with `TypeError: default_collate: batch must contain tensors, numpy arrays, numbers, dicts or lists; found <class 'sentence_transformers.readers.InputExample.InputExample'>`.

**Root cause**: `sentence_transformers` v3 changed the internal training API. The code used the v2 pattern: `InputExample` objects + `CosineSimilarityLoss` + a standard PyTorch `DataLoader`. In v3, `InputExample` is still importable but is no longer designed to flow through PyTorch's `default_collate`. The old pattern worked when sentence_transformers managed the DataLoader internally (via `model.fit()`), but breaks when a raw `DataLoader` is used (as required by `ray.train.torch.TorchTrainer`).

**Fix needed**: Replace `InputExample` + `CosineSimilarityLoss` with raw tensor operations compatible with PyTorch's `DataLoader`:
- Use a custom `Dataset` that returns `(text_a, text_b, label)` tuples
- Use a custom `collate_fn` that tokenizes text pairs into tensors
- Replace `CosineSimilarityLoss` with `torch.nn.CosineEmbeddingLoss` (note: expects labels as `+1`/`-1`, not `1.0`/`0.0`)
- Replace `build_training_pairs()` negative label `0.0` with `-1.0`

**Status**: Not yet fixed. This is the current blocker for the training job.

---

### Accessing the Ray Dashboard without port-forward

The cluster is private — no direct access from laptop. Two options:

**Option A — `kubectl port-forward` via bastion proxy (recommended for interactive use)**:
```bash
# Terminal 1: SSH tunnel to bastion (keeps proxy alive)
gcloud compute ssh ray-platform-bastion \
  --tunnel-through-iap --project=gen-ai-pritha --zone=asia-south1-b \
  -- -L 8888:localhost:8888

# Terminal 2: port-forward through the proxy
HTTPS_PROXY=http://localhost:8888 kubectl port-forward \
  svc/ray-cluster-head-svc 8265:8265 -n ray

# Browser: http://localhost:8265
```

**Option B — SOCKS5 proxy (accesses in-cluster IPs directly)**:
```bash
# SSH with SOCKS5 proxy on localhost:1080
ssh -D 1080 -N <bastion-user>@<bastion-ip>

# Configure browser to use SOCKS5 proxy at localhost:1080
# Then navigate directly to the head pod IP: http://10.1.x.x:8265
```
Option A is simpler. Option B is useful when you know the pod IP and don't want to set up port-forward for every service.
