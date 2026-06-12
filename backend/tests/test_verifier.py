"""Automated verification on new SOTA (§5.7): unit + integration tests."""
import json

import pytest

from app.verification import DEFERRED, SKIPPED, WRITTEN
from app.verifier import compute_verdict
from fakes import seed_agent, seed_result


AUDIT = "auditor/test-audit"
SCRATCH = "test-org/test-agent-1"


@pytest.fixture
def venv(make_env):
    return make_env(
        VERIFIER_ENABLED="true",
        VERIFIER_GUARD_FIELD="ppl",
        VERIFIER_GUARD_CAP="2.42",
        VERIFIER_SCORE_TOL="0.05",
    )


def seed_submission(hub, bucket=SCRATCH, prefix="submissions/v1"):
    hub.seed(f"{prefix}/manifest.json", '{"serve": ["python", "serve.py"]}', bucket=bucket)
    hub.seed(f"{prefix}/serve.py", "print('hi')", bucket=bucket)


def post_result(env, *, score=200.0, artifacts="hf://buckets/test-org/test-agent-1/submissions/v1/", extra_fm=""):
    fm = f"---\nscore: {score}\nmethod: vllm-fp8\nstatus: agent-run\ndescription: fast\n"
    if artifacts is not None:
        fm += f"artifacts: {artifacts}\n"
    fm += extra_fm + "---\nnotes\n"
    env.hub.seed("results/run.md", fm, bucket=SCRATCH)
    r = env.client.post(
        "/v1/results",
        json={"source": f"hf://buckets/test-org/test-agent-1/results/run.md"},
    )
    assert r.status_code == 201, r.text
    return r.json()["filename"]


def central_index(env):
    raw = env.hub.buckets[env.settings.central_bucket].get(
        "results/verification_status.json"
    )
    return json.loads(raw) if raw else {}


def good_summary(launch_to_hub, score=199.0, ppl=2.30):
    """on_launch hook: simulate the completed job writing summary.json."""
    hub, = launch_to_hub
    def hook(launch):
        hub.seed(
            f"{launch['run_prefix']}/summary.json",
            json.dumps({"score": score, "ppl": ppl, "completed": 80}),
            bucket=AUDIT,
        )
    return hook


# ───────────────────────── should_verify (SOTA check) ─────────────────────────


def test_should_verify_cold_start_any_positive_score(venv):
    seed_agent(venv.hub, "agent-1")
    assert venv.verifier.should_verify("x_agent-1.md", {"status": "agent-run", "score": 1.0})


def test_should_verify_requires_beating_valid_champion(venv):
    seed_agent(venv.hub, "agent-1")
    champ = seed_result(venv.hub, "20260601-100000-000", "agent-1", 300.0)
    pending = seed_result(venv.hub, "20260601-110000-000", "agent-1", 500.0)
    venv.hub.seed(
        "results/verification_status.json",
        json.dumps({champ: "valid", pending: "pending"}),
    )
    fm = {"status": "agent-run", "score": 300.0}
    assert not venv.verifier.should_verify("y_agent-1.md", fm)  # tie: not strictly greater
    assert venv.verifier.should_verify("y_agent-1.md", {**fm, "score": 300.1})
    # the 500-score pending result does NOT raise the bar — only `valid` does
    assert venv.verifier.should_verify("y_agent-1.md", {**fm, "score": 301.0})


def test_should_verify_rejects_non_agent_run_and_bad_score(venv):
    assert not venv.verifier.should_verify("x.md", {"status": "negative", "score": 999.0})
    assert not venv.verifier.should_verify("x.md", {"status": "agent-run", "score": -1})
    assert not venv.verifier.should_verify("x.md", {"status": "agent-run", "score": True})
    assert not venv.verifier.should_verify("x.md", {"status": "agent-run"})


def test_should_verify_skips_decided_and_in_flight(venv):
    seed_agent(venv.hub, "agent-1")
    a = seed_result(venv.hub, "20260601-100000-000", "agent-1", 100.0)
    venv.hub.seed("results/verification_status.json", json.dumps({a: "invalid"}))
    fm = {"status": "agent-run", "score": 100.0}
    assert not venv.verifier.should_verify(a, fm)  # already decided
    venv.verifier._in_flight.add("b_agent-1.md")
    assert not venv.verifier.should_verify("b_agent-1.md", fm)  # in flight


# ───────────────────────── submission resolution ─────────────────────────


def test_resolve_direct_hf_uri(venv):
    seed_submission(venv.hub)
    loc = venv.verifier.resolve_submission(
        {"artifacts": "hf://buckets/test-org/test-agent-1/submissions/v1/"},
        "agent-1",
    )
    assert loc == (SCRATCH, "submissions/v1")


def test_resolve_artifacts_dir_is_central(venv):
    venv.hub.seed("artifacts/sub_agent-1/manifest.json", "{}")
    venv.hub.seed("artifacts/sub_agent-1/serve.py", "x")
    loc = venv.verifier.resolve_submission({"artifacts": "artifacts/sub_agent-1/"}, "agent-1")
    assert loc == (venv.settings.central_bucket, "artifacts/sub_agent-1")


def test_resolve_relative_path_is_scratch_bucket(venv):
    seed_submission(venv.hub)
    loc = venv.verifier.resolve_submission({"submission": "submissions/v1"}, "agent-1")
    assert loc == (SCRATCH, "submissions/v1")


def test_resolve_run_dir_via_run_request(venv):
    seed_submission(venv.hub)
    venv.hub.seed(
        "runs/7/run_request.json",
        json.dumps({"submission_bucket": SCRATCH, "submission_prefix": "submissions/v1"}),
        bucket=SCRATCH,
    )
    loc = venv.verifier.resolve_submission({"artifacts": "runs/7/"}, "agent-1")
    assert loc == (SCRATCH, "submissions/v1")


def test_resolve_run_dir_via_job_status(venv):
    seed_submission(venv.hub)
    venv.hub.seed(
        "runs/8/job_status.json",
        json.dumps({"submission_prefix": "submissions/v1", "status": "completed"}),
        bucket=SCRATCH,
    )
    loc = venv.verifier.resolve_submission({"artifacts": "runs/8/"}, "agent-1")
    assert loc == (SCRATCH, "submissions/v1")


def test_resolve_unresolvable(venv):
    assert venv.verifier.resolve_submission({"artifacts": "nowhere/"}, "agent-1") is None
    assert venv.verifier.resolve_submission({}, "agent-1") is None


# ───────────────────────── verdict computation ─────────────────────────


def _verdict_settings(**overrides):
    from app.config import Settings

    kwargs = dict(
        HF_TOKEN="test-token",
        ORG="test-org",
        COLLAB_SLUG="test",
        AUDIT_BUCKET="auditor/test-audit",
        VERIFIER_SCORE_TOL="0.05",
        VERIFIER_GUARD_FIELD="ppl",
        VERIFIER_GUARD_CAP="2.42",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_verdict_score_boundary():
    s = _verdict_settings()
    # exactly ±5% is valid; just past it is invalid (both directions)
    v, d = compute_verdict(s, 100.0, {"score": 95.0, "ppl": 2.0})
    assert v == "valid" and d["score_ok"]
    v, _ = compute_verdict(s, 100.0, {"score": 94.9, "ppl": 2.0})
    assert v == "invalid"
    v, _ = compute_verdict(s, 100.0, {"score": 105.1, "ppl": 2.0})
    assert v == "invalid"


def test_verdict_guard_boundary():
    s = _verdict_settings()
    v, _ = compute_verdict(s, 100.0, {"score": 100.0, "ppl": 2.42})
    assert v == "valid"
    v, d = compute_verdict(s, 100.0, {"score": 100.0, "ppl": 2.43})
    assert v == "invalid" and not d["guard_ok"]


def test_verdict_no_guard_configured_skips_guard_check():
    s = _verdict_settings(VERIFIER_GUARD_FIELD="")
    v, d = compute_verdict(s, 100.0, {"score": 100.0})
    assert v == "valid" and "guard_ok" not in d


def test_verdict_missing_fields_is_undecidable():
    s = _verdict_settings()
    assert compute_verdict(s, 100.0, {"ppl": 2.0})[0] is None
    assert compute_verdict(s, 100.0, {"score": 100.0})[0] is None
    assert compute_verdict(s, 100.0, {"score": "fast", "ppl": 2.0})[0] is None


# ───────────────────────── set_verdict CAS ─────────────────────────


def ledger_path(filename):
    return f"verification_runs/{filename}/verdict.json"


def test_set_verdict_writes_over_pending_and_records_ledger(venv):
    venv.hub.seed("results/verification_status.json", json.dumps({"a.md": "pending"}))
    assert venv.verification.set_verdict("a.md", "valid", by="ver", details={"rerun_score": 1.0}) == WRITTEN
    assert central_index(venv)["a.md"] == "valid"
    rec = json.loads(venv.hub.buckets[AUDIT][ledger_path("a.md")])
    assert rec["state"] == "valid" and rec["by"] == "ver" and rec["rerun_score"] == 1.0


def test_set_verdict_writes_when_absent(venv):
    assert venv.verification.set_verdict("a.md", "invalid", by="ver") == WRITTEN
    assert central_index(venv)["a.md"] == "invalid"


def test_set_verdict_overwrites_own_prior_verdict(venv):
    venv.hub.seed("results/verification_status.json", json.dumps({"a.md": "pending"}))
    venv.verification.set_verdict("a.md", "valid", by="ver")
    assert venv.verification.set_verdict("a.md", "invalid", by="ver") == WRITTEN
    assert central_index(venv)["a.md"] == "invalid"


def test_set_verdict_defers_to_human_verdict(venv):
    # no ledger record → this `invalid` must be human-authored
    venv.hub.seed("results/verification_status.json", json.dumps({"a.md": "invalid"}))
    assert venv.verification.set_verdict("a.md", "valid", by="ver") == DEFERRED
    assert central_index(venv)["a.md"] == "invalid"


def test_set_verdict_defers_when_human_changed_our_verdict(venv):
    venv.hub.seed("results/verification_status.json", json.dumps({"a.md": "pending"}))
    venv.verification.set_verdict("a.md", "valid", by="ver")
    # a human flips it to invalid out-of-band
    venv.hub.seed("results/verification_status.json", json.dumps({"a.md": "invalid"}))
    assert venv.verification.set_verdict("a.md", "valid", by="ver") == DEFERRED
    assert central_index(venv)["a.md"] == "invalid"


def test_set_verdict_skips_on_unreadable_index(venv):
    venv.hub.seed("results/verification_status.json", "{corrupt")
    assert venv.verification.set_verdict("a.md", "valid", by="ver") == SKIPPED
    assert venv.hub.buckets[AUDIT].get(ledger_path("a.md")) is None


# ───────────────────────── integration (API + fakes) ─────────────────────────


def test_sota_post_triggers_launch_verdict_and_announcement(venv):
    seed_agent(venv.hub, "agent-1")
    seed_submission(venv.hub)
    venv.runner.on_launch = good_summary([venv.hub], score=199.0, ppl=2.30)

    filename = post_result(venv, score=200.0)

    # launch happened against the resolved submission, /state pre-created
    assert len(venv.runner.launches) == 1
    launch = venv.runner.launches[0]
    assert launch["submission_bucket"] == SCRATCH
    assert launch["submission_prefix"] == "submissions/v1"
    run_prefix = launch["run_prefix"]
    assert run_prefix == f"verification_runs/{filename}"
    request = json.loads(venv.hub.buckets[AUDIT][f"{run_prefix}/verification_request.json"])
    assert request["reported_score"] == 200.0

    # verdict recorded: index flipped + private ledger record
    assert central_index(venv)[filename] == "valid"
    rec = json.loads(venv.hub.buckets[AUDIT][ledger_path(filename)])
    assert rec["state"] == "valid" and rec["job_id"] == "job-1"
    assert rec["rerun_score"] == 199.0 and rec["reported_score"] == 200.0

    # job artifacts landed in the private run dir
    assert f"{run_prefix}/job_logs.txt" in venv.hub.buckets[AUDIT]
    assert f"{run_prefix}/job_status.json" in venv.hub.buckets[AUDIT]

    # board announcement by the verifier, fanned out to the owner's inbox
    msgs = venv.client.get("/v1/messages?expand=true&limit=1").json()["items"]
    assert msgs and "VERIFIED VALID" in msgs[0]["body"]
    assert "@agent-1" in msgs[0]["body"]
    assert msgs[0]["frontmatter"]["agent"] == "test-verifier"
    assert msgs[0]["frontmatter"]["refs"] == [filename]
    inbox = [p for p in venv.hub.buckets[venv.settings.central_bucket] if p.startswith("inbox/agent-1/")]
    assert inbox

    # API view reflects the verdict
    assert venv.client.get(f"/v1/results/{filename}").json()["verification"] == "valid"


def test_non_sota_post_does_not_launch(venv):
    seed_agent(venv.hub, "agent-1")
    seed_submission(venv.hub)
    champ = seed_result(venv.hub, "20260601-100000-000", "agent-1", 500.0)
    venv.hub.seed("results/verification_status.json", json.dumps({champ: "valid"}))
    filename = post_result(venv, score=200.0)
    assert venv.runner.launches == []
    assert central_index(venv)[filename] == "pending"


def test_invalid_verdict_announced_with_failing_check(venv):
    seed_agent(venv.hub, "agent-1")
    seed_submission(venv.hub)
    venv.runner.on_launch = good_summary([venv.hub], score=150.0, ppl=2.30)  # Δ 25%

    filename = post_result(venv, score=200.0)

    assert central_index(venv)[filename] == "invalid"
    msgs = venv.client.get("/v1/messages?expand=true&limit=1").json()["items"]
    assert "INVALID" in msgs[0]["body"] and "❌" in msgs[0]["body"]


def test_unresolvable_submission_nudges_and_stays_pending(venv):
    seed_agent(venv.hub, "agent-1")
    filename = post_result(venv, score=200.0, artifacts="nowhere/")
    assert venv.runner.launches == []
    assert central_index(venv)[filename] == "pending"
    msgs = venv.client.get("/v1/messages?expand=true&limit=1").json()["items"]
    assert msgs and "couldn't" in msgs[0]["body"] and "runnable submission" in msgs[0]["body"]


def test_failed_job_leaves_pending_no_announcement(venv):
    seed_agent(venv.hub, "agent-1")
    seed_submission(venv.hub)
    venv.runner.terminal = ("error", "ERROR", "boom")
    filename = post_result(venv, score=200.0)
    assert len(venv.runner.launches) == 1
    assert central_index(venv)[filename] == "pending"
    assert venv.client.get("/v1/messages").json()["items"] == []
    # single-flight marker released → a later event may retry
    assert filename not in venv.verifier._in_flight


def test_disabled_verifier_is_inert(env):
    # default env: VERIFIER_ENABLED=false
    seed_agent(env.hub, "agent-1")
    seed_submission(env.hub)
    post_result(env, score=200.0)
    assert env.runner.launches == []
    assert env.client.get("/v1/messages").json()["items"] == []
