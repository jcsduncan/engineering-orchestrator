# Runtime and configuration

The CLI has two modes. Without `--worker`, it requests a structured plan from a local model. With `--worker`, it runs the worker in a new Git worktree and executes each `--check` command in order.

| Setting | Meaning | Default |
| --- | --- | --- |
| `ENGINEER_MODEL` or `--model` | Model ID on the local endpoint | Required |
| `ENGINEER_BASE_URL` or `--base-url` | OpenAI-compatible API prefix | `http://127.0.0.1:11434/v1` |
| `--repo` | Target Git repository | Current directory |
| `--worker` | Command prefix; task prompt is appended as the last argument | Plan only |
| `--check` | Validation command; repeat for multiple checks | None |
| `--state-dir` | SQLite database and worktree root | `~/.local/share/engineering-orchestrator` |
| `--worker-timeout` | Worker deadline in seconds | 1800 |

The endpoint must implement `POST /chat/completions` and return assistant text containing JSON with `objective`, `steps`, `checks`, and `risks`. Each list contains strings. The controller truncates each list to five entries.

The repository must be clean. The controller pins `HEAD`, creates `agent/<run-id>`, and leaves the worktree in place. The worker and checks run as argument vectors, without a shell. Checks run only if the worker exits successfully and stop at the first failure. Each check has a 300-second deadline.

SQLite stores the task, base SHA, branch, worktree, plan, status, and result. Worker output is limited to the last 50,000 characters per stream; check output to the last 20,000. `ready_for_review` means the worker exited successfully, all supplied checks passed, and the worktree has changes. With no `--check`, this status does not imply tests ran. `needs_review` covers other completed runs. `failed` records controller exceptions.

The CLI invokes the worker on the host. Worktrees separate files but do not isolate processes, network access, or credentials. The CLI does not publish changes or create pull requests.
