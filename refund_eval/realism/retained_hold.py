"""Exact reviewed original native holds; no reconciliation or ledger writes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from refund_eval.realism import measurement, stages

VERSION = "retained-native-hold-v1"
PAIR_VERSION = "retained-native-holds-v2"
THREE_VERSION = "retained-native-holds-v3"
THREE_SCOPE_SHA256 = "045f23939ab3dae8636bca61899dc776e8b1a9d6026e29a1540a08d41d9d0a61"
THREE_ACKNOWLEDGEMENTS = {
    "retained_all_three_full_holds", "actual_and_invoice_unknown", "new_work_only",
    "no_original_replay_recovery_or_rescan", "reservation_is_not_provider_cap", "fourth_unknown_blocks",
}
PAIR_SCOPE_SHA256 = "cb42ebfd64bf9429ff8e9ab3479426e132a6e51f5b00d0b7e366de4e89aff45b"
# A reviewed scope, not authority for arbitrary future unknown stages. It pins
# the original case/proof identities without shipping any private trial content.
SCOPE_SHA256 = "0875abdb910398f9f1c0be5d20f8beb2c0eb3002895bd9b331a4308f059dcaee"
ARTIFACTS = {
    "scope_review", "terminal_review", "accounting_review", "exception_bundle",
    "terminal_disposition", "budget_review", "terminal_refresh",
    "request", "start", "result", "completion", "cleanup",
}
ACKNOWLEDGEMENTS = {
    "retained_full_hold", "actual_and_invoice_unknown", "new_work_only",
    "no_original_replay_recovery_or_rescan", "reservation_is_not_provider_cap",
}
FIELDS = {"version", "validator_sha256", "ledger", "stage", "artifacts", "future_binding"}
PAIR_FIELDS = (FIELDS - {"stage"}) | {"stages"}
PAIR_ACKNOWLEDGEMENTS = (ACKNOWLEDGEMENTS - {"retained_full_hold"}) | {"retained_both_full_holds"}

RECONCILED_VERSION = "retained-native-holds-v4"
RECONCILED_SCOPE_SHA256 = "de4db1c371968e68daa514dd46d12e960e2b8b19c360247f7709c3dbb2bde5b0"
RECONCILED_FIELDS = PAIR_FIELDS | {"reconciled_nonattempt"}

# One additional, request-bound terminal interaction, never arbitrary unknowns.
# These are original evidence digests, not billing or continuation approvals.
INTERACTION_VERSION = "retained-four-holds-continuation-v5"
INTERACTION_REQUEST_SHA256 = "87f249a06c05ac33d67e73cb575924986707a7a9e0af5e206b3e97c480955db9"
INTERACTION_ACCOUNTING_SHA256 = "ad0a207f6c9b3fc8c2cd89142efb531eadd2d887d7b612d6ed4666ba0ad9f25b"
INTERACTION_OWNER_REQUEST_SHA256 = "b649ec2e205797c407a4c2d4200511e6d512b65e19b7189b80951071255e88f7"
INTERACTION_FIELDS = RECONCILED_FIELDS | {"retained_interaction", "restart_authorization"}
INTERACTION_ACKNOWLEDGEMENTS = {
    "retained_all_four_full_holds", "actual_and_invoice_unknown", "new_work_only",
    "no_original_replay_recovery_or_rescan", "reservation_is_not_provider_cap",
    "no_cost_inference_or_hold_release", "fifth_unknown_blocks",
    "continuation_after_feedback_integration_only", "old_approvals_not_extended",
}
INTERACTION_SCOPE_FIELDS = {
    "version", "declaration_kind", "declared_by", "declared_at", "owner_request",
    "budget_usd", "ledger", "future_binding", "continuation_after_feedback_integration",
    "bounded_disposition_sha256", "acknowledgements",
}


# Finite historically reconciled FAILED scans, separate from unknown full holds.
FAILED_SCAN_VERSION = "retained-four-holds-known-failed-scans-v6"
FAILED_SCAN_OWNER_REQUEST_SHA256 = "6a82a23e063a46ad2b19a7932c0c507e465a03df288c5cb79784cbe4d2b26305"
FAILED_SCAN_V5_SCOPE_SHA256 = "e43c0f39814bf9187c322fe3e1acb341bf19e3adb117a4be1ec019cf0b6f84fd"
FAILED_SCAN_FIELDS = INTERACTION_FIELDS | {"reconciled_failed_scans"}
FAILED_SCAN_SCOPE_FIELDS = INTERACTION_SCOPE_FIELDS | {"original_v5_scope"}
FAILED_SCAN_ACKNOWLEDGEMENTS = INTERACTION_ACKNOWLEDGEMENTS | {
    "known_scans_failed_and_all_attempt_costs_exact", "original_failed_coverage_unaccepted",
    "old_v5_scope_provenance_only", "no_new_unknown_allowance",
}
FAILED_SCAN_PINS = {'loop-051047e3c5087e5c-candidate-tells': {'proof': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/claude-scan-accounting/normal-finish-proof-001.json',
                                                     'sha256': 'b3f81508cf17e2adc8b434d68d5eab0a21ba16bf743eb1a47b9d4feb97d4624e'},
                                           'receipt': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/claude-scan-accounting/normal-finish-receipt-001.json',
                                                       'sha256': '2dcc27b0745f1d989983ee74a434a5a4c78c7d75f4f8513806feca140e5939de'},
                                           'closure': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/claude-scan-accounting/attempt-closure-001.json',
                                                       'sha256': '6295544c070e2510d2b2d40001078137617f1e97305cb3ab708a5db9c0cdc31b'},
                                           'snapshot_sha256': '6bae19b6868af37c9238ea1e54c56355491ea7a64a84278a7f5379c766a1389c',
                                           'raw_charge_closure_sha256': '70b6419ce538d6687d5f1a8eae9092eb4cb2e43c5f9a37ee53427c76dd53353b',
                                           'actual_usd_decimal': '2.070124',
                                           'closure_kind': 'claude_refusal',
                                           'planned_parts': 4,
                                           'total_tokens': 497863,
                                           'original_run_directory': '.context/realism/20261030-continuous-001/loop-v4'},
 'deepseek-saved-native-001': {'proof': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/deepseek-scan-001-accounting/normal-finish-proof-001.json',
                                         'sha256': 'e7e96c86639339b316fad9667d86e11fedb297b3623a3a03859d37c8046a46ce'},
                               'receipt': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/deepseek-scan-001-accounting/normal-finish-receipt-001.json',
                                           'sha256': 'b4d0a4294e46a479f16069f0dfc509a17b49134735bdabe7465572122c249b7c'},
                               'closure': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/deepseek-scan-001-accounting/attempt-closure-001.json',
                                           'sha256': '1b229c7c12554b7418acd692a5f29e355f95197dd2f84322dd212e3415c23bb7'},
                               'snapshot_sha256': '6ba6e11ef0c6ea3886f57f0beba98a899ca568fdeb750704488df367d83a4656',
                               'raw_charge_closure_sha256': 'bad7935dddbb38cd5e639ecc61a4c0a4e7609f6e17d677e27405b9e5f57a4a93',
                               'actual_usd_decimal': '0.059002016',
                               'closure_kind': 'deepseek_reasoning_length',
                               'planned_parts': 3,
                               'total_tokens': 356944,
                               'original_run_directory': '.context/realism/20261030-continuous-001/deepseek-recovery-001/scan-001'}}


# Exactly three interrupted002 requests pinned via owner003, never arbitrary active stages.
SEVEN_VERSION = "retained-seven-holds-continuation-v7"
SEVEN_FIELDS = FAILED_SCAN_FIELDS | {"retained_interrupted", "regeneration_scope"}
SEVEN_SCOPE_FIELDS = FAILED_SCAN_SCOPE_FIELDS | {"parent_owner_request"}
SEVEN_ACKNOWLEDGEMENTS = (FAILED_SCAN_ACKNOWLEDGEMENTS - {"fifth_unknown_blocks", "no_new_unknown_allowance"}) | {
    "retained_all_seven_full_holds", "interrupted_started_status_is_not_terminal_success",
    "owner003_regeneration_and_parent002_normal_scope", "eighth_unknown_blocks",
    "no_interrupted_cost_inference_release_or_retry", "no_unpinned_unknown_allowance",
}
SEVEN_ARTIFACT_PINS = {'owner_request': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/owner-request-003.json',
                   'sha256': 'f12bc17dea2401b0ea53dc9344a515690c81d8da1b0ee19a0c0b0a9b1e61c7cf'},
 'parent_owner_request': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/owner-request-002.json',
                          'sha256': '6a82a23e063a46ad2b19a7932c0c507e465a03df288c5cb79784cbe4d2b26305'},
 'recovery_config': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-003/config-actual-001.json',
                     'sha256': '813f8018e716bf2b58cf37359e85538dd6598b0bf4d4c5060a4a5858c80772b1'},
 'ledger_before': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-003/run-001/ledger-before.json',
                   'sha256': 'deffc378bc8b730594d805a11b4eb64134c92493af1286a291a2c0f01c8ad393'},
 'original_config': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-002/config-actual-001.json',
                     'sha256': 'bf78f7bc826c2a50916dd3bbc6925eb5e8e993db5048a142b41cf99566832f89'},
 'original_coordinator': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-002/coordinator.py',
                          'sha256': '1c18dfcdd21fab009faddcea3493168e4b721d67cdfaee2af6bbf67f15c4a373'},
 'interruption': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-001/interruption-observation-001.json',
                  'sha256': '03f8713abf381074ea33d13e0224f4557499cb99874cdee9a3f60cdcdbd5e6d6'}}
SEVEN_INTERRUPTED_PINS = {'recovery-investigate-continuation-002-001-A-initial-001-01': {'snapshot_sha256': '1cafdd52d85b317a902cec1ad1ade3d4142767a4eaad8d9ec98d0460c6a1f241',
                                                                'reserve_usd': 2.008088,
                                                                'request': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-002/run-001/model-calls/recovery-investigate-continuation-002-001-A-initial-001-01-request.json',
                                                                            'sha256': 'b6163c6c0a3e7ada8bd1904228f8b0c7567ce9888539a5889d35478030e198d7'}},
 'recovery-investigate-continuation-002-001-B-initial-001-01': {'snapshot_sha256': 'b7d181005c9d85b4d16512110a96dc9fab06c5aab6b8aa789436d6565d5dbe5e',
                                                                'reserve_usd': 8.160792,
                                                                'request': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-002/run-001/model-calls/recovery-investigate-continuation-002-001-B-initial-001-01-request.json',
                                                                            'sha256': '0a90e94c0681ea1411b1f30ea007e7bbf5242326208f5a0be3a8934928614a1a'}},
 'recovery-investigate-continuation-002-001-C-initial-001-01': {'snapshot_sha256': '4dc70993317ff796a616655967576af61e158239638394b441454501d93d0f6d',
                                                                'reserve_usd': 4.093756,
                                                                'request': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-002/run-001/model-calls/recovery-investigate-continuation-002-001-C-initial-001-01-request.json',
                                                                            'sha256': 'fba4f4f2681a236f9608fa1be74210635f5b4cf9d515635cd5e7d949af094c5a'}}}


# A separate known-cost FAILED interaction, never an eighth unknown reservation.
EIGHT_VERSION = "retained-seven-holds-known-failed-interaction-v8"
EIGHT_FIELDS = SEVEN_FIELDS | {"reconciled_failed_interaction", "continuation_owner_request"}
EIGHT_SCOPE_FIELDS = SEVEN_SCOPE_FIELDS | {"continuation_owner_request"}
EIGHT_ACKNOWLEDGEMENTS = SEVEN_ACKNOWLEDGEMENTS | {
    "known004_failed_accounting_not_success", "no_eighth_unknown_allowance",
    "same1000_total_cap", "no_savedcall_resend", "stop_on_real_unknown_cost",
}
EIGHT_STAGE_ID = "recovery-investigate-regen004-stream-v1-A-initial-003-08"
EIGHT_GENERATION_ID = "gen-1793372867-ZztSLfL85cQQEUaQdgiH"
EIGHT_MODEL = "qwen/qwen3.8-max-0902"
EIGHT_CANONICAL_MODEL = "qwen/qwen3.8-max-20260902"
EIGHT_ORIGINAL_RUNNER = "585547231e7f241b2d76d3b7ce9c78418127ebe7"
EIGHT_RESERVE_USD = 2.060814
EIGHT_ACTUAL_USD = 0.40302
EIGHT_DETAIL = "Accounting only: exact saved provider generation reports USD 0.40302; provider status 499/client disconnect; complete answer remains unknown; no retry."
EIGHT_STAGE_PREFIX = "recovery-investigate-regen004-stream-v1-"
# Exact saved historical normal-finish evidence; future continuation bindings are separate.
EIGHT_STAGE_SHA256 = "2a0999724e21515462dbd732eafdf66393aab380cba5807d69c0f33832b42b08"
EIGHT_ACCOUNTING_PINS = {'proof': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/A-charge-recovery-001/case.json',
           'sha256': '8620056dd2ef5b1494ffab0c07a27bc8c312e6e5c7be9eaf6f6d85c2ac452b86'},
 'receipt': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/accounting-receipt.json',
             'sha256': 'bcfdeb4bef070294154e9426700985926bddd31611385f837610e821249f352a'},
 'whole_before': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/ledger-before.json',
                  'sha256': 'e9b0508a7673cb65ebfff048749600ef6a22d6146a12b4963844f622e35aa05d'},
 'whole_after': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/ledger-after.json',
                 'sha256': '25e0f1415552be8a1dce929d284eed2b65818fd36e6cfa7ce45215fa9630e49c'},
 'owner_resume': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/owner-resume-request.json',
                  'sha256': '948269bfd52d67f5e95cb0733d2cffcd7656b004b511a5aef8103b28fe04774a'},
 'finish_script': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/A-charge-recovery-001/reconcile_charge.py',
                   'sha256': '1d1f687ef0c8f93b8201eb4e20f9d56cf9ae10ab5e20a7fb7460c47b78fb3634'},
 'finish_source': {'path': 'refund_eval/realism/stages.py',
                   'sha256': 'a52bdf1cb7e199ba563392305aa51e568504d1e535cb58a3aed2222bb168a59c'},
 'readback': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/accounting-readback.json',
              'sha256': '38e9eabc7775e17a7ccbcc7b0b802f909d15220a60ecefe314bdd9bd994e981e'},
 'accounting_process': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/accounting-process-ended.json',
                        'sha256': '6cb428f453c6d557cc052b66f19508e05cd8dac7249e6e6494a117397b3a312d'},
 'accounting_intent': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/accounting-exclusive-intent.json',
                       'sha256': '1b7fecfc71a9224282432d186ad3c43f926a7b587b481efbae2ce7f4a2e987ea'},
 'accounting_stderr': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/accounting-stderr.log',
                       'sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'}}
EIGHT_ARTIFACT_PINS = {'request': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/model-calls/recovery-investigate-regen004-stream-v1-A-initial-003-08-request.json',
             'sha256': 'fb0a58971a685e1626684cf6d7fcd4bf6e1ca2d9e0ca51c1c6bddebd5ec35fa1'},
 'call_intent': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/call-intents/recovery-investigate-regen004-stream-v1-A-initial-003-08.json',
                 'sha256': '4374fb89182fa5b8053ff7ba523c4686f2ecafbedd387b698934eddd211ff6a0'},
 'manifest': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/model-calls/recovery-investigate-regen004-stream-v1-A-initial-003-08-stream/manifest.json',
              'sha256': 'fca52e4f8f3a879af941e78ed93362a710b9f1dc23d3d614391c40f474a75501'},
 'headers': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/model-calls/recovery-investigate-regen004-stream-v1-A-initial-003-08-stream/headers.json',
             'sha256': 'ec7a16d88e7cbd7ba7750c680c69c57f1349220e6cb35ef35310946d2dbde198'},
 'generation': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/model-calls/recovery-investigate-regen004-stream-v1-A-initial-003-08-stream/generation-id.json',
                'sha256': '269a2519153c0d6e0a945cb442e46afbfc4e97935ae097e4a23a07f2f3fbe01f'},
 'generation_chunk': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/model-calls/recovery-investigate-regen004-stream-v1-A-initial-003-08-stream/raw-000030.bin',
                      'sha256': '5eda588dc06e39527c32499d77637633c1503a9723c642520b75fab24c8291a1'},
 'metadata_receipt': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/provider-metadata-receipt.json',
                      'sha256': '3fafb67bdefe7984e98d4dfca800f012a50414322cf753de97414cac0d6e7224'},
 'metadata_body': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/provider-metadata.body',
                   'sha256': '4c210efdc7aa9f9b391cd4f9dcf6e362dface2cd9c50db1530ed1db8a0480941'},
 'content_receipt': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/provider-content-receipt.json',
                     'sha256': '561e8de71abfe0f47c99fd6d6e19a6ab2e20c84ea07607a181a39259c1139bed'},
 'content_body': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/provider-content.body',
                  'sha256': '9635b287d2698dfe3f941a8155fbc7934c565a9b6454cc6ce4c3341354f2fbaa'},
 'provider_read_intent': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/provider-read-intent.json',
                          'sha256': '8cdfca4c4a95547bcb8bbe6dde58228c56b6f5e61dbdc3806f5c9915385814b9'},
 'original_config': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/config-actual-001.json',
                     'sha256': 'd1d7dda3c0fbff5a4a1f5550084cde2006f6a3edc18a11c480384eee5b673016'},
 'process_ended': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/worker-001/process-ended.json',
                   'sha256': 'b040921304312f4b3a09e449e51679869e1bac908d03e538b0c63eafb0c0bfc6'},
 'task_ended': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/worker-001/task-ended.json',
                'sha256': 'dcbbb7cc30c8da118f02cd6d6838373ba3a5c97e9ac92cfa7d65b4455409770f'},
 'signal': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/worker-001/signals/000.json',
            'sha256': 'a0bdb5bf29f4bb93d5fa7db8a45abc8ee01b8cab34e64417a01baff4679ec106'},
 'task_signal': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/worker-001/task-signals/001.json',
                 'sha256': 'a3f63ed03ad55eeaa052253240741093367709c3f3f2e0f8de9104b02ffa3f0b'},
 'streaming_source': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/trusted-runner/refund_eval/realism/streaming_receipts.py',
                      'sha256': '0c8c6eadc16b3a8cab3799d45743e5f0598571cb1f5de4c7eb5aa4d58cc27a8a'},
 'adapter_source': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/trusted-runner/refund_eval/realism/investigation.py',
                    'sha256': '0bb0fd151e74085b60843f411e7197a6390709eed843642302ebe6724dd27d93'},
 'coordinator_source': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/coordinator.py',
                        'sha256': '8552368a0fa86cf7119ddeae31100e1252f4c65ee27575ccb2d0ce728e85f9de'},
 'preflight': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/recovery-coordinator-004/run-001/model-preflight.json',
               'sha256': '1c4adcc1756715504fac10a7ad2b129ea71771d53eac5d2c181b21fd7f1ac30a'},
 'provider_reader_source': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/interrupted-recovery-004-001/read_provider_record.py',
                            'sha256': '20fc2fd2c668ba418d82ac5a21dd5db6477fa1da90de9dbb366af6c6b39246fa'}}


def require(condition, message):
    if not condition:
        raise measurement.Rejected(message)


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys), "Noncanonical retained-hold object fields")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def money(value, *, positive=False):
    try:
        return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _time(value):
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _pairs(items):
    value = {}
    for key, item in items:
        require(key not in value, "Duplicate retained-hold JSON field")
        value[key] = item
    return value


def _json(raw):
    def invalid(_):
        raise measurement.Rejected("Nonfinite retained-hold JSON")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)
    require(type(value) is dict, "Expected retained-hold JSON object")
    return value


def _path(repo, relative):
    require(type(relative) is str and relative.startswith(".context/realism/"), "Use private retained-hold artifacts")
    path = repo / relative
    require(path.resolve() == path and path.is_file(), "Retained-hold artifact missing or noncanonical")
    return path


def _artifact(repo, reference):
    exact(reference, {"path", "sha256"})
    require(type(reference["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]),
            "Invalid retained-hold artifact digest")
    path = _path(repo, reference["path"])
    raw = path.read_bytes()
    require(digest(raw) == reference["sha256"], "Retained-hold artifact changed")
    return path, _json(raw)


def _read_snapshot(ledger, budget):
    """Caller owns the stable ledger lock; never acquire it recursively."""
    raw = Path(ledger).read_bytes()
    value = _json(raw)
    exact(value, {"version", "budget_usd", "stages", "budget_amendments"})
    require(type(value["version"]) is int and value["version"] == 1
            and money(value["budget_usd"]) and value["budget_usd"] == budget
            and type(value["stages"]) is dict and type(value["budget_amendments"]) is list,
            "Retained-hold ledger schema or budget differs")
    for stage in value["stages"].values():
        require(type(stage) is dict
                and {"kind", "status", "reserve_usd", "actual_usd", "started_at"} <= set(stage)
                and set(stage) <= {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"}
                and type(stage["kind"]) is str and stage["kind"] in {"trials", "tells", "interaction", "coding", "review"}
                and type(stage["status"]) is str and stage["status"] in {"started", "complete", "failed", "unknown"}
                and money(stage["reserve_usd"], positive=True)
                and (stage["actual_usd"] is None or money(stage["actual_usd"]))
                and _time(stage["started_at"]) is not None,
                "Noncanonical retained-hold stage fields or money")
        if "finished_at" in stage:
            require(_time(stage["finished_at"]) is not None
                    and _time(stage["finished_at"]) >= _time(stage["started_at"]),
                    "Noncanonical retained-hold finish time")
        if "detail" in stage:
            require(type(stage["detail"]) is str, "Noncanonical retained-hold detail")
    return value, digest(raw)


@contextmanager
def _snapshot(ledger, budget):
    """Use the very same stable lock inode as stages, without its write-on-exit."""
    require(money(budget, positive=True) and budget in (100, 1000), "Unsupported retained-hold snapshot allowance")
    ledger = Path(ledger).resolve()
    lock = ledger.with_suffix(ledger.suffix + ".lock")
    require(ledger.is_file() and lock.is_file(), "Existing retained-hold ledger and lock required")
    with lock.open("r") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield _read_snapshot(ledger, budget)


def read_spend(ledger, budget):
    with _snapshot(ledger, budget) as (value, _):
        return stages.summary(value)


def future_binding(repo, run_dir, base_request):
    """Bind the existing request BEFORE adding disposition/fingerprint, avoiding cycles."""
    repo, run_dir = Path(repo).resolve(), Path(run_dir).resolve()
    require(run_dir.is_relative_to(repo / ".context/realism") and run_dir != repo / ".context/realism",
            "Use a distinct private future round")
    require(type(base_request) is dict and "prior_unknown_disposition" not in base_request and "fingerprint" not in base_request,
            "Future binding requires the unextended request")
    return {"run_directory": str(run_dir.relative_to(repo)), "request_sha256": measurement._json_hash(base_request)}


def _proof_chain(repo, references):
    exact(references, ARTIFACTS)
    exact(references["scope_review"], {"path", "sha256"})
    require(references["scope_review"].get("sha256") == SCOPE_SHA256, "Exact original scope digest required")
    scope_path, scope = _artifact(repo, references["scope_review"])
    loaded = {"scope_review": (scope_path, scope)}
    require(references["scope_review"]["sha256"] == SCOPE_SHA256
            and scope.get("protocol_proposal") == VERSION and scope.get("scope_approved") is True
            and scope.get("implementation_scope_supported") is True, "Exact retained-hold scope approval required")
    loaded.update({name: _artifact(repo, ref) for name, ref in references.items() if name != "scope_review"})
    root = scope_path.parent.parent
    proof_names = {
        "terminal_review": "gemini-011-terminal-review.json",
        "accounting_review": "auditor/gemini-011-accounting-final-readback.json",
        "terminal_disposition": "gemini011-terminal-disposition-001.json",
        "exception_bundle": "loop-observer-gemini-011-scan-reconciliation-receipt.json",
        "budget_review": "budget-amendment-100-review-001.json",
    }
    approved_proofs = {item["path"]: item["sha256"] for item in scope["reused_proofs"]}
    for name, relative in proof_names.items():
        require(loaded[name][0] == root / relative and references[name]["sha256"] == approved_proofs[relative],
                "Original retained-hold proof identity differs")
    refresh = scope["termination_assessment"]["supplemental_refresh"]
    require(loaded["terminal_refresh"][0] == root / refresh["path"]
            and references["terminal_refresh"]["sha256"] == refresh["sha256"], "Exact saved terminal refresh required")
    bundle = loaded["exception_bundle"][1]
    original = Path(bundle["run"])
    require(original.is_relative_to(root), "Original retained-hold run differs")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        path = original / filename
        require(loaded[name][0] == path and references[name]["sha256"] == bundle["source_files"][str(path)],
                "Original sealed retained-hold artifact differs")
    return {name: value for name, (_, value) in loaded.items()}, original


def _original(proofs, references, current, binding):
    scope, request, result = (proofs[name] for name in ("scope_review", "request", "result"))
    fingerprint = request["fingerprint"]
    request_body = {key: value for key, value in request.items() if key != "fingerprint"}
    require(measurement._json_hash(request_body) == fingerprint
            and proofs["start"].get("fingerprint") == fingerprint
            and result.get("fingerprint") == fingerprint
            and proofs["completion"].get("fingerprint") == fingerprint
            and proofs["completion"].get("result_sha256") == measurement._json_hash(result), "Original retained-hold seal differs")
    terminal, accounting = proofs["terminal_review"], proofs["accounting_review"]
    require(terminal.get("approved") is True and terminal.get("request_fingerprint") == fingerprint
            and accounting.get("request_fingerprint") == fingerprint
            and accounting.get("original_result_sha256") == references["result"]["sha256"]
            and accounting.get("original_completion_sha256") == references["completion"]["sha256"]
            and result.get("status") == "reconciliation_required"
            and terminal.get("native_actual_usd") is None and accounting.get("native_actual_usd") is None,
            "Original retained-hold accounting remains unknown")
    stage_id = scope["exact_original_stage"]
    require(stage_id == f"loop-{fingerprint[:16]}-candidate-trials" and binding["request_sha256"] != fingerprint,
            "Retained disposition never authorizes original replay")
    old_stages = result["spend"]["stages"]
    require(stage_id in old_stages and stage_id in current["stages"], "Original stage is missing")
    for name, value in old_stages.items():
        require(name in current["stages"] and measurement._json_hash(value) == measurement._json_hash(current["stages"][name]),
                "An original retained ledger stage changed")
    require(measurement._json_hash(current["budget_amendments"])
            == measurement._json_hash(result["spend"]["budget_amendments"]), "Original budget amendment changed")
    expected = scope["frozen_stage_snapshot"]
    require(measurement._json_hash(current["stages"][stage_id]) == measurement._json_hash(expected)
            and expected["kind"] == "trials" and expected["status"] == "failed"
            and expected["actual_usd"] is None and money(expected["reserve_usd"], positive=True)
            and type(expected.get("finished_at")) is str and bool(expected["finished_at"]),
            "Original full unresolved native hold changed")
    for name, value in current["stages"].items():
        if name != stage_id:
            require(value.get("status") == "complete" and money(value.get("actual_usd"))
                    and money(value.get("reserve_usd"), positive=True) and bool(value.get("finished_at")),
                    "Every other unknown, active or unfinished stage blocks continuation")
    return stage_id, measurement._json_hash(expected)


def _termination(proofs, references):
    mapping = proofs["scope_review"]["termination_assessment"]["owned_terminal_ids"]
    cleanup, refresh = proofs["cleanup"], proofs["terminal_refresh"]
    require(type(cleanup.get("sessions")) is list and type(cleanup.get("terminated")) is list
            and sorted(cleanup["sessions"]) == sorted(mapping.values())
            and sorted(cleanup["terminated"]) == sorted(mapping) and cleanup.get("unresolved") == []
            and not cleanup.get("discovery_errors") and cleanup.get("remote_state") != "unknown"
            and proofs["result"].get("cleanup") == cleanup, "Exact original owned cleanup is incomplete")
    rows = refresh.get("rows")
    require(type(rows) is list and len(rows) == len(mapping)
            and {row.get("sandbox_id") for row in rows} == set(mapping)
            and refresh.get("original_cleanup_sha256") == references["cleanup"]["sha256"]
            and refresh.get("all_owned_and_terminal") is True, "Exact terminal refresh identities differ")
    require(all(row.get("owned") is True and row.get("terminal") is True and row.get("managed") == "true"
                and type(row.get("returncode")) is int and row["returncode"] == 137
                and row.get("session") == mapping[row["sandbox_id"]] for row in rows), "Original owned work is not verified stopped")


def _freeze(repo, review_path, *, ledger, budget, binding, required_reserve, snapshot, execution_fingerprint=None):
    """Read-only factory; call while the controller repository lock is held.

    The same factory and comparison run again at execution before reservations.
    An actual receipt is supplied by the independent reviewer, never generated here.
    """
    repo, ledger = Path(repo).resolve(), Path(ledger).resolve()
    review_path = Path(review_path).absolute()
    require(review_path.is_relative_to(repo / ".context/realism"), "Use a private retained-hold review")
    _path(repo, str(review_path.relative_to(repo)))
    raw = review_path.read_bytes()
    review = _json(raw)
    if review.get("version") == EIGHT_VERSION:
        return _freeze_eight_boundary(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                                      required_reserve=required_reserve, snapshot=snapshot,
                                      execution_fingerprint=execution_fingerprint)
    if review.get("version") == SEVEN_VERSION:
        return _freeze_seven(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                             required_reserve=required_reserve, snapshot=snapshot,
                             execution_fingerprint=execution_fingerprint)
    if review.get("version") == FAILED_SCAN_VERSION:
        return _freeze_known_failed_scans(repo, review_path, review, raw, ledger=ledger, budget=budget,
                                          binding=binding, required_reserve=required_reserve, snapshot=snapshot,
                                          execution_fingerprint=execution_fingerprint)
    if review.get("version") == INTERACTION_VERSION:
        return _freeze_interaction(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                                   required_reserve=required_reserve, snapshot=snapshot,
                                   execution_fingerprint=execution_fingerprint)
    if review.get("version") == RECONCILED_VERSION:
        return _freeze_reconciled(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                                  required_reserve=required_reserve, snapshot=snapshot,
                                  execution_fingerprint=execution_fingerprint)
    if review.get("version") == THREE_VERSION:
        return _freeze_three(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                             required_reserve=required_reserve, snapshot=snapshot,
                             execution_fingerprint=execution_fingerprint)
    if review.get("version") == PAIR_VERSION:
        return _freeze_pair(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                            required_reserve=required_reserve, snapshot=snapshot,
                            execution_fingerprint=execution_fingerprint)
    exact(review, FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["version"] == VERSION and review["review_kind"] == "retained_native_hold_disposition_review"
            and review["approved"] is True and type(review["reviewer"]) is str and bool(review["reviewer"].strip()),
            "Exact independent retained-hold disposition approval required")
    require(_time(review["reviewed_at"]) is not None, "Retained-hold review time requires a timezone")
    exact(review["acknowledgements"], ACKNOWLEDGEMENTS)
    require(all(value is True for value in review["acknowledgements"].values()), "Retained hold requires explicit limits acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"]), "Invalid future request binding")
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(review["future_binding"] == binding, "Retained-hold review binds a different future request")
    proofs, original = _proof_chain(repo, review["artifacts"])
    future = (repo / binding["run_directory"]).resolve()
    require(not Path(binding["run_directory"]).is_absolute()
            and str(future.relative_to(repo)) == binding["run_directory"]
            and future.is_relative_to(repo / ".context/realism") and not future.is_relative_to(original)
            and not original.is_relative_to(future), "Original run cannot be replayed or modified")
    marker = future / "execution-started.json"
    if marker.exists():
        require(execution_fingerprint is not None and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future paid execution already started; never replay")
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and str(ledger) == proofs["request"]["shared_ledger"]
            and str(ledger) == proofs["budget_review"]["original_ledger"], "Use only the original shared ledger")
    current, ledger_sha = snapshot
    require(money(budget, positive=True) and budget == 100, "Retained hold uses only the existing $100 allowance")
    require(review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Retained-hold ledger review is stale")
    require(bool(current["budget_amendments"]), "Original budget amendment required")
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == proofs["budget_review"]["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Retained-hold budget amendment differs")
    stage_id, stage_sha = _original(proofs, review["artifacts"], current, binding)
    exact(review["stage"], {"id", "sha256"})
    require(review["stage"] == {"id": stage_id, "sha256": stage_sha}, "Retained-hold stage identity differs")
    _termination(proofs, review["artifacts"])
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after the full retained hold")
    return {**{key: review[key] for key in FIELDS},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}


def _pair_references(repo, scope_path, scope):
    """Resolve only the two immutable original descriptors from the pinned scope."""
    root = scope_path.parent.parent

    def reference(item):
        exact(item, {"path", "sha256"})
        return {"path": str((root / item["path"]).relative_to(repo)), "sha256": item["sha256"]}

    originals = scope["originals"]
    require(type(originals) is list and len(originals) == 2, "Exactly two scoped original cases required")
    budget = [item for item in scope["reused_proofs"] if item["path"] == "budget-amendment-100-review-001.json"]
    require(len(budget) == 1, "Exact scoped budget proof required")
    return {"scope_review": {"path": str(scope_path.relative_to(repo)), "sha256": PAIR_SCOPE_SHA256},
            "budget_review": reference(budget[0]),
            "originals": [{"label": item["label"],
                           "artifacts": {name: reference(ref) for name, ref in item["artifacts"].items()}}
                          for item in originals]}


def _pair_original(repo, original, references, current, ledger, binding):
    proofs = {name: _artifact(repo, ref)[1] for name, ref in references.items()}
    request, result = proofs["request"], proofs["result"]
    fingerprint = request["fingerprint"]
    require(measurement._json_hash({key: value for key, value in request.items() if key != "fingerprint"}) == fingerprint
            and proofs["start"].get("fingerprint") == fingerprint and result.get("fingerprint") == fingerprint
            and proofs["completion"].get("fingerprint") == fingerprint
            and proofs["completion"].get("result_sha256") == measurement._json_hash(result)
            and result.get("status") == "reconciliation_required", "Original pair seals differ")
    directory = repo / original["run_directory"]
    require(directory.resolve() == directory and directory.is_relative_to(repo / ".context/realism"),
            "Scoped original directory differs")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        path = directory / filename
        require(repo / references[name]["path"] == path
                and proofs["exception_bundle"]["source_files"].get(str(path)) == references[name]["sha256"],
                "Original pair artifact identity differs")
    require(proofs["exception_bundle"].get("run") == str(directory), "Original pair exception identity differs")
    terminal, accounting = proofs["terminal_review"], proofs["accounting_review"]
    require(terminal.get("approved") is True and terminal.get("request_fingerprint") == fingerprint
            and accounting.get("request_fingerprint") == fingerprint
            and "native_actual_usd" in terminal and terminal["native_actual_usd"] is None
            and "native_actual_usd" in accounting and accounting["native_actual_usd"] is None
            and accounting.get("original_result_sha256") == references["result"]["sha256"]
            and accounting.get("original_completion_sha256") == references["completion"]["sha256"],
            "Original pair accounting remains unknown")
    descriptor, expected = original["stage"], original["frozen_stage_snapshot"]
    exact(descriptor, {"id", "sha256"})
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at"})
    require(descriptor["id"] == f"loop-{fingerprint[:16]}-candidate-trials"
            and descriptor["sha256"] == measurement._json_hash(expected)
            and measurement._json_hash(current["stages"].get(descriptor["id"])) == descriptor["sha256"]
            and expected["kind"] == "trials" and expected["status"] == "failed" and expected["actual_usd"] is None
            and money(expected["reserve_usd"], positive=True)
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["started_at"]) <= _time(expected["finished_at"]), "An original full pair hold changed")
    require(str(ledger) == request["shared_ledger"] and binding["request_sha256"] != fingerprint,
            "Only the original ledger and genuinely new future binding are allowed")
    for name, stage in result["spend"]["stages"].items():
        require(name in current["stages"] and measurement._json_hash(stage)
                == measurement._json_hash(current["stages"][name]), "An original pair ledger stage changed")
    require(measurement._json_hash(current["budget_amendments"])
            == measurement._json_hash(result["spend"]["budget_amendments"]), "Original pair amendment changed")
    if "terminal_refresh" in proofs:
        _termination(proofs, references)
    else:
        launch, cleanup = proofs["launch"], proofs["cleanup"]
        tasks = launch["owned_tasks"]
        require(type(tasks) is list and len(tasks) == 3 and len({task["name"] for task in tasks}) == 3,
                "Exact three scoped original tasks required")
        sessions = sorted(task["name"] + "__env" for task in tasks)
        require(launch["request_fingerprint"] == fingerprint
                and launch["candidate"] == request["candidate"] and launch["runner"] == request["runner"]
                and proofs["cleanup_targets"] == {"sessions": sessions}
                and sorted(cleanup["sessions"]) == sessions and cleanup["terminated"] == []
                and cleanup["unresolved"] == [] and not cleanup.get("discovery_errors")
                and cleanup.get("remote_state") != "unknown" and result.get("cleanup") == cleanup,
                "Exact scoped original no-match cleanup differs")
    return descriptor, directory


def _freeze_pair(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                 snapshot, execution_fingerprint):
    exact(review, PAIR_FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["review_kind"] == "retained_native_holds_disposition_review" and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None, "Exact independent two-hold approval required")
    exact(review["acknowledgements"], PAIR_ACKNOWLEDGEMENTS)
    require(all(value is True for value in review["acknowledgements"].values()), "Both full holds require explicit acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Two-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == PAIR_SCOPE_SHA256, "Exact two-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == PAIR_VERSION, "Exact two-original scope approval required")
    references = _pair_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only the two scope-pinned original proof chains are allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger and money(budget, positive=True) and budget == 100
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Two-hold ledger approval is stale")
    _, budget_proof = _artifact(repo, references["budget_review"])
    require(bool(current["budget_amendments"]), "Original budget amendment required")
    amendment = current["budget_amendments"][-1]
    require(str(ledger) == budget_proof["original_ledger"]
            and review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Two-hold amendment differs")
    future = (repo / binding["run_directory"]).resolve()
    require(not Path(binding["run_directory"]).is_absolute() and future != repo / ".context/realism"
            and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        descriptor, directory = _pair_original(repo, original, reference["artifacts"], current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original pair cannot be replayed or modified")
        descriptors.append(descriptor)
    require(len({item["id"] for item in descriptors}) == 2
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(descriptors), "Two exact original stages required")
    old = {item["id"] for item in descriptors}
    for name, stage in current["stages"].items():
        if name not in old:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None, "Every third unknown or unfinished stage blocks")
    marker = future / "execution-started.json"
    if marker.exists():
        require(execution_fingerprint is not None and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future pair execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after both full holds")
    return {**{key: review[key] for key in PAIR_FIELDS},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}



def _three_references(repo, scope_path, scope):
    """Only the fixed ordered three cases and budget proof in the pinned v3 scope."""
    root = scope_path.parent.parent

    def reference(item):
        exact(item, {"path", "sha256"})
        return {"path": str((root / item["path"]).relative_to(repo)), "sha256": item["sha256"]}

    originals = scope["originals"]
    require(type(originals) is list and [item["label"] for item in originals] == ["011", "013", "016"],
            "Exactly the ordered three scoped originals required")
    return {"scope_review": {"path": str(scope_path.relative_to(repo)), "sha256": THREE_SCOPE_SHA256},
            "budget_review": reference(scope["budget_review"]),
            "originals": [{"label": item["label"],
                           "artifacts": {name: reference(ref) for name, ref in item["artifacts"].items()}}
                          for item in originals]}


def _three_pins(repo, root, pins):
    """Pins include non-JSON documents; never reinterpret or rewrite their bytes."""
    require(type(pins) is dict and bool(pins), "Three-hold evidence pins required")
    for relative, sha256 in pins.items():
        path = _path(repo, str((root / relative).relative_to(repo)))
        require(type(sha256) is str and re.fullmatch(r"[0-9a-f]{64}", sha256)
                and digest(path.read_bytes()) == sha256, "Three-hold scoped evidence changed")


def _three_budget(repo, scope_path, scope, current, ledger):
    root, chain = scope_path.parent.parent, scope["budget_chain"]

    def load(reference):
        return _artifact(repo, {"path": str((root / reference["path"]).relative_to(repo)),
                               "sha256": reference["sha256"]})[1]

    before, after, proof = (load(chain[name]) for name in ("before", "after", "budget_review"))
    _three_pins(repo, root, scope["pins"])
    _three_pins(repo, root, proof["pins"])
    amendments = chain["entire_amendment_array_exact"]
    require(type(amendments) is list and len(amendments) == 2
            and measurement._json_hash(current["budget_amendments"]) == measurement._json_hash(amendments)
            and measurement._json_hash(before["budget_amendments"]) == measurement._json_hash(amendments[:1])
            and measurement._json_hash(after["budget_amendments"]) == measurement._json_hash(amendments)
            and money(before["budget_usd"]) and before["budget_usd"] == 100
            and money(after["budget_usd"]) and after["budget_usd"] == 1000
            and money(current["budget_usd"]) and current["budget_usd"] == 1000,
            "Exact old100 prefix and approved1000 append required")
    require(proof.get("approved") is True and proof.get("original_ledger") == str(ledger)
            and proof.get("before_ledger_sha256") == chain["before"]["sha256"]
            and proof.get("after_ledger_sha256") == chain["after"]["sha256"]
            and proof.get("amendment_id") == chain["amendment_id"] == amendments[-1]["id"]
            and proof.get("amendment_sha256") == chain["amendment_sha256"] == measurement._json_hash(amendments[-1]),
            "Exact reviewed1000 amendment required")
    restored = deepcopy(after)
    restored["budget_usd"], restored["budget_amendments"] = before["budget_usd"], before["budget_amendments"]
    require(measurement._json_hash(restored) == measurement._json_hash(before)
            and measurement._json_hash(before["stages"]) == chain["all28_stages_sha256"],
            "Budget amendment must preserve the entire baseline")
    for name, stage in after["stages"].items():
        require(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(stage),
                "A reviewed baseline stage changed")
    prior = deepcopy(before)
    del prior["stages"][scope["exact_original_stages"][2]]
    require(measurement._json_hash(prior) == chain["prior27_whole_ledger_canonical_sha256"]
            and measurement._json_hash(prior["stages"]) == chain["prior27_stage_map_canonical_sha256"],
            "Exact pre-amendment prior ledger required")
    return before, proof


def _three_original016(repo, original, references, current, ledger, binding, prefix):
    """016 is complete/null: its label never establishes a known actual cost."""
    proofs = {name: _artifact(repo, ref)[1] for name, ref in references.items()}
    request, result, bundle = (proofs[name] for name in ("request", "result", "exception_bundle"))
    fingerprint = request["fingerprint"]
    require(measurement._json_hash({key: value for key, value in request.items() if key != "fingerprint"}) == fingerprint
            and proofs["start"].get("fingerprint") == fingerprint and result.get("fingerprint") == fingerprint
            and proofs["completion"].get("fingerprint") == fingerprint
            and proofs["completion"].get("result_sha256") == measurement._json_hash(result)
            and result.get("status") == "reconciliation_required" and result.get("error_type") == "ValueError",
            "Original016 exception seals differ")
    directory = repo / original["run_directory"]
    require(directory.resolve() == directory and directory.is_relative_to(repo / ".context/realism")
            and bundle.get("run") == str(directory) and bundle.get("request_fingerprint") == fingerprint,
            "Original016 directory or sole bundle differs")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        path = directory / filename
        require(repo / references[name]["path"] == path
                and bundle["source_files"].get(str(path)) == references[name]["sha256"],
                "Original016 sealed artifact identity differs")
    terminal, accounting, harvest = (proofs[name] for name in ("terminal_review", "accounting_review", "harvest"))
    for proof in (terminal, accounting):
        require(proof.get("request_fingerprint") == fingerprint and "native_actual_usd" in proof
                and proof["native_actual_usd"] is None and proof.get("original_execution_session") == harvest.get("original_execution_session")
                and proof.get("original_status_remains") == "reconciliation_required"
                and proof.get("error_type") == "ValueError", "Original016 accounting remains unknown")
    require(terminal.get("approved") is True
            and terminal.get("observer_bundle_sha256") == references["exception_bundle"]["sha256"]
            and accounting.get("terminal_review_sha256") == references["terminal_review"]["sha256"]
            and accounting.get("original_result_sha256") == references["result"]["sha256"]
            and accounting.get("original_completion_sha256") == references["completion"]["sha256"]
            and type(harvest.get("original_execution_session")) is int
            and harvest["original_execution_session"] > 0 and type(harvest.get("exit_code")) is int and harvest["exit_code"] == 1
            and harvest.get("terminal_status") == "reconciliation_required" and harvest.get("error_type") == "ValueError"
            and harvest.get("sole_bundle_sha256") == references["exception_bundle"]["sha256"],
            "Original016 terminal/harvest identities differ")
    descriptor, expected = original["stage"], original["frozen_stage_snapshot"]
    exact(descriptor, {"id", "sha256"})
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at"})
    require(descriptor["id"] == f"loop-{fingerprint[:16]}-candidate-trials"
            and descriptor["sha256"] == measurement._json_hash(expected)
            and measurement._json_hash(current["stages"].get(descriptor["id"])) == descriptor["sha256"]
            and expected["kind"] == "trials" and expected["status"] == "complete" and expected["actual_usd"] is None
            and money(expected["reserve_usd"], positive=True) and expected["reserve_usd"] == 7.762499999999999
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["started_at"]) <= _time(expected["finished_at"]), "Original016 full null hold changed")
    require(str(ledger) == request["shared_ledger"] and binding["request_sha256"] != fingerprint,
            "Only original ledger and genuinely new future request allowed")
    for name, stage in result["spend"]["stages"].items():
        require(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(stage),
                "An original016 historical stage changed")
    require(measurement._json_hash(result["spend"]["budget_amendments"]) == measurement._json_hash(prefix),
            "Original016 old amendment prefix changed")
    launch, cleanup = proofs["launch"], proofs["cleanup"]
    tasks = launch["owned_tasks"]
    require(type(tasks) is list and len(tasks) == 3 and len({task["name"] for task in tasks}) == 3,
            "Exact three original016 tasks required")
    sessions = sorted(task["name"] + "__env" for task in tasks)
    require(launch.get("request_fingerprint") == fingerprint and launch.get("candidate") == request["candidate"]
            and launch.get("runner") == request["runner"] and proofs["cleanup_targets"] == {"sessions": sessions}
            and sorted(cleanup["sessions"]) == sessions and cleanup.get("terminated") == []
            and cleanup.get("unresolved") == [] and not cleanup.get("discovery_errors")
            and cleanup.get("remote_state") != "unknown" and result.get("cleanup") == cleanup,
            "Original016 owned no-match cleanup differs")
    return descriptor, directory


def _freeze_three(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                  snapshot, execution_fingerprint):
    exact(review, PAIR_FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["review_kind"] == "retained_native_three_holds_disposition_review" and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None, "Exact independent three-hold approval required")
    exact(review["acknowledgements"], THREE_ACKNOWLEDGEMENTS)
    require(all(value is True for value in review["acknowledgements"].values()), "All three full holds require acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Three-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == THREE_SCOPE_SHA256, "Exact three-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == THREE_VERSION, "Exact three-original scope approval required")
    references = _three_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only three scope-pinned proof chains allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and review["ledger"]["path"] == str(ledger.relative_to(repo))
            and money(budget, positive=True) and budget == 1000
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Three-hold ledger approval is stale")
    before, budget_proof = _three_budget(repo, scope_path, scope, current, ledger)
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Three-hold amendment differs")
    future = repo / binding["run_directory"]
    require(not Path(binding["run_directory"]).is_absolute() and future.resolve() == future
            and future != repo / ".context/realism" and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    # Legacy helpers receive the exact historical amendment prefix; their strict
    # failed-stage, seals and termination checks remain unchanged for v1/v2.
    old_current = {**current, "budget_amendments": before["budget_amendments"]}
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        if original["label"] == "016":
            descriptor, directory = _three_original016(repo, original, reference["artifacts"], current, ledger,
                                                        binding, before["budget_amendments"])
        else:
            descriptor, directory = _pair_original(repo, original, reference["artifacts"], old_current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original three runs cannot be replayed or modified")
        descriptors.append(descriptor)
    require(len({item["id"] for item in descriptors}) == 3
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(descriptors), "Three exact ordered stages required")
    originals = {item["id"] for item in descriptors}
    for name, stage in current["stages"].items():
        if name not in originals:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None, "Every fourth unknown or unfinished stage blocks")
    marker = future / "execution-started.json"
    require(not marker.is_symlink(), "Noncanonical future start marker")
    if marker.exists():
        require(marker.resolve() == marker and execution_fingerprint is not None
                and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future three-hold execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after all three full holds")
    return {**{key: review[key] for key in PAIR_FIELDS},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}


def freeze_disposition(repo, review_path, *, ledger, budget, binding, required_reserve):
    """Freeze independently supplied approval under repo -> stable ledger lock order.

    No ledger show/write, approval generation, reconciliation or paid work occurs.
    The caller holds the repository measurement lock throughout preparation/execute.
    """
    with _snapshot(ledger, budget) as snapshot:
        return _freeze(repo, review_path, ledger=ledger, budget=budget, binding=binding,
                       required_reserve=required_reserve, snapshot=snapshot)


class ReservationGuard:
    """Validate in reserve's existing lock; permit only this native -> scan transition."""

    def __init__(self, repo, review_path, *, ledger, budget, binding, disposition,
                 fingerprint, trial_reserve, scan_reserve):
        self.repo, self.review_path, self.ledger = repo, review_path, ledger
        self.budget, self.binding, self.disposition = budget, binding, disposition
        self.fingerprint = fingerprint
        self.trial_reserve, self.scan_reserve = trial_reserve, scan_reserve
        self.tag = f"loop-{fingerprint[:16]}-candidate"
        self.baseline = None
        self.native_start = None

    def reserve(self, stage, kind, amount):
        require(money(amount, positive=True), "Noncanonical reservation amount")

        def validate(locked):
            current, raw_sha = _read_snapshot(self.ledger, self.budget)
            require(measurement._json_hash(current) == measurement._json_hash(locked), "Locked ledger differs from its current bytes")
            if stage == self.tag + "-trials":
                require(kind == "trials" and amount == self.trial_reserve and self.baseline is None,
                        "Unexpected or repeated native reservation")
                snapshot = current, raw_sha
                required = self.trial_reserve + self.scan_reserve
            else:
                require(stage == self.tag + "-tells" and kind == "tells" and amount == self.scan_reserve
                        and self.baseline is not None, "Unexpected or unowned scan reservation")
                original, original_sha = self.baseline
                native = current["stages"].get(self.tag + "-trials", {})
                exact(native, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at"})
                require(native["kind"] == "trials" and native["status"] == "complete"
                        and money(native["actual_usd"]) and native["reserve_usd"] == self.trial_reserve
                        and type(native["started_at"]) is str and type(native["finished_at"]) is str
                        and native["started_at"] == self.native_start["started_at"]
                        and bool(native["finished_at"]),
                        "This native outcome is unfinished or unknown; no scan")
                historical = deepcopy(current)
                del historical["stages"][self.tag + "-trials"]
                require(measurement._json_hash(historical) == measurement._json_hash(original), "Historical ledger or foreign stage changed before scan")
                snapshot, required = (original, original_sha), self.trial_reserve + self.scan_reserve
                require(self.scan_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
                        "Scan reserve no longer fits after native actual cost")
            frozen = _freeze(self.repo, self.review_path, ledger=self.ledger, budget=self.budget,
                             binding=self.binding, required_reserve=required, snapshot=snapshot,
                             execution_fingerprint=self.fingerprint)
            require(frozen == self.disposition, "Retained-hold disposition changed before reservation")
            if self.baseline is None:
                self.baseline = deepcopy(current), raw_sha

        result = stages.reserve(self.ledger, self.budget, stage, kind, amount, validate=validate)
        if stage == self.tag + "-trials":
            self.native_start = deepcopy(result["stages"][stage])
        return result


def _reconciled_nonattempt(repo, descriptor, current, ledger, binding):
    """Validate the separately scoped failed-zero case against the real image."""
    exact(descriptor, {"scope_review", "stage", "run_directory", "request_fingerprint", "artifacts"})
    exact(descriptor["scope_review"], {"path", "sha256"})
    require(descriptor["scope_review"]["sha256"] == RECONCILED_SCOPE_SHA256,
            "Exact reconciled nonattempt scope required")
    _, scope = _artifact(repo, descriptor["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == RECONCILED_VERSION,
            "Reconciled nonattempt implementation scope differs")
    expected = {**scope["expected_reconciled_nonattempt"], "scope_review": descriptor["scope_review"]}
    require(measurement._json_hash(descriptor) == measurement._json_hash(expected),
            "Only the entire scope-pinned reconciled nonattempt is eligible")
    references = descriptor["artifacts"]
    names = {"request", "start", "result", "state", "completion", "trace", "raw_result", "launch_intent",
             "process", "cleanup", "native_accounting", "harvest", "callback", "terminal_review",
             "terminal_proof", "financial_gate", "financial_proof", "financial_proposal", "finish_script",
             "finish_intent", "before", "after", "finish_receipt", "final_accounting"}
    exact(references, names)
    proofs = {}
    for name, reference in references.items():
        exact(reference, {"path", "sha256"})
        require(type(reference["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]),
                "Invalid reconciled evidence digest")
        raw = _path(repo, reference["path"]).read_bytes()
        require(digest(raw) == reference["sha256"], "Reconciled evidence bytes changed")
        if name not in {"trace", "finish_script"}:
            proofs[name] = _json(raw)
    original = repo / descriptor["run_directory"]
    future = repo / binding["run_directory"]
    require(original.resolve() == original and original.is_relative_to(repo / ".context/realism")
            and not future.is_relative_to(original) and not original.is_relative_to(future),
            "Reconciled original cannot be replayed or overlap the future")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "state": "state.json", "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        require(repo / references[name]["path"] == original / filename, "Reconciled original artifact path differs")
    request, result, completion = (proofs[name] for name in ("request", "result", "completion"))
    fingerprint = descriptor["request_fingerprint"]
    require(request.get("fingerprint") == fingerprint
            == measurement._json_hash({key: value for key, value in request.items() if key != "fingerprint"})
            and proofs["start"].get("fingerprint") == result.get("fingerprint") == completion.get("fingerprint") == fingerprint
            and completion.get("result_sha256") == measurement._json_hash(result)
            and measurement._json_hash(proofs["raw_result"]) == measurement._json_hash(result)
            and request.get("shared_ledger") == str(ledger), "Reconciled original request or seals differ")
    require(request.get("mode") == "candidate_only" and request.get("baseline_evidence") is None
            and request.get("historical_reference") is None and result.get("historical_reference") is None
            and result.get("comparison") is None and result.get("status") == "reconciliation_required"
            and result.get("error_type") == "ValueError" and proofs["state"].get("status") == "reconciliation_required"
            and result.get("cleanup") == proofs["cleanup"]
            and proofs["native_accounting"].get("status") == "unknown"
            and proofs["native_accounting"].get("logs") == [], "Reconciled failure must retain its unknown native evidence")
    terminal, harvest, callback = (proofs[name] for name in ("terminal_review", "harvest", "callback"))
    session = harvest.get("original_execution_session")
    require(type(session) is int and session > 0 and harvest.get("closed") is True
            and type(harvest.get("exit_code")) is int and harvest["exit_code"] == 1
            and type(proofs["process"].get("returncode")) is int and proofs["process"]["returncode"] == 1,
            "Reconciled original must have its sole failed terminal harvest")
    for name in ("terminal_review", "terminal_proof", "callback", "process"):
        require(proofs[name].get("request_fingerprint") == fingerprint, "Reconciled terminal fingerprint differs")
    for name in ("terminal_review", "terminal_proof", "callback"):
        require(proofs[name].get("original_execution_session") == session, "Reconciled original session differs")
    require(terminal.get("approved") is True and terminal.get("native_model_nonattempt_supported") is True
            and terminal.get("native_job_run_reached") is False and terminal.get("owned_trial_sandbox_launch_reachable") is False
            and terminal.get("complete_native_samples") == 0 and terminal.get("strict_scan_coverage_accepted") is False
            and terminal.get("comparison_accepted") is False and terminal.get("comparison") is None,
            "Independent native model nonattempt proof required")

    def session_ref(name):
        root = original.parent
        return {"path": str((repo / references[name]["path"]).relative_to(root)), "sha256": references[name]["sha256"]}

    require(terminal.get("proof") == session_ref("terminal_proof")
            and terminal.get("original_harvest") == session_ref("harvest")
            and terminal.get("observer_exception_callback") == session_ref("callback")
            and harvest.get("after_observer_callback") == session_ref("callback")
            and harvest.get("process_receipt") == session_ref("process")
            and harvest.get("result") == session_ref("result") and harvest.get("completion") == session_ref("completion")
            and proofs["process"].get("stdout_sha256") == references["raw_result"]["sha256"],
            "Reconciled terminal evidence joins differ")
    intent = proofs["launch_intent"]
    earliest, deadline = (_time(intent.get(key)) for key in ("launch_intent_at", "fixed_observer_deadline"))
    require(earliest is not None and deadline is not None and (deadline - earliest).total_seconds() == 8765
            and intent.get("request_fingerprint") == fingerprint
            and callback.get("earliest_launch_intent") == intent["launch_intent_at"]
            and callback.get("fixed_observer_deadline") == intent["fixed_observer_deadline"]
            and callback.get("deadline_reset") is False and callback.get("normal_watch_started") is False
            and callback.get("normal_terminal_collector_invocations") == 0,
            "Reconciled original immutable deadline or missing-launch boundary differs")
    stage = descriptor["stage"]
    exact(stage, {"id", "sha256", "snapshot"})
    target, snapshot = stage["id"], stage["snapshot"]
    exact(snapshot, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"})
    require(target == f"loop-{fingerprint[:16]}-candidate-trials"
            and snapshot["kind"] == "trials" and snapshot["status"] == "failed"
            and money(snapshot["actual_usd"]) and snapshot["actual_usd"] == 0
            and money(snapshot["reserve_usd"], positive=True)
            and _time(snapshot["started_at"]) is not None and _time(snapshot["finished_at"]) is not None
            and _time(snapshot["finished_at"]) >= _time(snapshot["started_at"])
            and stage["sha256"] == measurement._json_hash(snapshot)
            == measurement._json_hash(current["stages"].get(target)), "Exact finite failed-zero stage required")
    before, before_sha = _read_snapshot(repo / references["before"]["path"], 1000)
    after, after_sha = _read_snapshot(repo / references["after"]["path"], 1000)
    old = before["stages"].get(target, {})
    restored = deepcopy(after)
    restored["stages"][target] = old
    require(old.get("status") == "failed" and old.get("actual_usd") is None
            and old.get("kind") == snapshot["kind"] and old.get("reserve_usd") == snapshot["reserve_usd"]
            and old.get("started_at") == snapshot["started_at"] and _time(old.get("finished_at")) is not None
            and _time(old["finished_at"]) <= _time(snapshot["finished_at"])
            and measurement._json_hash(restored) == measurement._json_hash(before)
            and measurement._json_hash(after["stages"].get(target)) == stage["sha256"],
            "Reconciled financial write changed unrelated history or native outcome")
    for name, value in after.items():
        if name != "stages":
            require(measurement._json_hash({name: current.get(name)}) == measurement._json_hash({name: value}),
                    "Reconciled unrelated ledger fields changed")
    for name, value in after["stages"].items():
        require(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(value),
                "Reconciled entire finalized historical ledger changed")
    gate, receipt, accounting = (proofs[name] for name in ("financial_gate", "finish_receipt", "final_accounting"))
    for name in ("financial_gate", "finish_intent", "finish_receipt"):
        record = proofs[name]
        require(record.get("stage_id") == target and money(record.get("actual_usd")) and record["actual_usd"] == 0
                and record.get("status") == "failed" and record.get("accounting_only") is True,
                "Reconciled financial evidence must preserve failed-zero accounting")
    require(gate.get("approved") is True and gate.get("one_normal_native_finish_approved") is True
            and gate.get("request_fingerprint") == receipt.get("request_fingerprint") == fingerprint
            and gate.get("script_sha256") == references["finish_script"]["sha256"]
            and gate.get("proposal_sha256") == references["financial_proposal"]["sha256"]
            and gate.get("proof") == session_ref("financial_proof")
            and gate.get("ledger_sha256") == before_sha and gate.get("detail") == snapshot["detail"],
            "Exact independent financial gate and detail required")
    normalized = deepcopy(after)
    normalization = gate.get("postimage_normalization", {})
    require(normalization.get("target") == target and normalization.get("only_field") == "finished_at"
            and type(normalization.get("placeholder")) is str, "Exact target-only postimage normalization required")
    normalized["stages"][target]["finished_at"] = normalization["placeholder"]
    require(measurement._json_hash(normalized) == gate.get("normalized_postimage_sha256")
            == receipt.get("normalized_postimage_sha256") == accounting.get("normalized_postimage_sha256"),
            "Reconciled entire normalized postimage differs")
    require(proofs["finish_intent"].get("gate_sha256") == receipt.get("gate_sha256") == references["financial_gate"]["sha256"]
            and proofs["finish_intent"].get("proposal_sha256") == receipt.get("proposal_sha256") == references["financial_proposal"]["sha256"]
            and proofs["finish_intent"].get("ledger_sha256") == receipt.get("before_ledger_sha256") == before_sha
            and receipt.get("after_ledger_sha256") == after_sha and type(receipt.get("normal_finish_calls")) is int
            and receipt["normal_finish_calls"] == 1 and receipt.get("original_execution_session") == session,
            "Reconciled sole financial write identity differs")
    require(accounting.get("approved") is True and accounting.get("financial_reconciliation_complete") is True
            and accounting.get("accounting_only") is True and accounting.get("request_fingerprint") == fingerprint
            and accounting.get("original_execution_session") == session and accounting.get("ledger_sha256") == after_sha
            and accounting.get("gate") == session_ref("financial_gate")
            and accounting.get("primary_finish_receipt") == session_ref("finish_receipt")
            and measurement._json_hash(accounting.get("current_stage_snapshot")) == measurement._json_hash(after)
            and accounting.get("original036_native_status") == "failed"
            and money(accounting.get("original036_native_actual_usd")) and accounting["original036_native_actual_usd"] == 0
            and accounting.get("original_sealed_status_preserved") == "reconciliation_required"
            and accounting.get("strict_scan_coverage_accepted") is False and accounting.get("comparison_accepted") is False,
            "Independent finalized accounting readback required")
    return target

def _freeze_reconciled(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                  snapshot, execution_fingerprint, retained_interaction=None, reconciled_failed_scans=None):
    # Only the v5 handler supplies its already validated single descriptor. The
    # ledger image is never filtered, rewritten or represented as a v4 snapshot.
    fields = RECONCILED_FIELDS if retained_interaction is None else INTERACTION_FIELDS
    kind = ("retained_native_three_holds_reconciled_nonattempt_disposition_review" if retained_interaction is None
            else "retained_four_holds_continuation_disposition")
    acknowledgements = THREE_ACKNOWLEDGEMENTS if retained_interaction is None else INTERACTION_ACKNOWLEDGEMENTS
    if reconciled_failed_scans is not None:
        require(retained_interaction is not None, "Known scans cannot extend the three-hold protocol")
        fields, kind, acknowledgements = (FAILED_SCAN_FIELDS, "retained_four_holds_known_failed_scans_disposition",
                                          FAILED_SCAN_ACKNOWLEDGEMENTS)
    exact(review, fields | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["review_kind"] == kind and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None,
            "Exact independent three-hold approval required" if retained_interaction is None
            else "Exact bounded continuation declaration required")
    exact(review["acknowledgements"], acknowledgements)
    require(all(value is True for value in review["acknowledgements"].values()),
            "All three full holds require acknowledgement" if retained_interaction is None
            else "All four full holds require acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Three-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == THREE_SCOPE_SHA256, "Exact three-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == THREE_VERSION, "Exact three-original scope approval required")
    references = _three_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only three scope-pinned proof chains allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and review["ledger"]["path"] == str(ledger.relative_to(repo))
            and money(budget, positive=True) and budget == 1000
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Three-hold ledger approval is stale")
    before, budget_proof = _three_budget(repo, scope_path, scope, current, ledger)
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Three-hold amendment differs")
    future = repo / binding["run_directory"]
    require(not Path(binding["run_directory"]).is_absolute() and future.resolve() == future
            and future != repo / ".context/realism" and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    # Legacy helpers receive the exact historical amendment prefix; their strict
    # failed-stage, seals and termination checks remain unchanged for v1/v2.
    old_current = {**current, "budget_amendments": before["budget_amendments"]}
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        if original["label"] == "016":
            descriptor, directory = _three_original016(repo, original, reference["artifacts"], current, ledger,
                                                        binding, before["budget_amendments"])
        else:
            descriptor, directory = _pair_original(repo, original, reference["artifacts"], old_current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original three runs cannot be replayed or modified")
        descriptors.append(descriptor)
    expected_descriptors = descriptors if retained_interaction is None else [*descriptors, retained_interaction]
    require(len({item["id"] for item in descriptors}) == 3
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(expected_descriptors),
            "Three exact ordered stages required" if retained_interaction is None
            else "Three exact ordered native stages and only the scoped interaction required")
    originals = {item["id"] for item in descriptors}
    reconciled = _reconciled_nonattempt(repo, review["reconciled_nonattempt"], current, ledger, binding)
    require(reconciled not in originals, "Reconciled case cannot replace an original full hold")
    originals.add(reconciled)
    if retained_interaction is not None:
        require(retained_interaction["id"] not in originals, "Retained interaction cannot replace a native case")
        originals.add(retained_interaction["id"])
    if reconciled_failed_scans is not None:
        for known_scan in reconciled_failed_scans:
            require(known_scan["id"] not in originals, "Known scan cannot replace an original hold/nonattempt")
            originals.add(known_scan["id"])
    for name, stage in current["stages"].items():
        if name not in originals:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None,
                    "Every fourth unknown or unfinished stage blocks" if retained_interaction is None
                    else "Every fifth unknown or unfinished stage blocks")
    marker = future / "execution-started.json"
    require(not marker.is_symlink(), "Noncanonical future start marker")
    if marker.exists():
        require(marker.resolve() == marker and execution_fingerprint is not None
                and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future three-hold execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after all three full holds" if retained_interaction is None
            else "New native and scan reserves do not fit after all four full holds")
    return {**{key: review[key] for key in fields},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}


def _freeze_interaction(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                        snapshot, execution_fingerprint, reconciled_failed_scans=None):
    """New continuation authority for one pinned unknown; never reconcile it.

    A coordinator declaration cites the owner's restart-after-integration request
    and binds all substantive disposition fields, including the whole ledger SHA,
    validator and exact future request. Its full-hold acknowledgements are the
    coordinator's interpretation, never fabricated owner billing instructions.
    Original denied accounting remains uncertainty, not continuation authority.
    """
    fields = INTERACTION_FIELDS if reconciled_failed_scans is None else FAILED_SCAN_FIELDS
    exact(review, fields | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    exact(binding, {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str, "Invalid continuation future directory")
    descriptor = review["retained_interaction"]
    exact(descriptor, {"stage", "original_request", "failure_accounting"})
    stage = descriptor["stage"]
    exact(stage, {"id", "sha256", "snapshot"})
    require(type(stage["id"]) is str and bool(stage["id"]), "Exact retained interaction identity required")
    expected = stage["snapshot"]
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"})
    require(expected["kind"] == "interaction" and expected["status"] == "unknown"
            and expected["actual_usd"] is None and money(expected["reserve_usd"], positive=True)
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["finished_at"]) >= _time(expected["started_at"])
            and type(expected["detail"]) is str, "Only a finished terminal unknown full interaction hold is eligible")
    current, _ = snapshot
    require(stage["sha256"] == measurement._json_hash(expected)
            == measurement._json_hash(current["stages"].get(stage["id"])), "Retained interaction stage changed")
    for name, sha256 in (("original_request", INTERACTION_REQUEST_SHA256),
                         ("failure_accounting", INTERACTION_ACCOUNTING_SHA256)):
        exact(descriptor[name], {"path", "sha256"})
        require(descriptor[name]["sha256"] == sha256, "Only the pinned original interaction evidence is eligible")
    request_path, request = _artifact(repo, descriptor["original_request"])
    accounting_path, accounting = _artifact(repo, descriptor["failure_accounting"])
    require(request_path.parent.name == "model-calls" and request_path.name == stage["id"] + "-request.json",
            "Exact original interaction request artifact required")
    require(accounting.get("review_kind") == "independent_original_http400_accounting_recovery"
            and accounting.get("approved") is False and accounting.get("outcome") == "unresolved_original_charge"
            and accounting.get("stage_id") == stage["id"]
            and accounting.get("original_request_sha256") == descriptor["original_request"]["sha256"]
            and accounting.get("ledger_path") == str(ledger.relative_to(repo))
            and accounting.get("ledger_before_sha256") == accounting.get("ledger_after_sha256")
            and "exact_actual_cost_usd" in accounting and accounting["exact_actual_cost_usd"] is None
            and accounting.get("one_time_normal_stages_finish_justified") is False
            and "supported_stage_update" in accounting and accounting["supported_stage_update"] is None
            and accounting.get("inference_retry_permitted") is False
            and accounting.get("old036_failed_zero_preserved") is True
            and money(request.get("reserve_usd"), positive=True)
            and request["reserve_usd"] == expected["reserve_usd"], "Original interaction accounting must remain unresolved")
    descriptors = review["stages"]
    require(type(descriptors) is list and len(descriptors) == 4, "Exactly three native holds and one interaction required")
    for item in descriptors:
        exact(item, {"id", "sha256"})
        require(type(item["id"]) is str, "Invalid retained-hold stage identity")
    require(descriptors[-1] == {"id": stage["id"], "sha256": stage["sha256"]}
            and len({item["id"] for item in descriptors}) == 4, "Interaction must be the sole fourth retained hold")
    originals = accounting.get("all_four_unknown_stage_snapshots")
    exact(originals, {item["id"] for item in descriptors})
    require(all(measurement._json_hash(originals[item["id"]]) == item["sha256"] for item in descriptors),
            "All four original accounting snapshots must remain exact")
    future = repo / binding["run_directory"]
    require(all(not future.is_relative_to(directory) and not directory.is_relative_to(future)
                for directory in (request_path.parent.parent, accounting_path.parent)),
            "Original interaction evidence cannot overlap new work")
    _, authorization = _artifact(repo, review["restart_authorization"])
    scope_fields = INTERACTION_SCOPE_FIELDS if reconciled_failed_scans is None else FAILED_SCAN_SCOPE_FIELDS
    acknowledgements = INTERACTION_ACKNOWLEDGEMENTS if reconciled_failed_scans is None else FAILED_SCAN_ACKNOWLEDGEMENTS
    owner_pin = INTERACTION_OWNER_REQUEST_SHA256 if reconciled_failed_scans is None else FAILED_SCAN_OWNER_REQUEST_SHA256
    version = INTERACTION_VERSION if reconciled_failed_scans is None else FAILED_SCAN_VERSION
    kind = ("coordinator_bounded_continuation_interpretation" if reconciled_failed_scans is None
            else "coordinator_known_failed_scans_continuation_interpretation")
    exact(authorization, scope_fields)
    exact(authorization["acknowledgements"], acknowledgements)
    exact(authorization["owner_request"], {"path", "sha256"})
    require(authorization["owner_request"]["sha256"] == owner_pin,
            "Exact original owner restart request required")
    _, owner_request = _artifact(repo, authorization["owner_request"])
    resume_authorized = (owner_request.get("restart_authorized_after_integration") is True
                         if reconciled_failed_scans is None else owner_request.get("resume_authorized") is True
                         and owner_request.get("model_switch_authorized") is True
                         and owner_request.get("no_unsubstantiated_hold_release") is True
                         and owner_request.get("ledger") == str(ledger))
    if reconciled_failed_scans is not None:
        exact(authorization["original_v5_scope"], {"path", "sha256"})
        require(authorization["original_v5_scope"]["sha256"] == FAILED_SCAN_V5_SCOPE_SHA256,
                "Exact original v5 declaration is provenance only; never extend its authority")
        _, old_scope = _artifact(repo, authorization["original_v5_scope"])
        exact(old_scope, INTERACTION_SCOPE_FIELDS)
        exact(old_scope["acknowledgements"], INTERACTION_ACKNOWLEDGEMENTS)
        require(old_scope["version"] == INTERACTION_VERSION
                and old_scope["declaration_kind"] == "coordinator_bounded_continuation_interpretation"
                and old_scope["owner_request"]["sha256"] == INTERACTION_OWNER_REQUEST_SHA256
                and all(value is True for value in old_scope["acknowledgements"].values()),
                "Original v5 provenance differs")
        _artifact(repo, old_scope["owner_request"])
        require(_time(authorization["declared_at"]) is not None
                and all(_time(authorization["declared_at"]) >= _time(item["snapshot"]["finished_at"])
                        for item in reconciled_failed_scans), "Declaration must follow both accounting completions")
    require(resume_authorized
            and money(owner_request.get("budget_total_usd"), positive=True)
            and owner_request["budget_total_usd"] == 1000
            and _time(owner_request.get("at")) is not None, "Owner request must preserve the existing restart scope and cap")
    bounded = {key: review[key] for key in fields - {"restart_authorization"}}
    require(authorization["version"] == version
            and authorization["declaration_kind"] == kind
            and type(authorization["declared_by"]) is str and bool(authorization["declared_by"].strip())
            and _time(authorization["declared_at"]) is not None
            and _time(authorization["declared_at"]) >= _time(owner_request["at"])
            and _time(authorization["declared_at"]) >= _time(expected["finished_at"])
            and authorization["continuation_after_feedback_integration"] is True
            and money(authorization["budget_usd"], positive=True) and authorization["budget_usd"] == budget == 1000
            and authorization["ledger"] == review["ledger"]
            and authorization["future_binding"] == binding == review["future_binding"]
            and authorization["bounded_disposition_sha256"] == measurement._json_hash(bounded)
            and all(value is True for value in authorization["acknowledgements"].values()),
            "New exact owner continuation-after-integration authorization required")
    return _freeze_reconciled(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                              required_reserve=required_reserve, snapshot=snapshot,
                              execution_fingerprint=execution_fingerprint, retained_interaction=descriptors[-1],
                              reconciled_failed_scans=reconciled_failed_scans)

def _reconciled_failed_scan(repo, descriptor, current, ledger, binding):
    """Validate one explicitly pinned finite FAILED scan; never authorize unknown cost."""
    exact(descriptor, {"stage", "proof", "receipt"})
    stage = descriptor["stage"]
    exact(stage, {"id", "sha256", "snapshot"})
    require(type(stage["id"]) is str and stage["id"] in FAILED_SCAN_PINS,
            "Only the two exact historically reconciled scans are eligible")
    pin = FAILED_SCAN_PINS[stage["id"]]
    expected = stage["snapshot"]
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"})
    require(stage["sha256"] == pin["snapshot_sha256"] == measurement._json_hash(expected)
            == measurement._json_hash(current["stages"].get(stage["id"]))
            and expected["kind"] == "tells" and expected["status"] == "failed"
            and money(expected["actual_usd"], positive=True)
            and Decimal(str(expected["actual_usd"])) == Decimal(pin["actual_usd_decimal"])
            and money(expected["reserve_usd"], positive=True) and expected["reserve_usd"] == 15.75
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["finished_at"]) >= _time(expected["started_at"]),
            "Exact finite known-cost FAILED scan snapshot required")
    for name in ("proof", "receipt"):
        exact(descriptor[name], {"path", "sha256"})
        require(descriptor[name] == pin[name], "Original failed-scan proof/receipt identity differs")
    proof_path, proof = _artifact(repo, descriptor["proof"])
    receipt_path, receipt = _artifact(repo, descriptor["receipt"])
    require(proof.get("target_stage") == receipt.get("target_stage") == stage["id"]
            and proof.get("normal_finish_status") == "failed" and proof.get("accounting_only") is True
            and proof.get("strict_scan_coverage_accepted") is False
            and proof.get("semantic_measurement_accepted") is False
            and receipt.get("status") == "accounting_reconciled_scan_still_failed"
            and type(receipt.get("normal_finish_calls")) is int and receipt["normal_finish_calls"] == 1
            and receipt.get("proof_sha256") == pin["proof"]["sha256"]
            and receipt.get("actual_usd") == expected["actual_usd"]
            and receipt.get("target_snapshot_after") == expected
            and receipt.get("only_target_accounting_fields_changed") is True
            and receipt.get("all_other_stages_and_old_seals_preserved") is True
            and receipt.get("strict_scan_coverage_accepted") is False,
            "Accounting closure cannot establish scan coverage or semantic success")
    fields = proof.get("normal_finish_fields", {})
    require(fields.get("path") == str(ledger) and fields.get("budget") == current.get("budget_usd") == 1000
            and fields.get("stage") == stage["id"] and fields.get("status") == "failed"
            and fields.get("actual") == expected["actual_usd"] and fields.get("detail") == expected["detail"],
            "Original ordinary normal-finish fields differ")
    preimage = proof["ledger_preimage"]
    _, before = _artifact(repo, preimage["saved_copy"])
    require(preimage["original_path"] == str(ledger)
            and preimage["raw_sha256"] == preimage["saved_copy"]["sha256"] == receipt["before_sha256"]
            and receipt["ledger_identity_after"]["path"] == str(ledger)
            and receipt["ledger_identity_after"]["sha256"] == receipt["after_sha256"],
            "Original whole-ledger accounting chain differs")
    predicted = proof["expected_whole_postimage"]
    _, normalized = _artifact(repo, {"path": predicted["path"], "sha256": predicted["raw_file_sha256"]})
    normalization = predicted["normalization"]
    require(normalization["target_stage"] == stage["id"] and normalization["only_field"] == "finished_at"
            and type(normalization["placeholder"]) is str, "Exact target-only timestamp normalization required")
    old = before["stages"].get(stage["id"], {})
    require(old.get("kind") == "tells" and old.get("status") == "failed" and old.get("actual_usd") is None
            and old.get("reserve_usd") == expected["reserve_usd"]
            and old.get("started_at") == expected["started_at"], "Original scan full-hold preimage differs")
    computed = deepcopy(before)
    computed["stages"][stage["id"]] = {**expected, "finished_at": normalization["placeholder"]}
    require(computed == normalized and measurement._json_hash(normalized)
            == predicted["normalized_semantic_sha256"] == receipt["normalized_whole_postimage_sha256"],
            "Entire original accounting postimage differs")
    computed["stages"][stage["id"]] = expected
    require(digest((json.dumps(computed, indent=2, allow_nan=False) + "\n").encode()) == receipt["after_sha256"],
            "Exact original ordinary writer postimage bytes differ")
    require(all(current.get(key) == value for key, value in before.items() if key != "stages")
            and all(current["stages"].get(name) == value for name, value in before["stages"].items()
                    if name != stage["id"]), "Historical preimage entries/top-level values changed")
    exact(proof["attempt_closure"], {"path", "sha256"})
    require(proof["attempt_closure"] == pin["closure"], "Exact all-attempt raw charge closure required")
    _, closure = _artifact(repo, proof["attempt_closure"])
    attempts = closure.get("attempts")
    require(type(attempts) is list and len(attempts) == closure.get("attempted_parts") == 3
            and closure.get("planned_parts") == pin["planned_parts"]
            and closure.get("exact_total_usd_decimal") == pin["actual_usd_decimal"]
            and measurement._json_hash(attempts) == pin["raw_charge_closure_sha256"],
            "All exact original attempted charges must close")
    checks = closure["all_attempted_charge_closure"]
    require(checks.get("every_attempt_retains_original_response_id_and_finite_cost") is True
            and checks.get("no_inferred_zero_or_missing_response") is True
            and checks.get("frozen_source_verified") is True
            and checks.get("no_retries") == {"sdk": 0, "model": 0, "refusals": 0, "answer_attempts": 1},
            "All original attempts require finite no-retry closure")
    charges, ids, tokens, refusals = [], set(), 0, 0
    for attempt in attempts:
        claude = pin["closure_kind"] == "claude_refusal"
        identifier = attempt.get("response_generation_id" if claude else "genuine_response_generation_id")
        usage = attempt.get("raw_usage", {})
        require(type(identifier) is str and bool(identifier) and identifier not in ids
                and money(usage.get("cost"), positive=True)
                and attempt.get("exact_frozen_prompt_match" if claude else "request_matches_exact_only_planned_part") is True,
                "Missing, duplicated, unknown or nonfinite original charge")
        ids.add(identifier)
        charge = Decimal(str(usage["cost"]))
        require(charge == Decimal(attempt["actual_usd_decimal" if claude else "exact_cost_usd_decimal"]),
                "Saved raw cost differs from charge closure")
        charges.append(charge)
        require(type(usage.get("total_tokens")) is int and usage["total_tokens"] > 0,
                "Original token closure missing")
        tokens += usage["total_tokens"]
        if claude:
            refusal = attempt["refusal_metadata"]
            require(refusal["sdk_max_retries"] == refusal["model_config_max_retries"]
                    == refusal["refusal_retry_count_allowed"] == 0, "Original retry policy differs")
            refusals += refusal["is_refusal"] is True
        else:
            require(attempt["finish_reason"] == attempt["native_finish_reason"] == "length"
                    and attempt["response_content"] is None and attempt["response_tool_calls"] is None
                    and attempt["response_refusal"] is None
                    and usage["completion_tokens"] == usage["completion_tokens_details"]["reasoning_tokens"] == 8192
                    and attempt["event_config"]["max_retries"] == 0,
                    "Exact original reasoning-exhaustion failure metadata differs")
    require(sum(charges, Decimal(0)) == Decimal(pin["actual_usd_decimal"])
            and tokens == pin["total_tokens"], "Original all-attempt charge/token closure differs")
    if pin["closure_kind"] == "claude_refusal":
        require(refusals == 2 and len(closure.get("not_attempted_after_refusal", [])) == 1
                and closure.get("total_tokens") == tokens,
                "Original Claude sequential refusal/unattempted closure differs")
        parquet = closure["original_scan_parquet"]
    else:
        require(closure.get("unattempted_parts") == 0 and closure.get("usable_results") == 0
                and closure.get("raw_model_events") == closure.get("unique_response_generation_ids") == 3
                and closure["token_totals"]["total"] == tokens
                and closure["token_totals"]["reasoning"] == 24576,
                "Original DeepSeek three-planned/three-attempted closure differs")
        parquet = closure["parquet"]
    _, preserved = _artifact(repo, proof["preservation"]["inventory"])
    for name, identity in preserved.items():
        path = Path(name)
        require(path.is_relative_to(repo), "Original evidence must remain inside repository")
        require(path.resolve() == path and path.is_file()
                and digest(path.read_bytes()) == identity["sha256"]
                and oct(path.stat().st_mode & 0o7777) == identity["mode"], "Original scan proof/source/seal changed")
    original = _path(repo, parquet["path"])
    require(digest(original.read_bytes()) == parquet["sha256"], "Original raw scan responses changed")
    original_run = repo / pin["original_run_directory"]
    require(original_run.resolve() == original_run and original_run.is_dir(), "Original run directory differs")
    future = repo / binding["run_directory"]
    require(original.is_relative_to(original_run)
            and all(not future.is_relative_to(old_dir) and not old_dir.is_relative_to(future)
                    for old_dir in (original_run, proof_path.parent, receipt_path.parent)),
            "Original failed scans/accounting cannot be replayed or overwritten")
    return stage


def _freeze_known_failed_scans(repo, review_path, review, raw, *, ledger, budget, binding,
                              required_reserve, snapshot, execution_fingerprint):
    """Separate one/two known FAILED scans from the same four unknown full holds."""
    exact(review, FAILED_SCAN_FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    descriptors = review["reconciled_failed_scans"]
    require(type(descriptors) is list and 1 <= len(descriptors) <= 2,
            "One or two exact known FAILED scan descriptors required")
    current, _ = snapshot
    validated = [_reconciled_failed_scan(repo, item, current, ledger, binding) for item in descriptors]
    require(len({item["id"] for item in validated}) == len(validated), "Duplicate known failed scan")
    require([item["id"] for item in validated] == [name for name in FAILED_SCAN_PINS
                                                if name in {item["id"] for item in validated}],
            "Canonical historical known-scan order required")
    return _freeze_interaction(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                               required_reserve=required_reserve, snapshot=snapshot,
                               execution_fingerprint=execution_fingerprint, reconciled_failed_scans=validated)


def _seven_scope(repo, review, current, ledger, binding):
    """Three exact interrupted unknowns; the saved whole ledger is never filtered."""
    scope = review["regeneration_scope"]
    exact(scope, SEVEN_ARTIFACT_PINS)
    documents = {}
    for name, pin in SEVEN_ARTIFACT_PINS.items():
        exact(scope[name], {"path", "sha256"})
        require(scope[name] == pin, "Only exact owner003/config003/original002 provenance is eligible")
        path = _path(repo, pin["path"])
        require(digest(path.read_bytes()) == pin["sha256"], "Interrupted-recovery provenance changed")
        if path.suffix == ".json":
            documents[name] = _json(path.read_bytes())
    owner, parent, config, before = (documents[name] for name in ("owner_request", "parent_owner_request",
                                                               "recovery_config", "ledger_before"))
    original, observation = documents["original_config"], documents["interruption"]
    ids = list(SEVEN_INTERRUPTED_PINS)
    require(owner.get("owner_messages_verbatim") == ["nop ejust regenerate"]
            and owner.get("regeneration_authorized") is True and owner.get("resume_authorized") is True
            and owner.get("unknown_holds_released") is False and owner.get("saved_native_only") is True
            and owner.get("budget_total_usd") == 1000 and owner.get("ledger") == str(ledger)
            and owner.get("original_ledger_sha256") == scope["ledger_before"]["sha256"]
            and owner.get("regenerated_interrupted_stage_ids") == config.get("regenerated_interrupted_stage_ids") == ids
            and type(owner.get("retained_unknown_stage_ids")) is list
            and len(owner["retained_unknown_stage_ids"]) == 7
            and set(owner["retained_unknown_stage_ids"]) == {item["id"] for item in review["stages"]},
            "Exact seven-full-hold owner regeneration scope required; no billing inference")
    require(parent.get("resume_authorized") is True and parent.get("no_unsubstantiated_hold_release") is True
            and parent.get("budget_total_usd") == 1000 and parent.get("ledger") == str(ledger)
            and parent.get("normal_future_rounds_remaining_after_recovery") == 8
            and owner.get("parent_scope") == {"path": str(repo / scope["parent_owner_request"]["path"]),
                                              "sha256": scope["parent_owner_request"]["sha256"]},
            "Parent owner002 normal continuation scope must remain separate from regeneration")
    require(config.get("ledger") == {"path": str(ledger), "sha256": scope["ledger_before"]["sha256"]}
            and config.get("owner_request") == {"path": str(repo / scope["owner_request"]["path"]),
                                                "sha256": scope["owner_request"]["sha256"]}
            and original.get("ledger", {}).get("path") == str(ledger)
            and observation.get("kind") == "coordinator_interruption_observation_not_provider_receipt"
            and observation.get("no_remote_completion_or_zero_charge_inferred") is True,
            "Pinned config/observation is not a provider settlement or zero-cost proof")
    require(before.get("budget_usd") == current.get("budget_usd") == 1000
            and set(current) == set(before)
            and all(measurement._json_hash({key: current.get(key)}) == measurement._json_hash({key: value})
                    for key, value in before.items() if key != "stages")
            and all(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(value)
                    for name, value in before["stages"].items()),
            "Every original003 whole-ledger entry and top-level value must remain exact")
    retained = config.get("retained_stages")
    expected_ids = {item["id"] for item in review["stages"]} | {
        review["reconciled_nonattempt"]["stage"]["id"]} | {item["stage"]["id"] for item in review["reconciled_failed_scans"]}
    require(type(retained) is list and len(retained) == len(expected_ids) == 10
            and {item.get("id") for item in retained} == expected_ids
            and all(item.get("sha256") == measurement._json_hash(before["stages"].get(item.get("id")))
                    for item in retained), "Exact original ten pending snapshots required")
    descriptors = review["retained_interrupted"]
    require(type(descriptors) is list and len(descriptors) == 3, "Only three exact interrupted full holds allowed")
    for descriptor, sid in zip(descriptors, ids, strict=True):
        exact(descriptor, {"stage", "original_request"})
        pin, stage = SEVEN_INTERRUPTED_PINS[sid], descriptor["stage"]
        exact(stage, {"id", "sha256", "snapshot"})
        expected = stage["snapshot"]
        exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at"})
        require(stage["id"] == sid and stage["sha256"] == pin["snapshot_sha256"]
                == measurement._json_hash(expected) == measurement._json_hash(before["stages"].get(sid))
                and expected["kind"] == "interaction" and expected["status"] == "started"
                and expected["actual_usd"] is None and expected["reserve_usd"] == pin["reserve_usd"]
                and money(expected["reserve_usd"], positive=True) and _time(expected["started_at"]) is not None
                and observation.get("new_uncertain_attempts", {}).get(sid) == expected,
                "Original interrupted stage remains exact started/null/full reservation")
        exact(descriptor["original_request"], {"path", "sha256"})
        require(descriptor["original_request"] == pin["request"], "Only exact interrupted request bytes are eligible")
        request_path, request = _artifact(repo, descriptor["original_request"])
        require(request_path.name == sid + "-request.json" and request_path.parent.name == "model-calls"
                and request.get("reserve_usd") == expected["reserve_usd"], "Original request/reservation differs")
    future = repo / binding["run_directory"]
    for name in ("recovery_config", "original_config"):
        directory = (repo / scope[name]["path"]).parent
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Recovery002/003 runs and configuration cannot be replayed or overwritten")
    return [item["stage"] for item in descriptors]


def _seven_authorization(repo, review, ledger, budget, binding):
    _, authorization = _artifact(repo, review["restart_authorization"])
    exact(authorization, SEVEN_SCOPE_FIELDS)
    exact(authorization["acknowledgements"], SEVEN_ACKNOWLEDGEMENTS)
    for name in ("owner_request", "parent_owner_request"):
        require(authorization[name] == SEVEN_ARTIFACT_PINS[name], "Exact new/parent owner provenance required")
    require(authorization["original_v5_scope"]["sha256"] == FAILED_SCAN_V5_SCOPE_SHA256,
            "Old v5 declaration remains provenance only")
    _, old = _artifact(repo, authorization["original_v5_scope"])
    exact(old, INTERACTION_SCOPE_FIELDS)
    require(old["version"] == INTERACTION_VERSION, "Original v5 provenance differs")
    owner = _artifact(repo, authorization["owner_request"])[1]
    require(authorization["version"] == SEVEN_VERSION
            and authorization["declaration_kind"] == "coordinator_seven_holds_continuation_interpretation"
            and type(authorization["declared_by"]) is str and bool(authorization["declared_by"].strip())
            and _time(authorization["declared_at"]) is not None and _time(owner.get("at")) is not None
            and _time(authorization["declared_at"]) >= _time(owner["at"])
            and all(_time(authorization["declared_at"]) >= _time(item["stage"]["snapshot"]["finished_at"])
                    for item in review["reconciled_failed_scans"])
            and authorization["continuation_after_feedback_integration"] is True
            and money(budget, positive=True) and authorization["budget_usd"] == budget == 1000
            and authorization["ledger"] == review["ledger"]
            and authorization["future_binding"] == binding == review["future_binding"]
            and authorization["bounded_disposition_sha256"] == measurement._json_hash({
                key: review[key] for key in SEVEN_FIELDS - {"restart_authorization"}})
            and all(value is True for value in authorization["acknowledgements"].values()),
            "New exact prospective coordinator disposition required; old approvals are not extended")

def _seven_original_interaction(repo, review, ledger, binding, snapshot):
    exact(binding, {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str, "Invalid continuation future directory")
    descriptor = review["retained_interaction"]
    exact(descriptor, {"stage", "original_request", "failure_accounting"})
    stage = descriptor["stage"]
    exact(stage, {"id", "sha256", "snapshot"})
    require(type(stage["id"]) is str and bool(stage["id"]), "Exact retained interaction identity required")
    expected = stage["snapshot"]
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"})
    require(expected["kind"] == "interaction" and expected["status"] == "unknown"
            and expected["actual_usd"] is None and money(expected["reserve_usd"], positive=True)
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["finished_at"]) >= _time(expected["started_at"])
            and type(expected["detail"]) is str, "Only a finished terminal unknown full interaction hold is eligible")
    current, _ = snapshot
    require(stage["sha256"] == measurement._json_hash(expected)
            == measurement._json_hash(current["stages"].get(stage["id"])), "Retained interaction stage changed")
    for name, sha256 in (("original_request", INTERACTION_REQUEST_SHA256),
                         ("failure_accounting", INTERACTION_ACCOUNTING_SHA256)):
        exact(descriptor[name], {"path", "sha256"})
        require(descriptor[name]["sha256"] == sha256, "Only the pinned original interaction evidence is eligible")
    request_path, request = _artifact(repo, descriptor["original_request"])
    accounting_path, accounting = _artifact(repo, descriptor["failure_accounting"])
    require(request_path.parent.name == "model-calls" and request_path.name == stage["id"] + "-request.json",
            "Exact original interaction request artifact required")
    require(accounting.get("review_kind") == "independent_original_http400_accounting_recovery"
            and accounting.get("approved") is False and accounting.get("outcome") == "unresolved_original_charge"
            and accounting.get("stage_id") == stage["id"]
            and accounting.get("original_request_sha256") == descriptor["original_request"]["sha256"]
            and accounting.get("ledger_path") == str(ledger.relative_to(repo))
            and accounting.get("ledger_before_sha256") == accounting.get("ledger_after_sha256")
            and "exact_actual_cost_usd" in accounting and accounting["exact_actual_cost_usd"] is None
            and accounting.get("one_time_normal_stages_finish_justified") is False
            and "supported_stage_update" in accounting and accounting["supported_stage_update"] is None
            and accounting.get("inference_retry_permitted") is False
            and accounting.get("old036_failed_zero_preserved") is True
            and money(request.get("reserve_usd"), positive=True)
            and request["reserve_usd"] == expected["reserve_usd"], "Original interaction accounting must remain unresolved")
    descriptors = review["stages"][:4]
    require(type(descriptors) is list and len(descriptors) == 4, "Exactly three native holds and one interaction required")
    for item in descriptors:
        exact(item, {"id", "sha256"})
        require(type(item["id"]) is str, "Invalid retained-hold stage identity")
    require(descriptors[-1] == {"id": stage["id"], "sha256": stage["sha256"]}
            and len({item["id"] for item in descriptors}) == 4, "Interaction must be the sole fourth retained hold")
    originals = accounting.get("all_four_unknown_stage_snapshots")
    exact(originals, {item["id"] for item in descriptors})
    require(all(measurement._json_hash(originals[item["id"]]) == item["sha256"] for item in descriptors),
            "All four original accounting snapshots must remain exact")
    future = repo / binding["run_directory"]
    require(all(not future.is_relative_to(directory) and not directory.is_relative_to(future)
                for directory in (request_path.parent.parent, accounting_path.parent)),
            "Original interaction evidence cannot overlap new work")
    return descriptors[-1]

def _freeze_seven(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                  snapshot, execution_fingerprint):
    """Independent exact-seven accounting branch; old v1-v6 authority unchanged."""
    fields, kind, acknowledgements = (SEVEN_FIELDS, "retained_seven_holds_continuation_disposition",
                                      SEVEN_ACKNOWLEDGEMENTS)
    exact(review, fields | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(type(review["stages"]) is list and len(review["stages"]) == 7, "Exactly seven ordered unknown descriptors required")
    exact(binding, {"run_directory", "request_sha256"})
    current, _ = snapshot
    interrupted = _seven_scope(repo, review, current, ledger, binding)
    retained_interaction = _seven_original_interaction(repo, review, ledger, binding, snapshot)
    known = review["reconciled_failed_scans"]
    require(type(known) is list and len(known) == 2
            and [item["stage"]["id"] for item in known] == list(FAILED_SCAN_PINS), "Both exact knownFAILED scans required")
    reconciled_failed_scans = [_reconciled_failed_scan(repo, item, current, ledger, binding) for item in known]
    _seven_authorization(repo, review, ledger, budget, binding)
    require(review["review_kind"] == kind and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None,
            "Exact independent three-hold approval required" if retained_interaction is None
            else "Exact bounded continuation declaration required")
    exact(review["acknowledgements"], acknowledgements)
    require(all(value is True for value in review["acknowledgements"].values()),
            "All three full holds require acknowledgement" if retained_interaction is None
            else "All seven full holds require acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Three-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == THREE_SCOPE_SHA256, "Exact three-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == THREE_VERSION, "Exact three-original scope approval required")
    references = _three_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only three scope-pinned proof chains allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and review["ledger"]["path"] == str(ledger.relative_to(repo))
            and money(budget, positive=True) and budget == 1000
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Three-hold ledger approval is stale")
    before, budget_proof = _three_budget(repo, scope_path, scope, current, ledger)
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Three-hold amendment differs")
    future = repo / binding["run_directory"]
    require(not Path(binding["run_directory"]).is_absolute() and future.resolve() == future
            and future != repo / ".context/realism" and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    # Legacy helpers receive the exact historical amendment prefix; their strict
    # failed-stage, seals and termination checks remain unchanged for v1/v2.
    old_current = {**current, "budget_amendments": before["budget_amendments"]}
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        if original["label"] == "016":
            descriptor, directory = _three_original016(repo, original, reference["artifacts"], current, ledger,
                                                        binding, before["budget_amendments"])
        else:
            descriptor, directory = _pair_original(repo, original, reference["artifacts"], old_current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original three runs cannot be replayed or modified")
        descriptors.append(descriptor)
    expected_descriptors = [*descriptors, retained_interaction, *({"id": item["id"], "sha256": item["sha256"]} for item in interrupted)]
    require(len({item["id"] for item in descriptors}) == 3
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(expected_descriptors),
            "Three exact ordered stages required" if retained_interaction is None
            else "Seven exact ordered unknown stages required")
    originals = {item["id"] for item in descriptors}
    reconciled = _reconciled_nonattempt(repo, review["reconciled_nonattempt"], current, ledger, binding)
    require(reconciled not in originals, "Reconciled case cannot replace an original full hold")
    originals.add(reconciled)
    if retained_interaction is not None:
        require(retained_interaction["id"] not in originals, "Retained interaction cannot replace a native case")
        originals.add(retained_interaction["id"])
    if reconciled_failed_scans is not None:
        for known_scan in reconciled_failed_scans:
            require(known_scan["id"] not in originals, "Known scan cannot replace an original hold/nonattempt")
            originals.add(known_scan["id"])
    for item in interrupted:
        require(item["id"] not in originals, "Interrupted case cannot replace any historical hold")
        originals.add(item["id"])
    for name, stage in current["stages"].items():
        if name not in originals:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None,
                    "Every fourth unknown or unfinished stage blocks" if retained_interaction is None
                    else "Every eighth unknown or foreign unfinished stage blocks")
    marker = future / "execution-started.json"
    require(not marker.is_symlink(), "Noncanonical future start marker")
    if marker.exists():
        require(marker.resolve() == marker and execution_fingerprint is not None
                and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future three-hold execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after all three full holds" if retained_interaction is None
            else "New reserves do not fit after all seven full holds")
    return {**{key: review[key] for key in fields},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}


def _eight_failed_interaction(repo, descriptor, current, ledger, binding):
    """One finite-cost FAILED004 interaction; never an eighth unknown allowance."""
    exact(descriptor, {"stage", "artifacts", "accounting"})
    exact(descriptor["stage"], {"id", "sha256", "snapshot"})
    exact(descriptor["artifacts"], EIGHT_ARTIFACT_PINS)
    exact(descriptor["accounting"], EIGHT_ACCOUNTING_PINS)
    docs = {}
    for name, pin in {**EIGHT_ARTIFACT_PINS, **EIGHT_ACCOUNTING_PINS}.items():
        require(type(pin) is dict and set(pin) == {"path", "sha256"}
                and type(pin["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]),
                "Exact004 accounting/receipt/wholepostimage/owner pins are unsupplied; do not use")
        ref = descriptor["artifacts" if name in EIGHT_ARTIFACT_PINS else "accounting"][name]
        require(ref == pin, "Only exact pinned original004 evidence and settlement is eligible")
        path = repo / pin["path"] if name == "finish_source" else _path(repo, pin["path"])
        require(path.resolve() == path and path.is_file() and path.is_relative_to(repo), "Canonical exact source path required")
        raw = path.read_bytes()
        require(digest(raw) == pin["sha256"], "Original004/settlement source bytes changed")
        if name not in {"generation_chunk", "streaming_source", "adapter_source", "coordinator_source",
                        "finish_script", "finish_source", "provider_reader_source", "accounting_stderr"}:
            docs[name] = _json(raw)
    sid, generation = EIGHT_STAGE_ID, EIGHT_GENERATION_ID
    stage, expected = descriptor["stage"], descriptor["stage"]["snapshot"]
    require(type(EIGHT_STAGE_SHA256) is str and re.fullmatch(r"[0-9a-f]{64}", EIGHT_STAGE_SHA256),
            "Exact normal-finish target snapshot pin is unsupplied")
    require(stage["id"] == sid and stage["sha256"] == EIGHT_STAGE_SHA256
            == measurement._json_hash(expected) == measurement._json_hash(current["stages"].get(sid))
            and expected.get("kind") == "interaction" and expected.get("status") == "failed"
            and money(expected.get("actual_usd"), positive=True) and expected["actual_usd"] == EIGHT_ACTUAL_USD
            and expected.get("reserve_usd") == EIGHT_RESERVE_USD
            and _time(expected.get("finished_at")) is not None
            and _time(expected.get("started_at")) is not None
            and _time(expected["finished_at"]) >= _time(expected["started_at"]),
            "Only exact finite FAILED004 status/cost/full snapshot is eligible")
    request, manifest, headers, gen = (docs[name] for name in ("request", "manifest", "headers", "generation"))
    request_sha = EIGHT_ARTIFACT_PINS["request"]["sha256"]
    require(request.get("reserve_usd") == EIGHT_RESERVE_USD
            and request.get("transport_profile") == "openrouter-chat-sse-receipts-v1"
            and request.get("payload", {}).get("stream") is True
            and request.get("payload", {}).get("model") == EIGHT_MODEL
            and request.get("bound", {}).get("metadata", {}).get("id") == EIGHT_MODEL
            and request["bound"]["metadata"].get("canonical_slug") == EIGHT_CANONICAL_MODEL
            and docs["preflight"].get(EIGHT_MODEL) == request["bound"]["metadata"]
            and docs["call_intent"].get("stage_id") == sid
            and docs["call_intent"].get("payload_sha256") == measurement._json_hash(request["payload"]),
            "Original004 request/canonical model/full reserve/call intent differs")
    for item in (manifest, headers, gen):
        require(item.get("stage_id") == sid and item.get("request_sha256") == request_sha
                and item.get("model") == EIGHT_MODEL
                and item.get("transport_profile") == "openrouter-chat-sse-receipts-v1",
                "Generation receipt must bind this original request and stream")
    require(headers.get("status_code") == 200 and gen.get("generation_id") == generation
            and gen.get("source") == "complete SSE JSON event id"
            and manifest.get("streaming_source_sha256") == EIGHT_ARTIFACT_PINS["streaming_source"]["sha256"]
            and manifest.get("base_adapter_source_sha256") == EIGHT_ARTIFACT_PINS["adapter_source"]["sha256"],
            "Exact original004 streaming source/generation binding differs")
    chunk = gen.get("through_chunk", {})
    chunk_path = _path(repo, EIGHT_ARTIFACT_PINS["generation_chunk"]["path"])
    chunk_raw = chunk_path.read_bytes()
    require(chunk.get("file") == chunk_path.name and chunk.get("sha256") == digest(chunk_raw)
            and chunk.get("bytes") == len(chunk_raw) and type(chunk.get("sequence")) is int,
            "Original generation ID raw event source differs")
    events = [_json(line[5:].strip()) for line in chunk_raw.splitlines()
              if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"]
    require(any(event.get("id") == generation for event in events), "Generation ID is not present in pinned raw event")
    intent, metadata_receipt = docs["provider_read_intent"], docs["metadata_receipt"]
    content_receipt, metadata = docs["content_receipt"], docs["metadata_body"].get("data", {})
    require(intent.get("method") == "GET" and intent.get("redirects") is False and intent.get("retries") == 0
            and intent.get("inference_calls") == intent.get("ledger_writes") == 0
            and intent.get("stage_id") == sid and intent.get("generation_id") == generation
            and intent.get("request_sha256") == request_sha
            and intent.get("generation_receipt_sha256") == EIGHT_ARTIFACT_PINS["generation"]["sha256"],
            "Original saved GET-only recovery intent differs")
    for item, name, code, endpoint in ((metadata_receipt, "metadata_body", 200, "/generation"),
                                     (content_receipt, "content_body", 404, "/generation/content")):
        body_raw = _path(repo, EIGHT_ARTIFACT_PINS[name]["path"]).read_bytes()
        require(item.get("generation_id") == generation and item.get("status_code") == code
                and item.get("endpoint") == endpoint and item.get("body_sha256") == digest(body_raw)
                and item.get("bytes") == len(body_raw), "Saved provider GET receipt/body/ID join differs")
    provider = metadata.get("provider_responses")
    require(metadata.get("id") == generation and metadata.get("model") == EIGHT_CANONICAL_MODEL
            and metadata.get("streamed") is True and metadata.get("is_byok") is False
            and money(metadata.get("total_cost"), positive=True) and metadata["total_cost"] == EIGHT_ACTUAL_USD
            and metadata.get("usage") == EIGHT_ACTUAL_USD
            and type(provider) is list and len(provider) == 1 and provider[0].get("status") == 499
            and provider[0].get("is_byok") is False
            and provider[0].get("model_permaslug") == EIGHT_CANONICAL_MODEL
            and provider[0].get("id") == metadata.get("upstream_id")
            and metadata.get("finish_reason") is None and metadata.get("native_finish_reason") is None,
            "Exact original finite charge/provider499/canonical Qwen metadata required; no completion claim")
    ended, task, signal, task_signal = (docs[name] for name in ("process_ended", "task_ended", "signal", "task_signal"))
    require(ended.get("child_exit_observed") is True and ended.get("child_returncode") == 143
            and ended.get("remote_state") == "not_established_by_local_exit" and ended.get("automatic_replay") is False
            and task.get("outcome") == "catchable_signal" and task.get("exit_code") == 143
            and task.get("scope") == "python_finalizer" and task.get("signals") == [15]
            and task.get("os_exit_observed") is False and ended.get("child") == task.get("process")
            and signal.get("number") == task_signal.get("signal") == 15 and signal.get("name") == "SIGTERM",
            "Original local SIGTERM records do not prove remote success or zero cost")
    config = docs["original_config"]
    require(config.get("runner_commit") == EIGHT_ORIGINAL_RUNNER
            and config.get("ledger", {}).get("path") == str(ledger), "Original004 source/ledger config differs")
    proof, receipt = docs["proof"], docs["receipt"]
    before, after = docs["whole_before"], docs["whole_after"]
    old = before.get("stages", {}).get(sid, {})
    require(before.get("budget_usd") == after.get("budget_usd") == 1000
            and old.get("kind") == "interaction" and old.get("status") == "unknown"
            and old.get("actual_usd") is None and old.get("reserve_usd") == EIGHT_RESERVE_USD
            and _time(old.get("finished_at")) is not None
            and _time(expected["finished_at"]) >= _time(old["finished_at"])
            and expected.get("detail") == EIGHT_DETAIL
            and all(expected.get(key) == old.get(key) for key in ("kind", "reserve_usd", "started_at"))
            and after.get("stages", {}).get(sid) == expected,
            "Exact original unknown/null to finite FAILED normal accounting postimage required")
    restored = deepcopy(after)
    restored["stages"][sid] = deepcopy(old)
    require(measurement._json_hash(restored) == measurement._json_hash(before),
            "Whole normal finish changed unrelated ledger entries/top-level fields")
    for key, value in after.items():
        if key != "stages":
            require(measurement._json_hash({key: current.get(key)}) == measurement._json_hash({key: value}),
                    "Post-accounting top-level ledger changed")
    require(set(current) == set(after)
            and all(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(value)
                    for name, value in after["stages"].items()),
            "Every complete post-accounting stage must remain exact in future whole ledger")
    expected_post = deepcopy(before)
    expected_post["stages"][sid].update(status="failed", actual_usd=EIGHT_ACTUAL_USD,
        finished_at=expected["finished_at"], detail=EIGHT_DETAIL)
    require(measurement._json_hash(after) == measurement._json_hash(expected_post),
            "Only status/actual/finish time/detail of exact target may change")
    require(sum(name.startswith(EIGHT_STAGE_PREFIX) and row.get("status") == "complete"
                for name, row in before["stages"].items()) == 61
            and sum(row.get("actual_usd") is None for name, row in before["stages"].items() if name != sid) == 7,
            "Exact61 COMPLETE original004 calls and seven other full holds must remain")
    require(proof.get("version") == 1 and type(proof.get("version")) is int
            and proof.get("stage_id") == sid and proof.get("generation_id") == generation
            and proof.get("budget_usd") == 1000 and proof.get("reserve_usd") == EIGHT_RESERVE_USD
            and proof.get("actual_usd") == EIGHT_ACTUAL_USD
            and proof.get("ledger_before") == {"path": str(repo / EIGHT_ACCOUNTING_PINS["whole_before"]["path"]),
                                                "sha256": EIGHT_ACCOUNTING_PINS["whole_before"]["sha256"]}
            and proof.get("stage_before_sha256") == digest(json.dumps(old, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())
            and proof.get("stages_source") == {"path": str(repo / EIGHT_ACCOUNTING_PINS["finish_source"]["path"]),
                                                "sha256": EIGHT_ACCOUNTING_PINS["finish_source"]["sha256"]},
            "Exact frozen A accounting case/source/before-stage proof required")
    proof_names = {"request": "request", "manifest": "manifest", "generation_receipt": "generation", "headers": "headers",
                   "preflight": "preflight", "provider_read_intent": "provider_read_intent", "provider_reader_source": "provider_reader_source",
                   "metadata_receipt": "metadata_receipt", "metadata_body": "metadata_body", "content_receipt": "content_receipt", "content_body": "content_body"}
    exact(proof.get("artifacts"), proof_names)
    require(all(proof["artifacts"][name] == {"path": str(repo / EIGHT_ARTIFACT_PINS[key]["path"]),
                                            "sha256": EIGHT_ARTIFACT_PINS[key]["sha256"]}
                for name, key in proof_names.items()), "Accounting case must bind every original raw artifact/source")
    journal = _path(repo, EIGHT_ARTIFACT_PINS["manifest"]["path"]).parent
    chunks = proof.get("journal_chunks")
    require(type(chunks) is list and bool(chunks)
            and {path.name for path in journal.glob("raw-*.bin")} == {f"raw-{i:06d}.bin" for i in range(len(chunks))},
            "Entire original interrupted raw journal inventory differs")
    for i, item in enumerate(chunks):
        exact(item, {"sequence", "file", "bytes", "sha256"})
        raw = (journal / f"raw-{i:06d}.bin").read_bytes()
        require(type(item["sequence"]) is int and item["sequence"] == i and item["file"] == f"raw-{i:06d}.bin"
                and item["bytes"] == len(raw) and len(raw) > 0 and item["sha256"] == digest(raw), "Original interrupted journal bytes changed")
    require(gen["through_chunk"] in chunks
            and not any((journal / name).exists() for name in ("eof.json", "complete.json", "invalid.json"))
            and not (journal.parent / (sid + "-response.json")).exists(),
            "Original interrupted stream remains incomplete; never invent a terminal answer")
    require(receipt.get("executed") is True and type(receipt.get("ledger_writes")) is int and receipt["ledger_writes"] == 1
            and receipt.get("stage_id") == sid and receipt.get("generation_id") == generation
            and receipt.get("status") == "failed" and receipt.get("actual_usd") == EIGHT_ACTUAL_USD
            and receipt.get("budget_usd") == 1000 and receipt.get("reserve_usd") == EIGHT_RESERVE_USD
            and receipt.get("answer_status") == "unknown"
            and receipt.get("provider_metadata_sha256") == EIGHT_ARTIFACT_PINS["metadata_body"]["sha256"]
            and receipt.get("ledger_after_sha256") == EIGHT_ACCOUNTING_PINS["whole_after"]["sha256"],
            "Exact ONE normal FAILED accounting receipt and whole postimage required; answer stays unknown")
    spend = stages.summary(after)
    exact(receipt.get("summary"), {"budget_usd", "actual_usd", "held_usd", "committed_usd", "remaining_usd"})
    require(all(receipt["summary"][key] == spend[key] for key in receipt["summary"]), "Normal finish receipt whole-ledger summary differs")
    readback, process, accounting_intent = (docs[name] for name in ("readback", "accounting_process", "accounting_intent"))
    def absolute_ref(name):
        return {"path": str(repo / EIGHT_ACCOUNTING_PINS[name]["path"]), "sha256": EIGHT_ACCOUNTING_PINS[name]["sha256"]}
    require(readback.get("accounting_receipt") == absolute_ref("receipt")
            and readback.get("accounting_process") == absolute_ref("accounting_process")
            and readback.get("ledger_after_copy") == absolute_ref("whole_after")
            and readback.get("before_ledger_sha256") == EIGHT_ACCOUNTING_PINS["whole_before"]["sha256"]
            and readback.get("after_ledger_sha256") == EIGHT_ACCOUNTING_PINS["whole_after"]["sha256"]
            and readback.get("target_stage_id") == sid and readback.get("target_snapshot_before") == old
            and readback.get("target_snapshot_after") == expected
            and readback.get("target_stage_canonical_sha256") == EIGHT_STAGE_SHA256
            and readback.get("entire_other_204_stages_and_top_level_preserved") is True
            and readback.get("seven_old_full_unknown_holds_unchanged") is True
            and readback.get("stable_lock_unchanged") is True
            and type(readback.get("normal_finish_calls")) is int and readback["normal_finish_calls"] == 1
            and readback.get("answer_status") == "unknown" and readback.get("new_inference_calls") == 0
            and readback.get("summary") == receipt["summary"], "Exact saved normal-finish readback closure differs")
    argv = [str(repo / ".venv/bin/python"), "-u", str(repo / EIGHT_ACCOUNTING_PINS["finish_script"]["path"]),
            "--case", str(repo / EIGHT_ACCOUNTING_PINS["proof"]["path"]),
            "--case-sha256", EIGHT_ACCOUNTING_PINS["proof"]["sha256"],
            "--source-sha256", EIGHT_ACCOUNTING_PINS["finish_script"]["sha256"], "--ledger", str(ledger), "--execute"]
    require(process.get("returncode") == 0 and type(process.get("returncode")) is int
            and process.get("argv") == accounting_intent.get("argv") == argv
            and process.get("cwd") == accounting_intent.get("cwd") == str(repo)
            and process.get("stdout") == absolute_ref("receipt") and process.get("stderr") == absolute_ref("accounting_stderr")
            and process.get("automatic_retry") is False
            and accounting_intent.get("source") == absolute_ref("finish_script")
            and accounting_intent.get("case") == absolute_ref("proof")
            and accounting_intent.get("ledger_before", {}).get("path") == str(ledger)
            and accounting_intent["ledger_before"].get("sha256") == EIGHT_ACCOUNTING_PINS["whole_before"]["sha256"]
            and accounting_intent.get("credentials_absent") is True
            and accounting_intent.get("normal_finish_calls_authorized") == 1
            and accounting_intent.get("status") == "failed" and accounting_intent.get("actual_usd") == EIGHT_ACTUAL_USD
            and accounting_intent.get("answer_status") == "unknown" and accounting_intent.get("uncertain_retry_allowed") is False,
            "Saved ONE successful accounting process/source/case/receipt join differs; never execute it again")
    created = _time(metadata.get("created_at"))
    require(created is not None and _time(old["started_at"]) <= created <= _time(old["finished_at"])
            and money(metadata.get("generation_time"))
            and created.timestamp() + metadata["generation_time"] / 1000 <= _time(old["finished_at"]).timestamp() + 1
            and _time(metadata_receipt.get("at")) is not None and _time(metadata_receipt["at"]) >= _time(old["finished_at"]),
            "Original provider/stage/read timing differs; local signal alone does not prove billing")
    original = (_path(repo, EIGHT_ARTIFACT_PINS["original_config"]["path"])).parent
    future = repo / binding["run_directory"]
    require(not future.is_relative_to(original) and not original.is_relative_to(future),
            "Original004 cannot be replayed or overlap future normal continuation")
    return stage


def _eight_authorization(repo, review, ledger, budget, binding):
    _, authorization = _artifact(repo, review["restart_authorization"])
    exact(authorization, EIGHT_SCOPE_FIELDS)
    exact(authorization["acknowledgements"], EIGHT_ACKNOWLEDGEMENTS)
    for name in ("owner_request", "parent_owner_request"):
        require(authorization[name] == SEVEN_ARTIFACT_PINS[name], "Original003/parent002 provenance remains exact")
    resume = EIGHT_ACCOUNTING_PINS["owner_resume"]
    require(review["continuation_owner_request"] == authorization["continuation_owner_request"] == resume,
            "Exact supplied hourly43 resume/sameUSD1000/no resend/stop-real-unknown scope required")
    _, resume_value = _artifact(repo, resume)
    require(resume_value.get("resume_authorized") is True and resume_value.get("budget_total_usd") == 1000
            and resume_value.get("ledger") == str(ledger) and resume_value.get("unknown_limit_increase_authorized") is False
            and resume_value.get("saved_reply_replays_authorized") is False
            and resume_value.get("new_numeric_rerating_authorized") is False
            and resume_value.get("replacement_target_trials_authorized") is False
            and resume_value.get("original_goal_preserved") is True
            and _time(resume_value.get("at")) is not None, "Hourly43 exact same-cap/no-resend/no-new-unknown scope required")
    require(authorization["original_v5_scope"]["sha256"] == FAILED_SCAN_V5_SCOPE_SHA256,
            "Old v5 declaration remains provenance only")
    _, old = _artifact(repo, authorization["original_v5_scope"])
    exact(old, INTERACTION_SCOPE_FIELDS)
    require(old["version"] == INTERACTION_VERSION, "Original v5 provenance differs")
    require(authorization["version"] == EIGHT_VERSION
            and authorization["declaration_kind"] == "coordinator_seven_holds_known_failed004_interpretation"
            and type(authorization["declared_by"]) is str and bool(authorization["declared_by"].strip())
            and _time(authorization["declared_at"]) is not None
            and _time(authorization["declared_at"]) >= _time(resume_value["at"])
            and _time(authorization["declared_at"]) >= _time(review["reconciled_failed_interaction"]["stage"]["snapshot"]["finished_at"])
            and authorization["continuation_after_feedback_integration"] is True
            and money(budget, positive=True) and authorization["budget_usd"] == budget == 1000
            and authorization["ledger"] == review["ledger"]
            and authorization["future_binding"] == binding == review["future_binding"]
            and authorization["bounded_disposition_sha256"] == measurement._json_hash({
                key: review[key] for key in EIGHT_FIELDS - {"restart_authorization"}})
            and all(value is True for value in authorization["acknowledgements"].values()),
            "Fresh exact-bound coordinator accounting declaration required, never old authority or an external review gate")

def _freeze_eight_boundary(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                  snapshot, execution_fingerprint):
    """Independent knownFAILED004 branch; old v1-v7 source/authority unchanged."""
    fields, kind, acknowledgements = (EIGHT_FIELDS, "retained_seven_holds_known_failed004_disposition",
                                      EIGHT_ACKNOWLEDGEMENTS)
    exact(review, fields | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(type(review["stages"]) is list and len(review["stages"]) == 7, "Exactly seven ordered unknown descriptors required")
    exact(binding, {"run_directory", "request_sha256"})
    current, _ = snapshot
    interrupted = _seven_scope(repo, review, current, ledger, binding)
    retained_interaction = _seven_original_interaction(repo, review, ledger, binding, snapshot)
    known = review["reconciled_failed_scans"]
    require(type(known) is list and len(known) == 2
            and [item["stage"]["id"] for item in known] == list(FAILED_SCAN_PINS), "Both exact knownFAILED scans required")
    reconciled_failed_scans = [_reconciled_failed_scan(repo, item, current, ledger, binding) for item in known]
    failed_interaction = _eight_failed_interaction(repo, review["reconciled_failed_interaction"], current, ledger, binding)
    _eight_authorization(repo, review, ledger, budget, binding)
    require(review["review_kind"] == kind and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None,
            "Exact independent three-hold approval required" if retained_interaction is None
            else "Exact bounded continuation declaration required")
    exact(review["acknowledgements"], acknowledgements)
    require(all(value is True for value in review["acknowledgements"].values()),
            "All three full holds require acknowledgement" if retained_interaction is None
            else "All seven full holds require acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Three-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == THREE_SCOPE_SHA256, "Exact three-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == THREE_VERSION, "Exact three-original scope approval required")
    references = _three_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only three scope-pinned proof chains allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and review["ledger"]["path"] == str(ledger.relative_to(repo))
            and money(budget, positive=True) and budget == 1000
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Three-hold ledger approval is stale")
    before, budget_proof = _three_budget(repo, scope_path, scope, current, ledger)
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Three-hold amendment differs")
    future = repo / binding["run_directory"]
    require(not Path(binding["run_directory"]).is_absolute() and future.resolve() == future
            and future != repo / ".context/realism" and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    # Legacy helpers receive the exact historical amendment prefix; their strict
    # failed-stage, seals and termination checks remain unchanged for v1/v2.
    old_current = {**current, "budget_amendments": before["budget_amendments"]}
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        if original["label"] == "016":
            descriptor, directory = _three_original016(repo, original, reference["artifacts"], current, ledger,
                                                        binding, before["budget_amendments"])
        else:
            descriptor, directory = _pair_original(repo, original, reference["artifacts"], old_current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original three runs cannot be replayed or modified")
        descriptors.append(descriptor)
    expected_descriptors = [*descriptors, retained_interaction, *({"id": item["id"], "sha256": item["sha256"]} for item in interrupted)]
    require(len({item["id"] for item in descriptors}) == 3
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(expected_descriptors),
            "Three exact ordered stages required" if retained_interaction is None
            else "Seven exact ordered unknown stages required")
    originals = {item["id"] for item in descriptors}
    reconciled = _reconciled_nonattempt(repo, review["reconciled_nonattempt"], current, ledger, binding)
    require(reconciled not in originals, "Reconciled case cannot replace an original full hold")
    originals.add(reconciled)
    if retained_interaction is not None:
        require(retained_interaction["id"] not in originals, "Retained interaction cannot replace a native case")
        originals.add(retained_interaction["id"])
    if reconciled_failed_scans is not None:
        for known_scan in reconciled_failed_scans:
            require(known_scan["id"] not in originals, "Known scan cannot replace an original hold/nonattempt")
            originals.add(known_scan["id"])
    for item in interrupted:
        require(item["id"] not in originals, "Interrupted case cannot replace any historical hold")
        originals.add(item["id"])
    require(failed_interaction["id"] not in originals, "KnownFAILED004 cannot replace any historical descriptor")
    originals.add(failed_interaction["id"])
    for name, stage in current["stages"].items():
        if name not in originals:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None,
                    "Every fourth unknown or unfinished stage blocks" if retained_interaction is None
                    else "Every eighth unknown or foreign unfinished stage blocks")
    marker = future / "execution-started.json"
    require(not marker.is_symlink(), "Noncanonical future start marker")
    if marker.exists():
        require(marker.resolve() == marker and execution_fingerprint is not None
                and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future three-hold execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after all three full holds" if retained_interaction is None
            else "New reserves do not fit after all seven full holds")
    return {**{key: review[key] for key in fields},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}
