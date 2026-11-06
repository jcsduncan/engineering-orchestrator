# Engineering Orchestrator

The [benchmark suite](benchmarks.md) runs reproducible fixture tasks and retains evidence for comparing controller and model changes.

Without a worker, the CLI prints a plan from a local model. With a worker, it creates a Git worktree, runs the worker and validation commands, and records the result.

- [System design](HLD.md)
- [Runtime and configuration](runtime.md)
- [Architecture decisions](adr/README.md)
- [Repository tooling](tooling.md)
- [Coding standards](standards.md)
