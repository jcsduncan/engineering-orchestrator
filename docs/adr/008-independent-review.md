# ADR 008: Independent advisory review

Status: Accepted

## Decision

Review a validated implementation result in a separate controller step. This reviewer is an external run-level validation step, not an agent within the worker runtime's hierarchy. It receives the worker runtime's final result as a bounded evidence snapshot, not workspace or publication tools. The controller does not inspect or coordinate the runtime's internal agents. Persist severity and file/line findings against the validated diff hash. A review cannot approve or change the run outcome; human review controls merge.

## Reason

Separating review from execution prevents the worker from approving its own change. Diff binding prevents findings from being presented as current after code changes.

## Consequence

The controller may publish a validated change with review findings or an unavailable review, but must show that distinction. Benchmark correctness provides a decision proxy; humans still adjudicate individual findings.
