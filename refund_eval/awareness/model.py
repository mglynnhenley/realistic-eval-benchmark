"""Read an open-weights model's residual stream over agent turns (torch and transformers imported lazily).

`Reader.conversation_activations` returns one vector per assistant message, from decoder layer `layer`:
- `pool="mean"` (published probes): each turn replayed as generated (`turn_ids`), averaged over its own tokens.
- `pool="last"`: the token just before each turn's content, in one chunked rendering of the whole chat (`locate`).
"""

from __future__ import annotations

import numpy as np

from refund_eval.awareness.probe import windows

NEEDLE = 200  # chars of each message used to find it in the rendered chat


class _Stop(Exception):
    """Raised by the layer hook to end the forward pass early."""


def _plain(messages: list[dict]) -> list[dict]:
    return [{"role": m["role"], "content": m["content"]} for m in messages]


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
    plain = _plain(messages)
    try:
        return tokenizer.apply_chat_template(plain, tokenize=False, add_generation_prompt=add_generation_prompt)
    except Exception:
        if not plain or plain[0]["role"] != "system":
            raise
        return tokenizer.apply_chat_template(
            _fold_system(plain), tokenize=False, add_generation_prompt=add_generation_prompt
        )


def _needles(content: str) -> list[str]:
    """Stripped prefixes to search for; also the text after a `</think>` block, which some templates drop."""
    out = [content.strip()[:NEEDLE]]
    if "</think>" in content:
        out.append(content.split("</think>")[-1].strip()[:NEEDLE])
    return [n for n in out if n]


def locate(tokenizer, messages: list[dict]) -> tuple[list[int], list[tuple[int, int, int]], int]:
    """Tokenise the rendered chat; return `(ids, [(list_pos, token_before_content, content_end)], prefix_len)`.

    Content tokens are `token_before_content + 1` up to `content_end` (exclusive); if the template rewrote the
    message, the span stops at the end of the matched prefix.
    `prefix_len` covers the system message (plus trailing special tokens) so chunks can repeat it.
    Messages are found in order with `str.find`; assistant messages that can't be found are skipped.
    """
    text = render(tokenizer, messages, add_generation_prompt=False)
    enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = list(enc["input_ids"])
    ends = np.maximum.accumulate(np.array([e for _, e in enc["offset_mapping"]] or [0]))

    def first_ending_after(char: int) -> int:
        return int(np.searchsorted(ends, char, side="right"))

    found, prefix, cursor = [], 0, 0
    for i, m in enumerate(messages):
        for k, needle in enumerate(_needles(m["content"])):
            at = text.find(needle, cursor)
            if at >= 0:
                break
        else:
            continue
        cursor = at + len(needle)
        if m["role"] == "assistant":
            whole, begin = m["content"].strip(), at
            lead = len(m["content"]) - len(m["content"].lstrip())  # leading whitespace the template kept
            if k == 0 and lead and text.startswith(m["content"][:lead], at - lead):
                at -= lead
            token = first_ending_after(at)  # first token holding content
            end = begin + len(whole) if k == 0 and text.startswith(whole, begin) else cursor
            if 0 < token < len(ids):
                found.append((i, token - 1, max(first_ending_after(end - 1) + 1, token + 1)))
        elif m["role"] == "system" and i == 0:
            whole = m["content"].strip()
            end = at + len(whole) if text.startswith(whole, at) else cursor
            prefix = min(first_ending_after(end - 1) + 1, len(ids))
            special = set(tokenizer.all_special_ids)
            while prefix < len(ids) and ids[prefix] in special:
                prefix += 1
    return ids, found, prefix


class Reader:
    """Hidden states of decoder layer `layer` (its residual-stream output) over whole chats."""

    def __init__(self, model_id: str, layer: int | None = None, device: str | None = None,
                 dtype: str = "bfloat16", window: int = 8192):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        torch_dtype = torch.float32 if device == "cpu" else getattr(torch, dtype)
        self.model_id, self.device, self.window = model_id, device, window
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        if not self.tokenizer.is_fast:
            raise ValueError(f"{model_id}: a fast tokenizer is needed for offset mapping")
        self.model = AutoModelForCausalLM.from_pretrained(model_id, dtype=torch_dtype).to(device).eval()
        self.layers = _decoder_layers(self.model)
        self.n_layers = len(self.layers)
        self.layer = self.n_layers // 2 if layer is None else layer
        if not 0 <= self.layer < self.n_layers:
            raise ValueError(f"layer {self.layer} out of range for {self.n_layers} layers")
        config = self.model.config
        self.d = (config.get_text_config() if hasattr(config, "get_text_config") else config).hidden_size

    def _read(self, ids: list[int], positions: list[int]) -> np.ndarray:
        """One forward pass over `ids`, stopping after `self.layer`; returns `[len(positions), d]` float32."""
        import torch

        got = {}

        def hook(_module, _inputs, output):
            hidden = output[0] if isinstance(output, tuple) else output
            got["h"] = hidden[0, positions].float().cpu().numpy()
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

    def conversation_activations(self, messages: list[dict], pool: str = "last") -> list[tuple[int, np.ndarray]]:
        """`(list position, activation)` for each assistant message: read just before its content in one rendering
        of the whole chat (`pool="last"`), or averaged over the turn as generated (`pool="mean"`, see `turn_ids`)."""
        if pool == "mean":
            return [(k, self._mean_turn(messages, k)) for k, m in enumerate(messages) if m["role"] == "assistant"]
        if pool != "last":
            raise ValueError(f"unknown pool {pool!r}")
        ids, found, prefix_len = locate(self.tokenizer, messages)
        found = [(i, t) for i, t, _ in found if t >= prefix_len]
        if not found:
            return []
        if self.window - prefix_len < 2:
            raise ValueError(f"system prefix ({prefix_len} tokens) leaves no room in window {self.window}")
        prefix, body = ids[:prefix_len], ids[prefix_len:]
        vectors = {}
        for start, end, read in windows(len(body), [t - prefix_len for _, t in found], self.window - prefix_len):
            acts = self._read(prefix + body[start:end], [prefix_len + p - start for p in read])
            vectors.update({p + prefix_len: a for p, a in zip(read, acts)})
        return [(i, vectors[t]) for i, t in found]

    def _mean_turn(self, messages: list[dict], k: int) -> np.ndarray:
        ids, span = turn_ids(self.tokenizer, messages, k, self.window)
        return self._read(ids, list(range(len(ids) - span, len(ids)))).mean(axis=0)


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
    turn, reasoning = messages[k], messages[k].get("reasoning", "")
    generated = turn["content"]
    if reasoning:
        opened = "" if prompt.endswith("<think>\n") else "<think>\n"
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
