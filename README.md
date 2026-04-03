# Ray AI Platform on GKE

ML training pipeline running on KubeRay, writing to Apache Iceberg via Nessie, deployed on GKE.

---

## Setup

### 1. Services to enable

```bash
gcloud services enable \
  container.googleapis.com \
  compute.googleapis.com \
  iam.googleapis.com \
  artifactregistry.googleapis.com \
  iap.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --project=<PROJECT_ID>
```

### 2. GCP resources to create before Terraform apply.

**Terraform state bucket:**
```bash
gcloud storage buckets create gs://<PROJECT_ID>-tf-state \
  --location=asia-south1 \
  --uniform-bucket-level-access
```

**Artifact Registry:**
```bash
gcloud auth configure-docker asia-south1-docker.pkg.dev
gcloud artifacts repositories create ray-platform \
  --repository-format=docker \
  --location=asia-south1 \
  --project=<PROJECT_ID>
```

**Workload Identity Federation for GitHub Actions** 
```bash
gcloud iam workload-identity-pools create github-pool \
  --project=<PROJECT_ID> \
  --location=global

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project=<PROJECT_ID> \
  --location=global \
  --workload-identity-pool=github-pool \
  --display-name="GitHub Provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="attribute.repository=='pritha-athena93/contextq'"

gcloud iam service-accounts create github-sa

gcloud projects add-iam-policy-binding gen-ai-pritha \
  --member="serviceAccount:github-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"

gcloud iam service-accounts add-iam-policy-binding \
  github-sa@<PROJECT_ID>.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/attribute.repository/pritha-athena93/contextq"

gcloud projects add-iam-policy-binding gen-ai-pritha \
  --member="serviceAccount:github-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/container.clusterViewer"

gcloud storage buckets add-iam-policy-binding gs://gen-ai-pritha-tf-state \
  --member="serviceAccount:github-sa@gen-ai-pritha.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding gen-ai-pritha \
  --member="serviceAccount:github-sa@gen-ai-pritha.iam.gserviceaccount.com" \
  --role="roles/viewer"

gcloud projects add-iam-policy-binding gen-ai-pritha \
  --member="serviceAccount:github-sa@gen-ai-pritha.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountAdmin"

gcloud projects add-iam-policy-binding gen-ai-pritha \
  --member="serviceAccount:github-sa@gen-ai-pritha.iam.gserviceaccount.com" \
  --role="roles/compute.admin"

gcloud projects add-iam-policy-binding gen-ai-pritha \
  --member="serviceAccount:github-sa@gen-ai-pritha.iam.gserviceaccount.com" \
  --role="roles/storage.admin"
```

### 3. Configure Terraform variables

Using the default value for this assignment, override values can be set using tfvars or via command-line arguments.

### 4. Deploy infrastructure

```bash
cd terraform
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

This creates:
- VPC + private subnet
- GKE cluster (`ray-platform`) with head-pool (On-Demand), worker-pool (Spot), and train-pool (GPU)
- GCS buckets: `gen-ai-pritha-ray-raw-data` and `gen-ai-pritha-ray-iceberg-warehouse`
- GCP Service Account `ray-sa` with `storage.objectAdmin` and `aiplatform.user` roles
- Workload Identity binding: `ray-ksa` (K8s SA) -> `ray-sa` (GCP SA)

### 5. Build and push docker images.

Can be done via the deploy github action too. Or follow the instructions below -
```bash
cd docker
docker build -t asia-south1-docker.pkg.dev/gen-ai-pritha/ray-platform/ray-pipeline:<image_tag> .
docker push asia-south1-docker.pkg.dev/gen-ai-pritha/ray-platform/ray-pipeline:<image_tag>
```

### 6. Configure kubectl

```bash
gcloud container clusters get-credentials ray-platform \
  --region=asia-south1 \
  --project=<PROJECT_ID>
```

For private cluster access, use the bastion host via IAP tunnel:

```bash
gcloud compute ssh ray-platform-bastion \
  --tunnel-through-iap \
  --project=<PROJECT_ID> \
  --zone=asia-south1-b \
  -- -L 8888:localhost:8888

export HTTPS_PROXY=localhost:8888
kubectl get nodes
```

### 6. Helm setup

First create the namespaces -

```bash
kubectl apply -f k8s/namespaces/namespaces.yaml
```

Next add the third party helm chart repos -
```bash
helm repo add jetstack https://charts.jetstack.io
helm repo add nessie https://charts.projectnessie.org
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
```

Install cert-manager and reflector, make sure to pin the right versions -

```bash
helm upgrade --install cert-manager cert-manager/cert-manager -n cert-manager --debug --version 1.20.1 -f k8s/third-party/cert-manager-values.yaml

helm upgrade --install reflector emberstack/reflector \
  --namespace reflector \
  --create-namespace \
  --values k8s/third-party/reflector-values.yaml --debug --version 10.0.26
```

Create cert-issuer and cert for nessie (iceberg), create postgres PV and secret before installing the helm charts

```bash
kubectl apply -f k8s/third-party/nessie-tls.yaml
kubectl get cert -n nessie-ns

kubectl apply -f k8s/third-party/postgres.yaml

kubectl create secret generic nessie-postgres-creds \
  -n nessie-ns \
  --from-literal=username=nessie \
  --from-literal=password=<password>

helm upgrade --install nessie nessie/nessie \
  -n nessie-ns \
  -f k8s/third-party/nessie-values.yaml --debug --version 0.107.3
```

Finally install the kuberay operation and then the ray cluster -

```bash
kubectl apply -f k8s/app/networkpolicy.yaml

helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  -n kuberay-system --debug

helm upgrade --install ray-cluster k8s/ray-cluster \
  -n ray \
  --set global.projectId=gen-ai-pritha \
  --set image.tag=latest \
  --atomic
```

## Spot Instance Strategy

### Why Spot for workers

GCP Spot VMs cost 60–91% less than On-Demand and can be preempted at any time.

1. **Head pod on On-Demand** (`head-pool`) — the head manages task scheduling and the object store. Head death = cluster restart. It must not be on Spot.

2. **Workers on Spot** (`worker-pool`) — tasks are automatically retried when a worker disappears. The `EntityResolver` actor is automatically restarted on a surviving node.

3. **Workers on GPU** (`train-pool`) - I read that tasks for training models should run on Spot Instances, but I have never tried, and in my person GCP project, I don't have quota to experiment with GPU.

3. **Autoscaling to zero** — `minReplicas: 0` means worker nodes are terminated when idle. You only pay for Spot VMs while the job is running.

### How scheduling is enforced

```yaml
# Worker pods land only on worker-pool nodes
nodeSelector:
  cloud.google.com/gke-nodepool: worker-pool

# Worker pods tolerate the Spot eviction taint
tolerations:
  - key: cloud.google.com/gke-spot
    value: "true"
    effect: NoSchedule
```

```python
# EntityResolver actor pinned to worker nodes via Ray custom resource
@ray.remote(resources={"worker_pool": 1})
class EntityResolver: ...
```

The custom resource `worker_pool: 1` is advertised by worker nodes via `rayStartParams.resources`. The head does not advertise it, so the actor can never land there.

### What happens during preemption

1. GCP preempts the Spot VM (no warning for Spot instances)
2. Ray head detects heartbeat timeout (~10–30s)
3. Tasks on the evicted worker are marked `FAILED` and re-queued
4. KubeRay spawns a replacement worker pod
5. GKE cluster autoscaler provisions a new Spot VM (~60–90s)

---

## Failure Simulation

```bash
./scripts/simulate-spot-failure.sh
```

Use `--dry-run` to preview steps without deleting anything. Observe the Ray Dashboard task timeline — there will be a gap at eviction time followed by resumed execution on surviving workers.
