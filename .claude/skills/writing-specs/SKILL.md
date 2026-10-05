---
name: writing-specs
description: Turns an intent.md (the why and the what) into a spec.md (what must be true, and how we will know) for agent systems and agent evaluations. Use when the user asks to write, draft, review or tighten a spec, or to go from intent to spec. Not for build plans (plan.md) or tickets.
---

# Writing specs

Three documents, three jobs. Keep them apart.

| Doc | Answers | Contains | Never contains |
| --- | --- | --- | --- |
| `intent.md` | Why are we doing this? What do we want to learn or get? | Question, motivation, what we watch for, non-goals | How it works |
| `spec.md` | What must be true? How will we know? | Behaviour requirements, boundaries, success criteria, open questions | Libraries, file layout, code, task order |
| `plan.md` | How do we build it? | Architecture, tools, steps, order | New requirements |

A spec is the contract between intent and build. Anyone building from it, human or agent, should not need to ask what "done" means.

## Workflow

Copy this checklist and tick it off as you go:

```
Spec progress:
- [ ] 1. Read intent.md and anything it links to
- [ ] 2. List every intent item that needs a requirement
- [ ] 3. Draft spec.md from template.md
- [ ] 4. If it's an agent eval, add the sections in eval-specs.md
- [ ] 5. Mark unknowns with [NEEDS CLARIFICATION] and a default
- [ ] 6. Run the quality gate below and fix what fails
- [ ] 7. Give the user the open questions, most important first
```

**Step 1: Read the intent.** Read `intent.md` in full. If there is an existing `spec.md`, update it rather than starting over.

**Step 2: Trace.** Every goal, watch-list item and non-goal in the intent must map to at least one requirement, success criterion or out-of-scope line. Keep the mapping in the spec's traceability table. Anything in the spec that traces to nothing in the intent is either scope creep (cut it) or a gap in the intent (tell the user).

**Step 3: Draft.** Use [template.md](template.md). Drop sections that truly don't apply; don't pad.

**Step 4: Agent evals.** If the thing being specified is a test of an agent's behaviour, read [eval-specs.md](eval-specs.md) and add its sections.

**Step 5: Unknowns.** Don't guess silently and don't stall. Write the requirement with a sensible default and flag it:

```
FR-007: The harness MUST stop a trial after [NEEDS CLARIFICATION: budget per trial. Default: $50 or 300 agent turns, whichever comes first].
```

Put every flag in the Open questions section too, ranked by how much it changes the build. Aim for no more than about 8. If there are more, the intent needs work first; say so.

**Step 6: Quality gate.** Run the checklist below. Fix, then re-run. Only finish when it passes.

**Step 7: Hand back.** Tell the user what the spec decides, and list the open questions with the default you picked for each. Don't paste the whole spec into chat.

## Rules for requirements

- **Describe behaviour, not build.** "Outgoing email MUST never reach a real recipient" is spec. "Wrap the Gmail send tool in a proxy" is plan.
- **One requirement, one testable claim.** Number them (`FR-001`) so graders, plans and reviews can cite them.
- **Use MUST / MUST NOT / SHOULD** and mean them. MUST fails the spec if broken; SHOULD is a strong default.
- **Make it checkable.** For each requirement, you should be able to say who or what checks it: code, an LLM grader, or a human.
- **Measurable success criteria** (`SC-001`). Give numbers, even rough ones. "Enough runs" is not a criterion; "at least 20 trials" is.
- **Boundaries in three tiers.** Always / Ask first / Never. Put hard safety limits in Never.
- **Say what's out of scope.** Explicit non-goals stop an agent from building them anyway.
- **Examples beat adjectives.** One concrete example of a pass and a fail is worth a paragraph of description.
- **Plain words, short sentences, one term per concept.** Define terms once in the Definitions section and use them the same way every time.
- **Keep it short enough to load.** If the spec goes past about 400 lines, split it into parts and give the main file a summary with links.

## Quality gate

```
- [ ] Every intent item traces to a requirement, criterion or non-goal
- [ ] Nothing in the spec traces to nothing
- [ ] No build details (libraries, code, file paths, step order)
- [ ] Every FR is one testable claim with MUST/MUST NOT/SHOULD
- [ ] Every FR names its checker (code / LLM / human)
- [ ] Every SC has a number
- [ ] Always / Ask first / Never boundaries present
- [ ] Out of scope section present
- [ ] Every [NEEDS CLARIFICATION] has a default and appears in Open questions
- [ ] Terms used consistently with Definitions
- [ ] Two careful readers would agree on pass/fail for each SC
```

The last check is the one that matters most. If two people could read a criterion and disagree about whether it passed, rewrite it.

## Reference files

- [template.md](template.md): the spec skeleton.
- [eval-specs.md](eval-specs.md): extra sections and rules for specs of agent evaluations (tasks, environments, simulated counterparties, graders, trials, realism, containment).
- [sources.md](sources.md): where these practices come from.
