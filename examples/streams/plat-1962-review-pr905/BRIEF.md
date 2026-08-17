---
stream: plat-1962-review-pr905
title: Review platform#905 — CloudSQLInstance composite
state: archived
owner: opus-review
updated: 2026-08-13T23:20:00Z
claims:
  repo: [acme/platform]
  branch: []
  worktree: []
  issue: [PLAT-1962]
  pr: [905]
---

## Goal

Review `acme/platform#905` (CloudSQLInstance composite, PLAT-1719 lineage) and reach a
disposition: approve, request changes, or close as stale. Part of the review backlog.

## Ground truth

![[ground-truth]]

## Decided

- 2026-08-13 — Close as superseded rather than review-and-merge. Why: every piece of the composite shipped on `main` under different names during the rest of the PLAT-1719 build-out; each commit traced to a live replacement — the `Datastore` XRD/Composition in `infra/compositions/datastore.{xrd,composition}.yaml`, merged as a1b2c3d4e / #1025, with claims live in every environment.
- 2026-08-13 — File no review findings. Why: nothing in the PR should land, and findings against a branch that will never merge create work with no target.
- 2026-08-13 — Any real gap discovered later (a `databaseFlags` shape or `status` field the composite exposed and `Datastore` does not) becomes a focused issue against `datastore.xrd.yaml`. Why: the branch is stacked on the stale base `feat/PLAT-1719/2`; reviving it costs more than reimplementing the gap.

## Open

None. Stream is closed.

## Next

Nothing. Reopen only if a `Datastore` gap traces back to this composite — see the third decision.

## Do not

- Revive this branch or its stack — stale base, superseded scope.
- Treat PLAT-1719 as one stream. It spans at least nine live branches; each independently-decidable piece is its own stream.
