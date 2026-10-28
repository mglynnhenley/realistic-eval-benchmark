"""Published eval-awareness probes, downloaded and converted to `Probe`; nothing is trained here.

Source: Abdelnabi & Salem, "The Hawthorne Effect in Reasoning Models: Evaluating and Steering Test
Awareness" (arXiv 2505.14617), https://github.com/microsoft/Test_Awareness_Steering (MIT). One linear probe per
layer (`model_<i>.pth`: `fc.weight [2, d]`, `fc.bias [2]`, class 1 = evaluation-aware) read from
`output_hidden_states[i]`, averaged over spans of the model's own reasoning that show (or lack) awareness.
"""

from __future__ import annotations

import io

import numpy as np

from refund_eval.awareness.probe import Probe

REPO = "https://github.com/microsoft/Test_Awareness_Steering"
RAW = "https://raw.githubusercontent.com/microsoft/Test_Awareness_Steering/main/scripts/output_models"

# name: (Hugging Face model, directory, the paper's chosen layer, its held-out accuracy there; Table 2)
PUBLISHED = {
    "qwen3-32b": ("Qwen/Qwen3-32B", "qwen3_from_evidence_negative_awareness_positive_awareness_avg_mlp", 63, 0.951),
    "qwq-32b": ("Qwen/QwQ-32B", "qwq_from_evidence_negative_awareness_positive_awareness_avg_mlp", 62, 0.954),
    "r1-distill-qwen-32b": ("deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
                            "deepseek_qwen_from_evidence_negative_awareness_positive_awareness_avg_mlp", 62, 0.927),
}
DEFAULT = "qwen3-32b"


def convert(weight, bias, *, model: str, hidden_index: int, accuracy: float | None = None, source: str = "") -> Probe:
    """A two-class linear head on `output_hidden_states[hidden_index]` as a `Probe` whose score is the aware logit
    margin (class 1 minus class 0); that hidden state is the output of decoder layer `hidden_index - 1`."""
    if hidden_index < 1:
        raise ValueError("hidden state 0 is the embeddings, not a decoder layer")
    weight, bias = np.asarray(weight, dtype=np.float64), np.asarray(bias, dtype=np.float64)
    return Probe(model, hidden_index - 1, weight[1] - weight[0], float(bias[0] - bias[1]), 1.0,
                 accuracy=accuracy, pool="mean", source=source)


def fetch(name: str = DEFAULT, layer: int | None = None) -> Probe:
    """Download one published probe; `layer` uses the paper's numbering (default: the layer it chose)."""
    import httpx
    import torch

    model, directory, chosen, accuracy = PUBLISHED[name]
    index = chosen if layer is None else layer
    url = f"{RAW}/{directory}/model_{index}.pth"
    response = httpx.get(url, follow_redirects=True, timeout=60)
    response.raise_for_status()
    state = torch.load(io.BytesIO(response.content), map_location="cpu", weights_only=True)
    return convert(state["fc.weight"].float().numpy(), state["fc.bias"].float().numpy(), model=model,
                   hidden_index=index, accuracy=accuracy if index == chosen else None, source=url)
