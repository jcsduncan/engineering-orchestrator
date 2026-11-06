# ADR 004: Opt-in GitHub issue intake

Status: Accepted

## Decision

An issue is eligible only while it is open and labelled `agent:run` in a configured repository. A GitHub App webhook prompts intake; a periodic scan reconciles missed events. Both paths claim work through one durable, idempotent operation. The issue is snapshotted at claim time. Reruns require an explicit new generation.

## Reason

Issue creation alone is too broad a trigger. Webhooks provide prompt notification but can be missed or arrive out of order. A label controlled by repository collaborators gives a visible authorization signal; reconciliation and a unique claim key prevent gaps and duplicates.

## Consequence

The webhook handler verifies the GitHub signature and persists the delivery before acknowledging it. Removing the label or closing the issue cancels unfinished work. Issue edits do not mutate an active run.

References: [GitHub webhook practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks), [signature verification](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).
