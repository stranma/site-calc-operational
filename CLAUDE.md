# CLAUDE.md

Guidance for Claude Code sessions opened inside `client-operational/`.

## Schema mirroring

`site_calc_operational/models/` hand-mirrors selected Pydantic models from
`server-onprem/src/site_calc_onprem/schemas.py` and selected dataclasses from
`site_calc.domain.devices.*` / `site_calc.domain.ans.*`. Before editing
anything in `models/`, or when an upstream wire schema changes in the parent
repo, **read `docs/MIRRORING.md`** -- it lists every mirror, the
drift-detection tests, and the sync procedure.

Drift is pinned by:

- `tests/test_reservation_bid_models.py::test_server_wire_field_parity`
- `tests/test_device_property_models.py::test_property_field_parity_with_server_translator`

A failing parity test means the upstream changed and the client mirror has
not caught up. Don't relax the assertion -- update the mirror.

## Development commands

```
uv venv && uv sync --extra dev --extra mcp
uv run pytest                                  # full suite
uv run ruff check site_calc_operational tests
uv run ruff format site_calc_operational tests
```

Tests use `respx` to mock the on-prem HTTP layer; no live server needed.

## Versioning

The package versions independently from the parent monorepo's MAJOR.MINOR
stream (the v0.1.0 CHANGELOG entry explains why). Bump rules:

- Patch (`0.x.y -> 0.x.(y+1)`): additive (new models, new methods, new
  optional fields). Existing callers don't change.
- Minor (`0.x.y -> 0.(x+1).0`): breaking (renamed field, removed method,
  changed method signature).

Two files carry the version: `pyproject.toml` and
`site_calc_operational/__init__.py`. Update both.

## Submodule etiquette

This repo is a git submodule of the parent `site-calc` monorepo. After
landing changes here, bump the parent's submodule pointer in a follow-up PR
on the parent (see the parent `CLAUDE.md` for the submodule workflow).
