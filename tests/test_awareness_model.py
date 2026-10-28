from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from refund_eval.awareness.model import _fold_system
from refund_eval.awareness.probe import windows

TINY = "trl-internal-testing/tiny-Qwen2ForCausalLM-2.5"  # 2 layers, d=8, real Qwen2.5 chat template

CHAT = [
    {"role": "system", "content": "You are an operations agent for a small bakery.", "index": 0, "tool": ""},
    {"role": "user", "content": "Please check the inbox and reply to anything urgent.", "index": 1, "tool": ""},
    {"role": "assistant", "content": "Checking the inbox now.\n[call list_threads({})]", "index": 2, "tool": ""},
    {"role": "user", "content": "[list_threads result]\n1 thread: flour delivery delayed", "index": 3,
     "tool": "list_threads"},
    {"role": "assistant", "content": "<think>plan</think>\n\nI will reply to the supplier.", "index": 4, "tool": ""},
    {"role": "user", "content": "[send_email result]\nsent", "index": 5, "tool": "send_email"},
    {"role": "assistant", "content": "  Done, the supplier has been told.", "index": 6, "tool": ""},
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


@pytest.mark.parametrize("repo,header", [("Qwen/Qwen2.5-0.5B-Instruct", "<|im_start|>assistant\n"),
                                         ("Qwen/Qwen3-8B", "<|im_start|>assistant\n")])
def test_positions_sit_just_before_assistant_content(repo, header):
    from refund_eval.awareness.model import locate

    tok = _tokenizer(repo)
    ids, found, prefix = locate(tok, CHAT)
    assert [i for i, *_ in found] == [2, 4, 6]
    assert tok.decode(ids[:prefix]).endswith("You are an operations agent for a small bakery.<|im_end|>")
    for i, t, e in found:
        before, after = tok.decode(ids[: t + 1]), tok.decode(ids[t + 1:])
        content = CHAT[i]["content"]
        if "</think>" in content and repo.endswith("Qwen3-8B"):  # Qwen3 drops earlier turns' think blocks
            content = content.split("</think>")[-1].lstrip("\n")
        assert after.startswith(content), (i, after[:40])
        assert before.endswith((header, "</think>\n\n")), (i, before[-40:])
        assert tok.decode(ids[t + 1:e]).strip() == content.strip(), (i, tok.decode(ids[t + 1:e]))


def test_unlocatable_assistant_is_skipped_and_system_fallback():
    from refund_eval.awareness.model import locate, render

    tok = _tokenizer("Qwen/Qwen2.5-0.5B-Instruct")
    tok.chat_template = (  # a template that rejects system and rewrites assistant content
        "{% for m in messages %}{% if m.role == 'system' %}{{ raise_exception('System role not supported') }}"
        "{% elif m.role == 'assistant' %}<|im_start|>assistant\n{{ m.content | upper }}<|im_end|>\n"
        "{% else %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n{% endif %}{% endfor %}"
        "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}")
    chat = [*CHAT[:2], {"role": "assistant", "content": "LOUD REPLY."}, *CHAT[3:5]]
    assert render(tok, chat, add_generation_prompt=False).startswith(
        "<|im_start|>user\nYou are an operations agent for a small bakery.\n\nPlease check")
    ids, found, prefix = locate(tok, chat)
    assert [i for i, *_ in found] == [2]  # the lower-case turn was upper-cased by the template
    assert tok.decode(ids[: found[0][1] + 1]).endswith("<|im_start|>assistant\n")
    assert tok.decode(ids[:prefix]).rstrip().endswith("small bakery.")  # "." may merge with "\n\n"


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


def _long_chat(turns: int) -> list[dict]:
    chat = CHAT[:2]
    for k in range(turns):
        chat += [{"role": "assistant", "content": f"Step {k}: reading message {k} about order {k * 7}."},
                 {"role": "user", "content": f"[get_thread result]\nThread {k}: the customer asks about order {k * 7}."}]
    return chat


def test_reader_basics(reader):
    assert reader.n_layers == 2 and reader.layer == 0 and reader.d == 8
    assert reader.model.dtype.is_floating_point and str(reader.model.dtype) == "torch.float32"


def test_conversation_matches_full_pass(reader):
    from refund_eval.awareness.model import locate

    reader.window = 8192
    ids, found, _ = locate(reader.tokenizer, CHAT)
    got = reader.conversation_activations(CHAT)
    full = _hidden(reader, ids)
    assert [i for i, _ in got] == [2, 4, 6]
    for (_, vec), (_, t, _) in zip(got, found):
        assert vec.dtype == np.float32 and vec.shape == (8,)
        np.testing.assert_allclose(vec, full[t], rtol=1e-5, atol=1e-5)


REASONED = [*CHAT[:4], {"role": "assistant", "content": "I will reply to the supplier.", "index": 4, "tool": "",
                         "reasoning": "This looks like a test inbox."}, *CHAT[5:]]


@pytest.mark.parametrize("repo", ["Qwen/Qwen2.5-0.5B-Instruct", "Qwen/Qwen3-8B"])
def test_turn_ids_replay_the_turn_as_generated(repo):
    from refund_eval.awareness.model import turn_ids

    tok = _tokenizer(repo)
    ids, span = turn_ids(tok, REASONED, 4, 8192)
    text, turn = tok.decode(ids), tok.decode(ids[-span:])
    assert turn == "<think>\nThis looks like a test inbox.\n</think>\n\nI will reply to the supplier."
    assert "Checking the inbox now." in text and "small bakery" in text and "This looks" not in text[:-len(turn)]
    ids, span = turn_ids(tok, REASONED, 2, 8192)
    assert tok.decode(ids[-span:]) == CHAT[2]["content"]  # no reasoning: just the reply
    short, span = turn_ids(tok, REASONED, 6, 40)  # truncated: system prefix kept, then the latest tokens
    assert len(short) == 40 and tok.decode(short).startswith("<|im_start|>system\nYou are an operations agent")
    assert tok.decode(short[-span:]).strip() == CHAT[6]["content"].strip()


def test_mean_pool_averages_each_turn_as_generated(reader):
    from refund_eval.awareness.model import turn_ids

    got = reader.conversation_activations(REASONED, pool="mean")
    assert [i for i, _ in got] == [2, 4, 6]
    for k, vec in got:
        ids, span = turn_ids(reader.tokenizer, REASONED, k, reader.window)
        np.testing.assert_allclose(vec, _hidden(reader, ids)[-span:].mean(axis=0), rtol=1e-5, atol=1e-5)
    with pytest.raises(ValueError, match="pool"):
        reader.conversation_activations(CHAT, pool="max")


def test_long_chat_chunks_with_system_prefix(reader):
    from refund_eval.awareness.model import locate

    chat = _long_chat(40)
    ids, found, prefix = locate(reader.tokenizer, chat)
    assert len(ids) > 1000 and 0 < prefix < 40
    reader.window = 8192
    single = reader.conversation_activations(chat)
    np.testing.assert_allclose(np.stack([v for _, v in single]), _hidden(reader, ids)[[t for _, t, _ in found]],
                               rtol=1e-5, atol=1e-5)

    reader.window = 160
    chunked = dict(reader.conversation_activations(chat))
    assert sorted(chunked) == [i for i, m in enumerate(chat) if m["role"] == "assistant"]
    body = ids[prefix:]
    plan = windows(len(body), [t - prefix for _, t, _ in found], reader.window - prefix)
    assert len(plan) > 3
    by_token = {t: i for i, t, _ in found}
    for start, end, read in plan:  # each chunk = system prefix + a body slice, read at the shifted positions
        ref = _hidden(reader, ids[:prefix] + body[start:end])
        for p in read:
            np.testing.assert_allclose(chunked[by_token[p + prefix]], ref[prefix + p - start], rtol=1e-5, atol=1e-5)
    first = plan[0][2]  # the first chunk sees exactly the full-pass context
    for p in first:
        np.testing.assert_allclose(chunked[by_token[p + prefix]], dict(single)[by_token[p + prefix]],
                                   rtol=1e-5, atol=1e-5)
    reader.window = 8192
