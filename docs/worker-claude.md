# Claude Code worker

The [image recipe](https://github.com/jcsduncan/engineering-orchestrator/blob/main/images/claude/Dockerfile) installs Claude Code 2.1.319 on a pinned Node 22 base. Build from the repository root:

```bash
docker build -f images/claude/Dockerfile -t engineering-orchestrator-claude:2.1.319 .
docker image inspect engineering-orchestrator-claude:2.1.319 --format '{{.Id}}'
```

Use the reported `sha256:` image ID, not the mutable tag, in a [worker profile](runtime.md#providers) after its probes pass. The controller resolves and pins the image ID for each run. The image contains `git`, Python 3.11, and `uv`; target repositories may need more validation tools.

The [local container probe](probes/claude-code-2.1.319.json) records the built image ID, version, missing-auth behavior, timeout, cancellation, output truncation, and observed cgroup limits. Authenticated model behavior remains unverified until the dedicated key is supplied.

Create a dedicated Anthropic API key file outside the repository, owned by the runner with mode `0600`. The container mounts that file at `/run/secrets/worker-auth` for worker execution only. `claude-worker` reads it, exports `ANTHROPIC_API_KEY`, and starts `claude -p` with `dontAsk` permissions and an explicit tool allowlist. It does not inherit the host home or GitHub credentials. The worker can read and send its own model credential over the network; use a key with a spend limit. Public CI cannot run authenticated probes without a separately provisioned key.

```bash
uv run --locked engineer 'Fix a bounded issue' \
  --repo /path/to/repo \
  --model local-planner \
  --worker claude-worker \
  --image engineering-orchestrator-claude:2.1.319 \
  --worker-auth-file /path/to/private/claude-api-key \
  --container-network bridge \
  --check 'uv run --locked pytest -q'
```

For the claimed issue controller, use the same `worker`, `image`, `container_network`, and absolute `worker_auth_file` fields in its trusted JSON config. The planner remains independently configured. The wrapper grants `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep`, and `Agent`; actions outside those permissions are denied without a prompt. Docker still bounds filesystem access, CPU, memory, process count, output, and duration.

## Capability evidence

Run a contained single-agent edit and an `Agent` subagent task with the pinned image and credential. Record the image ID, command digest, Claude Code version, prompt, exit status, observed tool events, diff, and validation result in a probe artifact without raw credentials. Probe timeout, cancellation, output truncation, and resource limits through `ContainerRuntime` as separate failure cases. A profile must mark a mode `supported` only after that mode's probe passes for the exact image and command.

Claude Code's [headless documentation](https://code.claude.com/docs/en/headless) supports `-p` and documents its permission flags. Its [subagent documentation](https://code.claude.com/docs/en/sub-agents) names the `Agent` tool. Its [Agent Teams documentation](https://code.claude.com/docs/en/agent-teams) states that `-p` and Agent SDK sessions do not spawn teammates; `agent_teams` is therefore `unsupported` for this adapter's invocation. Internal subagents remain separate from teams. Persistent sessions are `unsupported` because each run starts a disposable container.
