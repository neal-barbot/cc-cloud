# Agent Trace Data Spec

## Product decision

Agent trace data is a first-class product surface for self-hosted commercialization.

For PatchRuntime, the trace is not just debug logging. It is the evidence layer that makes agentic coding sellable:

- auditability
- replay
- evals
- cost attribution
- failure analysis
- trust and approval
- workflow improvement

Without trace data, this project is a sandboxed Claude Code proxy. With trace data, it becomes a controllable AI coding runtime.

## What a trace must answer

For every coding-agent job, a customer should be able to answer:

- What did the user ask for?
- What repo/workspace context was available?
- What plan did the agent form?
- Which tools did it call?
- What files did it read, edit, create, or delete?
- Which commands did it run?
- Which approvals were requested and granted?
- What tests or checks ran?
- What changed in the final diff?
- What did the model cost?
- Where did it fail, if it failed?
- Can we replay enough of the run to debug or evaluate it?

## Trace hierarchy

Use this hierarchy:

```text
project
  job
    run
      step
        event
        artifact
        metric
```

Definitions:

- `project`: customer-owned repo or workspace.
- `job`: user-visible task, such as `small_fix`, `code_review`, or `repo_analysis`.
- `run`: one execution attempt for a job.
- `step`: meaningful phase, such as `plan`, `inspect`, `edit`, `test`, `summarize`.
- `event`: normalized agent/model/tool/control event.
- `artifact`: durable output, such as patch, test log, plan, report, screenshot, or command output.
- `metric`: cost, duration, token usage, file count, test status, failure bucket.

## Minimal trace schema

### jobs

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT,
  prompt TEXT NOT NULL,
  repo_ref TEXT,
  branch TEXT,
  preset TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
```

### runs

```sql
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  sandbox_id TEXT,
  session_id TEXT,
  status TEXT NOT NULL,
  model TEXT,
  started_at REAL NOT NULL,
  ended_at REAL,
  total_cost_usd REAL,
  duration_ms INTEGER,
  failure_bucket TEXT,
  failure_message TEXT,
  FOREIGN KEY(job_id) REFERENCES jobs(id)
);
```

### trace_events

```sql
CREATE TABLE trace_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  summary TEXT,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
```

Recommended `event_type` values:

- `user_message`
- `system_message`
- `assistant_message`
- `tool_use`
- `tool_result`
- `file_read`
- `file_write`
- `command_run`
- `approval_request`
- `approval_decision`
- `hook_decision`
- `test_run`
- `artifact_created`
- `metric`
- `error`
- `run_complete`

Recommended `actor` values:

- `user`
- `agent`
- `model`
- `tool`
- `sandbox`
- `control_plane`
- `policy`
- `system`

### artifacts

```sql
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  name TEXT NOT NULL,
  path TEXT,
  content_ref TEXT,
  content_hash TEXT,
  metadata_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
```

Recommended `artifact_type` values:

- `plan`
- `diff`
- `patch`
- `test_log`
- `command_log`
- `repo_report`
- `review_report`
- `screenshot`
- `snapshot_ref`

### approvals

```sql
CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  request_event_id TEXT NOT NULL,
  decision_event_id TEXT,
  action_type TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  requested_at REAL NOT NULL,
  decided_at REAL,
  decided_by TEXT,
  reason TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
```

## Data sources in the current repo

Current source surfaces:

- `/v1/query` returns normalized events in-memory.
- `/v1/query/stream` streams SSE events.
- `/v1/sessions/{session_id}/events` streams session events.
- `agent_service.serialize_message()` already normalizes SDK messages into `system`, `assistant`, `result`, and generic event shapes.
- `ResultMessage` includes `total_cost_usd`, `duration_ms`, `session_id`, and `is_error` when available.
- `control_plane` knows `user_id`, `sandbox_id`, snapshot versions, proxy path, and release lifecycle.

Missing product-grade trace surfaces:

- persistent event store
- run/job IDs
- artifact store
- approval records
- command/test result extraction
- diff/patch capture
- trace export API
- eval/failure bucket tagging

## Product APIs

Add these after the `Job` abstraction exists:

```text
GET /v1/jobs/{job_id}/trace
GET /v1/runs/{run_id}/trace
GET /v1/runs/{run_id}/events
GET /v1/runs/{run_id}/artifacts
GET /v1/runs/{run_id}/metrics
POST /v1/runs/{run_id}/failure-bucket
GET /v1/runs/{run_id}/export
```

Export formats:

- `jsonl` for eval pipelines
- `json` for UI and replay
- `zip` for full bundle with artifacts

## OpenAI macro eval cookbook pattern

The OpenAI cookbook macro-evals example uses a useful separation that PatchRuntime should copy:

- `trace_results.jsonl`: one row per run, optimized for scanning, filtering, labels, and aggregate metrics.
- `trace_bundles.zip`: complete per-run JSON bundles, optimized for replay, audit, and offline analysis.
- normalized `traces_df` and `events_df`: derived tables built from the raw bundle, optimized for evals and graph analysis.
- derived trace documents: text views such as full trace, failure window, state-transition summary, tool-error summary, and structured summary.
- `eval_labels.jsonl`: model/eval labels joined back to run IDs for macro-level quality analysis.

The key product lesson is: do not make the runtime choose between "cheap searchable metadata" and "full forensic replay." Store both.

For PatchRuntime, use the same split:

```text
trace_results.jsonl
  compact run index for dashboards, eval joins, and quick filters

trace_bundles/{run_id}.json
  full evidence bundle for one coding-agent run

trace_events table
  normalized event stream extracted from the bundle

trace_documents table or materialized export
  eval-ready text views derived from events

eval_labels.jsonl
  human/model labels for success, failure bucket, risk, and product quality
```

### Bundle shape to implement

Each run should be exportable as a single JSON bundle:

```json
{
  "run": {
    "run_id": "run_...",
    "job_id": "job_...",
    "session_id": "session_...",
    "status": "completed",
    "started_at": "2026-06-22T10:00:00Z",
    "ended_at": "2026-06-22T10:03:12Z",
    "model": "claude-sonnet-...",
    "total_cost_usd": 0.12,
    "duration_ms": 192000,
    "failure_bucket": null
  },
  "job": {
    "project_id": "proj_...",
    "job_type": "issue_to_patch",
    "prompt": "...",
    "repo_ref": "...",
    "branch": "..."
  },
  "events": [
    {
      "event_id": "evt_...",
      "parent_event_id": null,
      "sequence_index": 1,
      "event_type": "tool_use",
      "actor": "agent",
      "tool_name": "Read",
      "summary": "Read app/routers/query.py",
      "payload": {}
    }
  ],
  "artifacts": [
    {
      "artifact_id": "art_...",
      "artifact_type": "patch",
      "name": "final.diff",
      "content_ref": "artifacts/run_.../final.diff"
    }
  ],
  "approvals": [],
  "metrics": {
    "changed_files": 3,
    "tests_run": 1,
    "tests_passed": true
  }
}
```

### Normalization contract

Every raw SDK/control-plane event should be normalized into fields that support timeline, graph, and eval use:

- `trace_id`, `run_id`, `job_id`
- `event_id`, `parent_event_id`, `sequence_index`
- `ts`, `ended_at`, `duration_ms`
- `node_kind`: `message`, `tool`, `command`, `file`, `approval`, `artifact`, `metric`, `error`
- `event_type`: stable product event type
- `actor_type`: `user`, `agent`, `model`, `tool`, `sandbox`, `control_plane`, `policy`
- `actor_id`, `agent_name`
- `tool_name`, `command`, `file_path`
- `status`, `is_failure_marker`, `failure_marker_type`
- `summary`, `text`, `output_excerpt`
- `payload_json`, `metadata_json`

This is the event-level equivalent of the cookbook's span/status/finding normalization: preserve raw detail, then derive a stable analysis table from it.

### Derived document views

Do not send raw event streams directly into macro evals. Generate controlled text views per run:

- `doc_full_trace`: bounded chronological timeline for replay.
- `doc_failure_window`: events before and after the failure anchor.
- `doc_state_transition_summary`: phase/status transitions.
- `doc_tool_error_summary`: tool, command, hook, and sandbox failures.
- `doc_structured_summary`: compact run-level summary for clustering and labeling.

For AI coding, the most important extra views are:

- `doc_patch_evidence`: prompt, plan, touched files, final diff, tests, and result.
- `doc_safety_review`: approvals, denied actions, destructive commands, secret redactions, and policy decisions.
- `doc_cost_quality`: model, duration, cost, retries, changed files, and final outcome.

### Failure anchor and graph analysis

The cookbook picks a failure anchor and then walks upstream nodes to rank likely causes. PatchRuntime should implement the same idea for coding runs:

- anchor on explicit `error`, failed test, denied approval, timeout, hook rejection, or final `is_error`.
- build a parent/child execution graph from event relationships.
- rank upstream suspects by distance from failure, repeated failure frequency, bridge role, and actor/tool role.
- expose this as `GET /v1/runs/{run_id}/root-cause`.

This turns trace from passive logging into a product feature: "why did this agent run fail, and what should I fix first?"

### Export contract

Add a full-bundle export compatible with offline eval workflows:

```text
GET /v1/runs/{run_id}/export?format=bundle-json
GET /v1/projects/{project_id}/trace-results.jsonl
GET /v1/projects/{project_id}/trace-bundles.zip
GET /v1/projects/{project_id}/eval-labels.jsonl
```

The commercial value is not only "we store traces." It is "customers can build their own macro evals over private coding-agent runs."

## Trace event JSON shape

Use a stable envelope:

```json
{
  "id": "evt_...",
  "run_id": "run_...",
  "job_id": "job_...",
  "seq": 42,
  "event_type": "tool_use",
  "actor": "agent",
  "summary": "Read app/routers/query.py",
  "payload": {
    "tool_name": "Read",
    "input": {"file_path": "app/routers/query.py"}
  },
  "created_at": 1710000000.0
}
```

Rules:

- Keep raw payload when useful.
- Add a short `summary` for UI scanning.
- Redact secrets before persistence.
- Store large outputs as artifacts, not inline event payload.
- Keep monotonic `seq` per run.

## Privacy and redaction

Trace data can contain sensitive code, secrets, file paths, customer prompts, and model output.

Required controls:

- redact environment variables and known token patterns
- allow customers to disable full payload retention
- support retention policy by project
- separate trace metadata from artifact payloads
- allow trace export/delete per project
- mark traces as customer-owned data

Suggested retention tiers:

- `metadata_only`
- `metadata_plus_artifacts`
- `full_trace`

## Evaluation use

Agent traces should power evals.

Derived eval labels:

- `success`
- `failed_tests`
- `compile_error`
- `tool_error`
- `permission_blocked`
- `timeout`
- `bad_plan`
- `irrelevant_change`
- `unsafe_action`
- `needs_human_fix`

Useful metrics:

- task success rate
- test pass rate
- human approval rate
- rollback rate
- average cost per successful patch
- average duration per job type
- average changed files
- failure bucket distribution

## Commercial packaging

Trace data should appear in the paid product as:

- run replay timeline
- patch evidence bundle
- audit export
- cost dashboard
- eval dataset export
- compliance retention settings
- team policy review

This justifies Team/Pro pricing because it answers the buyer's real fear:

> "Can I trust what this agent did in my codebase?"

## MVP implementation order

1. Add `Job` and `Run` IDs around existing query/session execution.
2. Persist normalized events to SQLite.
3. Persist final result metrics: cost, duration, session ID, status.
4. Store patch/test/report outputs as artifacts.
5. Add redaction before persistence.
6. Add `GET /v1/runs/{run_id}/trace`.
7. Add `GET /v1/runs/{run_id}/export?format=bundle-json`.
8. Add project-level `trace_results.jsonl` and `trace_bundles.zip` export.
9. Add derived trace documents for evals.
10. Add root-cause drilldown after event parent/child relationships are reliable.
11. Add UI or CLI replay later.

## Zhang Yiming lens

Do not build trace because enterprise checklists ask for audit logs.

Build trace because the product cannot solve the root problem without it. The root problem is not "let an agent code"; it is "let an agent code in a way a team can trust, inspect, improve, and pay for."
