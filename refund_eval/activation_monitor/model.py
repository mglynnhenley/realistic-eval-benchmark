"""Local-only Hugging Face model loading and claim-span activation extraction."""

from __future__ import annotations

import importlib.metadata
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from refund_eval.activation_monitor.core import claim_char_span, claim_token_indices


@dataclass
class LocalModel:
    model: Any
    tokenizer: Any
    identity: dict[str, Any]
    hidden_dim: int
    num_layers: int
    max_context: int


def _config_value(config: Any, *names: str) -> int:
    for name in names:
        value = getattr(config, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    raise ValueError(f"Model config must declare one of: {', '.join(names)}")


def _identity(model_dir: Path, config: Any) -> dict[str, Any]:
    path = model_dir.resolve()
    repo_id = None
    revision = getattr(config, "_commit_hash", None)
    if path.parent.name == "snapshots" and path.parent.parent.name.startswith("models--"):
        repo_id = path.parent.parent.name.removeprefix("models--").replace("--", "/")
        if re.fullmatch(r"[0-9a-f]{40}", path.name):
            revision = path.name
    return {
        "name": repo_id or path.name,
        "revision": revision if isinstance(revision, str) else None,
        "local_path": str(path),
    }


def load_local_model(model_dir: str | Path) -> LocalModel:
    directory = Path(model_dir).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"--model-dir must be a local model directory: {directory}")
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as error:
        raise RuntimeError("Activation monitoring requires the optional awareness dependencies (torch, transformers)") from error
    tokenizer = AutoTokenizer.from_pretrained(
        str(directory), local_files_only=True, trust_remote_code=False, use_fast=True
    )
    if not getattr(tokenizer, "is_fast", False):
        raise ValueError("A fast tokenizer with offset mappings is required")
    model = AutoModelForCausalLM.from_pretrained(
        str(directory), local_files_only=True, trust_remote_code=False
    )
    model.to(torch.device("cpu"))
    model.eval()
    hidden_dim = _config_value(model.config, "hidden_size", "n_embd", "d_model")
    num_layers = _config_value(model.config, "num_hidden_layers", "n_layer", "num_layers")
    context_limits = [
        value for value in (
            getattr(model.config, "max_position_embeddings", None),
            getattr(model.config, "n_positions", None),
            getattr(tokenizer, "model_max_length", None),
        )
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**7
    ]
    if not context_limits:
        raise ValueError("Model config/tokenizer must declare a finite context limit")
    return LocalModel(model, tokenizer, _identity(directory, model.config), hidden_dim, num_layers,
                      min(context_limits))


def extract_claim_vector(local: LocalModel, example: dict[str, Any], layer_index: int) -> list[float]:
    if not 0 <= layer_index < local.num_layers:
        raise ValueError("layer_index is outside decoder output layers")
    prompt, claim_start, claim_end = claim_char_span(example["evidence_context"], example["claim"])
    encoded = local.tokenizer(
        prompt, return_offsets_mapping=True, return_tensors="pt", truncation=False
    )
    offsets = encoded.pop("offset_mapping", None)
    if offsets is None:
        raise ValueError("Fast tokenizer did not return character offset mappings")
    token_offsets = [tuple(map(int, offset)) for offset in offsets[0].tolist()]
    token_indices = claim_token_indices(token_offsets, claim_start, claim_end)
    if len(token_offsets) > local.max_context:
        raise ValueError(
            f"Example {example['id']} has {len(token_offsets)} tokens, over model context {local.max_context}; refusing truncation"
        )
    try:
        import torch
    except ImportError as error:
        raise RuntimeError("Activation monitoring requires torch") from error
    inputs = {key: value.to("cpu") for key, value in encoded.items() if key in {"input_ids", "attention_mask"}}
    if "input_ids" not in inputs:
        raise ValueError("Tokenizer did not return input_ids")
    with torch.no_grad():
        outputs = local.model(**inputs, output_hidden_states=True, use_cache=False)
    hidden = outputs.hidden_states[layer_index + 1][0, token_indices, :].mean(dim=0).detach().cpu().tolist()
    vector = [float(value) for value in hidden]
    if len(vector) != local.hidden_dim or any(not math.isfinite(value) for value in vector):
        raise ValueError("Model returned an invalid claim activation vector")
    return vector


def library_versions() -> dict[str, str]:
    return {name: importlib.metadata.version(distribution) for name, distribution in (
        ("torch", "torch"), ("transformers", "transformers"), ("huggingface_hub", "huggingface-hub")
    )}
