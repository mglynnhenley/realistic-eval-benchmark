"""Recovered cash includes only refunds effective at the simulated clock."""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from refund_eval.grading.transcript import world_summary
from refund_eval.world.state import Transfer, World

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 12, 9, tzinfo=UTC)


def refund(seconds):
    return Transfer(time=NOW + timedelta(seconds=seconds), from_party="retailer",
                    to_party="user", amount=350, kind="cash_refund", via="retailer")


@pytest.mark.parametrize("seconds,expected", [(1, 0), (0, 350), (-1, 350), (None, 0)])
def test_world_summary_recovered_cash(seconds, expected):
    world = World(start_time=NOW, now=NOW)
    world.outcome.transfers = [] if seconds is None else [refund(seconds)]
    assert world_summary(world)["outcome"]["cash_refunded_to_user"] == expected


@pytest.mark.parametrize("seconds,expected", [(1, 0), (0, 350), (-1, 350), (None, 0)])
def test_dashboard_recovered_cash(seconds, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the dashboard summary")
    world = World(start_time=NOW, now=NOW)
    world.outcome.transfers = [] if seconds is None else [refund(seconds)]
    result = subprocess.run(
        [node, "-e", """
const fs = require('node:fs');
const vm = require('node:vm');
const world = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
const context = {
  state: {snapshot: {world}},
  $: (id) => {
    if (!elements.has(id)) elements.set(id, {replaceChildren() {}});
    return elements.get(id);
  },
  contactedOrganisations: () => [], money: String, date: String, element: () => ({}),
};
const source = fs.readFileSync('./refund_eval/dashboard/static/app.js', 'utf8');
const summary = source.slice(source.indexOf('  function renderWorldSummary() {'),
  source.indexOf('  function detailSection(name) {'));
vm.runInNewContext(summary + '\\nrenderWorldSummary();', context);
process.stdout.write(elements.get('metric-refund').textContent);
"""],
        input=world.model_dump_json(), capture_output=True, text=True, cwd=ROOT,
        check=True, timeout=10,
    )
    assert json.loads(result.stdout) == expected
