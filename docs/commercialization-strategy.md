# Commercialization Strategy

## One-line decision

Do not commercialize this as a generic Claude Code HTTP wrapper.

Commercialize it first as a **self-hostable AI coding agent runtime for controlled repo work**:

> Give teams a private, auditable, sandboxed way to turn issues into verified patches, without locking them into one IDE, one model vendor, or one hosted agent platform.

In Zhang Yiming terms: the root problem is not "call Claude Code through HTTP"; the root problem is "teams want agentic coding work to happen safely, repeatedly, and reviewably inside their own engineering environment."

## Why this direction

The current market already has strong generic coding agents:

- Anthropic positions Claude Code as an agentic coding system that reads a codebase, changes files, runs tests, and delivers committed code.
- GitHub Copilot cloud agent is issue-to-PR oriented: assign an issue, it plans, writes, runs tests, opens a PR, and asks for review.
- OpenAI Codex is moving toward a command center for supervising multiple long-running coding agents.
- Cursor is positioning around coding agents across IDE, terminal, Slack, and PR review workflows.

Competing head-on with these is a bad first battlefield.

The better wedge is where generic hosted agents are uncomfortable:

- private deployment
- China / custom Anthropic-compatible model gateways
- non-GitHub or mixed Git providers
- strict sandboxing
- custom tool, MCP, hook, and permission policies
- audit logs and replay
- repeatable engineering workflows, not open-ended chat

## Product name direction

Working names:

- `CodeRuntime Cloud`
- `PatchRuntime`
- `Agentic Dev Runtime`
- `Code Sandbox Runtime`

Best current name:

**PatchRuntime**

Reason: it sells the outcome. Users buy verified patches, not runtime internals.

## Initial ICP

Start with teams that feel the pain now and can pay without huge procurement:

1. AI product studios and dev agencies
   - They manage many repos.
   - They need repeated small fixes, migrations, docs, tests, and repo analysis.
   - They care about throughput more than IDE polish.

2. Small to mid-size engineering teams
   - They have backlog pressure.
   - They want coding agents but do not want to give cloud tools uncontrolled repo access.
   - They need reviewable diffs, tests, and auditability.

3. Internal platform teams in model-restricted environments
   - They need BYO model gateway.
   - They need self-hosting.
   - They may be outside the default GitHub/Copilot/Codex path.

Avoid as first ICP:

- individual vibe coders
- broad IDE users
- enterprises that need a 9-month security review before first value
- generic "AI coding for everyone"

## First paid use case

**Issue to verified patch in a private sandbox.**

Input:

- repo URL or mounted repo
- issue/task text
- allowed tools
- test command
- approval policy

Output:

- plan
- changed files
- test output
- failure log
- patch/diff
- replayable event stream
- optional PR

This fits the existing repo because it already has:

- FastAPI query/session API
- SSE streaming
- Claude Agent SDK wrapper
- hooks, MCP, skills, plugins passthrough
- one-user-one-sandbox control plane
- snapshot restore/release lifecycle

## Product shape

### Layer 1: Runtime API

Keep existing lower-level APIs, but add a product-level `Job` abstraction:

- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/events`
- `GET /v1/jobs/{job_id}/artifacts`
- `POST /v1/jobs/{job_id}/approve`
- `POST /v1/jobs/{job_id}/cancel`

Job types:

- `repo_analysis`
- `code_review`
- `small_fix`
- `test_repair`
- `dependency_upgrade`
- `docs_update`

### Layer 2: Workflow presets

Package permissions, prompts, hooks, and tools into presets:

- `read_only_review`
- `safe_patch`
- `test_repair`
- `dependency_upgrade`
- `full_sandbox`

Users should not have to hand-author raw `permissionMode`, `hooks`, `agents`, and `mcpServers` every time.

### Layer 3: Evidence and eval

Every job should produce:

- objective outcome
- test command status
- changed files
- event timeline
- model/tool cost
- failure bucket
- human review state

This is the commercial trust layer. Without it, the product is a demo.

Agent trace data should be a first-class data product, not incidental logs. The trace layer should persist every job/run's agent events, tool calls, approvals, artifacts, cost, duration, failure bucket, and replay metadata. See `docs/agent-trace-data-spec.md` for the proposed trace hierarchy, schema, APIs, retention controls, and eval usage.

### Layer 4: Deployment packaging

Commercial packaging should be:

- Docker Compose for one-box teams
- Kubernetes chart for serious teams
- hosted option later
- BYO Anthropic-compatible endpoint
- S3/OSS snapshot backend

## Differentiation

The positioning should not be "better model."

The positioning should be:

1. **Private agentic coding runtime**
   - Run coding agents inside your own sandbox and infra.

2. **Model-gateway agnostic**
   - Works with Claude-compatible endpoints and future adapters.

3. **Workflow-first**
   - Jobs, artifacts, approvals, tests, and audit logs are first-class.

4. **Policy-controlled**
   - Hooks and permission presets make agent actions governable.

5. **Replayable and recoverable**
   - Snapshot lifecycle makes agent work resumable and inspectable.

## Pricing hypothesis

Do not start with pure seat pricing. Agentic coding cost is usage-heavy.

Use:

- platform fee
- usage pass-through or usage credits
- enterprise self-host fee

Suggested test pricing:

- Starter self-host: RMB 999/month, 1 runtime, BYO model key
- Team: RMB 4,999/month, 5 runtimes, shared dashboard, audit logs
- Pro runtime: RMB 9,999/month+, queueing, snapshots, S3/OSS backend, policy presets
- Enterprise: custom, Kubernetes, SSO, audit export, model gateway integration

This is only a pricing test. The key is to charge for controlled runtime capacity and workflow reliability, not for "AI chat."

## 30/60/90 roadmap

### First 30 days: make it sellable

- Add `Job` abstraction above raw query/session.
- Implement `repo_analysis`, `code_review`, and `small_fix`.
- Store job events, artifacts, status, and test results.
- Add preset permissions: `read_only_review` and `safe_patch`.
- Produce a one-command local demo.
- Write one landing README around "private issue-to-patch agent runtime."

Success metric:

- A user can run one repo task and receive a reviewable artifact without knowing Claude Code internals.

### Days 31-60: make it trustworthy

- Add Git provider integration for GitHub first.
- Add PR creation or patch export.
- Add eval/failure buckets.
- Add cost tracking per job.
- Add persistent agent trace storage and export.
- Add approval gates for risky actions.
- Add job replay from event log.
- Add Docker Compose production profile.

Success metric:

- A small team can use it on real repos for review/fix tasks and know what happened.

### Days 61-90: make it commercially credible

- Add workspace/project dashboard.
- Add multi-user auth.
- Add S3/OSS snapshot backend docs and verified path.
- Add team policy templates.
- Add queue/concurrency controls.
- Add hosted pilot option or private deployment script.
- Recruit 3 design partners.

Success metric:

- 3 teams use it weekly for repeated repo work, and at least 1 pays.

## What not to build yet

- Do not build a full IDE.
- Do not build a generic chatbot.
- Do not compete with Cursor UI.
- Do not build a model marketplace first.
- Do not overbuild memory before job-level repeat value is proven.
- Do not sell "agent platform" before selling one concrete workflow.

## Zhang Yiming lens

### 同理心

The user does not want "Claude Code over HTTP." They want engineering work to move forward while staying safe and reviewable.

### 从根本上解决问题

The root blocker is not API access. It is reliable task execution with sandboxing, evidence, approval, and repeatability.

### 不要为了竞争而竞争

GitHub, OpenAI, Anthropic, and Cursor are all racing toward general agentic coding. The first commercial wedge should avoid their main battlefield.

### 务实浪漫

The romantic part is: agents can become an always-on engineering workforce.

The pragmatic part is: start with narrow jobs, strong evals, explicit permissions, and reviewable patches.

## Final recommendation

Choose this path:

**PatchRuntime: private, self-hostable issue-to-verified-patch runtime for AI coding agents.**

Build it as a workflow product first and infrastructure product second.

The first sellable promise should be:

> Connect a repo, describe a small task, run an isolated coding agent, get a tested patch with full logs and review controls.

That is concrete enough to sell, narrow enough to build, and broad enough to grow into a real AI Coding Agent Runtime.
