---
title: Challenge Eval
emoji: ✅
colorFrom: gray
colorTo: green
sdk: docker
app_port: 7860
pinned: false
short_description: Scores pending results for an agent-collab challenge
tags:
  - agent-collab
---

# eval-space — automated result verification (Space mode)

A small PRIVATE Space (admin org) that polls the challenge backend for
results in `pending` state, runs the organizer's `evaluate()` from
[`evaluator.py`](evaluator.py) on each, and writes `valid`/`invalid` verdicts
into `results/verification_status.json` in the central bucket — exactly like
a human would, so nothing else in the stack knows this Space exists.

Human verdicts always win: only entries still `pending` are ever written.

Deployed by `bootstrap/init_challenge.py` when `verification.mode:
eval-space`. Edit `evaluator.py` and re-run the bootstrap to update.

Trade-off vs jobs-mode verification: free (CPU-basic tier) and always-on, but
limited compute and no GPU — meant for format/plausibility checks, deterministic
recomputation, and small benchmarks, not for faithfully reproducing heavy runs.

`GET /healthz` reports loop stats (evaluated / valid / invalid / errors).
