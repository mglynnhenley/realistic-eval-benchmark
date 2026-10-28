"""Read an open-weights model's residual stream over agent turns (torch and transformers imported lazily).

`Reader.conversation_activations` replays each assistant turn as the model generated it (`turn_ids`) and averages
decoder layer `layer`'s output over the turn's own tokens, which is how the published probes were trained.
"""

from __future__ import annotations

import numpy as np


class _Stop(Exception):
    """Raised by the layer hook to end the forward pass early."""


def _fold_system(messages: list[dict]) -> list[dict]:
    """Merge a leading system message into the first user message, for templates without a system role."""
    if not messages or messages[0]["role"] != "system":
        return messages
    system, rest = messages[0]["content"], list(messages[1:])
    if rest and rest[0]["role"] == "user":
        return [{"role": "user", "content": f"{system}\n\n{rest[0]['content']}"}, *rest[1:]]
    return [{"role": "user", "content": system}, *rest]


def render(tokenizer, messages: list[dict], *, add_generation_prompt: bool) -> str:
    """Render a chat with the tokenizer's template, folding system into user if the template rejects it."""
    plain = [{"role": m["role"], "content": m["content"]} for m in messages]
    try:
        return tokenizer.apply_chat_template(plain, tokenize=False, add_generation_prompt=add_generation_prompt)
    except Exception:
        if not plain or plain[0]["role"] != "system":
            raise
        return tokenizer.apply_chat_template(
            _fold_system(plain), tokenize=False, add_generation_prompt=add_generation_prompt
        )


def turn_ids(tokenizer, messages: list[dict], k: int, window: int) -> tuple[list[int], int]:
    """Tokens for assistant message `k` as the model generated it, and how many trailing tokens are the turn.

    The context is the chat before `k` with the generation prompt (earlier turns without their reasoning, as
    reasoning templates show them), then `<think>reasoning</think>` and the visible reply. Long contexts keep the
    system message and the most recent tokens, so the result fits in `window`.
    """
    system = messages[:1] if messages and messages[0]["role"] == "system" else []
    start, size = k, 0
    while start > len(system) and size < window * 8:  # ~chars; enough history to fill the window
        start -= 1
        size += len(messages[start]["content"])
    while start < k and messages[start]["role"] != "user":  # templates expect a user message first
        start += 1
    prompt = render(tokenizer, system + messages[start:k], add_generation_prompt=True)
    generated, reasoning = messages[k]["content"], messages[k].get("reasoning", "")
    if reasoning:
        opened = "" if prompt.endswith("<think>\n") else "<think>\n"  # QwQ and R1-Distill prompts already open it
        generated = f"{opened}{reasoning}\n</think>\n\n{generated}"
    head = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    tail = tokenizer(generated, add_special_tokens=False)["input_ids"]
    if len(head) + len(tail) <= window:
        return head + tail, len(tail)
    keep = len(tokenizer(render(tokenizer, system, add_generation_prompt=False), add_special_tokens=False)
               ["input_ids"]) if system else 0
    if window - keep < 2:
        raise ValueError(f"system prefix ({keep} tokens) leaves no room in window {window}")
    ids = head[:keep] + (head + tail)[-(window - keep):]
    return ids, min(len(tail), window - keep)


class Reader:
    """Decoder layer `layer`'s residual-stream output, averaged over each agent turn."""

    def __init__(self, model_id: str, layer: int | None = None, device: str | None = None,
                 dtype: str = "bfloat16", window: int = 8192):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        torch_dtype = torch.float32 if device == "cpu" else getattr(torch, dtype)
        self.model_id, self.device, self.window = model_id, device, window
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForCausalLM.from_pretrained(model_id, dtype=torch_dtype).to(device).eval()
        self.layers = _decoder_layers(self.model)
        self.n_layers = len(self.layers)
        self.layer = self.n_layers // 2 if layer is None else layer
        if not 0 <= self.layer < self.n_layers:
            raise ValueError(f"layer {self.layer} out of range for {self.n_layers} layers")

    def conversation_activations(self, messages: list[dict]) -> list[tuple[int, np.ndarray]]:
        """`(list position, activation)` for each assistant message: one forward pass per turn."""
        return [(k, self._mean(*turn_ids(self.tokenizer, messages, k, self.window)))
                for k, m in enumerate(messages) if m["role"] == "assistant"]

    def _mean(self, ids: list[int], span: int) -> np.ndarray:
        """Forward pass over `ids`, stopping after `self.layer`; the mean of its output over the last `span` tokens."""
        import torch

        got = {}

        def hook(_module, _inputs, output):
            hidden = output[0] if isinstance(output, tuple) else output
            got["h"] = hidden[0, -span:].float().mean(dim=0).cpu().numpy()
            raise _Stop

        handle = self.layers[self.layer].register_forward_hook(hook)
        try:
            with torch.no_grad():
                self.model(input_ids=torch.tensor([ids], device=self.device), use_cache=False)
        except _Stop:
            pass
        finally:
            handle.remove()
        return got["h"].astype(np.float32)


def _decoder_layers(model):
    """The text decoder's layer list: first `nn.ModuleList` named `layers` outside vision/audio towers."""
    import torch

    try:
        root = model.get_decoder() or model
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        root = model
    for name, module in root.named_modules():
        if (isinstance(module, torch.nn.ModuleList) and name.split(".")[-1] == "layers"
                and not any(k in name for k in ("vision", "visual", "audio"))):
            return module
    raise ValueError(f"no decoder `layers` ModuleList found in {type(model).__name__}")
