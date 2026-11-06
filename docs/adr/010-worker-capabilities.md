# ADR 010: Version-bound worker capabilities

Status: Accepted

## Decision

Select `single`, `subagents`, or `agent_teams` as an execution mode for one worker runtime. A trusted worker profile records the runtime version, immutable image ID, command hash, headless invocation, and a supported, unsupported, or unverified state for each capability with probe evidence for supported claims. The controller checks the requested mode, command, and local image ID before accepting an issue claim, dispatches with that immutable image ID, then binds the selected profile to the run snapshot. Direct provider calls also check the worker's recorded image and command identity. Unknown or unsupported modes cannot run. Existing command workers retain the single-worker default.

## Reason

Agent runtimes and invocation modes expose different capabilities. A model name or command string does not prove that internal delegation works in a packaged image.

## Consequence

The controller validates the runtime boundary but never manages agents inside it. Changing a profile or image requires a new run ID. Interactive-only team modes need a separate bounded invocation adapter.
