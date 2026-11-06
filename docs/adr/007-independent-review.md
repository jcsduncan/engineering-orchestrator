# ADR 007: Independent advisory review

Status: Accepted

## Decision

Review a validated implementation result in a separate controller step. The reviewer receives a bounded evidence snapshot, not workspace or publication tools. Persist severity and file/line findings against the validated diff hash. A review cannot approve or change the run outcome; human review controls merge.

## Reason

Separating review from execution prevents the worker from approving its own change. Diff binding prevents findings from being presented as current after code changes.

## Consequence

The controller may publish a validated change with review findings or an unavailable review, but must show that distinction. Review quality is measured against benchmark correctness and human adjudication.
