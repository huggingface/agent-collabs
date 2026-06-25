#!/usr/bin/env python3
"""Bootstrap (or reconfigure) one agent-collab challenge from challenge.yaml.

    export HF_TOKEN=hf_...        # fine-grained, scoped to BOTH orgs;
                                  # job.write on the challenge org if jobs on
    python bootstrap/init_challenge.py [--config challenge.yaml] [--skip-wait]

Idempotent — safe to re-run after editing challenge.yaml: buckets are created
with exist_ok, code uploads overwrite, Space variables are upserted, and the
central-bucket README is only (re)written with --write-readme or when absent.

What it does, in order:
  1. validate the config + token (whoami)
  2. create the central bucket (challenge org) and the PRIVATE audit bucket
     (admin org)
  3. create the Spaces (Docker SDK) and upload backend/ and dashboard/
     (the dashboard Space card gets `hf_oauth_authorized_org: <org>`), plus
     eval-space/ into the admin org when verification.mode is eval-space
  4. write Space variables (from challenge.yaml) + the HF_TOKEN secret
  5. seed the central bucket: README.md (agent onboarding doc, generated),
     results/verification_status.json
  6. poll the Spaces' health endpoints

The inputs that can NOT be automated (collect them first, once):
  - create the two HF orgs: the challenge org (participants) and the admin
    org (organizers only — audit bucket, eval Space)
  - mint a FINE-GRAINED token scoped to both orgs — it is stored as a secret
    on the Spaces, so keep its scope minimal
  - create a challenge-org invite link (org page → Settings → Members →
    Share invite link) for challenge.dashboard.invite_url
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from central_readme import build_central_readme
from huggingface_hub import (
    add_space_secret,
    add_space_variable,
    batch_bucket_files,
    create_bucket,
    create_repo,
    get_token,
    list_bucket_tree,
    space_info,
    upload_folder,
    whoami,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ───────────────────────── config ─────────────────────────


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    problems = []
    ch = cfg.get("challenge") or {}
    for key in ("org", "slug", "title"):
        if not ch.get(key):
            problems.append(f"challenge.{key} is required")
    st = cfg.get("storage") or {}
    sp = cfg.get("spaces") or {}
    sc = cfg.get("scoring") or {}
    if sc.get("order") not in (None, "asc", "desc"):
        problems.append("scoring.order must be 'asc' or 'desc'")
    ver = cfg.get("verification") or {}
    mode = ver.get("mode", "manual")
    if mode not in ("manual", "eval-space", "jobs"):
        problems.append("verification.mode must be manual, eval-space, or jobs")
    if mode == "jobs" and not (cfg.get("jobs") or {}).get("enabled"):
        problems.append("verification.mode: jobs requires jobs.enabled")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        sys.exit(f"invalid config: {path}")
    # defaults
    ch.setdefault("admin_org", f"{ch['org']}-admin")
    st.setdefault("central_bucket", f"{ch['org']}/{ch['slug']}-main-bucket")
    # The audit bucket lives in the ADMIN org: a fine-grained org-scoped token
    # covers it, and participants (challenge-org members) can never read it.
    st.setdefault("audit_bucket", f"{ch['admin_org']}/{ch['slug']}-audit")
    sp.setdefault("backend", f"{ch['org']}/{ch['slug']}-bucket-sync")
    sp.setdefault("dashboard", f"{ch['org']}/{ch['slug']}-dashboard")
    sp.setdefault("eval", f"{ch['admin_org']}/{ch['slug']}-eval")
    ver.setdefault("mode", "manual")
    cfg["storage"], cfg["spaces"], cfg["verification"] = st, sp, ver
    if st["audit_bucket"].split("/")[0] == ch["org"]:
        print(
            "  ⚠ storage.audit_bucket is inside the CHALLENGE org — participants "
            "may be able to read audit records (caller IPs) and any private eval "
            "data. Recommended: keep it in the admin org "
            f"({ch['admin_org']}/{ch['slug']}-audit)."
        )
    return cfg


def resolve_token() -> str:
    # Explicit beats ambient: HF_TOKEN env var, then the repo's .env
    # (gitignored — the deliberate hand-over file for agent-driven setups),
    # then the standard HF token file (`hf auth login`). A stale broad token
    # cached by an earlier login must not shadow a fine-grained token the
    # user explicitly provided in .env.
    import os

    token = os.environ.get("HF_TOKEN")
    if not token:
        env_file = REPO_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key.strip() == "HF_TOKEN":
                    token = value.strip().strip("'\"")
                    break
    if not token:
        token = get_token()
    if not token:
        sys.exit(
            "no HF token found. Provide one via `hf auth login`, or write\n"
            "  HF_TOKEN=hf_...\n"
            f"to {REPO_ROOT / '.env'} (gitignored)."
        )
    return token


# ───────────────────────── env-var mapping ─────────────────────────


def backend_variables(cfg: dict) -> dict[str, str]:
    ch, st = cfg["challenge"], cfg["storage"]
    sc = cfg.get("scoring") or {}
    jobs = cfg.get("jobs") or {}
    ver = cfg.get("verification") or {}
    # Only jobs-mode verification involves the backend (it launches the
    # re-run); eval-space and manual modes write the index out-of-band.
    jobs_verifier = ver.get("mode") == "jobs"
    required = sc.get("required_fields") or ["score", "method", "status", "description"]
    out = {
        "ORG": ch["org"],
        "COLLAB_SLUG": ch["slug"],
        "CENTRAL_BUCKET": st["central_bucket"],
        "AUDIT_BUCKET": st["audit_bucket"],
        "SCORE_FIELD": sc.get("score_field", "score"),
        "SCORE_UNIT": sc.get("score_unit", "points"),
        "SCORE_ORDER": sc.get("order", "desc"),
        "REQUIRED_RESULT_FIELDS": ",".join(required),
        "JOBS_ENABLED": str(bool(jobs.get("enabled"))).lower(),
        "VERIFIER_ENABLED": str(jobs_verifier).lower(),
    }
    if jobs.get("enabled"):
        out.update(
            {
                "JOB_IMAGE": str(jobs.get("image", "python:3.12")),
                "JOB_FLAVOR": str(jobs.get("flavor", "a10g-small")),
                "JOB_TIMEOUT_MINUTES": str(jobs.get("timeout_minutes", 40)),
                "HARNESS_PREFIX": str(jobs.get("harness_prefix", "shared_resources/harness")),
                "JOB_HARNESS_ENTRYPOINT": str(jobs.get("harness_entrypoint", "run.py")),
                "JOB_EXTRA_ARGS": json.dumps([str(a) for a in jobs.get("extra_args") or []]),
                "JOB_PER_AGENT_PER_DAY": str(jobs.get("per_agent_per_day", 10)),
                "JOB_PER_USER_PER_DAY": str(jobs.get("per_user_per_day", 30)),
            }
        )
    if jobs_verifier:
        out.update(
            {
                "VERIFIER_AGENT": str(ver.get("agent", "")),
                "VERIFIER_SCORE_TOL": str(ver.get("score_tol", 0.05)),
                "VERIFIER_GUARD_FIELD": str(ver.get("guard_field", "")),
                "VERIFIER_GUARD_CAP": str(ver.get("guard_cap", 0.0)),
            }
        )
    return out


def eval_space_variables(cfg: dict, backend_url: str) -> dict[str, str]:
    return {
        "BACKEND_API_URL": backend_url,
        "CENTRAL_BUCKET": cfg["storage"]["central_bucket"],
        "EVAL_POLL_S": str((cfg.get("verification") or {}).get("eval_poll_s", 60)),
    }


def dashboard_variables(cfg: dict, backend_url: str) -> dict[str, str]:
    ch, st = cfg["challenge"], cfg["storage"]
    sc = cfg.get("scoring") or {}
    dash = cfg.get("dashboard") or {}
    return {
        "ORG": ch["org"],
        "BUCKET": st["central_bucket"],
        "CHALLENGE_TITLE": ch["title"],
        "CHALLENGE_TAGLINE": str(ch.get("tagline", "")).strip(),
        "SCORE_FIELD": sc.get("score_field", "score"),
        "SCORE_LABEL": sc.get("score_label", "Score"),
        "SCORE_UNIT": sc.get("score_unit", "points"),
        "SCORE_ORDER": sc.get("order", "desc"),
        "SECONDARY_FIELD": str(sc.get("secondary_field", "") or ""),
        "SECONDARY_LABEL": str(sc.get("secondary_label", "") or ""),
        "INVITE_URL": str(dash.get("invite_url", "") or ""),
        "BACKEND_API_URL": backend_url,
    }


# ───────────────────────── helpers ─────────────────────────


def bucket_has(bucket: str, path: str, token: str) -> bool:
    try:
        for e in list_bucket_tree(bucket_id=bucket, prefix=path, token=token):
            if getattr(e, "path", None) == path:
                return True
    except Exception:
        pass
    return False


def space_url(repo_id: str, token: str) -> str:
    try:
        sub = space_info(repo_id, token=token).subdomain
        if sub:
            return f"https://{sub}.hf.space"
    except Exception:
        pass
    return "https://" + repo_id.replace("/", "-").replace("_", "-").replace(".", "-").lower() + ".hf.space"


def upload_dashboard(repo_id: str, cfg: dict, token: str) -> None:
    """Upload dashboard/ with a challenge-specific Space card: OAuth gated to
    the challenge org, and title/short_description from challenge.yaml — the
    dashboard is the one Space carrying the `agent-collab` discovery tag, so
    its card is what directories/meta-spaces display."""
    import re

    ch = cfg["challenge"]
    title = ch["title"]
    short = str(ch.get("short_description") or "")[:60] or f"Agent collab: {title}"[:60]
    src = REPO_ROOT / "dashboard"
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "dashboard"
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        card = dst / "README.md"
        text = card.read_text().replace(
            "hf_oauth_authorized_org: REPLACED_BY_BOOTSTRAP",
            f"hf_oauth_authorized_org: {ch['org']}",
        )
        text = re.sub(r"^title: .*$", f"title: {json.dumps(title)}", text, count=1, flags=re.M)
        text = re.sub(
            r"^short_description: .*$",
            f"short_description: {json.dumps(short)}",
            text, count=1, flags=re.M,
        )
        card.write_text(text)
        upload_folder(repo_id=repo_id, repo_type="space", folder_path=str(dst), token=token)


def wait_healthy(url: str, path: str, *, token: str | None = None, timeout_s: int = 600) -> bool:
    # `token` is needed for PRIVATE Spaces (the eval space): their *.hf.space
    # endpoint requires bearer auth.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}{path}", timeout=10, follow_redirects=True, headers=headers)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(10)
    return False


# ───────────────────────── main ─────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(REPO_ROOT / "challenge.yaml"))
    ap.add_argument("--write-readme", action="store_true",
                    help="(re)write the central bucket README even if it exists")
    ap.add_argument("--skip-wait", action="store_true",
                    help="don't poll the Spaces for health after deploy")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    token = resolve_token()
    ch, st, sp = cfg["challenge"], cfg["storage"], cfg["spaces"]
    ver = cfg["verification"]

    me = whoami(token=token)
    user = me.get("name") if isinstance(me, dict) else None
    orgs = {o.get("name") for o in (me.get("orgs") or []) if isinstance(o, dict)} if isinstance(me, dict) else set()
    print(f"token: {user} (orgs: {', '.join(sorted(orgs)) or 'none'})")
    auth = (me.get("auth") or {}).get("accessToken") or {} if isinstance(me, dict) else {}
    role = auth.get("role")
    if role in ("write", "read"):
        print(
            f"  ⚠ this is a broad personal '{role}' token. It will be stored as a "
            "secret on the Spaces — use a FINE-GRAINED token scoped to the two "
            "challenge orgs instead (see SETUP.md step 0.2)."
        )
    # Fine-grained tokens: fail BEFORE creating anything if either org is
    # missing from the token's scopes (e.g. a similarly-named org was
    # selected by mistake in the token UI).
    scoped = (auth.get("fineGrained") or {}).get("scoped") or []
    if scoped:
        writable = {
            s.get("entity", {}).get("name")
            for s in scoped
            if "repo.write" in (s.get("permissions") or [])
        }
        missing = [o for o in (ch["org"], ch["admin_org"]) if o not in writable]
        if missing:
            sys.exit(
                f"the fine-grained token has no write scope on: {', '.join(missing)}\n"
                f"(it is scoped to: {', '.join(sorted(n for n in writable if n)) or 'nothing'})\n"
                "→ edit the token at https://huggingface.co/settings/tokens and add "
                "write access for the missing org(s)."
            )
    for org, role in ((ch["org"], "challenge"), (ch["admin_org"], "admin")):
        if org not in orgs:
            print(f"  ⚠ token user is not visibly a member of the {role} org "
                  f"'{org}' — continuing, but creation there may fail "
                  "(create the org at https://huggingface.co/organizations/new)")
    print(f"verification mode: {ver['mode']}")
    if not (cfg.get("dashboard") or {}).get("invite_url"):
        print("  ⚠ dashboard.invite_url is empty — the join modal will skip the "
              "org-invite step. Create one (org → Settings → Members → Share "
              "invite link), add it to challenge.yaml, and re-run.")

    # 1 ── buckets
    print(f"central bucket  {st['central_bucket']}")
    create_bucket(st["central_bucket"], exist_ok=True, token=token)
    print(f"audit bucket    {st['audit_bucket']} (private, admin org)")
    try:
        create_bucket(st["audit_bucket"], private=True, exist_ok=True, token=token)
    except Exception as exc:
        sys.exit(
            f"could not create {st['audit_bucket']}: {exc}\n"
            f"→ does the admin org '{st['audit_bucket'].split('/')[0]}' exist, "
            "and is the token scoped to it?"
        )

    # 2 ── spaces: create + upload code
    for repo_id in (sp["backend"], sp["dashboard"]):
        create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True, token=token)
    print(f"backend space   {sp['backend']}: uploading code")
    upload_folder(
        repo_id=sp["backend"], repo_type="space", folder_path=str(REPO_ROOT / "backend"),
        ignore_patterns=["__pycache__/**", "*.pyc", ".pytest_cache/**"], token=token,
    )
    print(f"dashboard space {sp['dashboard']}: uploading code (oauth org = {ch['org']})")
    upload_dashboard(sp["dashboard"], cfg, token)

    backend_url = space_url(sp["backend"], token)
    dashboard_url = space_url(sp["dashboard"], token)

    if ver["mode"] == "eval-space":
        print(f"eval space      {sp['eval']}: uploading code (private, admin org)")
        create_repo(sp["eval"], repo_type="space", space_sdk="docker",
                    private=True, exist_ok=True, token=token)
        upload_folder(
            repo_id=sp["eval"], repo_type="space",
            folder_path=str(REPO_ROOT / "eval-space"),
            ignore_patterns=["__pycache__/**", "*.pyc"], token=token,
        )
        for k, v in eval_space_variables(cfg, backend_url).items():
            add_space_variable(sp["eval"], k, v, token=token)
        add_space_secret(sp["eval"], "HF_TOKEN", token, token=token,
                         description="writes verification verdicts to the central bucket")

    # 3 ── variables + secrets (upserts; setting them restarts the Space)
    print("backend variables:")
    for k, v in backend_variables(cfg).items():
        print(f"  {k}={v}")
        add_space_variable(sp["backend"], k, v, token=token)
    print("dashboard variables:")
    for k, v in dashboard_variables(cfg, backend_url).items():
        print(f"  {k}={v if k != 'CHALLENGE_TAGLINE' else v[:60] + '…' if len(v) > 60 else v}")
        add_space_variable(sp["dashboard"], k, v, token=token)
    for repo_id in (sp["backend"], sp["dashboard"]):
        add_space_secret(repo_id, "HF_TOKEN", token, token=token,
                         description="org-admin token; writes the central bucket")

    # 4 ── seed the central bucket
    seeds: list[tuple[bytes, str]] = []
    if args.write_readme or not bucket_has(st["central_bucket"], "README.md", token):
        seeds.append(
            (build_central_readme(cfg, backend_url, dashboard_url).encode(), "README.md")
        )
    if not bucket_has(st["central_bucket"], "results/verification_status.json", token):
        seeds.append((b"{}\n", "results/verification_status.json"))
    # the trace-sharing client agents download from the central bucket — always
    # refreshed so it tracks the repo (see backend/TRACES_DESIGN.md).
    seeds.append(
        ((REPO_ROOT / "backend" / "clients" / "share_trace.py").read_bytes(),
         "clients/share_trace.py")
    )
    if seeds:
        print(f"seeding central bucket: {', '.join(p for _, p in seeds)}")
        batch_bucket_files(st["central_bucket"], add=seeds, token=token)

    # 5 ── health checks
    print(f"\nbackend:    {backend_url}")
    print(f"dashboard:  {dashboard_url}")
    if ver["mode"] == "eval-space":
        print(f"eval space: https://huggingface.co/spaces/{sp['eval']} (private)")
    if not args.skip_wait:
        print("waiting for the Spaces to build (first build takes a few minutes)…")
        checks = [("backend", backend_url, "/v1/healthz", None),
                  ("dashboard", dashboard_url, "/api/health", None)]
        if ver["mode"] == "eval-space":
            checks.append(("eval", space_url(sp["eval"], token), "/healthz", token))
        ok = True
        for name, url, path, tok in checks:
            healthy = wait_healthy(url, path, token=tok)
            print(f"  {name:9s} {path:12s} {'✓ ok' if healthy else '✗ TIMED OUT — check the Space logs'}")
            ok = ok and healthy
        if not ok:
            return 1

    remaining = []
    if not (cfg.get("dashboard") or {}).get("invite_url"):
        remaining.append(
            "create an org invite link, put it in challenge.yaml → "
            "dashboard.invite_url, and re-run this script (cheap — variables only)"
        )
    if (cfg.get("jobs") or {}).get("enabled"):
        remaining.append(
            "grant org contributors Jobs *read* (NOT write) so they can view their jobs"
        )
        harness = (cfg.get("jobs") or {}).get("harness_prefix", "shared_resources/harness")
        remaining.append(
            f"upload the job harness to {st['central_bucket']}/{harness}/"
        )
    if ver["mode"] == "jobs":
        remaining.append(
            "upload the private eval set to the audit bucket under "
            "eval_dataset/, and register the verifier agent"
        )
    if ver["mode"] == "eval-space":
        remaining.append(
            "implement evaluate() in eval-space/evaluator.py and re-run this "
            "script to deploy it (until then, all results stay pending)"
        )
    print("\ndone.")
    if remaining:
        print(f"remaining manual steps (org settings on huggingface.co/{ch['org']}):")
        for step in remaining:
            print(f"  - {step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
