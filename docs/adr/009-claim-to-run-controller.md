# ADR 009: Claim-to-run controller

Status: Accepted

## Decision

A controller transfers each active issue claim into the durable run queue and acknowledges it in one transaction. It dispatches only claims that remain active, invokes one configured worker runtime per run ID, and finalizes the issue claim from a persisted queue outcome. Issue revocation signals cancellation to the active run. Interrupted runs retain their capacity reservation until their process or container is confirmed stopped.

## Reason

Webhook delivery, queueing, execution, and claim completion can each be interrupted. A stable run ID and replayable state transitions prevent duplicate work and avoid starting a revoked claim.

## Consequence

Repository paths, worker commands, models, checks, and limits come from trusted controller configuration. Issue text supplies task data only. The controller never addresses agents inside the worker runtime. Publication remains a separate controller operation with its own credentials.
