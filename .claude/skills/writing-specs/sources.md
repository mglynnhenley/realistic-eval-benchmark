# Sources

The practices in this skill come from:

- Addy Osmani, "How to write a good spec for AI agents": start from a short brief, structure the spec like a PRD, three-tier boundaries (always / ask first / never), keep the spec focused and loadable, treat it as a living document. https://addyosmani.com/blog/good-spec/
- GitHub Spec Kit: separate spec (what and why) from plan (how) and tasks; numbered functional requirements; measurable success criteria; `[NEEDS CLARIFICATION]` markers; a quality checklist that acts as "unit tests for the requirements". https://github.com/github/spec-kit
- Anthropic, "Demystifying evals for AI agents": task, trial, transcript, outcome, grader; two experts should reach the same verdict; grade outcomes not paths; combine code, LLM and human graders; calibrate LLM graders; read transcripts; isolate trials; pass@k and pass^k. https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Anthropic, Petri: simulated users and tools for behaviour audits, rubric-based LLM judging, and work on keeping simulated scenarios realistic enough to avoid eval awareness. https://www.anthropic.com/research/petri-open-source-auditing
- Inspect Scout: scanning agent transcripts at scale with LLM and pattern scanners. https://meridianlabs-ai.github.io/inspect_scout/
- Anthropic, "Skill authoring best practices": the structure of this skill itself. https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
