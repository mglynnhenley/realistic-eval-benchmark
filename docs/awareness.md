# Eval-awareness probe

A model that suspects it is being tested may behave differently from one that believes the task is real. The [realism loop](environment-improvement-loop.md#realism-loop) finds artefacts that an agent visibly noticed. A model can also register that something looks like a test without saying so. The awareness probe looks for that: it reads an open-weights model's internal activations at each agent turn and measures how far they point towards "this is an evaluation" rather than "this is real use".

The probe is optional and self-contained. The reasons behind its design are recorded in [`refund_eval/awareness/AGENTS.md`](../refund_eval/awareness/AGENTS.md). Nothing else in the repository uses it unless you pass its output to a realism command.

## How it works

We do not train a probe. `fetch` downloads a published one from Abdelnabi and Salem, [The Hawthorne Effect in Reasoning Models: Evaluating and Steering Test Awareness](https://arxiv.org/abs/2505.14617) (code and probes: [microsoft/Test_Awareness_Steering](https://github.com/microsoft/Test_Awareness_Steering), MIT licence). They released one linear probe per layer for three reasoning models:

| `fetch` name | Model | Paper's layer | Reported accuracy |
|---|---|---|---|
| `qwen3-32b` (default) | `Qwen/Qwen3-32B` | 63 | 95.1% |
| `qwq-32b` | `Qwen/QwQ-32B` | 62 | 95.4% |
| `r1-distill-qwen-32b` | `deepseek-ai/DeepSeek-R1-Distill-Qwen-32B` | 62 | 92.7% |

Each probe was trained on the model's own reasoning: sentences where it said it was, or was not, being tested, averaged over their tokens. The reported accuracy is on their held-out data, not ours. `fetch` uses the layer the paper chose unless `--layer` gives another (in the paper's numbering, where layer *i* is the output of decoder layer *i* − 1). The score is the probe's logit margin for "aware" over "not aware"; above 0 reads as test-aware.

`score` replays each agent turn the way the model generated it: the conversation so far, then the turn's reasoning (`<think>…</think>`) and its reply. It averages the probe's layer over the turn's own tokens, matching how the probe was trained, and records the message that came immediately before the turn as the likely trigger. Earlier turns appear without their reasoning, as they do in these models' chat templates.

There are two ways to use it:

- **Off-policy.** Replay any trial, for example one where Claude Haiku was the agent, through the open model. This asks: would this model, in the agent's seat, think it was being tested?
- **On-policy, with reasoning (recommended for hardening).** Run the open model itself as the agent with its reasoning returned, then probe the same weights: `realism trials --model openrouter/qwen/qwen3-32b --reasoning --history-tokens 24000 ...` (most OpenRouter providers of Qwen3-32B have a 41k-token context) (or `reasoning: true` in the `realism-cycle` workflow). The probe then reads the same kind of text it was trained on, and the turns where it rises point at the messages to make more realistic. The scores then describe the model that actually acted. `same_model` in the output records whether a trial's agent matches the probe model.

## Setup

The probe needs PyTorch, Transformers and Modal, which are an optional extra:

```bash
uv sync --extra awareness
uv run modal token new   # once, for GPU runs on Modal
```

`HF_TOKEN` in `.env` is optional; it is needed only for gated Hugging Face models and is passed to Modal as a secret. Without `--modal`, `score` runs locally on CUDA, Apple MPS or the CPU (`--device` overrides this). The 32B models need about 65 GB in bfloat16, so in practice use `--modal` with an 80 GB GPU.

## Fetch and score

`fetch` only downloads a small file from GitHub. **`score --modal` starts a paid GPU run** (default `A100-80GB`, set with `--gpu`). It makes no model API calls, but Modal bills the GPU time.

```bash
uv run --extra awareness python -m refund_eval.awareness fetch qwen3-32b \
  --out .context/awareness/probe.json
uv run --extra awareness python -m refund_eval.awareness score logs/example.eval \
  --probe .context/awareness/probe.json --out .context/awareness/awareness.json --modal
```

`score` accepts several logs and prints each trial's mean, maximum and share of test-like turns. `--window` sets the tokens per forward pass (default 8192).

Keep scores in ignored storage such as `.context/`: `awareness.json` quotes transcript text.

## The output

`awareness.json` has one entry per trial:

```json
{"model": "Qwen/Qwen3-32B", "layer": 62, "probe": ".context/awareness/probe.json",
 "trials": [{"log": "/abs/example.eval", "sample_id": "late", "epoch": 1,
             "target_model": "openrouter/anthropic/claude-haiku-4.5", "same_model": false,
             "turns": [{"index": 12, "score": -0.8,
                        "trigger": {"index": 11, "tool": "get_thread", "quote": "first 300 characters of the message before"}}],
             "mean": -0.4, "max": 1.3, "aware_fraction": 0.1}]}
```

`index` is the message's position in the Inspect sample. `aware_fraction` is the share of turns scoring above 0.

## Using it in the realism loop

All three uses are opt-in; without `--awareness` the realism commands behave as before.

- `realism open --awareness FILE...` adds findings for triage. Each is a turn that scored above 0, ranked by how much the score rose from the previous turn, with the trigger message as the quote. They carry `source: "awareness"` and have no agent reaction: a rise in the probe, not the agent remarking on something.
- `realism score LOGS --scans DIR --awareness FILE...` adds `awareness_per_trial` (the mean of per-trial mean scores), `awareness_ci` and `per_trial_awareness`.
- `realism compare --metric awareness` decides on the per-trial probe means instead of tell counts. Lower is better.

The `realism-loop` workflow accepts `awareness: [paths]`, alone or with logs, scans or probes. The `realism-cycle` workflow accepts `awarenessProbe` (a probe path), which scores every trial batch on Modal after the trials, optionally `awarenessGpu`, and `metric: "awareness"` to make the probe score the decision and plateau metric. With the awareness metric, the plateau is an absolute drop in mean probe score rather than a relative drop in tells. Tell counts are still reported. A failed awareness run stops an awareness-metric cycle but only logs a warning in a tells-metric one.

## Limitations

- The published probes were trained on reasoning text where the model talked about being tested. Turns without reasoning (off-policy runs, or trials without `--reasoning`) are a shift from that training data. Check the scores before trusting them, for example on turns you have labelled yourself.
- Each turn is one forward pass over up to `--window` tokens (default 8192), keeping the system prompt and the most recent history. The agent itself saw a longer history.
- The probes exist only for these three 32B models. On-policy runs therefore need one of them as the agent.
- Off-policy scores measure the open model's reading of a transcript, not the agent's. A different model may notice different things.
- Long transcripts are read in overlapping chunks, with the system prompt kept at the start of each. This approximates the agent's own truncated history; it is not identical to it.
- A rise in score after a message is a lead, not proof that the message caused it.
- GPU spend is not counted in the realism cycle's budget, which covers trial and scan costs only.
