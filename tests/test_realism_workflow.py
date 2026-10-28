"""Execute the actual JS workflow with a fake agent runtime; never run its shell commands."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".claude/workflows/realism-cycle.js"
HARNESS = r'''
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(process.argv[1], 'utf8').replace(/^export const meta = \{[\s\S]*?^\}\n/m, '');
const calls = [], stages = {}, events = [];
let candidate = 'BASE';
const note = {id:'n1',factor:'company',kind:'code',tell:'bug',status:'accepted'};
const summary = () => Object.values(stages).reduce((n,s)=>n+(s.actual==null?s.reserve:s.actual),0);
const replyBudget = () => ({ok:true, committed:summary(), remaining:input.args.budget-summary()});
const config = input.case || {};
const agent = async (prompt, opts) => {
 const label = opts.label;
 calls.push({label,prompt});
 if(label==='setup') return {ok:true,head:'BASE'};
 if(label==='manifest') return {ok:true};
 if(label==='budget status') return replyBudget();
 if(label.endsWith(' reserve')) {
  const m = prompt.match(/reserve --stage '([^']+)' --kind (\w+) --reserve ([0-9.e+-]+)/);
  if(!m || stages[m[1]]) throw Error('duplicate/malformed reserve');
  stages[m[1]]={kind:m[2],reserve:Number(m[3]),actual:null};
  return replyBudget();
 }
 if(label.endsWith(' account')) {
  const m = prompt.match(/finish --stage '([^']+)' --status (\w+)(?: --actual ([0-9.e+-]+))?/);
  if(!m || !stages[m[1]]) throw Error('unreserved finish');
  stages[m[1]].actual=m[3]===undefined?null:Number(m[3]); stages[m[1]].status=m[2];
  return replyBudget();
 }
 if(label.endsWith(' provenance')) return {ok:!config.badSource,head:config.wrongSourceHead && (config.wrongSourceHead==='baseline'?label.startsWith('r0'):!label.startsWith('r0'))?'WRONG':(label.startsWith('r0')?'BASE':candidate)};
 if(label.endsWith(' trials')) {
  const n=input.args.trials || 5;
  return {ok:!config.failedBatch,failed:config.failedBatch?1:0,
   logs:Array.from({length:config.failedBatch?n-1:n},(_,i)=>`${label}-${i}.eval`),probes:[]};
 }
 if(label.endsWith(' trial usage')) return {cost_usd:.01};
 if(label.endsWith(' scan')) return {ok:!config.failedScan};
 if(label.endsWith(' score')) return {trials:config.partialScore?1:(input.args.trials||5),tells_per_trial:10,ci:[9,11],cost_usd:.01,per_trial:[10,10,10]};
 if(label.endsWith(' artifact')) return {ok:true};
 if(label.endsWith(' triage')) return {base:'BASE',clusters:config.noChange?[]:[{key:'company:org',factor:'company',org:'org',count:1,yes:0,findings:[],notes:[]}]};
 if(label.includes(' investigate ')) return {notes:[note]};
 if(label.includes(' verify ')) return {verdicts:[{verdict:'accept',note,reason:'synthetic'}]};
 if(label.endsWith(' merge')) return {file:'notes.json',output:'ok',ok:true};
 if(label.includes(' fix ')) return {note_id:'n1',changed:true,branch:'fix-n1',commit:'FIX',worktree:'fake',files:['refund_eval/x.py'],status:'done',summary:'fix'};
 if(label.includes(' review ')) return {note_id:'n1',commit:config.wrongReview?'OTHER':'FIX',classification:config.style?'realism':'correctness',verified:!config.unverified,reproduced_defect:'reproduced',before_evidence:'fails',after_evidence:'passes',checks_passed:true,scenario_preserved:true,reason:'checked'};
 if(label.endsWith(' integrate')) {
  const branches=JSON.parse(prompt.split('Branches and reviewed commits: ')[1]);
  candidate=branches.length?'CANDIDATE':'BASE';
  return {ok:true,candidate,merged:branches.map(b=>b.branch),conflicts:[],memory_committed:false};
 }
 if(label.includes('compare')) return {decision:config.better?'better':'unclear',delta:0,ci:[-1,1],relative:0};
 if(label.endsWith(' finish')) return {ok:true,head:config.selective?'RETAINED':(config.style||config.unverified?'REVERTED':candidate),reverted:config.style||config.unverified?['R']:[],diff_stat:'',files:[]};
 if(label.endsWith(' report')) return {path:'report.md',summary:'report'};
 if(label==='summary') return {path:'summary.md',summary:'done'};
 throw new Error('unhandled fake agent label '+label);
};
const parallel=async tasks=>Promise.all(tasks.map(f=>f()));
const pipeline=async(items,...steps)=>Promise.all(items.map(async item=>{let x=item; for(const step of steps)x=await step(x,item);return x;}));
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
(async()=>{
 try {
  const result=await new AsyncFunction('args','agent','parallel','pipeline','phase','log',source)(input.args,agent,parallel,pipeline,x=>events.push(x),()=>{});
  process.stdout.write(JSON.stringify({result,calls,stages,events}));
 }catch(error){process.stdout.write(JSON.stringify({error:error.message,calls,stages,events}));}
})();
'''


def run_workflow(case=None, **overrides):
    if not shutil.which("node"):
        pytest.skip("Node required to execute workflow")
    args = {"budget": 100, "trials": 3, "tokenBudget": 12345, "maxTurns": 77,
            "timeLimit": 888, "costLimit": .1, "scanCostPerTrial": .1,
            "interactionReserve": .1, "runId": "test", **overrides}
    process = subprocess.run(["node", "-e", HARNESS, str(WORKFLOW)],
                             input=json.dumps({"args": args, "case": case or {}}),
                             capture_output=True, text=True, check=True, timeout=15)
    return json.loads(process.stdout)


def labels(result):
    return [call["label"] for call in result["calls"]]


def test_zero_budget_and_historical_bound_do_not_launch_paid_stages():
    zero = run_workflow(budget=0)
    assert zero["result"]["stage"] == "prepared"
    assert not zero["stages"]
    bounded = run_workflow(budget=100, remainingBudget=.01)
    assert bounded["result"]["stage"] == "budget"
    assert not bounded["stages"]


def test_all_arms_use_identical_declared_short_protocol_and_unclear_stops():
    run = run_workflow()
    assert "error" not in run
    trials = [c for c in run["calls"] if c["label"].endswith(" trials")]
    assert len(trials) == 2
    for call in trials:
        for flag in ("--trials 3", "--token-budget 12345", "--max-turns 77", "--time-limit 888"):
            assert flag in call["prompt"]
    assert not any("ab-" in label for label in labels(run))
    assert run["result"]["rounds"][-1]["decision"] == "unclear"
    assert run["result"]["rounds"][-1]["correctness_retained"] == ["n1"]


@pytest.mark.parametrize("case,error", [
    ({"failedBatch": True}, "successful trials"),
    ({"partialScore": True}, "invalid score/coverage"),
    ({"failedScan": True}, "tells coverage failed"),
    ({"badSource": True}, "source provenance"),
])
def test_partial_failed_or_wrong_source_baseline_is_not_scored_as_success(case, error):
    run = run_workflow(case)
    assert run["result"]["stage"] == "baseline"
    assert error in run["result"]["error"]
    assert "r1 candidate trials" not in labels(run)
    if case.get("failedBatch"):
        assert not any(s["kind"] == "tells" for s in run["stages"].values())


@pytest.mark.parametrize("case", [{"style": True}, {"unverified": True}])
def test_unverified_or_style_fix_is_reverted_when_realism_unclear(case):
    run = run_workflow(case)
    row = run["result"]["rounds"][-1]
    assert row["correctness_retained"] == []
    finish = next(c["prompt"] for c in run["calls"] if c["label"] == "r1 finish")
    assert "verified correctness repairs: []" in finish
    assert row["reverted"] == ["R"]


def test_selectively_retained_source_has_no_borrowed_score():
    run = run_workflow({"selective": True})
    assert run["result"]["champion"] == "RETAINED"
    assert run["result"]["measured"] is False
    assert run["result"]["tells_per_trial"] is None
    assert run["result"]["stop"] == "needs-fresh-baseline"


def test_wrong_review_commit_excluded_and_no_unchanged_future_charges():
    run = run_workflow({"wrongReview": True})
    integrate = next(c["prompt"] for c in run["calls"] if c["label"] == "r1 integrate")
    assert integrate.endswith("Branches and reviewed commits: []")
    assert "r1 candidate trials" not in labels(run)
    assert len(run["stages"]) == 2
    assert run["result"]["rounds"][-1]["correctness_retained"] == []


def test_no_findings_does_not_reserve_candidate_or_interaction_stages():
    run = run_workflow({"noChange": True})
    assert len(run["stages"]) == 2
    assert "r1 candidate trials" not in labels(run)
    assert run["result"]["rounds"][-1]["decision"] == "no change"


def test_wrong_source_commit_rejected_even_if_provenance_agent_says_ok():
    baseline = run_workflow({"wrongSourceHead": "baseline"})
    assert baseline["result"].get("stage") == "baseline"
    assert not baseline["stages"]
    candidate = run_workflow({"wrongSourceHead": "candidate"})
    assert "r1 candidate trials" not in labels(candidate)
    assert candidate["result"]["rounds"][-1]["decision"] == "failed"
