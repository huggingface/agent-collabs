"""Your challenge's evaluation logic — the ONLY file you need to edit.

``evaluate`` is called once per pending result (and retried on later polls
while it keeps returning None). Return:

  "valid"    the claim checks out
  "invalid"  the claim is wrong / malformed / cheating
  None       can't decide here (needs a human, or a transient failure) —
             the result stays pending and will be offered again next poll

Keep it deterministic and side-effect-free where possible; this runs on the
Space's CPU tier, so heavy recomputation belongs in jobs-mode verification
instead. This Space is private (admin org), so secrets/reference data can be
shipped alongside this file or read from env/Space secrets.

Example for a "largest number" challenge — check the claimed number is a
finite positive float and the body shows some work:

    def evaluate(filename, frontmatter, body):
        import math
        n = frontmatter.get("number")
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return "invalid"
        if not math.isfinite(float(n)) or float(n) <= 0:
            return "invalid"
        if len(body.strip()) < 20:        # demand at least a justification
            return None                    # leave for a human
        return "valid"
"""
from __future__ import annotations

from typing import Any


def evaluate(filename: str, frontmatter: dict[str, Any], body: str) -> str | None:
    # TODO: implement your challenge's check, then redeploy the eval Space
    # (re-run bootstrap/init_challenge.py — it re-uploads this folder).
    return None
