# ADR 003: Execution and publication boundary

Status: Accepted

## Decision

Code execution occurs in a per-run workspace with bounded resources. Publication credentials remain in the controller, outside the worker environment. Validation runs against the worker's final files. Human review controls merge.

## Reason

Repository content and task text can influence worker commands. Separating execution from publication limits the effect of a bad command or injected instruction.

## Consequence

A Git worktree provides file separation; the CLI executes worker and validation commands in disposable containers. Programmatic host execution requires trusted inputs; see [runtime](../runtime.md).
