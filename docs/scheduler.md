# Bounded scheduling

`RunQueue` stores immutable issue snapshots in the same SQLite database as intake and run records. Call `enqueue()` for each new `IssueClaim`, then `run_queued()` with a controller-owned callback that invokes `run_task` using the snapshot's run ID. The callback receives a cancellation predicate; it must stop its worker when that predicate becomes true and return the partial result. The worker, checks, worktree, and logs remain owned by the run controller.

Configure positive `max_running` and `max_per_repository` limits when opening the queue. Dispatch is ordered by insertion sequence and skips a repository at capacity so another eligible repository can start. `request_cancel()` cancels queued work immediately and signals running work durably. The next `run_queued()` call preserves queued items and marks interrupted running items `needs_review`; it does not repeat uncertain execution.

One scheduler loop holds an exclusive file lock. Queue mutations use SQLite transactions, so intake can add or cancel claims from another process. The callback must not receive GitHub publication credentials.
