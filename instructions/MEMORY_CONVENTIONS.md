# Memory conventions (how the scratch space works)

- Memories are Markdown files under `data/memory/`, one topic per file, indexed for full-text search.
  Use `memory_search` before answering anything that depends on what was said before; read the hits that
  matter with `memory_read`.
- Write a memory whenever something durable is learned: a habit and its rules, a policy the user set, a
  decision, a recurring commitment, a preference, a person, a project's current state, a check you have
  promised to make. Prefer updating an existing memory over creating a near-duplicate.
- Dates are recorded for you: the store stamps each memory with the conversation date and channel it
  came from (`sources`) and every date it was edited (`edits`). Nothing is removed from those lists. If a
  memory comes from an earlier conversation than today's, pass `conversation_date` so the right
  transcript is cited. Transcripts live under `data/transcripts/YYYY-MM-DD.jsonl` and can be read with
  `read_transcript`.
- Keep bodies factual and short; separate what the user said from what you inferred, and mark inferences.
- Suggested tags: `habit`, `policy`, `project`, `person`, `preference`, `commitment`, `metric-baseline`,
  `check`, `about-me` (things the agent wants remembered about itself and its experience).
