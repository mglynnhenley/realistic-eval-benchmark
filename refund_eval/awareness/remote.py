"""Modal GPU app wrapping the awareness jobs; only imported for `--modal` runs."""

from __future__ import annotations

import os
import sys

import modal

DEFAULT_GPU = "A100-80GB"
TIMEOUT = 6 * 60 * 60
HF_CACHE = "/root/.cache/huggingface"

app = modal.App("refund-eval-awareness")
image = (
    modal.Image.debian_slim(python_version=f"{sys.version_info.major}.{sys.version_info.minor}")
    .uv_pip_install(  # the versions uv.lock resolves for the `awareness` extra
        "torch==2.14.1", "transformers==5.21.0", "accelerate==1.15.0", "numpy==2.5.3")
    .env({"HF_HOME": HF_CACHE})
    .add_local_python_source("refund_eval")
)
# Weights (about 65 GB for a 32B model) are downloaded once into this volume and reused by later runs.
hf_cache = modal.Volume.from_name("refund-eval-awareness-hf-cache", create_if_missing=True)
# Only needed for gated models; passed through from the local environment, never read from a file here.
secrets = [modal.Secret.from_dict({"HF_TOKEN": os.environ["HF_TOKEN"]})] if os.environ.get("HF_TOKEN") else []
options = {"image": image, "gpu": DEFAULT_GPU, "timeout": TIMEOUT, "volumes": {HF_CACHE: hf_cache}, "secrets": secrets}


@app.function(**options)
def score(probe: dict, conversations: list[dict], window: int) -> list[list[dict]]:
    from refund_eval.awareness.jobs import score_job

    return score_job(probe, conversations, window=window)


def run_score(probe: dict, conversations: list[dict], *, window: int = 8192, gpu: str = DEFAULT_GPU) -> list[list[dict]]:
    """Run `score_job` on a Modal GPU."""
    with modal.enable_output(), app.run():
        return score.with_options(gpu=gpu).remote(probe, conversations, window)
