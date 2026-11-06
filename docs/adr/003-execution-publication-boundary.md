# ADR 003: Execution and publication boundary

Status: Accepted

## Decision

Code execution occurs in a per-run workspace with bounded resources. Publication credentials remain in the controller, outside the worker environment. Validation runs against the worker's final files. Publication uses a separate command and a GitHub App token scoped to one repository. Human review controls merge.

## Reason

Repository content and task text can influence worker commands. Separating execution from publication limits the effect of a bad command or injected instruction.

## Consequence

A Git worktree provides file separation; the CLI executes worker and validation commands in disposable containers. The publisher rechecks the validated diff, then pushes and finds or creates a PR by run ID. Programmatic host execution requires trusted inputs; see [runtime](../runtime.md).
