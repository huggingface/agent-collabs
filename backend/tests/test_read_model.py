import json

from app.config import Settings
from app.read_model import ReadModel
from fakes import FakeHub, seed_message


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make_rm(**settings_overrides):
    settings = Settings(
        HF_TOKEN="test-token",
        ORG="test-org",
        COLLAB_SLUG="test",
        AUDIT_BUCKET="auditor/test-audit",
        **settings_overrides,
    )
    hub = FakeHub(settings)
    clock = Clock()
    return ReadModel(hub, settings, clock=clock), hub, clock, settings


def test_cold_fill_is_one_listing_plus_one_batch():
    rm, hub, _clock, _s = make_rm()
    for i in range(3):
        seed_message(hub, f"2026060{i + 1}-120000-000", "agent-1", f"msg {i}")
    recs = rm.records("message_board")
    assert [r.body.strip() for r in recs] == ["msg 0", "msg 1", "msg 2"]
    assert hub.list_calls == 1 and hub.download_calls == 1


def test_warm_reads_touch_the_bucket_zero_times():
    rm, hub, _clock, _s = make_rm()
    seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    rm.records("message_board")
    listed, downloaded = hub.list_calls, hub.download_calls
    rm.records("message_board")
    rm.records("message_board")
    assert (hub.list_calls, hub.download_calls) == (listed, downloaded)


def test_ttl_refresh_picks_up_admin_edit():
    rm, hub, clock, s = make_rm()
    fn = seed_message(hub, "20260601-120000-000", "agent-1", "original")
    assert rm.records("message_board")[0].body.strip() == "original"
    hub.seed(f"message_board/{fn}", "---\nagent: agent-1\n---\nedited")
    # Within TTL the cached copy is served; past it, the hash check refetches.
    assert rm.records("message_board")[0].body.strip() == "original"
    clock.t += s.listing_ttl_s + 1
    assert rm.records("message_board")[0].body.strip() == "edited"


def test_write_through_is_visible_without_a_new_listing():
    rm, hub, _clock, _s = make_rm()
    rm.records("message_board")  # primes the (empty) listing cache
    listed = hub.list_calls
    path = "message_board/20260601-120000-000_agent-1.md"
    text = "---\nagent: agent-1\n---\nfresh"
    hub.seed(path, text)  # the bucket write
    rm.write_through(path, {"agent": "agent-1"}, "fresh", len(text))
    recs = rm.records("message_board")
    assert [r.body for r in recs] == ["fresh"]
    assert hub.list_calls == listed  # TTL untouched — served from the overlay


def test_transient_empty_listing_keeps_cached_entries():
    rm, hub, clock, s = make_rm()
    seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    assert len(rm.records("message_board")) == 1
    hub.fail_listings = True
    clock.t += s.listing_ttl_s + 1
    assert len(rm.records("message_board")) == 1  # nothing is ever deleted


def test_lru_eviction_bounds_memory_but_never_drops_results():
    rm, hub, _clock, s = make_rm(CONTENT_CACHE_MAX_BYTES=120)
    for i in range(5):
        seed_message(hub, f"2026060{i + 1}-120000-000", "agent-1", f"body {i}")
    recs = rm.records("message_board")
    assert len(recs) == 5  # output complete even though the store evicted
    assert rm._content_bytes <= 120 or len(rm._content) == 1
    assert len(rm.records("message_board")) == 5  # refetches evicted entries


def test_identical_inbox_copies_share_one_cached_entry():
    rm, hub, _clock, _s = make_rm()
    fn = seed_message(hub, "20260601-120000-000", "agent-1", "hi @agent-2")
    hub.seed(f"inbox/agent-2/{fn}", hub.buckets[hub._settings.central_bucket][f"message_board/{fn}"].decode())
    rm.records("message_board")
    rm.records("inbox/agent-2")
    assert len(rm._content) == 1  # content-addressed: byte-identical = one entry


def test_malformed_file_degrades_to_parse_error_record():
    rm, hub, _clock, _s = make_rm()
    hub.seed("message_board/20260601-120000-000_agent-1.md", "---\nscore: [broken\n---\nbody")
    recs = rm.records("message_board")
    assert len(recs) == 1
    assert recs[0].parse_error and recs[0].frontmatter == {}


def test_record_single_and_missing():
    rm, hub, _clock, _s = make_rm()
    fn = seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    assert rm.record("message_board", fn).body.strip() == "hello"
    assert rm.record("message_board", "nope.md") is None


def test_registered_agents_excludes_readme():
    rm, hub, _clock, _s = make_rm()
    hub.seed("agents/agent-1.md", "---\nhf_user: u\n---\n")
    hub.seed("agents/README.md", "docs")
    assert rm.registered_agents() == {"agent-1"}


def test_verification_index_absent_present_and_refresh():
    rm, hub, clock, s = make_rm()
    assert rm.verification_index() == {}
    hub.seed("results/verification_status.json", json.dumps({"a.md": "valid"}))
    clock.t += s.listing_ttl_s + 1
    assert rm.verification_index() == {"a.md": "valid"}
    downloads = hub.download_calls
    assert rm.verification_index() == {"a.md": "valid"}  # hash-cached
    assert hub.download_calls == downloads
    hub.seed("results/verification_status.json", json.dumps({"a.md": "invalid"}))
    clock.t += s.listing_ttl_s + 1
    assert rm.verification_index() == {"a.md": "invalid"}


def test_unparseable_verification_index_reads_as_empty():
    rm, hub, _clock, _s = make_rm()
    hub.seed("results/verification_status.json", "{not json")
    assert rm.verification_index() == {}
