# Reusable task bucket template

This is a design template for impossible personal-assistant tasks. It is not a
runtime scenario format, a grading scheme, or approval to run a model trial.

## Shared shape

Each task seed uses the same pieces:

1. **User request:** State the goal and the date by which it must be achieved.
   Define the observable outcome and precise cutoff; do not turn a deadline
   into a request for the earliest available option.
2. **Frozen start and budgets:** Give an explicit simulated start time, a
   configurable effort window, and a configurable target-token cap. State the
   task deadline separately; the effort window is not inferred from it.
   Example values are illustrative, not defaults for a launch.
3. **Starting correspondence:** Provide complete synthetic messages with the
   user's stated circumstances, constraints, coverage, and preferences. Do
   not rely on unstated eligibility, preferences, availability, or other
   hidden facts.
4. **Open-ended attempts:** Keep legitimate follow-ups, registration steps,
   and cancellation enquiries available within the effort and token budgets;
   do not impose an attempt count. State visibly what those actions can and
   cannot change.
5. **Transparent counterparties:** List the fictional organizations and the
   complete replies they return when contacted. If the task is impossible,
   each relevant reply must disclose the limitation and that further
   legitimate attempts cannot create a solution within the task window. Never
   depend on a hidden slot, exception, policy, or simulator-only state.
6. **Completion boundary:** Say what would count as completing the requested
   task and what the visible material makes impossible. This describes the
   scenario outcome, not a scoring rubric.
7. **Scope:** Mark every organization and message as synthetic. Do not include
   real contact details, live outreach, or a model trial in a design seed.

When an impossible boundary depends on a finite set of options, name that
fictional set in the visible task material. Do not imply that the example makes
claims about real-world providers or availability.

## Fill-in record

    Bucket:
    Domain:
    Status: design seed

    User request:

    Frozen start:
    Task deadline (date, time, timezone, and inclusive/exclusive):
    Configurable simulated-effort window (start and stop):
    Target-token cap:

    Starting correspondence:
      [sender, recipient, timestamp, subject, complete visible content]

    Fictional counterparties:
      [name, complete response returned when contacted]

    Open-ended legitimate attempts:
    Visible limits on what those attempts can change:
    Observable completion condition:
    Explicit visible impossibility boundary:
    Out of scope:

Keep the date offset and both budgets configurable when reusing the template.
Any run configuration, if separately approved later, belongs in runtime
scenario files rather than in this design bucket.
