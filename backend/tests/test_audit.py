import json
import logging
from datetime import datetime, timezone

import app.audit as audit_mod
from app.audit import AuditLogger, event_name
from fakes import seed_agent


def audit_objects(env) -> dict[str, dict]:
    return {
        p: json.loads(b)
        for p, b in env.hub.buckets[env.settings.audit_bucket].items()
        if p.startswith("audit/")
    }


def test_event_name_is_a_filename_safe_route_slug():
    assert event_name("/v1/messages") == "messages"
    assert event_name("/v1/jobs:run") == "jobs-run"
    assert event_name("/v1/channels/{name}/subscribe") == "channels-name-subscribe"


def test_one_object_per_event_with_distinct_names_even_in_the_same_ms(env, monkeypatch):
    frozen = datetime(2026, 9, 29, 12, 0, 0, 123000, tzinfo=timezone.utc)
    monkeypatch.setattr(audit_mod, "utc_now", lambda: frozen)
    logger = AuditLogger(env.hub)
    for i in range(3):
        logger.write(
            agent_id=f"agent-{i}", route="/v1/messages", via="raw", source=None,
            target_path=None, bytes_count=i, status_code=201,
        )
    objs = audit_objects(env)
    assert sorted(objs) == [
        "audit/202609/20260929-120000-123_messages.json",
        "audit/202609/20260929-120000-124_messages.json",
        "audit/202609/20260929-120000-125_messages.json",
    ]
    assert [objs[p]["agent_id"] for p in sorted(objs)] == ["agent-0", "agent-1", "agent-2"]


def test_each_post_writes_its_own_audit_object(env):
    seed_agent(env.hub, "agent-1")
    for body in ("one", "two"):
        r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": body})
        assert r.status_code == 201
    objs = audit_objects(env)
    assert len(objs) == 2
    assert all(p.endswith("_messages.json") for p in objs)
    assert {o["route"] for o in objs.values()} == {"/v1/messages"}


def test_failed_audit_write_does_not_fail_the_request(env, monkeypatch, caplog):
    seed_agent(env.hub, "agent-1")
    write_audit = env.hub.write_bytes_audit

    def failing_audit_write(path, data):
        env.hub.fail_next_write()  # only the audit write fails, not the message
        return write_audit(path, data)

    monkeypatch.setattr(env.hub, "write_bytes_audit", failing_audit_write)
    with caplog.at_level(logging.WARNING, logger="app.audit"):
        r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "hi"})
    assert r.status_code == 201
    assert f"message_board/{r.json()['filename']}" in env.hub.buckets[env.settings.central_bucket]
    assert audit_objects(env) == {}
    assert any(
        rec.levelno == logging.WARNING and "messages" in rec.getMessage()
        for rec in caplog.records
    )
