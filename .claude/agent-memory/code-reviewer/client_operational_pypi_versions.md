---
name: client-operational-pypi-versions
description: only 0.1.0 of site-calc-operational is actually published on PyPI, despite git history containing release commits up to 0.3.0 -- check PyPI directly, not git log, before flagging a version bump as a downgrade
metadata:
  type: project
---

`site-calc-operational`'s git history has commits `release: v0.2.0`, `v0.2.1`,
`v0.3.0`, but only `v0.1.0` is an actual git tag, and `pypi.org/pypi/site-calc-operational/json`
confirms only `0.1.0` was ever published (`releases` dict has one key,
`0.1.0`). The 0.2.x/0.3.x "release" commits describe work that was never
actually shipped.

**Why:** this matters because PR stranma/site-calc-operational#9 (the
from-scratch rewrite, see [[operational_client_rewrite]]) resets the version to
`0.2.0` to match the operational server's own `0.2.x` line. That reads like a
downgrade from `0.3.1` if you only look at git log, but on PyPI it is a normal
forward bump from the one real release, `0.1.0` -- no existing installed users
would be stranded on a phantom `0.3.1`.

**How to apply:** before flagging any version-number change in a public
client package (`client-investment`, `client-operational`) as a downgrade or
compatibility risk, check the actual PyPI JSON API
(`https://pypi.org/pypi/<name>/json`) for the `releases` keys -- do not trust
git commit messages or CHANGELOG headers alone, they can describe releases
that were prepared but never published.
