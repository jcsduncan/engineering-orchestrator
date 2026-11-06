# Runtime and configuration

The CLI has two modes. Without `--worker`, it requests a structured plan from a local model. With `--worker`, it runs the worker in a new Git worktree and executes each `--check` command in order.

| Setting | Meaning | Default |
| --- | --- | --- |
| `ENGINEER_MODEL` or `--model` | Model ID on the local endpoint | Required |
| `ENGINEER_BASE_URL` or `--base-url` | OpenAI-compatible API prefix | `http://127.0.0.1:11434/v1` |
| `--repo` | Target Git repository | Current directory |
| `--worker` | Command prefix; task prompt is appended as the last argument | Plan only |
| `--check` | Validation command; pass a list for multiple checks | None |
| `--state-dir` | SQLite database and worktree root | `~/.local/share/engineering-orchestrator` |
| `--worker-timeout` | Worker deadline in seconds | 1800 |
| `--run-id` | Stable run identity for safe resume | Generated |

The endpoint must implement `POST /chat/completions` and return assistant text containing JSON with `objective`, `steps`, `checks`, and `risks`. Each list contains strings. The controller truncates each list to five entries.

For multiple validation commands, use `--check='["ruff check .", "pytest -q"]'`. Repeating `--check` is rejected so a command cannot be silently dropped.

The repository must be clean. The controller pins `HEAD`, creates `agent/<run-id>`, and leaves the worktree in place. The worker and checks run as argument vectors, without a shell. Checks run only if the worker exits successfully and stop at the first failure. Each check has a 300-second deadline.

SQLite stores the task, base SHA, branch, worktree, plan, status, and result. Worker output is limited to the last 50,000 characters per stream; check output to the last 20,000. Each check result records argv, start time, duration, exit code, status, and bounded output. Validation hashes tracked and untracked changes before and after checks; Git-ignored files are outside the hash. A changed hash or unsupported untracked entry invalidates validation. `ready_for_review` requires a successful worker, a nonempty diff, at least one check, every check passing, and matching hashes. The operator chooses checks relevant to the task. `needs_review` covers other completed runs, including runs without checks. `failed` records controller exceptions.

Each run starts with a persisted task snapshot, then moves through `accepted`, `planning`, `workspace_pending`, `executing`, and `validating`. The state database records each transition and checkpoints worker and check results. Repeating the same command with the same `--run-id` resumes from `accepted` or `workspace_pending`, or returns an already recorded result. If the prior process stopped during planning, execution, or validation, the controller records `needs_review` with available evidence and does not repeat the uncertain call. A run ID cannot be reused with different task or provider settings.

The CLI invokes the worker on the host. Worktrees separate files but do not isolate processes, network access, or credentials. The CLI does not publish changes or create pull requests.

## Providers

`PlanningProvider` and `WorkerProvider` define the programmatic interfaces. `OpenAICompatiblePlanner` keeps planning on the configured model endpoint; `CommandWorker` runs the selected CLI. `execute_task` accepts other implementations independently of the CLI defaults.

Each run records provider names and a sanitized configuration summary in `providers_json` and its result. The planner record includes model, endpoint origin, and timeout. The worker record includes executable name, command hash, and timeout. Existing SQLite run databases gain the new column on use.
