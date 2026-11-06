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

SQLite stores the task, mode, base SHA, branch, worktree, plan, status, and result. Worker output is limited to the last 50,000 characters per stream; check output to the last 20,000. Each check result records argv, start time, duration, exit code, status, and bounded output. Validation hashes tracked and untracked changes before and after checks; Git-ignored files are outside the hash. A changed hash or unsupported untracked entry invalidates validation. `ready_for_review` requires a successful worker, a nonempty diff, at least one check, every check passing, and matching hashes. The operator chooses checks relevant to the task. `needs_review` covers other completed implementation runs, including runs without checks. Existing SQLite databases gain the `mode` column on use.

For an investigation, the worker must end its output with one line beginning `ENGINEERING_REPORT_JSON=` followed by a JSON object. The object contains `outcome` (`substantiated` or `inconclusive`), `summary`, `findings` (each with a claim and nonempty evidence list), `commands` (each with an argv list and result), and `uncertainty` (a list of strings). A substantiated report needs an evidenced finding; an inconclusive report needs an explicit uncertainty. A successful, unchanged investigation becomes `report_for_review` or `inconclusive`; checks may be absent and are then recorded as `not_run`. A failed worker, malformed report, changed worktree, or failed or invalidated validation becomes `failed` with the worker output and available check evidence retained. The commands and evidence in the report are worker claims for human review; the controller does not verify that the worker ran them. An empty implementation diff never becomes reviewable.

Each run starts with a persisted task snapshot, then moves through `accepted`, `planning`, `workspace_pending`, `executing`, and `validating`. The state database records each transition and checkpoints worker and check results. Repeating the same command with the same `--run-id` resumes from `accepted` or `workspace_pending`, or returns an already recorded result. If the prior process stopped during planning, execution, or validation, the controller records `needs_review` with available evidence and does not repeat the uncertain call. A run ID cannot be reused with different task or provider settings.

The CLI resolves the local image to its immutable ID, then runs the worker and checks in disposable containers using that image. Each container mounts the run worktree read/write and pinned Git metadata read-only, with repository and worktree Git configs masked to hide remote credentials. It inherits no host environment variables or home directory and has no Docker socket. Its root filesystem is read-only, with a writable temporary filesystem. Docker applies CPU, memory, and process limits; the controller terminates a timed-out container. The controller rejects a changed worktree `.git` pointer before reading Git state on the host. `bridge` networking adds `host.docker.internal` for an explicitly configured model endpoint. The CLI does not publish changes or create pull requests. Programmatic host execution remains available for trusted integrations.

## Providers

`PlanningProvider` and `WorkerProvider` define the programmatic interfaces. `OpenAICompatiblePlanner` keeps planning on the configured model endpoint; `CommandWorker` runs the selected CLI. `execute_task` accepts other implementations independently of the CLI defaults.

Each run records provider names and a sanitized configuration summary in `providers_json` and its result. The planner record includes model, endpoint origin, and timeout. The worker record includes executable name, command hash, and timeout. Existing SQLite run databases gain the new column on use.
