#!/bin/bash
# Grades the trial with refund_eval.grading.verifier (reward.json and details.json in /logs/verifier).
# Inputs, restored by Harbor from the task's artifacts:
#   /var/log/world/       the world sidecar's state.json and events.jsonl
#   /logs/agent/inspect/  the agent's single .eval (temporary; see task.toml)
set -euo pipefail
mkdir -p /logs/verifier
cd /app

# One artifacts directory for the verifier: the world's state and event log and the agent's .eval.
grading=$(mktemp -d)
cp /var/log/world/state.json /var/log/world/events.jsonl "$grading"/
if compgen -G "/logs/agent/inspect/*.eval" > /dev/null; then
  cp /logs/agent/inspect/*.eval "$grading"/
fi

exec python -m refund_eval.grading.verifier \
  --state "$grading/state.json" \
  --events "$grading/events.jsonl" \
  --artifacts "$grading" \
  --out /logs/verifier/reward.json
