# Runtime and configuration

Without `--worker`, the CLI requests a structured plan from a local model. With `--worker`, it runs the worker in a new Git worktree and executes each `--check` command in order. Use `--mode implementation` (default) for code changes or `--mode investigation` for a report without file changes.

| Setting | Meaning | Default |
| --- | --- | --- |
| `ENGINEER_MODEL` or `--model` | Model ID on the local endpoint | Required |
| `ENGINEER_BASE_URL` or `--base-url` | OpenAI-compatible API prefix | `http://127.0.0.1:11434/v1` |
| `--repo` | Target Git repository | Current directory |
| `--worker` | Command prefix; task prompt is appended as the last argument | Plan only |
| `--image` | Local container image containing the worker and validation tools | Required with `--worker` |
| `--check` | Validation command; pass a list for multiple checks | None |
| `--state-dir` | SQLite database and worktree root | `~/.local/share/engineering-orchestrator` |
| `--worker-timeout` | Worker deadline in seconds | 1800 |
| `--run-id` | Stable run identity for safe resume | Generated |
| `--container-cpus` | CPU limit | 2 |
| `--container-memory-mb` | Memory limit in MiB | 2048 |
| `--container-pids` | Process limit | 128 |
| `--container-network` | `none` or `bridge` | `none` |
| `--mode` | `implementation` or `investigation` | `implementation` |

The endpoint must implement `POST /chat/completions` and return assistant text containing JSON with `objective`, `steps`, `checks`, and `risks`. Each list contains strings. The controller truncates each list to five entries.

For multiple validation commands, use `--check='["ruff check .", "pytest -q"]'`. Repeating `--check` is rejected so a command cannot be silently dropped.

The repository must be clean. The controller pins `HEAD`, creates `agent/<run-id>`, and leaves the worktree in place. The worker and checks run as argument vectors, without a shell. Checks run only if the worker exits successfully and stop at the first failure. Each check has a 300-second deadline.

SQLite stores the task, mode, base SHA, branch, worktree, plan, status, and result. Worker output is limited to the last 50,000 characters per stream; check output to the last 20,000. Each check result records argv, start time, duration, exit code, status, and bounded output. Validation hashes tracked and untracked changes before and after checks; Git-ignored files are outside the hash. A changed hash or unsupported untracked entry invalidates validation. `ready_for_review` requires a successful worker, a nonempty diff, at least one check, every check passing, and matching hashes. The operator chooses checks relevant to the task. `needs_review` covers other completed implementation runs, including runs without checks. Existing SQLite databases gain the `mode`, cancellation, and retry lineage columns on use.

For an investigation, the worker must end its output with one line beginning `ENGINEERING_REPORT_JSON=` followed by a JSON object. The object contains `outcome` (`substantiated` or `inconclusive`), `summary`, `findings` (each with a claim and nonempty evidence list), `commands` (each with an argv list and result), and `uncertainty` (a list of strings). A substantiated report needs an evidenced finding; an inconclusive report needs an explicit uncertainty. A successful, unchanged investigation becomes `report_for_review` or `inconclusive`; checks may be absent and are then recorded as `not_run`. A failed worker, malformed report, changed worktree, or failed or invalidated validation becomes `failed` with the worker output and available check evidence retained. The commands and evidence in the report are worker claims for human review; the controller does not verify that the worker ran them. An empty implementation diff never becomes reviewable.

Each run starts with a persisted task snapshot, then moves through `accepted`, `planning`, `workspace_pending`, `executing`, and `validating`. The state database records each transition and checkpoints worker and check results. Repeating the same command with the same `--run-id` resumes from `accepted` or `workspace_pending`, or returns an already recorded result. If the prior process stopped during planning, execution, or validation, the controller records `needs_review` with available evidence and does not repeat the uncertain call. A run ID cannot be reused with different task or provider settings.

Use `engineer status RUN_ID` to read the current state and evidence. `engineer cancel RUN_ID --reason 'Operator requested'` records a cancellation request; the controller stops an active worker or check, preserves bounded partial output, and marks the run `cancelled`. An idle run is cancelled immediately. `engineer retry SOURCE_RUN_ID 'task'` requires the same execution options as the source run and creates a fresh worktree at its pinned base commit. Only `failed` or `cancelled` runs can be retried, with at most three new generations. Each source run can create one child generation. Use `--run-id` to choose the new generation's ID; otherwise one is generated.

Programmatic `run_task(..., external_cancel_reason=callback)` accepts a controller signal that maps a run ID to a reason or `None`. The first operator or external reason is persisted in the run record. The standard worker and checks poll the composed signal during execution; planning checks it before and after the bounded model request. `execute_task` accepts the same callback for state boundaries; custom worker and check providers must also poll the run cancellation record to stop a long-running call.

`engineer review RUN_ID --model LOCAL_REVIEW_MODEL` reviews a `ready_for_review` implementation run before publication. The reviewer receives task text, the validated tracked diff, untracked text files, changed paths, and recorded check evidence through an OpenAI-compatible endpoint. It has no workspace access or execution tools, though check output can contain a workspace path. The controller rejects a worktree whose diff hash changed, validates finding severity and old/new diff hunk lines, and stores one review per run and diff. Review errors and findings are advisory; they do not change run status or approve publication. A completed review is reused only with the same provider and cannot be replaced through this command; a failed or unavailable review can be retried, including with another provider. `ENGINEER_REVIEW_MODEL` selects a default review model, falling back to `ENGINEER_MODEL`.

`engineer-bench-review OUTPUT_DIR --model LOCAL_REVIEW_MODEL` reviews saved implementation candidates from `engineer-bench` and writes separate `review.json` and `review_summary.json` artifacts. The impact proxy compares accepting every reviewable candidate with holding candidates that receive a major or critical finding. It counts incorrect candidates flagged and correct candidates flagged using each benchmark's oracle. This is a decision proxy; findings still need human adjudication, and an unreviewed candidate is not a measured improvement.

The CLI resolves the local image to its immutable ID, then runs the worker and checks in disposable containers using that image. Each container mounts the run worktree read/write and pinned Git metadata read-only, with repository and worktree Git configs masked to hide remote credentials. It inherits no host environment variables or home directory and has no Docker socket. Its root filesystem is read-only, with a writable temporary filesystem. Docker applies CPU, memory, and process limits; the controller terminates a timed-out container. The controller rejects a changed worktree `.git` pointer before reading Git state on the host. `bridge` networking adds `host.docker.internal` for an explicitly configured model endpoint. Worker execution and PR publication use separate commands. Programmatic host execution remains available for trusted integrations.

## Providers

`PlanningProvider` and `WorkerProvider` define the programmatic interfaces. `OpenAICompatiblePlanner` keeps planning on the configured model endpoint; `CommandWorker` runs the selected CLI. `execute_task` accepts other implementations independently of the CLI defaults.

A worker command is one runtime invocation per engineering run. That runtime may manage internal agents, but it must return through the same worker result contract. The controller does not configure or resume those internal agents. Internal delegation depends on the selected runtime's support for the invocation mode; for example, [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams) require an interactive session, while this CLI invokes workers noninteractively.

Each run records provider names and a sanitized configuration summary in `providers_json` and its result. The planner record includes model, endpoint origin, and timeout. The worker record includes executable name, command hash, and timeout. Existing SQLite run databases gain the new column on use.

## GitHub issue reconciliation

`engineer-issues` runs one reconciliation pass for a configured GitHub App installation and repository:

```bash
ENGINEER_GITHUB_TOKEN=... uv run --locked engineer-issues jcsduncan engineering-orchestrator 12345
```

The token needs read access to repository issues and dependencies. The command prints unacknowledged claims and cancellations as JSON. Schedule repeated passes outside the command. It fetches the repository ID, lists issues labelled `agent:run`, then fetches each issue and its blocking relationships again before claiming. With `--issue-number`, it fetches that issue directly. Only open issues with no open blockers are claimed. Pull requests are excluded.

Claims use the same SQLite database as runs. Each claim stores an immutable title, body, objective, acceptance criteria, issue URL, and update time. The unique key is installation ID, repository ID, issue number, and generation; the returned run ID is stable for that key. Each pass returns an unacknowledged claim again. The controller must durably register the run ID, then call `IssueClaimStore.acknowledge_claim(run_id)`; repeated deliveries use the same run ID and cannot create another claim. Editing the issue does not change its snapshot.

After a run finishes, its controller calls `IssueClaimStore.complete(run_id)`. The method accepts only an active claim. If cancellation won the race and completion raises `ValueError`, the controller handles the pending cancellation instead. To claim a completed or cancelled issue again, specify both `--issue-number` and an increased `--generation`. An increased generation cannot trigger reruns of other issues in the repository.

Closing the issue, removing `agent:run`, or finding that the issue has left the repository revokes an unfinished claim. The JSON cancellation result includes its run ID and reason. `engineer-issues` does not stop running workers: a controller consuming that result must stop the run, then call `IssueClaimStore.acknowledge_cancel(run_id)`. Pending cancellations appear on every pass until acknowledged and prevent a new generation from starting. Issue text is data; it cannot set worker commands, checks, or permissions. Transient API failures abort the pass before changing claims.

## GitHub PR publication

`engineer-publish` takes a completed run ID from an active or completed GitHub issue claim:

```bash
ENGINEER_GITHUB_TOKEN=... uv run --locked engineer-publish RUN_ID
```

The token must be a GitHub App installation token limited to the claimed repository, with Contents and Pull requests write access. Run publication as a separate controller process so the token is never present in the worker environment. Use `--state-dir` for a nondefault state path and `--title 'fix: ...'` to override the conventional PR title derived from the issue title.

Only `ready_for_review` implementation runs are eligible. The publisher verifies the claim, repository scope, base SHA, and current diff hash before committing the run worktree. It pushes the fixed `agent/<run-id>` branch, then finds or creates a PR. SQLite records the commit and PR identity. Retrying the same run ID reuses them, including after a network timeout; a merged PR is returned without recreating a deleted branch. A conflicting remote branch, unrelated PR, changed worktree, cancelled claim, or investigation result is rejected.

The PR links the issue and includes the base SHA, validated diff hash, check outcomes, planner risks, advisory review findings or an explicit "not run" state, and unverified work. Run `engineer review` before `engineer-publish` to include its findings; publication commits the worktree, so that review command cannot inspect the original uncommitted diff afterward. `feat:` and `fix:` titles add the repository's release commit override. The publisher does not merge or approve the PR.

## GitHub issue webhooks

`engineer-webhooks` serves `POST /webhooks/github` on `127.0.0.1:8080` by default. Put an HTTPS reverse proxy in front of it and configure the GitHub App webhook URL and secret there.

```bash
ENGINEER_GITHUB_TOKEN_FILE=/run/secrets/github-installation-token \
  ENGINEER_WEBHOOK_SECRET=... \
  uv run --locked engineer-webhooks jcsduncan engineering-orchestrator 12345
```

The receiver verifies `X-Hub-Signature-256` against the original request bytes, checks the installation and repository, and persists the `X-GitHub-Delivery` ID before returning `202`. It does not fetch issues or run a model in the HTTP handler. Payloads above 1 MiB are rejected. Duplicate delivery IDs are acknowledged once and do not create another task.

A separate processor retries persisted deliveries after a crash or API failure. It fetches the current issue through the same `reconcile` operation used by `engineer-issues`; event action and issue text in the webhook are only hints. The server also scans all eligible issues every 300 seconds by default (`--scan-interval`) to recover missed webhooks. Claims and cancellations remain available through the `engineer-issues` command and the claim store. The token file is reread for each API request so an external GitHub App credential process can rotate its short-lived installation token. `ENGINEER_GITHUB_TOKEN` is accepted for a single token.
