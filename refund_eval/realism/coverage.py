"""Require complete, unambiguous tells coverage before comparing trial scores."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit


def source_identity(source: str) -> str:
    """Match local paths and file URIs without collapsing distinct log directories."""
    uri = urlsplit(str(source))
    if uri.scheme == "file":
        if uri.netloc not in ("", "localhost"):
            raise ValueError(f"Unsupported file host in scan source: {uri.netloc}")
        return str(Path(unquote(uri.path)).resolve())
    if uri.scheme:
        return str(source)
    return str(Path(str(source)).resolve())


def _object(value, label: str) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid {label} JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"Missing or invalid {label}")  # noqa: TRY004 -- invalid scan evidence
    return value


def _present(value) -> bool:
    # Scout dataframe missing fields may be pandas.NA, None or floating NaN.
    import pandas as pd

    return value is not None and not pd.isna(value) and str(value) != ""


def scan_rows(scans: str | Path, judge: str | None = None) -> list[dict]:
    """Read raw Scout rows, preserving empty answers, failures and duplicates."""
    from inspect_scout import scan_list, scan_results_df

    root = Path(scans)
    if not (root / "_scan.json").exists() and not any(root.glob("scan_id=*")):
        judges = sorted(d for d in root.iterdir() if d.is_dir())
        if judge:
            root = root / judge.split("/")[-1]
        elif len(judges) == 1:
            root = judges[0]
        else:
            raise ValueError("Select exactly one judge's scan directory")
    statuses = [s for s in scan_list(str(root)) if "tells" in s.spec.scanners]
    if not statuses:
        raise ValueError("No tells scan found")
    rows = []
    for status in statuses:
        if not status.complete or not status.summary or not status.summary.complete:
            raise ValueError("Tells scan is incomplete")
        summary = status.summary.scanners.get("tells")
        if summary is None or summary.errors or status.errors:
            raise ValueError("Tells scan contains errors")
        frame = scan_results_df(status.location, scanner="tells").scanners["tells"]
        if len(frame) != summary.results:
            raise ValueError("Tells scan result count does not match its summary")
        rows.extend(frame.to_dict("records"))
    return rows


def validated_samples(logs: list[str], scans: str | Path, *, expected_trials: int | None = None,
                      judge: str | None = None, source_aliases: dict[str, str] | None = None) -> list[tuple]:
    """Return (evaluation, sample, tells) only with exactly one valid answer per trial.

    Unlike load_tells, a complete {"tells": []} counts as coverage. An absent,
    failed, duplicate or unrelated scan never becomes a zero-tell trial. Deliberate
    rescans should use their own scan directory rather than silently taking latest.
    """
    from inspect_ai.log import read_eval_log

    from scout.scanners import Tell

    expected = {}
    if not logs:
        raise ValueError("No trial logs supplied")
    sources = [source_identity(log) for log in logs]
    if len(set(sources)) != len(sources):
        raise ValueError("Duplicate trial log supplied")
    aliases = {}
    if source_aliases is not None:
        if not isinstance(source_aliases, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in source_aliases.items()):
            raise ValueError("Source aliases must map original sources to local log paths")
        for key, value in source_aliases.items():
            original, relocated = source_identity(key), source_identity(value)
            if original in aliases or relocated not in sources:
                raise ValueError("Source aliases must be unambiguous and target supplied logs")
            if original in sources and original != relocated:
                raise ValueError("Source aliases cannot replace a supplied log identity")
            aliases[original] = relocated
        if len(set(aliases.values())) != len(aliases):
            raise ValueError("Source aliases must be one-to-one")
    identities = set()
    for log, source in zip(logs, sources, strict=True):
        evaluation = read_eval_log(log)
        if evaluation.status != "success" or evaluation.error is not None or not evaluation.samples:
            raise ValueError(f"Trial log is unsuccessful or incomplete: {log}")
        results = evaluation.results
        if (results is None or results.total_samples != len(evaluation.samples)
                or results.completed_samples != len(evaluation.samples)):
            raise ValueError(f"Trial log has incomplete declared sample counts: {log}")
        for sample in evaluation.samples:
            if sample.error or not sample.completed_at or getattr(sample, "invalidation", None):
                raise ValueError(f"Trial sample is unsuccessful or incomplete: {log}")
            key = (source, str(sample.id), str(sample.epoch))
            eval_id = getattr(getattr(evaluation, "eval", None), "eval_id", None)
            if not eval_id:
                raise ValueError("Trial log lacks its evaluation identity")
            identity = (str(eval_id), str(sample.id), str(sample.epoch))
            if identity in identities:
                raise ValueError("Duplicate evaluation sample across trial logs")
            identities.add(identity)
            if key in expected:
                raise ValueError(f"Duplicate trial sample: {key}")
            expected[key] = (evaluation, sample)
    if expected_trials is not None and (expected_trials <= 0 or len(expected) != expected_trials):
        raise ValueError(f"Expected {expected_trials} completed trials, found {len(expected)}")
    answers = {}
    for row in scan_rows(scans, judge):
        if _present(row.get("scan_error")) or _present(row.get("transcript_error")):
            raise ValueError("Failed tells scan result")
        meta = _object(row.get("transcript_metadata"), "transcript metadata")
        if "id" not in meta or "epoch" not in meta:
            raise ValueError("Scan result lacks sample id or epoch")
        original = source_identity(row["transcript_source_uri"])
        key = (aliases.get(original, original), str(meta["id"]), str(meta["epoch"]))
        if key not in expected:
            raise ValueError(f"Scan result does not match a requested trial: {key}")
        if key in answers:
            raise ValueError(f"Duplicate tells scan result: {key}")
        evaluation, _ = expected[key]
        eval_id = getattr(getattr(evaluation, "eval", None), "eval_id", None)
        if meta.get("eval_status", "success") != "success" or (eval_id and meta.get("eval_id") != eval_id):
            raise ValueError("Scan result has mismatched evaluation identity or status")
        metadata = _object(row.get("metadata") or {}, "result metadata") if isinstance(row.get("metadata"), (str, dict)) else {}
        if metadata.get("failed_segments", 0):
            raise ValueError("Tells scan contains failed segments")
        answer = _object(row.get("value"), "tells answer")
        if not isinstance(answer.get("tells"), list):
            raise ValueError("Tells answer lacks a tells list")  # noqa: TRY004 -- invalid scan evidence
        answers[key] = [Tell.model_validate(tell).model_dump() for tell in answer["tells"]]
    missing = set(expected) - set(answers)
    if missing:
        raise ValueError(f"Missing tells scans for {len(missing)} requested trial(s)")
    return [(evaluation, sample, answers[key]) for key, (evaluation, sample) in expected.items()]
