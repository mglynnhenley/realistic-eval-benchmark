import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "harbor_timing.py"
FIXTURES = ROOT / "tests" / "fixtures"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


class HarborTimingCliTests(unittest.TestCase):
    def test_synthetic_fixture_never_reports_measured_elapsed_time(self):
        result = run_cli("validate", str(FIXTURES / "harbor_timing_synthetic.json"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("evidence_class=synthetic_fixture", result.stdout)
        self.assertIn("interval.request_to_result=NOT_MEASURED_SYNTHETIC", result.stdout)
        self.assertNotIn("seconds=", result.stdout)

    def test_redacted_summary_reports_only_supported_finish_interval(self):
        result = run_cli("validate", str(FIXTURES / "harbor_timing_redacted_rail_a.json"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("events=observed:2 simulated:0 missing:12", result.stdout)
        self.assertIn("interval.trial_to_job_finish=OBSERVED seconds=0.186241", result.stdout)
        self.assertIn("interval.request_to_result=UNAVAILABLE missing=request_received,result_verified", result.stdout)

    def test_ambiguous_historical_job_timestamp_remains_missing(self):
        fixture = FIXTURES / "harbor_timing_redacted_rail_b.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        result = run_cli("validate", str(fixture))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("events=observed:1 simulated:0 missing:13", result.stdout)
        self.assertIn("interval.trial_to_job_finish=UNAVAILABLE missing=job_finished", result.stdout)
        self.assertIn("without a timezone", data["events"]["job_finished"]["note"])
        self.assertNotIn("seconds=0.574303", result.stdout)

    def test_naive_timestamp_is_rejected(self):
        data = json.loads((FIXTURES / "harbor_timing_redacted_rail_a.json").read_text(encoding="utf-8"))
        data["events"]["trial_finished"]["timestamp_utc"] = "2026-11-24T12:35:55.970960"

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "invalid.json"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            result = run_cli("validate", str(manifest))

        self.assertEqual(result.returncode, 1)
        self.assertIn("timestamp must be an ISO-8601 UTC string ending in Z", result.stdout)

    def test_out_of_order_timestamps_are_flagged_without_rewriting(self):
        data = json.loads((FIXTURES / "harbor_timing_redacted_rail_a.json").read_text(encoding="utf-8"))
        original_trial_finish = data["events"]["trial_finished"]["timestamp_utc"]
        data["events"]["job_finished"]["timestamp_utc"] = "2026-11-24T12:35:55.000000Z"

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "out-of-order.json"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            result = run_cli("validate", str(manifest))
            written = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("timestamp_order=REVIEW pairs=trial_finished->job_finished", result.stdout)
        self.assertIn("interval.trial_to_job_finish=REVIEW negative_duration_seconds=", result.stdout)
        self.assertEqual(written["events"]["trial_finished"]["timestamp_utc"], original_trial_finish)

    def test_empty_capture_does_not_claim_timestamp_order_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "capture.json"
            created = run_cli(
                "init",
                "--manifest",
                str(manifest),
                "--scenario",
                "train_refund_late",
                "--run-ref",
                "empty-capture",
            )
            validated = run_cli("validate", str(manifest))

        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("events=observed:0 simulated:0 missing:14", validated.stdout)
        self.assertIn("timestamp_order=UNAVAILABLE observed_pairs=0", validated.stdout)

    def test_init_and_mark_create_a_private_observed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "capture.json"
            created = run_cli(
                "init",
                "--manifest",
                str(manifest),
                "--scenario",
                "train_refund_late",
                "--run-ref",
                "local-test",
            )
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

            marked = run_cli(
                "mark",
                "--manifest",
                str(manifest),
                "--event",
                "request_received",
                "--source",
                "unit test fixture",
                "--at-utc",
                "2026-11-26T12:00:00Z",
            )
            validated = run_cli("validate", str(manifest))

        self.assertEqual(marked.returncode, 0, marked.stdout + marked.stderr)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertIn("events=observed:1 simulated:0 missing:13", validated.stdout)
        self.assertIn("interval.request_to_result=UNAVAILABLE missing=result_verified", validated.stdout)


if __name__ == "__main__":
    unittest.main()
