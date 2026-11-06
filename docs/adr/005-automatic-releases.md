# ADR 005: Automatic release publication

Status: Accepted

## Decision

Human review gates code changes. Release Please creates or updates a release PR after a releasable change reaches main. A repository-scoped GitHub App creates that PR so its CI runs without manual approval. The release workflow enables squash auto-merge; required checks and unresolved review threads still gate the merge. A merged release PR produces the version tag, GitHub release, and GHCR image.

## Reason

Reviewing the code PR is the human release gate. A second manual approval for generated version and changelog changes adds delay without changing the code review decision. An App token avoids GitHub's approval prompt for PR workflows created by `GITHUB_TOKEN`.

## Consequence

The App has Contents and Pull requests write access only to this repository. Without its credentials, release PRs use the existing manual path. Failed checks or unresolved review threads prevent publication.
