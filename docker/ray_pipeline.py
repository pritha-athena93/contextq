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
def ingest_data(source_name: str, source_path: str = "", n: int = 1000) -> pd.DataFrame:
    if source_path:
        import ray.data as rd
        ds = rd.read_parquet(source_path)
        return ds.to_pandas()

    return pd.DataFrame({
        "corporate_name": [fake.company() for _ in range(n)],
        "revenue": np.random.uniform(1e6, 1e10, n).round(2),
        "source": source_name,
    })


@ray.remote(resources={"worker_pool": 1})
class EntityResolver:
    def __init__(self):
        self._model = None
        if not GCP_PROJECT:
            print("WARNING: GOOGLE_CLOUD_PROJECT not set — falling back to hash-based resolution.")
            return

        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel

            vertexai.init(project=GCP_PROJECT, location=VERTEX_REGION)
            self._model = GenerativeModel("gemini-2.5-flash")
        except Exception as e:
            print(f"WARNING: Vertex AI initialisation failed ({e}) — falling back to hash-based resolution.")

    def _llm_canonical_name(self, name: str) -> str:
        """Normalise a company name via Gemini, or hash-based fallback."""
        if self._model is None:
            import re
            canonical = re.sub(r"\b(Inc|LLC|Ltd|Corp|Co|Group|Holdings)\.?$", "", name, flags=re.I).strip()
            return canonical.title()

        try:
            prompt = (
                f"Return only the canonical legal name of this company, "
                f"no explanation: '{name}'"
            )
            response = self._model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            print(f"WARNING: Gemini call failed for '{name}' ({e}) — using hash fallback.")
            return name

    def resolve_batch(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["canonical_name"] = df["corporate_name"].apply(self._llm_canonical_name)
        import hashlib
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



def run_pipeline() -> None:
    ds1_ref = ingest_data.remote("dataset_a", SOURCE_A_PATH)
    ds2_ref = ingest_data.remote("dataset_b", SOURCE_B_PATH)
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
        help="pipeline: ingest + resolve + write to Iceberg (worker-pool). "
             "train: fine-tune entity matcher on GPU (train-pool).",
    )
    args = parser.parse_args()

    ray.init()

    if args.mode == "pipeline":
        run_pipeline()
    else:
        run_training()
