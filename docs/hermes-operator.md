# Hermes operator guide

Use this guide when Hermes initiates the portable runner and an external Codex or Claude CLI
performs the executor and independent verifier roles. Hermes supervises the run; it is not a
pipeline adapter, worker, board, or state store.

## Keep the four identities separate

- **Hermes operator** submits the assignment, watches the one runner process, and reviews
  evidence. It does not replace the runner's executor or verifier roles.
- **Worker runtime** is the explicitly selected `codex` or `claude` CLI. Choose one for the
  whole run; the runner pins that selection and rejects a resume that switches it.
- **Project profile** is the project-relative configuration supplied by `--profile`. It routes
  task types and owns project policy; it is not a Hermes profile.
- **Model provider** is Hermes's own model/provider configuration. Selecting `openai-codex`
  inside Hermes neither selects nor authenticates the Codex CLI. Authenticate and authorize the
  chosen external CLI independently.

Do not use `auto` for a live pilot: it follows adapter availability/preference, not the
initiating Hermes runtime. Do not mix adapters, bypass permissions, copy credentials, or switch
providers to recover quota. Resolve authentication, role, and quota with the owner of the
selected CLI account instead.

## Preflight

Before dispatching, identify a single operator who may approve the plan and inspect project
evidence. Confirm all of the following:

1. The selected worker executable is installed and its normal authentication/status command
   succeeds for the intended account; confirm its executor and read-only verifier roles have
   the required repository access.
2. The operator can read the project, agents, and core anchors, and the worker receives only
   the directory grants required by the runner.
3. The project profile, project skill, plan, prompt, selected task, and its dependency closure
   are known. Use POSIX logical paths below their anchors; do not pass absolute paths, `..`, or
   backslash-separated logical arguments.
4. A plan approval exists before execution. Preserve one writer: do not run another pipeline
   process or edit runner-owned board/state while this run holds its lease.

For long Hermes instructions, provide UTF-8 text through the Hermes interface's file or stdin
mechanism rather than fragile shell quoting. State the chosen adapter, the task ID, and that
Hermes must wait for runner evidence rather than infer success from its own exit status.

## Command templates

Replace every angle-bracket value. Anchor values are filesystem paths; the remaining path
arguments are POSIX logical paths relative to their documented anchor. Keep the same values for
preview, execute, and resume.

### Codex worker

Preview without dispatching a worker:

```sh
uv run python scripts/run_pipeline.py \
  --project-root <project-root> --agents-root <agents-root> --core-root <core-root> \
  --profile <profile-rel> --project-skill <project-skill-rel> \
  --plan <plan-rel> --prompt <prompt-rel> --task <task-id> \
  --verify-dependency-chain --adapter codex --mode plan-only --dry-run
```

After reviewing the preview and obtaining plan approval, execute:

```sh
uv run python scripts/run_pipeline.py \
  --project-root <project-root> --agents-root <agents-root> --core-root <core-root> \
  --profile <profile-rel> --project-skill <project-skill-rel> \
  --plan <plan-rel> --prompt <prompt-rel> --task <task-id> \
  --verify-dependency-chain --adapter codex --mode execute --approve-plan
```

### Claude worker

Preview without dispatching a worker:

```sh
uv run python scripts/run_pipeline.py \
  --project-root <project-root> --agents-root <agents-root> --core-root <core-root> \
  --profile <profile-rel> --project-skill <project-skill-rel> \
  --plan <plan-rel> --prompt <prompt-rel> --task <task-id> \
  --verify-dependency-chain --adapter claude --mode plan-only --dry-run
```

After reviewing the preview and obtaining plan approval, execute:

```sh
uv run python scripts/run_pipeline.py \
  --project-root <project-root> --agents-root <agents-root> --core-root <core-root> \
  --profile <profile-rel> --project-skill <project-skill-rel> \
  --plan <plan-rel> --prompt <prompt-rel> --task <task-id> \
  --verify-dependency-chain --adapter claude --mode execute --approve-plan
```

`--verify-dependency-chain` makes the dependency closure explicit. If reuse is intended
instead, omit it only after checking the runner's eligibility evidence; never use a second
writer to manufacture dependency state.

## Operate and inspect

Start one runner process and record its process identifier, exact sanitized argv, start time,
selected adapter, and stdout/stderr log locations. Monitor that process to completion; do not
start native Hermes workers or use native Hermes kanban to duplicate its work. The runner owns
durable run state, leases, task projection, verification commands, repair bounds, and verdict
evidence.

Treat the runner's terminal result and stored evidence as the result. For execute, inspect the
run record, runner-captured verification-command output, both independent verifier verdicts,
task terminal status, scope/attribution evidence, and the projected board/task view where the
plan is Markdown-backed. `0` means every selected task verified; `10` means the plan gate was
not satisfied; `20` means a task is blocked; `30` means a runner error. Parser failures and the
separate `--push` refusal (`1`) are not execute outcomes. A Hermes success code alone proves
only that Hermes finished its own action.

Execute covers stages 5–9 only. It does not complete documentation, Graphify, final
verification, final-diff approval, release, commit, or push; handle those later stages through
their distinct approved procedures.

## Resume the same runner identity

Resume only after inspecting why the original process stopped and only with the same anchors,
profile, project skill, plan, prompt, task selection, dependency-closure choice, feature, and
pinned adapter. For example, retain the adapter-specific value from the original command:

```sh
uv run python scripts/run_pipeline.py \
  --project-root <project-root> --agents-root <agents-root> --core-root <core-root> \
  --profile <profile-rel> --project-skill <project-skill-rel> \
  --plan <plan-rel> --prompt <prompt-rel> --task <task-id> --feature <feature-name> \
  --verify-dependency-chain --adapter <codex-or-claude> --mode execute --approve-plan --resume
```

Hermes resuming its conversation or job is a different action from runner `--resume`; it does
not establish runner identity or authorize redispatch. Never use resume to change adapter,
scope, dependencies, or approvals. If the run is blocked by access, role, quota, or a stale
lease, preserve the evidence and escalate rather than bypassing ownership controls.
