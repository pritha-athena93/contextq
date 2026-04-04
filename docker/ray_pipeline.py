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


@ray.remote(resources={"worker_pool": 1})
def ingest_data(source_name: str, source_path: str, n: int = 1000) -> pd.DataFrame:
    if source_path:
        import ray.data as rd
        ds = rd.read_parquet(source_path)
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
        from google.api_core import exceptions as gex

        for attempt in range(_GEMINI_MAX_RETRIES):
            try:
                return self._model.generate_content(prompt).text.strip()

            except gex.ResourceExhausted:
                # 429 — exponential backoff
                if attempt == _GEMINI_MAX_RETRIES - 1:
                    raise
                wait = _GEMINI_BASE_BACKOFF * (2 ** attempt)
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
        text = self._call_with_retry(prompt)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        result = []
        for line in lines:
            if ". " in line:
                line = line.split(". ", 1)[1]
            elif ") " in line:
                line = line.split(") ", 1)[1]
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
            "uri":       NESSIE_URI,
            "ref":       "main",
            "warehouse": ICEBERG_WAREHOUSE,
        },
    )

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
    import torch
    from sentence_transformers import SentenceTransformer, losses, InputExample
    from torch.utils.data import DataLoader
    import ray.train

    pairs = config["pairs"]
    epochs = config.get("epochs", 3)
    batch_size = config.get("batch_size", 16)

    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    model = ray.train.torch.prepare_model(model)

    train_examples = [InputExample(texts=[a, b], label=label) for a, b, label in pairs]
    dataloader = DataLoader(train_examples, shuffle=True, batch_size=batch_size)
    loss_fn = losses.CosineSimilarityLoss(model)

    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-5)
    optimizer = ray.train.torch.prepare_optimizer(optimizer)

    for epoch in range(epochs):
        total_loss = 0.0
        for batch in dataloader:
            features, labels = batch
            loss_value = loss_fn(features, labels)
            optimizer.zero_grad()
            loss_value.backward()
            optimizer.step()
            total_loss += loss_value.item()

        avg_loss = total_loss / len(dataloader)
        ray.train.report({"epoch": epoch + 1, "loss": avg_loss})
        print(f"Epoch {epoch + 1}/{epochs} — loss: {avg_loss:.4f}")

    if ray.train.get_context().get_world_rank() == 0 and MODEL_OUTPUT_PATH:
        model.save(MODEL_OUTPUT_PATH)
        print(f"Model saved to {MODEL_OUTPUT_PATH}")


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
            use_gpu=True,
            resources_per_worker={"GPU": 1, "train_worker": 1},
        ),
    )

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
    batches = [combined.iloc[i : i + batch_size] for i in range(0, len(combined), batch_size)]
    resolved_batches = ray.get([resolver.resolve_batch.remote(b) for b in batches])
    resolved_df = pd.concat(resolved_batches, ignore_index=True)
    print(f"Resolved {len(resolved_df)} entities.")

    write_to_iceberg(resolved_df)
    print(f"Pipeline complete. {len(resolved_df)} records written to corporate_registry.")


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
