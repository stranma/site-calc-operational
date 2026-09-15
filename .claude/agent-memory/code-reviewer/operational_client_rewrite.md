---
name: operational-client-rewrite
description: client-operational's from-scratch rewrite (PR #9, branch feat/planning-client) targeting the new operational planning server -- what it replaced and how to cross-check it
metadata:
  type: project
---

`client-operational` (public package `site-calc-operational`) was rewritten from
scratch on branch `feat/planning-client` (PR stranma/site-calc-operational#9,
reviewed 2026-09-10) to talk to the new self-hosted operational planning server
(`server-onprem-operational`, contract in its `docs/SPEC.md` sections 3-5 and
`src/site_calc_onprem_operational/schemas/*.py` + `translation.py` +
`routers/{runs,health}.py`). It dropped the entire old surface: `OnPremClient`,
the legacy async SaaS `OperationalClient` job API, the reservation-bid /
device-planning models, the MCP server (20 tools), and the schema-mirroring
docs (`docs/MIRRORING.md`, `docs/SDK_PLAN.md`). New shape: one sync
`OperationalClient` (`httpx`) with `plan_reservation`, `plan_day_ahead`,
`health`, `get_run`, `list_runs`, `cancel_active`; request models strict
(`extra="forbid"`), response models lenient (`extra="allow"`); `docs/WIRE.md`
is the human wire reference.

**Why:** the old on-prem server this client's previous incarnation talked to
no longer exists (per its own CHANGELOG: "nothing was migrated"); this is a
clean-slate client for a genuinely different server.

**How to apply:** when reviewing further PRs against this branch/package,
the field-for-field cross-check (client model vs. `schemas/*.py`,
response model vs. `translation.py` dict-building functions and
`routers/runs.py` / `routers/health.py`) came back with **zero drift** on the
first rewrite -- treat any future divergence there as a real bug, not a style
nit. See [[client_operational_pypi_versions]] for the version-numbering
context on this same rewrite.
