"""Model reader: chat rendering, turn replay and activations, against cached tiny models (never downloads)."""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from refund_eval.awareness.model import _fold_system

TINY = "trl-internal-testing/tiny-Qwen2ForCausalLM-2.5"  # 2 layers, d=8, real Qwen2.5 chat template

CHAT = [
    {"role": "system", "content": "You are an operations agent for a small bakery.", "index": 0, "tool": ""},
    {"role": "user", "content": "Please check the inbox and reply to anything urgent.", "index": 1, "tool": ""},
    {"role": "assistant", "content": "Checking the inbox now.\n[call list_threads({})]", "index": 2, "tool": ""},
    {"role": "user", "content": "[list_threads result]\n1 thread: flour delivery delayed", "index": 3,
     "tool": "list_threads"},
    {"role": "assistant", "content": "I will reply to the supplier.", "index": 4, "tool": "",
     "reasoning": "This looks like a test inbox."},
    {"role": "user", "content": "[send_email result]\nsent", "index": 5, "tool": "send_email"},
    {"role": "assistant", "content": "Done, the supplier has been told.", "index": 6, "tool": ""},
]


def _local(repo: str) -> str:
    """Path of a cached Hub snapshot (tests never download); skip if absent."""
    hub = pytest.importorskip("huggingface_hub")
    try:
        return hub.snapshot_download(repo, local_files_only=True)
    except Exception as exc:  # noqa: BLE001 - any cache miss means skip
        pytest.skip(f"{repo} not cached: {exc}")


def _tokenizer(repo: str):
    transformers = pytest.importorskip("transformers")
    return transformers.AutoTokenizer.from_pretrained(_local(repo))


def test_module_imports_without_torch():
    code = ("import sys, refund_eval.awareness.model; "
            "assert not {'torch', 'transformers'} & set(sys.modules), 'heavy import at module level'")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_fold_system_into_first_user():
    sys_, user, bot = ({"role": r, "content": c} for r, c in [("system", "S"), ("user", "U"), ("assistant", "A")])
    assert _fold_system([sys_, user, bot]) == [{"role": "user", "content": "S\n\nU"}, bot]
    assert _fold_system([sys_, bot]) == [{"role": "user", "content": "S"}, bot]
    assert _fold_system([user, bot]) == [user, bot]


def test_render_folds_system_when_the_template_rejects_it():
    from refund_eval.awareness.model import render

    tok = _tokenizer("Qwen/Qwen2.5-0.5B-Instruct")
    tok.chat_template = (
        "{% for m in messages %}{% if m.role == 'system' %}{{ raise_exception('System role not supported') }}"
        "{% else %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n{% endif %}{% endfor %}")
    assert render(tok, CHAT[:2], add_generation_prompt=False).startswith(
        "<|im_start|>user\nYou are an operations agent for a small bakery.\n\nPlease check")


@pytest.mark.parametrize("repo", ["Qwen/Qwen2.5-0.5B-Instruct", "Qwen/Qwen3-8B"])
def test_turn_ids_replay_the_turn_as_generated(repo):
    from refund_eval.awareness.model import turn_ids

    tok = _tokenizer(repo)
    ids, span = turn_ids(tok, CHAT, 4, 8192)
    text, turn = tok.decode(ids), tok.decode(ids[-span:])
    assert turn == "<think>\nThis looks like a test inbox.\n</think>\n\nI will reply to the supplier."
    assert text[:-len(turn)].endswith("<|im_start|>assistant\n")
    assert "Checking the inbox now." in text and "small bakery" in text and "This looks" not in text[:-len(turn)]
    ids, span = turn_ids(tok, CHAT, 2, 8192)
    assert tok.decode(ids[-span:]) == CHAT[2]["content"]  # no reasoning: just the reply
    short, span = turn_ids(tok, CHAT, 6, 40)  # truncated: system prefix kept, then the latest tokens
    assert len(short) == 40 and tok.decode(short).startswith("<|im_start|>system\nYou are an operations agent")
    assert tok.decode(short[-span:]) == CHAT[6]["content"]


@pytest.fixture(scope="module")
def reader():
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from refund_eval.awareness.model import Reader

    return Reader(_local(TINY), layer=0, device="cpu")


def _hidden(reader, ids: list[int]) -> np.ndarray:
    """Reference: full forward pass, `output_hidden_states[layer + 1]` (= output of decoder layer `layer`)."""
    import torch

    with torch.no_grad():
        out = reader.model(input_ids=torch.tensor([ids]), output_hidden_states=True)
    return out.hidden_states[reader.layer + 1][0].float().numpy()


def test_reader_basics(reader):
    assert reader.n_layers == 2 and reader.layer == 0
    assert str(reader.model.dtype) == "torch.float32"


def test_activations_average_each_turn_as_generated(reader):
    from refund_eval.awareness.model import turn_ids

    got = reader.conversation_activations(CHAT)
    assert [i for i, _ in got] == [2, 4, 6]
    for k, vec in got:
        ids, span = turn_ids(reader.tokenizer, CHAT, k, reader.window)
        assert vec.dtype == np.float32 and vec.shape == (8,)
        np.testing.assert_allclose(vec, _hidden(reader, ids)[-span:].mean(axis=0), rtol=1e-5, atol=1e-5)
