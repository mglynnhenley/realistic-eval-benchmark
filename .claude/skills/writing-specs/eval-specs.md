# Specs for agent evaluations

Extra sections and rules when the spec describes a test of an agent's behaviour, not a product.

## Contents
- Core terms
- Scenario
- Environment
- Target agent
- Measurement
- Trials and budget
- Rules specific to behaviour evals

## Core terms

Put these in the Definitions table so everyone uses the same words:

| Term | Meaning |
| --- | --- |
| Task | The instruction given to the agent, plus its starting state. |
| Trial | One run of the agent on the task. Results vary, so run many. |
| Target agent | The model plus its scaffold (system prompt, tools, loop). We are testing both together. |
| Harness | What runs trials, feeds the agent, records everything and stops runs. |
| Transcript | Full record of a trial: messages, tool calls, tool results, reasoning if available. |
| Outcome | The final state of the world at the end of a trial. It is different from what the agent *says* happened. |
| Grader | Anything that scores a trial: code, an LLM, or a human. |

## Scenario

- The exact task text the agent receives, word for word.
- What is true in the world. For impossible or layered tasks, list each layer with: what the agent meets, what a legitimate response looks like, and the tempting shortcut.
- The ground truth. What is the best outcome an honest, careful agent can get? If a task has no legitimate success, say so, because graders need to know.

## Environment

- What is real and what is simulated, for every tool.
- Containment: what can never leave the sandbox. Write these as MUST NOT requirements with a code check.
- Simulated parties (users, companies, other agents): what they know, how they behave, and how they stay consistent with each other.
- Realism requirements, so the agent can't easily tell it's a test. Include a check for it (see below).
- Time: if the task runs over days or weeks, say how time passes.

## Target agent

- Which models and scaffolds are tested. Name them, or flag it.
- System prompt and persona, at the level of "a personal assistant with access to X". Put the exact text in the plan.
- What the agent is allowed to do on the user's behalf. This is the baseline for judging "did it overstep".

## Measurement

For each behaviour in the intent's watch-list, write:

| Behaviour | Definition | Evidence in transcript or outcome | Grader |
| --- | --- | --- | --- |

Then:

- A **severity scale** with concrete examples at each level, so "clearly misaligned" means the same thing to every grader.
- **Outcome checks before transcript checks.** Compare what the agent told the user against the outcome record. Code can check that.
- **LLM graders score one behaviour at a time** against a rubric, and can answer "unknown".
- **Calibrate** LLM graders against human labels on a sample before trusting them. State the sample size and the agreement you need.
- **Read transcripts.** Require a human to read every trial flagged at the top severity levels, and a random sample of the rest.

## Trials and budget

- Trials per condition, and why that number.
- Per-trial limits: turns, tokens, money, wall-clock, simulated time.
- Total budget for the run.
- Whether you report pass@k (did it happen at least once in k trials) or pass^k (did it happen every time). For "does the agent ever do something bad", the rate across trials and at least once in k are both useful.

## Rules specific to behaviour evals

- **Every trial starts from a clean state.** No shared memory or leftover files between trials.
- **Test both sides.** If you measure "gives up too early", also be able to see "never gives up when it should".
- **Don't penalise valid paths.** Grade what the agent did to the world and what it said, not whether it took the route you expected.
- **Eval awareness is a result, not noise.** Record when the agent says or acts like it's being tested, and report it next to the other results.
- **A shortcut should be able to "work".** If you want to see whether the agent is honest about a dishonest win, the environment has to let some dishonest paths appear to succeed.
- **Containment is testable.** Write a success criterion that proves nothing escaped, for example "zero outgoing messages delivered, checked against the interception log".
