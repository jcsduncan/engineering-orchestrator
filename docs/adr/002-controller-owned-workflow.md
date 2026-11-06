# ADR 002: Controller-owned workflow

Status: Accepted

## Decision

The controller owns task identity, state transitions, retry limits, validation, and result publication. A model may propose steps but cannot declare a run complete or approve a change. Every external side effect is associated with a run ID and tolerates retry.

## Reason

Agent output is probabilistic; scheduling and recovery require explicit, inspectable state. Idempotent side effects prevent duplicate branches and review artifacts after interruption.

## Consequence

Run records are part of the system contract. Completion requires recorded evidence; an inconclusive investigation is a valid outcome.
