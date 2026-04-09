import argparse
import os
import ray
import pandas as pd
import numpy as np
import pyarrow as pa
from faker import Faker
from pyiceberg.catalog import load_catalog

fake = Faker()

NESSIE_URI        = os.environ.get("NESSIE_URI",        "https://nessie.nessie-ns:19120/iceberg")
ICEBERG_WAREHOUSE = os.environ.get("ICEBERG_WAREHOUSE", "gs://ray-iceberg-warehouse/warehouse")
GCP_PROJECT       = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
VERTEX_REGION     = os.environ.get("VERTEX_REGION",     "asia-south1")
MODEL_OUTPUT_PATH = os.environ.get("MODEL_OUTPUT_PATH", "")
NUM_WORKERS       = int(os.environ.get("NUM_WORKERS", "2"))

SOURCE_A_PATH = os.environ.get("SOURCE_A_PATH", "")
SOURCE_B_PATH = os.environ.get("SOURCE_B_PATH", "")

# ray.remote is a decorator
# ingest_data = ray.remote(ingest_data) -- wraps the function
# without this it would run in head pod driver process
# head pod has two things running inside it:
# Head Pod
# ├── Ray Head process    — the cluster brain (tracks workers, schedules tasks, 
# │                         runs the dashboard). Always running.
# │
# └── Driver process      — YOUR script (ray_pipeline.py). The main() that 
#                           calls ingest_data.remote(), ray.get(), etc.
#                           Starts when the RayJob submitter triggers it.
#                           Exits when the pipeline finishes.

@ray.remote(resources={"worker_pool": 1})
def ingest_data(source_name: str, source_path: str, n: int = 1000) -> pd.DataFrame:
    if source_path:
        # inside the if block so that it imports only if needed
        # doesn't matter in this case as the dockerfile importa
        # entire ray package
        import ray.data as rd
        ds = rd.read_parquet(source_path)
        # converts whatever data structure you have into a pandas 
        # DataFrame — a table with named columns you can print, 
        # slice, and inspect easily
        df = ds.to_pandas()
        df["source"] = source_name
        return df

    return pd.DataFrame({
        "corporate_name": [fake.company() for _ in range(n)],
        "revenue": np.random.uniform(1e6, 1e10, n).round(2),
        "source": source_name,
    })


_GEMINI_BATCH_SIZE  = 50
_GEMINI_MAX_RETRIES = 5
_GEMINI_BASE_BACKOFF = 1.0  # seconds; doubles each retry for 429s

# ray task is a remote function execution
# Worker pod = machine
# Task = job run on that machine
# Actor = a long-lived process on that machine
@ray.remote(resources={"worker_pool": 1})
class EntityResolver:
    def __init__(self):
        import vertexai
        from vertexai.generative_models import GenerativeModel

        vertexai.init(project=GCP_PROJECT, location=VERTEX_REGION)
        self._model = GenerativeModel("gemini-2.5-flash")

    def _call_with_retry(self, prompt: str) -> str:
        """Call Gemini with retry on 429 and transient errors. Raises on exhaustion."""
        import time
        # low-level shared plumbing (retry logic, auth, exception types) 
        # that all Google Cloud libraries use under the hood
        from google.api_core import exceptions as gex

        for attempt in range(_GEMINI_MAX_RETRIES):
            try:
                return self._model.generate_content(prompt).text.strip()

            except gex.ResourceExhausted:
                # 429 — HTTP status code - "Too Many Requests - exponential backoff
                if attempt == _GEMINI_MAX_RETRIES - 1:
                    # re-raises the current exception, letting it propagate up
                    raise
                # 2 to the power of attempt
                wait = _GEMINI_BASE_BACKOFF * (2 ** attempt)
                # printed as (attempt 1/5) (attempt 2/5) etc
                # 1.0 to 1, :.0f - f = float, .0 = 0 decimal places
                print(f"[Gemini] 429 rate-limited (attempt {attempt + 1}/{_GEMINI_MAX_RETRIES}), "
                      f"retrying in {wait:.0f}s …")
                time.sleep(wait)

            except (gex.ServiceUnavailable, gex.InternalServerError):
                # 503/500 transient — fixed 2 s, max 3 attempts
                if attempt >= 2:
                    raise
                print(f"[Gemini] transient error (attempt {attempt + 1}), retrying in 2s …")
                time.sleep(2.0)

            # All other exceptions (auth, invalid arg, etc.) propagate immediately

    def _resolve_chunk(self, names: list[str]) -> list[str]:
        """Resolve one batch of names in a single Gemini call."""
        numbered = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(names))
        prompt = (
            "Return only the canonical legal names of these companies, "
            "one per line in the same order, prefixed with the same number. "
            "No explanations:\n" + numbered
        )

        # Return only the canonical legal names of these companies, 
        # one per line in the same order, prefixed with the same number. 
        # No explanations:
        # 1. Acme Corp
        # 2. Google LLC
        # 3. Apple Inc

        text = self._call_with_retry(prompt)

        # strip -- truncates,
        # splitlines -- returns a list comprising of the string of each line
        # output of text --
        # 1. Alphabet Inc.
        # 2. Apple Inc.
        # 3) Microsoft Corporation

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        result = []
        for line in lines:
            if ". " in line:
                line = line.split(". ", 1)[1]
            elif ") " in line:
                line = line.split(") ", 1)[1]
            # ["Alphabet Inc.", "Apple Inc.", "Microsoft Corporation"]
            result.append(line.strip())

        if len(result) != len(names):
            raise ValueError(
                f"Gemini returned {len(result)} names for {len(names)} inputs. "
                f"Raw response: {text[:300]}"
            )
        return result

    def resolve_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        import hashlib, time

        df = df.copy()
        names = df["corporate_name"].tolist()
        canonical_names: list[str] = []

        for i in range(0, len(names), _GEMINI_BATCH_SIZE):
            # batch further from 200 to 50
            chunk = names[i : i + _GEMINI_BATCH_SIZE]
            canonical_names.extend(self._resolve_chunk(chunk))
            # brief pause between batches to stay within RPM quota
            if i + _GEMINI_BATCH_SIZE < len(names):
                time.sleep(1.0)

        df["canonical_name"] = canonical_names
        df["canonical_id"]   = df["canonical_name"].str.lower().apply(
            lambda name: hashlib.sha256(name.encode()).hexdigest()[:16]
        )
        df["is_resolved"] = True
        return df



def write_to_iceberg(df: pd.DataFrame) -> None:
    from pyiceberg.exceptions import NoSuchTableError
    from pyiceberg.schema import Schema
    from pyiceberg.types import NestedField, StringType, DoubleType, BooleanType

    catalog = load_catalog(
        "nessie",
        **{
            # Nessie's REST API lives (in-cluster DNS)
            "uri":       NESSIE_URI,
            # Nessie branch to read/write. Like git
            "ref":       "main",
            # the location for actual data files
            "warehouse": ICEBERG_WAREHOUSE,
        },
    )

    # Iceberg organizes tables in namespaces — 
    # like a database/schema in SQL. 
    # default.corporate_registry means table corporate_registry in namespace default
    # "default"    # string
    # ("default")  # also a string — parentheses don't make a tuple
    # ("default",) # tuple with one element ← the comma does it
    if ("default",) not in catalog.list_namespaces():
        catalog.create_namespace("default")

    table_name  = "default.corporate_registry"
    arrow_table = pa.Table.from_pandas(df)

    try:
        table = catalog.load_table(table_name)
        table.overwrite(arrow_table)
    except NoSuchTableError:
        schema = Schema(
            NestedField(1, "corporate_name",  StringType(),  required=False),
            NestedField(2, "revenue",          DoubleType(),  required=False),
            NestedField(3, "source",           StringType(),  required=False),
            NestedField(4, "canonical_name",   StringType(),  required=False),
            NestedField(5, "canonical_id",     StringType(),  required=False),
            NestedField(6, "is_resolved",      BooleanType(), required=False),
        )
        table = catalog.create_table(table_name, schema=schema)
        table.overwrite(arrow_table)



def build_training_pairs() -> list[tuple[str, str, float]]:
    """Returns list of (text_a, text_b, label) where label=1.0 is a match."""
    try:
        catalog = load_catalog(
            "nessie",
            **{"uri": NESSIE_URI, "ref": "main", "warehouse": ICEBERG_WAREHOUSE},
        )
        table = catalog.load_table("default.corporate_registry")
        df = table.scan().to_pandas()
        if df.empty:
            raise ValueError("corporate_registry is empty")
    except Exception as e:
        print(f"WARNING: Could not read Iceberg table ({e}) — using synthetic pairs.")
        df = pd.DataFrame({
            "corporate_name": [fake.company() for _ in range(500)],
            "canonical_name": [fake.company() for _ in range(500)],
            "canonical_id":   [str(i % 100) for i in range(500)],
        })

    pairs: list[tuple[str, str, float]] = []

    grouped = df.groupby("canonical_id")["corporate_name"].apply(list)
    for names in grouped:
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pairs.append((names[i], names[j], 1.0))

    all_names = df["corporate_name"].tolist()
    canonical_ids = df.set_index("corporate_name")["canonical_id"].to_dict()
    rng = np.random.default_rng(42)
    neg_count = 0
    target_neg = len(pairs)
    attempts = 0
    while neg_count < target_neg and attempts < target_neg * 10:
        a, b = rng.choice(all_names, 2, replace=False)
        if canonical_ids.get(a) != canonical_ids.get(b):
            pairs.append((a, b, 0.0))
            neg_count += 1
        attempts += 1

    rng.shuffle(pairs)
    return pairs



def train_loop_per_worker(config: dict) -> None:
    import os
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from sentence_transformers import SentenceTransformer
    from torch.utils.data import DataLoader
    import ray.train

    pairs = config["pairs"]
    epochs = config.get("epochs", 3)
    batch_size = config.get("batch_size", 16)

    # SentenceTransformer - Python library that wraps transformer models
    # converts text → dense vector (embedding)
    # MiniLM is a version of BERT - Google's langugae model that reads text in both directions
    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    # find_unused_parameters=True because the BERT pooler (used for [CLS]-based
    # tasks) is part of the model but not used in mean-pooling sentence embeddings,
    # so it never receives gradients. DDP requires this flag when any parameters
    # are intentionally left out of the backward pass.
    model = ray.train.torch.prepare_model(
        model, parallel_strategy_kwargs={"find_unused_parameters": True}
    )

    # ray.train - ray's training library
    # ray.train.torch - submodule for PyTorch
    # returns the device this specific worker should use 
    # (cuda:0 - first GPU, cuda:1 - second GPU, cpu, etc.)
    # in case of time slicing - cuda:0 for worker 0, 1 etc
    # in case of mig - cuda:0 for worker 0 - first slice, cuda:1 for worker 1 - second slice
    device = ray.train.torch.get_device()

    # torch does the actual maths, ray.train.torch coordinates multiple PyTorch workers
    # loss rate - how wrong the model's prediction is
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)
    # optimiser - ensures gradient updates are synchronized across workers so all model copies update identically
    optimizer = ray.train.torch.prepare_optimizer(optimizer)
    # measures angle between two embedding vectors. 
    # Label 1.0 → penalizes if embeddings are far apart.
    # Label -1.0 → penalizes if embeddings are close. 
    # Forces similar company names to cluster together in vector space, dissimilar ones to diverge
    loss_fn = torch.nn.CosineEmbeddingLoss()

    # Keep texts as plain strings; collate into lists + label tensor.
    # CosineEmbeddingLoss expects 1.0 (similar) or -1.0 (dissimilar).
    # custom collate because default couldn't process str, str, float tuple
    # collate_fn - DataLoader - processes pairs one by one then combine them into a batch
    def collate_fn(batch):
        texts_a = [item[0] for item in batch]
        texts_b = [item[1] for item in batch]
        # torch tensor converts python list to pytorch tensor - arrary that can be read by GPU
        labels = torch.tensor(
            [1.0 if item[2] > 0.5 else -1.0 for item in batch],
            dtype=torch.float32,
        )
        return texts_a, texts_b, labels

    dataloader = DataLoader(pairs, shuffle=True, batch_size=batch_size, collate_fn=collate_fn)

    # Unwrap DDP to reach SentenceTransformer's tokenizer and forward.
    inner_model = model.module if hasattr(model, "module") else model
    tokenizer = inner_model.tokenizer

    for epoch in range(epochs):
        total_loss = 0.0
        model.train()
        for texts_a, texts_b, labels in dataloader:
            labels = labels.to(device)
            n = len(texts_a)
            # Encode both sets in ONE forward pass to avoid a shared internal
            # buffer (position_ids) being modified in-place between two separate
            # forward calls, which would corrupt the autograd graph.
            features = tokenizer(
                texts_a + texts_b,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=256,
            )
            features = {k: v.to(device) for k, v in features.items()}
            all_emb = model(features)["sentence_embedding"]
            emb_a, emb_b = all_emb[:n], all_emb[n:]
            optimizer.zero_grad()
            loss_value = loss_fn(emb_a, emb_b, labels)
            loss_value.backward()
            optimizer.step()
            total_loss += loss_value.item()

        avg_loss = total_loss / len(dataloader)
        ray.train.report({"epoch": epoch + 1, "loss": avg_loss})
        print(f"Epoch {epoch + 1}/{epochs} — loss: {avg_loss:.4f}")

    # ray.train.get_context().get_world_rank() - returns the worker rank - worker 0 is 0
    # ensures only one worker (here 0) saves the model
    if ray.train.get_context().get_world_rank() == 0 and MODEL_OUTPUT_PATH:
        import tempfile
        save_model = model.module if hasattr(model, "module") else model
        if MODEL_OUTPUT_PATH.startswith("gs://"):
            # SentenceTransformer.save() is local-only; save to a temp dir
            # then upload each file to GCS.
            # REQUESTS_CA_BUNDLE is set cluster-wide to the Nessie self-signed
            # CA, which breaks TLS to storage.googleapis.com. Unset it for the
            # upload so the system CA bundle is used instead.
            from google.cloud import storage as gcs_storage
            with tempfile.TemporaryDirectory() as tmp_dir:
                save_model.save(tmp_dir)
                gcs_path = MODEL_OUTPUT_PATH[5:]  # strip "gs://"
                bucket_name, blob_prefix = gcs_path.split("/", 1)
                nessie_ca = os.environ.pop("REQUESTS_CA_BUNDLE", None)
                try:
                    client = gcs_storage.Client()
                    bucket = client.bucket(bucket_name)
                    for root, _, files in os.walk(tmp_dir):
                        for fname in files:
                            local = os.path.join(root, fname)
                            rel = os.path.relpath(local, tmp_dir)
                            blob = bucket.blob(f"{blob_prefix}/{rel}")
                            blob.upload_from_filename(local)
                finally:
                    if nessie_ca:
                        os.environ["REQUESTS_CA_BUNDLE"] = nessie_ca
        else:
            save_model.save(MODEL_OUTPUT_PATH)
        print(f"Model saved to {MODEL_OUTPUT_PATH}")

# LLM generates the labeled data, model learns from it
def run_training() -> None:
    from ray.train import ScalingConfig
    from ray.train.torch import TorchTrainer

    print("Building training pairs from Iceberg...")
    pairs = build_training_pairs()
    print(f"Built {len(pairs)} training pairs.")

    trainer = TorchTrainer(
        train_loop_per_worker,
        train_loop_config={
            "pairs": pairs,
            "epochs": 3,
            "batch_size": 16,
        },
        scaling_config=ScalingConfig(
            num_workers=NUM_WORKERS,
            use_gpu=False,
            resources_per_worker={"worker_pool": 1},
        ),
    )
    
    # Kicks off the distributed training job. 
    # Ray spawns train_loop_per_worker on each worker pod, 
    # passes train_loop_config, and runs all epochs. 
    # Blocks until all workers finish. 
    # Returns metrics (loss etc.) reported via ray.train.report()
    result = trainer.fit()
    print(f"Training complete. Final metrics: {result.metrics}")



def run_pipeline(env: str) -> None:
    if env == "prod":
        if not SOURCE_A_PATH or not SOURCE_B_PATH:
            raise ValueError(
                "SOURCE_A_PATH and SOURCE_B_PATH must be set in prod mode."
            )
        a_path, b_path = SOURCE_A_PATH, SOURCE_B_PATH
    else:
        a_path, b_path = "", ""

    ds1_ref = ingest_data.remote("dataset_a", a_path)
    ds2_ref = ingest_data.remote("dataset_b", b_path)
    df1, df2 = ray.get([ds1_ref, ds2_ref])
    combined = pd.concat([df1, df2], ignore_index=True)
    print(f"Ingested {len(combined)} records from 2 sources.")

    resolver = EntityResolver.remote()
    batch_size = 200
    # iloc[0 : 200]   → rows 0–199
    # iloc[200 : 400] → rows 200–399
    # iloc[400 : 600] → rows 400–599
    # ...
    # iloc[1800:2000] → rows 1800–1999
    batches = [combined.iloc[i : i + batch_size] for i in range(0, len(combined), batch_size)]
    # ray.get() waits until the .remote() tasks finish in parallel
    resolved_batches = ray.get([resolver.resolve_batch.remote(b) for b in batches])
    resolved_df = pd.concat(resolved_batches, ignore_index=True)
    print(f"Resolved {len(resolved_df)} entities.")

    write_to_iceberg(resolved_df)
    print(f"Pipeline complete. {len(resolved_df)} records written to corporate_registry.")


# every Python file has a built-in variable __name__
# if run directly: python ray_pipeline.py -- __main__
# if imported by another file: import ray_pipeline -- ray_pipeline
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["pipeline", "train"],
        default="pipeline",
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default="dev",
    )
    args = parser.parse_args()

    ray.init()

    if args.mode == "pipeline":
        run_pipeline(args.env)
    else:
        run_training()
