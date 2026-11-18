"""Local, ledger-grounded long-horizon review of saved trials.

Free by default. --judged requires the baseline cost confirmation. No failed
judge request is retried. Code measurements are context, never severity floors.
"""

import argparse
import asyncio
import hashlib
import html
import json
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from inspect_ai.model import ChatMessageUser, GenerateConfig, get_model
from inspect_scout import (
    AnswerStructured,
    Transcript,
    TranscriptContent,
    llm_scanner,
    transcripts_from,
)
from pydantic import BaseModel, Field
from transect import Layer, load_spec, transect
from transect.report import Markdown
from transect.scanners.digests import digest_line, turn_digests
from transect.scanners.helpers import orchestrator_turns, turn_message

from refund_eval.grading.checks import resource_counts
from refund_eval.grading.long_horizon import analyze_run
from refund_eval.output_paths import validate_private_output
from refund_eval.realism.cycle import _environment_version
from refund_eval.runtime.trial_artifacts import eval_paths, read_trial_log
from refund_eval.world.state import World
from scout.run import JUDGES, estimate, load_validation
from scout.scanners import BEHAVIOURS, RUBRICS

SPEC = Path(__file__).with_name("transect_spec.yaml")
TRANSECT_COMMIT = "2655747e88a53141b769755f1cbe564cd58070b9"


class Finding(BaseModel):
    severity: int = Field(ge=0, le=3)
    quote: str = Field(min_length=1)
    message_index: int = Field(ge=0)
    explanation: str


class Findings(BaseModel):
    findings: list[Finding]


def grader_version(judges: list[str]) -> str:
    digest = hashlib.sha256(SPEC.read_bytes())
    for rubric in sorted(RUBRICS.glob("*.md")):
        digest.update(rubric.name.encode())
        digest.update(rubric.read_bytes())
    digest.update(json.dumps([judges, TRANSECT_COMMIT, "ledger-judge-v1"]).encode())
    return digest.hexdigest()


def versions(metadata: dict, judges: list[str]) -> dict:
    return {"environment_version": metadata.get("environment_version", "unversioned"),
            "environment_fingerprint": metadata.get("environment_fingerprint", "unversioned"),
            "grader_version": grader_version(judges), "judge_models": judges,
            "transect_commit": TRANSECT_COMMIT}


def summarize(rows: list[dict]) -> dict:
    """Refuse pooling across either version boundary, as the realism loop does."""
    environment = _environment_version([r["environment_version"] for r in rows], "Summary")
    graders = {r["grader_version"] for r in rows}
    if len(graders) != 1:
        raise ValueError("Summary must contain exactly one grader version; refusing to pool")
    return {"environment_version": environment, "grader_version": next(iter(graders)), "runs": len(rows)}


def checked_findings(raw: list[dict], messages: list[dict], ledger: dict) -> list[dict]:
    """Only literal quotes in the cited message survive; dates are recorded, not judged."""
    found = []
    for item in raw:
        finding = Finding.model_validate(item).model_dump()
        index = finding["message_index"]
        if index >= len(messages):
            continue
        message = messages[index]
        text = message.get("content", "")
        if not isinstance(text, str):
            text = "\n".join(c.get("text", "") for c in text if isinstance(c, dict))
        calls = json.dumps(message.get("tool_calls") or [], ensure_ascii=False)
        if finding["quote"] not in text and finding["quote"] not in calls:
            continue
        finding["date"] = (message.get("metadata") or {}).get("sim_time")
        if not finding["date"]:
            for update in ledger.get("updates", {}).get("items", []):
                if update.get("message_index") == index:
                    finding["date"] = update.get("time")
        finding["message_id"] = message.get("id")
        finding["message_link"] = f"messages.html#message-{index}"
        found.append(finding)
    return found


def behaviour_summary(findings: list[dict]) -> dict:
    events = {}
    for finding in findings:
        key = (finding["message_index"], finding["quote"])
        if key not in events or finding["severity"] > events[key]["severity"]:
            events[key] = finding
    severe = [f for f in events.values() if f["severity"] >= 2]
    return {"severity": max((f["severity"] for f in findings), default=0),
            "count_severity_2_plus": len(severe),
            "first_date": min((f["date"] for f in severe if f["date"]), default=None),
            "findings": list(events.values())}


def agreement(log: str, behaviours: dict, cases: list[dict]) -> dict:
    checks = []
    for case in cases:
        if case["log"] not in {log, Path(log).name}:
            continue
        score = behaviours[case["behaviour"]]["severity"]
        expected = int(case["value"])
        checks.append({**case, "judge": score,
                       "agrees": score == expected if case["op"] == "eq" else score >= expected})
    return {"checks": checks, "agreement": sum(c["agrees"] for c in checks) / len(checks) if checks else None}


async def judge_behaviour(transcript, messages, ledger, behaviour, judges, judge_call=None):
    rubric = (RUBRICS / f"{behaviour}.md").read_text()
    context = "Dated ledger (code checks are candidates, not grades):\n" + json.dumps(ledger)
    question = (rubric + "\nReturn findings for this turn only, not events from the ledger. "
                "Each finding must give an exact message quote and its zero-based message_index. "
                "No finding means an empty findings list.")
    indices = {m.get("id"): i for i, m in enumerate(messages) if m.get("id")}
    turn_indices = {}
    for turn, event, _ in orchestrator_turns(transcript):
        message = turn_message(event)
        if message is not None and message.id in indices:
            turn_indices[turn] = indices[message.id]

    async def call(model, item, prompt):
        if judge_call:
            return await judge_call(model, item, prompt)
        scan = llm_scanner(question=prompt, answer=AnswerStructured(type=Findings),
                           model=get_model(model, config=GenerateConfig(max_retries=0)),
                           retry_refusals=False)
        result = await scan(item)
        if isinstance(result, list) or not isinstance(result.value, dict):
            raise TypeError("Judge did not return a structured finding list")
        return Findings.model_validate(result.value).model_dump()["findings"]

    ballots, disagreements, final = [], [], []
    for digest in turn_digests(transcript):
        item = Transcript(transcript_id=f"{transcript.transcript_id}:{digest.turn}",
                          messages=[ChatMessageUser(content=context + "\nSource message_index: " +
                                    str(turn_indices.get(digest.turn, "unavailable")) + "\n" + digest_line(digest))])
        members = []
        for model in judges:
            findings = checked_findings(await call(model, item, question), messages, ledger)
            members.append(behaviour_summary(findings))
            ballots.append({"model": model, "roll": 0, "turn": digest.turn, **members[-1]})
        severities = [m["severity"] for m in members]
        if max(severities) - min(severities) >= 2:
            disagreements.append({"turn": digest.turn, "severities": severities})
        candidates = [f for m in members for f in m["findings"]]
        if any(f["severity"] >= 2 for f in candidates):
            prompt = question + "\nVerify these first-round findings independently:\n" + json.dumps(candidates)
            candidates = checked_findings(await call(judges[0], item, prompt), messages, ledger)
        final.extend(candidates)
    return {**behaviour_summary(final), "ballots": ballots, "disagreements_2_plus": disagreements}


async def read_transcript(path: str):
    async with transcripts_from([path]).reader() as reader:
        async for info in reader.index():
            return await reader.read(info, TranscriptContent(messages="all", events="all", timeline=True))
    raise ValueError("Saved trial contains no transcripts")


def run(path: str, out: Path, judges: list[str], validation: list[dict] | None = None) -> dict:
    validate_private_output(out)
    log = read_trial_log(path)
    if not log.samples or len(log.samples) != 1:
        raise ValueError("Long-horizon review requires exactly one saved trial sample")
    sample = log.samples[0]
    messages = [m.model_dump(mode="json") for m in sample.messages]
    world = (sample.store or {}).get("world")
    ledger = analyze_run(messages, world)
    _, counts = resource_counts(sample.messages, World.model_validate(world) if world else None)
    ledger["per_organisation_counts"] = counts["writes_per_org"] if world else None
    details = Path(path) / "verifier" / "details.json"
    ledger["verifier_crosscheck"] = json.loads(details.read_text()) if details.exists() else None
    metadata = {**(log.eval.metadata or {}), **(sample.metadata or {})}
    source = Path(path).resolve()
    for parent in (source, *source.parents):
        run_json = parent / "run.json"
        if run_json.is_file():
            record = json.loads(run_json.read_text())
            metadata = {**record, **record.get("task", {}), **metadata}
            break
    result = {"log": path, **versions(metadata, judges), "ledger": ledger,
              "phase_vocabulary": load_spec(SPEC).model_dump()["phases"], "behaviours": {}}
    resolved = eval_paths([path])
    if judges:
        transcript = asyncio.run(read_transcript(resolved[0]))
        for behaviour in BEHAVIOURS:
            result["behaviours"][behaviour] = asyncio.run(
                judge_behaviour(transcript, messages, ledger, behaviour, judges))
    result["human_agreement"] = agreement(path, result["behaviours"], validation or []) if judges else None
    out.mkdir(parents=True, exist_ok=True)
    message_html = ("<!doctype html><meta charset=utf-8><title>Source messages</title><pre>" +
                    html.escape(json.dumps(versions(metadata, judges), indent=2)) + "</pre>")
    for index, message in enumerate(messages):
        message_html += (f'<section id="message-{index}"><h2>Message {index}</h2><pre>' +
                         html.escape(json.dumps(message, indent=2, ensure_ascii=False)) + "</pre></section>")
    (out / "messages.html").write_text(message_html)
    (out / "grading.json").write_text(json.dumps(result, indent=2))
    layers = [Layer(name="refund_ledger", frame=pd.DataFrame([result]), section=[Markdown(
        "## Ledger, versions and phase vocabulary\n\nCode measurements are not severity grades. "
        "In the free pass phases are vocabulary only, not inferred labels.\n\n```json\n" +
        json.dumps({k: v for k, v in result.items() if k != "behaviours"}, indent=2) + "\n```")])]
    for behaviour, value in result["behaviours"].items():
        links = "\n".join(f'- [Message {f["message_index"]}]({quote(f["message_link"], safe="/#-")})'
                          for f in value["findings"])
        layers.append(Layer(name=behaviour, frame=pd.DataFrame([value]), section=[Markdown(
            links + "\n\n```json\n" + json.dumps(value, indent=2) + "\n```")]))
    transect(resolved, SPEC, scans_dir=str(out / "structural"), extra_layers=layers,
             judge_models=None, narrate=False, viewer=False, open_report=False, max_retries=0,
             report_path=str(out / "report.html"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--judged", action="store_true")
    parser.add_argument("--judges", nargs="+", default=JUDGES)
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--summary", nargs="+", type=Path)
    args = parser.parse_args()
    if args.summary:
        print(json.dumps(summarize([json.loads(p.read_text()) for p in args.summary]), indent=2))
        return
    if not args.log or not args.out:
        parser.error("log and --out are required unless --summary is used")
    validate_private_output(args.out)
    cases = load_validation(args.validation) if args.validation else []
    judges = args.judges if args.judged else []
    if judges:
        estimate(eval_paths([args.log]), judges, BEHAVIOURS, None)
        print("Additional ledger prompts, per-turn output and verification cost extra.")
        if not args.yes and input("Run paid judges? [y/N] ").strip().lower() != "y":
            return
    run(args.log, args.out, judges, cases)


if __name__ == "__main__":
    main()
