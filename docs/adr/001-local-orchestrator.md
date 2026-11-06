# ADR 001: Local model for orchestration

Status: Accepted

## Decision

The controller calls a locally hosted model through an OpenAI-compatible API for planning. The coding worker is selected independently. Model output is data to validate, not authority over workflow state or credentials.

## Reason

Local orchestration keeps task routing under operator control and permits model changes without changing the task contract. Separating the worker lets coding quality be evaluated independently of planning quality.

## Consequence

The operator supplies the model server. Run records identify the model used. Malformed plans are rejected.
