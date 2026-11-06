# ADR 006: Diff-bound validation and delivery evidence

Status: Accepted

## Decision

Investigation tasks may end with an evidence-backed report; implementation tasks require a diff. Every validation result identifies its command, timing, exit status, and bounded output. The controller hashes the diff before and after validation and rejects evidence if the diff changed. Publication records the merged revision and, when deployed, the deployment run and smoke result.

## Reason

A successful command says little if it ran against different code. A merged PR also does not establish that a deployed system is healthy.

## Consequence

The controller owns validation and delivery records. Workers cannot mark their own results complete or substitute an earlier test run for the final diff.
