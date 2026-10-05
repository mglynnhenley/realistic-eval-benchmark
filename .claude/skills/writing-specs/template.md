# Spec template

Use this as the default shape. Drop a section only if it truly doesn't apply. Sections marked (eval) come from [eval-specs.md](eval-specs.md).

```markdown
# Spec - <name>

Status: draft | agreed
Intent: [intent.md](intent.md)

## Summary
Three to five sentences: what this is, what it must do, and how we'll know it worked.

## Definitions
| Term | Meaning |
| --- | --- |

## Traceability
| Intent item | Covered by |
| --- | --- |

## Scenario (eval)
## Environment (eval)
## Target agent (eval)

## Requirements
### <Area>
- FR-001: <Actor> MUST <behaviour>. Checked by: <code | LLM | human>.

## Boundaries
**Always:** ...
**Ask first:** ...
**Never:** ...

## Measurement (eval)
## Trials and budget (eval)

## Success criteria
- SC-001: <measurable outcome with a number>.

## Out of scope
- ...

## Risks
| Risk | Why it matters | Mitigation |
| --- | --- | --- |

## Open questions
1. <Question>. Default: <what we do if nobody answers>. Affects: FR-xxx.
```

## Writing the Summary

Write it last. It should let someone who reads nothing else know what gets built and what "done" means.

## Example requirement, good and bad

Bad: "The environment should be realistic."

Good: "FR-021: Every simulated reply MUST come from the sender address and in the format the real organisation uses. Checked by: human review of 10 sampled replies against real examples."
