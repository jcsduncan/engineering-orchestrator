# High-level design

This document specifies the system boundary. [Runtime](runtime.md) describes the CLI's implemented behavior.

## Purpose

Turn a bounded engineering task into a reviewable code change or an evidence-backed report. Merge approval belongs to a human.

## Components

```mermaid
flowchart LR
    Task[Task source] --> Controller[Controller]
    Controller --> Planner[Local planning model]
    Controller --> Workspace[Per-run workspace]
    Workspace --> Worker[Coding worker]
    Workspace --> Checks[Validation]
    Controller --> Records[(Run records)]
    Controller --> Review[Review artifact]
```

| Component | Responsibility |
| --- | --- |
| Task source | Supplies the objective, repository, and acceptance criteria. |
| Controller | Owns run state, limits, retries, tool access, and publication. |
| Local planning model | Proposes steps, checks, and risks; its output is advisory. |
| Workspace | Pins the base commit and separates each run's files and processes. |
| Coding worker | Investigates and edits within the workspace. Its model is replaceable. |
| Validation | Runs explicit checks and records results against the final diff. |
| Review artifact | Presents a pull request or investigation report with findings, checks, and uncertainty. |

## Run contract

Input: task identity, task text, repository, acceptance criteria, base commit, execution limits, and validation commands. Repeated delivery of the same task must resolve to the same run ID.

The controller persists state before each external side effect. On restart, it reconciles the worktree and review artifact by run ID before retrying. Retries have explicit limits; cancellation stops the worker and records the partial result.

```text
accepted → planning → executing → validating → reviewable
                                  ↘ inconclusive
Any active state → failed | cancelled
```

The worker can propose actions but cannot grant itself publication permissions or approve its own work. It treats the plan as a hypothesis and checks it against repository evidence before editing.

Outcomes are a reviewable change, an inconclusive investigation, or a failure. An inconclusive result states what was inspected, what was tested, and what evidence is missing. A successful process exit alone is not a validated change.

## Evidence and review

Each result identifies the task, base commit, final diff or commit, changed files, worker output, check commands and exit codes, and unresolved risks. Validation runs after worker execution. A reviewable change requires at least one relevant check. The review artifact distinguishes tests that passed from tests that were not run.

Human review controls merge. Automated review may find defects but cannot replace that gate.

## Trust boundary

Task text, repository files, and model output are untrusted inputs. Code execution runs with bounded resources and without publication credentials. The controller uses a separate, scoped identity for repository writes. Execution and publication are distinct operations.
