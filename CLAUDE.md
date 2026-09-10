# CLAUDE.md

Guidance for Claude Code sessions opened inside `client-operational/`.

This is a PUBLIC package: a thin, typed client for the site-calc operational
planning server. Nothing here may reference private packages, private
repository paths, solver or algorithm internals, or private version numbers.
The README, model field descriptions, docstrings, examples and error messages
are the product; a change to any of them needs the fresh-reader review
described in the parent repository's `CLAUDE.md`.

## Shape

- `site_calc_operational/client.py`: `OperationalClient` (sync `httpx`),
  `plan_reservation`, `plan_day_ahead`, `health`, `get_run`, `list_runs`,
  `cancel_active`; BUSY retry policy; `Idempotency-Key` support.
- `site_calc_operational/exceptions.py`: one exception per server error code,
  built by `error_from_response`; transport and timeout failures wrapped.
- `site_calc_operational/models/`: request models are strict
  (`extra="forbid"`, server rules re-checked locally), response models are
  lenient (`extra="allow"`). `docs/WIRE.md` is the human-readable contract.

The wire is defined by the server. When the server's contract changes, update
the models, `docs/WIRE.md`, the README and the CHANGELOG together.

## Development commands

```
uv venv && uv sync --extra dev
uv run pytest                        # mocked, no server needed
uv run pytest -m production          # live server; needs SITE_CALC_OPERATIONAL_URL and _API_KEY
uv run ruff check site_calc_operational tests examples && uv run ruff format site_calc_operational tests examples
uv run mypy site_calc_operational
```

## Versioning

MAJOR.MINOR is locked to the operational server's: a client 0.2.x talks to a
server 0.2.x. Patch releases are additive. Two files carry the version:
`pyproject.toml` and `site_calc_operational/__init__.py`; update both.

## Submodule etiquette

This repo is a git submodule of a private monorepo. After landing changes
here, the parent's submodule pointer is bumped in a follow-up PR there.
