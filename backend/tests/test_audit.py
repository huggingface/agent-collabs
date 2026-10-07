import json

import pytest

from app.audit import AuditLogger
from app.config import Settings
from fakes import FakeHub


@pytest.mark.parametrize(
    "org, audit_bucket, in_org",
    [
        ("test-org", "test-org/test-audit", True),
        ("test-org", "test-org-admin/test-audit", False),
        # Hub namespaces are case-insensitive: these are all the same org.
        ("test-org", "Test-Org/test-audit", True),
        ("test-org", "TEST-ORG/test-audit", True),
        ("Test-Org", "test-org/test-audit", True),
        ("test-org", "Test-Org-Admin/test-audit", False),
    ],
)
def test_caller_ip_omitted_when_audit_bucket_is_in_challenge_org(org, audit_bucket, in_org):
    settings = Settings(ORG=org, COLLAB_SLUG="test", AUDIT_BUCKET=audit_bucket)
    assert settings.audit_bucket_in_org is in_org
    hub = FakeHub(settings)
    AuditLogger(hub, record_client=not settings.audit_bucket_in_org).write(
        agent_id="agent-1", route="/v1/sync", via=None, source=None,
        target_path=None, bytes_count=0, status_code=200,
        caller_ip="203.0.113.7", user_agent="curl/8",
    )
    (log,) = hub.buckets[audit_bucket].values()
    record = json.loads(log)
    assert record["agent_id"] == "agent-1"
    assert ("caller_ip" in record) is not in_org
    assert ("user_agent" in record) is not in_org
