# Contributing

Run `uv sync --locked --dev`, then `uv run pre-commit install`. Before opening a pull request, run `uv run pre-commit run --all-files`, `uv run pytest -q`, and `uv run mkdocs build --strict`.

Use Conventional Commit subjects such as `fix: reject invalid tasks` or `feat: add issue intake`. Release Please derives versions from these subjects.
