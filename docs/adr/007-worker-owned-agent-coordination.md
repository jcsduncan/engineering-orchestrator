# ADR 007: Worker-owned agent coordination

Status: Accepted

## Decision

The controller schedules one worker runtime per engineering run. The runtime owns any internal agent roles, delegation, messages, and task lists. The controller does not create or persist those internal agents. A worker returns one result through the existing `WorkerProvider` contract, whether it used one agent or several.

## Reason

Agent runtimes differ in how they coordinate agents. Keeping that coordination inside the selected runtime preserves a stable run contract and avoids coupling task state to a vendor's agent hierarchy.

## Consequence

The controller still owns run identity, isolation, limits, cancellation, validation evidence, and publication. A locally hosted planner remains independent of the worker runtime. Internal delegation is optional and must work in the runtime's configured execution mode; a run does not require it to complete.
