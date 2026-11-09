# Lightweight Agent implementation

## What is implemented

- A fixed retrieval/generation/validation workflow with a bounded, read-only Function Calling extension.
- MySQL/SQLite conversation records, task-linked turns, optimistic conversation revisions and idempotent retries.
- A snapshot of the previous draft, its evidence and explicit user constraints for follow-up edits.
- Recent four user turns as context, maximum 12,000 characters of accumulated user constraints and 60,000 characters of memory snapshot. These are character limits, not measured token counts.
- No automatic summaries. Oversized memory is rejected explicitly rather than silently discarding a formula or constraint.
- Two tools: `search_knowledge` and `read_previous_draft`. Project scope is server-selected, never accepted as a tool argument. Unknown tools/extra arguments/duplicate calls are rejected.
- At most two planning rounds and three tool calls per task; each planning request has a 1,024 output-token cap. The final generation keeps its configured output cap.
- Tool/planning usage, errors and fallback events are available at `/api/tasks/{id}/usage`. No private chain-of-thought is requested or logged.
- Planner or read-tool failures fall back to the fixed RAG workflow with existing evidence. A task deadline does not trigger more calls. Missing evidence becomes a clarification question or failed task, not fabricated support.
- Formal models still require explicit human review and confirmation. Tools cannot execute SQL, access arbitrary files, run code or write formal models.

## Reliability fixes

- Numeric evidence checks compare complete numeric values (including scientific notation and thousands separators), not substrings: 2000 does not match 12000. This is a numeric-presence check, **not full semantic entailment**; parameter ownership, units and comparisons still require review.
- Expired-lease recovery uses compare-and-swap on the observed token and lease expiry, so a racing heartbeat is not overwritten.
- The execution deadline is saved on the first worker claim and never reset by recovery. Exhausted recovered tasks fail rather than replaying provider calls. A periodic pending-task scan retries failed executor handoffs, with in-process submission deduplication to avoid flooding the queue.
- Deadline propagates into query embedding and retrieval; connect/read timeouts and retry backoff use remaining budget. This is cooperative enforcement at stage/network boundaries. Requests read timeouts are inactivity timeouts; this does not guarantee hard cancellation of an in-flight provider inference or prevent duplicate provider billing after a timeout.

## Cost and evaluation

- Exact response cache is scoped by project, knowledge version, model settings and complete conversation snapshot. Repeated transport retries reuse the task rather than adding a conversation turn.
- Retrieval cache is project/version scoped. No approximate semantic response cache is used for model edits because distinct numeric constraints must not share an answer.
- Only successful API responses can supply provider token usage. Missing tokens and failed calls are not assumed free. Cost estimates use configurable prices, not billing receipts; actual regional/model pricing must be configured before paid use.
- Offline regression metrics: test-case pass count, schema/business-rule acceptance, preserved element/relationship counts after a follow-up edit, tool allowlist rejection and no unauthorized formal writes.
- Future real-model evaluation: task success rate = reviewed tasks meeting all requirements / evaluated tasks; acceptance rate = drafts accepted without edits / reviewed drafts; unsupported-value rate = reviewed numeric claims without matching evidence / reviewed numeric claims. Report dataset size and manual labeling rules. Do not present offline mock passes as real-model accuracy.

## Deliberately excluded

No Multi-Agent, automatic summarization, autonomous long-running ReAct, arbitrary model-written code, or silent model substitution. The rule-based mock demonstrates plumbing and supports an explicit mass-limit follow-up edit only; it is not a general reasoning model.

## Run and migrate

Stop the owned application process before migration; do not stop database containers or delete volumes. With `MBSES_ENV_FILE=.env.real` and paid calls disabled:

```powershell
.\.venv\Scripts\python.exe scripts/migrate_runtime.py
.\.venv\Scripts\python.exe scripts/migrate_runtime.py --apply
```

The migration writes a private JSON data snapshot under `.data/backups/`, checks there are no active tasks, adds the lease/deadline columns when absent and creates additive tables. MySQL startup fails with an explicit migration instruction if these columns are absent. The snapshot is for data recovery/audit, not an automated schema rollback.

UI: each new generation starts a conversation if none exists. Follow-up input continues that conversation. The history allows loading previous task drafts. Use **New conversation** to start independently. Each turn is a new draft task; a confirmed model is never silently overwritten by a follow-up.
