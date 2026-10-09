import json
import threading

import pytest

from app.config import Settings
from app.frontmatter import serialise
from app.hub import DownloadFailed, ListingFailed
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


def test_failed_listing_keeps_cached_entries():
    rm, hub, clock, s = make_rm()
    seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    assert len(rm.records("message_board")) == 1
    hub.fail_listings = True
    clock.t += s.listing_ttl_s + 1
    assert len(rm.records("message_board")) == 1  # cached truth, not []
    assert rm._folder("message_board").last_error is not None
    hub.fail_listings = False
    clock.t += s.listing_ttl_s + 1
    rm.records("message_board")
    assert rm._folder("message_board").last_error is None  # cleared on success


def test_failed_listing_backs_off_one_ttl():
    rm, hub, clock, s = make_rm()
    rm.records("message_board")
    hub.fail_listings = True
    clock.t += s.listing_ttl_s + 1
    rm.records("message_board")
    listed = hub.list_calls
    rm.records("message_board")
    assert hub.list_calls == listed  # an outage is not one listing per read


def test_failed_listing_on_a_cold_folder_raises():
    rm, hub, _clock, _s = make_rm()
    seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    hub.fail_next_listing("message_board")
    with pytest.raises(ListingFailed):
        rm.records("message_board")
    assert len(rm.records("message_board")) == 1  # the next read retries


def test_empty_listing_is_the_truth_once_failures_are_explicit():
    rm, hub, clock, s = make_rm()
    fn = seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    assert len(rm.records("message_board")) == 1
    del hub.buckets[s.central_bucket][f"message_board/{fn}"]  # admin removal
    clock.t += s.listing_ttl_s + 1
    assert rm.records("message_board") == []


def test_partial_listing_is_not_cached_and_a_cursor_does_not_skip():
    """A listing interrupted mid-way must not replace the folder: here it
    would have shown m3 without m2, a watcher would advance its after= cursor
    to m3, and m2 would be lost to it for good once the full listing
    returned."""
    rm, hub, clock, s = make_rm()
    m1 = seed_message(hub, "20260601-120000-000", "agent-1", "one")
    assert [r.filename for r in rm.records("message_board")] == [m1]
    cached = dict(rm._folder("message_board").files)

    seed_message(hub, "20260603-120000-000", "agent-1", "three")
    m2 = seed_message(hub, "20260602-120000-000", "agent-1", "two")  # listed last
    hub.partial_listing("message_board", drop=1)  # the page holding m2 fails
    clock.t += s.listing_ttl_s + 1

    def after(cursor):
        return [r.filename for r in rm.records("message_board") if r.filename > cursor]

    assert after(m1) == []  # the cached truth, not "m3 but no m2"
    assert rm._folder("message_board").files == cached
    clock.t += s.listing_ttl_s + 1
    assert after(m1)[0] == m2  # nothing skipped once the listing completes


def test_failed_batch_download_raises_rather_than_dropping_records():
    rm, hub, _clock, _s = make_rm()
    fn = seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    hub.fail_next_read("message_board/")
    with pytest.raises(DownloadFailed):
        rm.record("message_board", fn)  # not None, which would be a false 404
    assert rm.record("message_board", fn).body.strip() == "hello"


def test_concurrent_cold_reads_share_one_batch_download():
    """After a restart every watcher reconnects at once; N cold readers of a
    folder must cost one batch download, not N."""
    rm, hub, _clock, _s = make_rm()
    for i in range(3):
        seed_message(hub, f"2026060{i + 1}-120000-000", "agent-1", f"msg {i}")
    hub.latency_s = 0.2
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(rm.records("message_board")))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [len(r) for r in results] == [3, 3]
    assert hub.list_calls == 1 and hub.download_calls == 1


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


def test_inbox_records_unions_broadcasts_for_any_handle():
    rm, hub, _clock, _s = make_rm()
    bcast = "20260601-120000-000_human-org.md"
    mention = "20260601-110000-000_agent-2.md"
    hub.seed(f"broadcasts/{bcast}", serialise({"agent": "human-org", "broadcast": True}, "all hands"))
    hub.seed(f"inbox/agent-1/{mention}", serialise({"agent": "agent-2"}, "ping @agent-1"))

    # a handle's own fan-out copies UNION every broadcast, ascending by filename
    assert [r.filename for r in rm.inbox_records("agent-1")] == [mention, bcast]
    # a handle with no inbox folder (never seen / joined later) still sees it
    assert [r.filename for r in rm.inbox_records("human-newcomer")] == [bcast]


def test_inbox_records_dedups_by_filename():
    rm, hub, _clock, _s = make_rm()
    fn = "20260601-120000-000_human-org.md"
    content = serialise({"agent": "human-org", "broadcast": True}, "hello")
    hub.seed(f"broadcasts/{fn}", content)
    hub.seed(f"inbox/agent-1/{fn}", content)  # same name in both sources
    assert [r.filename for r in rm.inbox_records("agent-1")] == [fn]


# ── notify levels & the unified stream (WATCH_DESIGN.md §4.2/§4.3) ─────


def _member(hub, channel: str, handle: str, notify: str | None = None):
    fm = {"channel": channel, "agent": handle, "subscribed": "2026-06-01 10:00 UTC",
          "via": "bucket"}
    if notify is not None:
        fm["notify"] = notify
    hub.seed(f"channels/{channel}/members/{handle}.md", serialise(fm, ""))


def _channel(hub, name: str):
    hub.seed(f"channels/{name}/README.md", serialise({"channel": name}, "theme"))


def test_channel_notify_levels_defaults_to_mentions():
    """An ABSENT key reads as the quiet default, so every membership written
    before this feature existed is already correct — no migration."""
    rm, hub, _clock, _s = make_rm()
    _channel(hub, "quiet")
    _channel(hub, "loud")
    _member(hub, "quiet", "agent-1")                 # legacy marker, no notify
    _member(hub, "loud", "agent-1", notify="all")
    assert rm.channel_notify_levels("agent-1") == {"loud": "all", "quiet": "mentions"}


def test_channel_notify_levels_ignores_garbage_values():
    """An unrecognised level must fall back to the QUIET side: failing open
    would turn a hand-edited typo into a notification flood."""
    rm, hub, _clock, _s = make_rm()
    _channel(hub, "c1")
    _member(hub, "c1", "agent-1", notify="URGENT")
    assert rm.channel_notify_levels("agent-1") == {"c1": "mentions"}


def test_channel_notify_levels_are_per_handle():
    rm, hub, _clock, _s = make_rm()
    _channel(hub, "c1")
    _member(hub, "c1", "agent-1", notify="all")
    _member(hub, "c1", "agent-2")
    assert rm.channel_notify_levels("agent-1") == {"c1": "all"}
    assert rm.channel_notify_levels("agent-2") == {"c1": "mentions"}
    assert rm.channel_notify_levels("nobody") == {}


def test_updates_records_unions_inbox_and_notify_all_channels():
    rm, hub, _clock, _s = make_rm()
    _channel(hub, "loud")
    _channel(hub, "quiet")
    _member(hub, "loud", "agent-1", notify="all")
    _member(hub, "quiet", "agent-1")

    mention = "20260601-100000-000_agent-2.md"
    hub.seed(f"inbox/agent-1/{mention}", serialise({"agent": "agent-2"}, "ping @agent-1"))
    bcast = "20260601-110000-000_human-org.md"
    hub.seed(f"broadcasts/{bcast}", serialise({"agent": "human-org", "broadcast": True}, "hi"))
    loud = "20260601-120000-000_agent-3.md"
    hub.seed(f"channels/loud/{loud}", serialise({"agent": "agent-3", "channel": "loud"}, "x"))
    quiet = "20260601-130000-000_agent-3.md"
    hub.seed(f"channels/quiet/{quiet}", serialise({"agent": "agent-3", "channel": "quiet"}, "y"))

    recs = rm.updates_records("agent-1")
    got = {r.filename: r.reasons for r in recs}
    assert got == {mention: ["mention"], bcast: ["broadcast"], loud: ["channel:loud"]}
    # Chronological by filename, so one cursor drains the union.
    assert [r.filename for r in recs] == sorted(got)


def test_updates_records_merges_reasons_for_one_delivery():
    """The same filename in the channel AND in the inbox is one item with two
    reasons — the double-delivery bug the unified stream exists to kill."""
    rm, hub, _clock, _s = make_rm()
    _channel(hub, "loud")
    _member(hub, "loud", "agent-1", notify="all")
    fn = "20260601-120000-000_agent-3.md"
    content = serialise({"agent": "agent-3", "channel": "loud"}, "@agent-1 look")
    hub.seed(f"channels/loud/{fn}", content)
    hub.seed(f"inbox/agent-1/{fn}", content)

    recs = rm.updates_records("agent-1")
    assert len(recs) == 1
    assert sorted(recs[0].reasons) == ["channel:loud", "mention"]


def test_updates_records_leaves_other_views_untagged():
    """`reasons` is set only on this view, so nothing elsewhere reads meaning
    into a field it never populated."""
    rm, hub, _clock, _s = make_rm()
    fn = "20260601-100000-000_agent-2.md"
    hub.seed(f"inbox/agent-1/{fn}", serialise({"agent": "agent-2"}, "ping @agent-1"))
    assert rm.updates_records("agent-1")[0].reasons == ["mention"]
    assert rm.inbox_records("agent-1")[0].reasons is None


# ── health stats during recovery; single-flight for record() ────────


class _RecoveringFolder:
    """A folder whose listing recovers right after stats() first looks at it:
    every later read of last_error sees the cleared value."""

    def __init__(self, err):
        self._err, self.reads = err, 0

    @property
    def last_error(self):
        self.reads += 1
        return self._err if self.reads == 1 else None


def test_stats_reads_each_listing_error_once():
    rm, _hub, clock, _s = make_rm()
    rm._folders["message_board"] = _RecoveringFolder(("type=HfHubHTTPError status=503", clock()))
    errors = rm.stats()["listing_errors"]  # a re-read would see None and crash
    assert errors["message_board"]["error"] == "type=HfHubHTTPError status=503"


def _hold_first_download(hub):
    """Block the first download_many until `release` is set; count all calls."""
    entered, release = threading.Event(), threading.Event()
    download = hub.download_many
    calls = []

    def held(bucket, paths):
        calls.append(list(paths))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return download(bucket, paths)

    hub.download_many = held
    return entered, release, calls


@pytest.mark.parametrize("first", ["record", "folder"])
def test_a_cold_record_read_waits_for_an_inflight_fetch(first):
    """Two cold reads of one message, or a read during the folder's warm-up
    fetch, cost one download: the second waits, then hits the cache."""
    rm, hub, _clock, _s = make_rm()
    fn = seed_message(hub, "20260601-120000-000", "agent-1", "hello")
    entered, release, calls = _hold_first_download(hub)
    results = {}
    start = (lambda: rm.record("message_board", fn)) if first == "record" else (lambda: rm.records("message_board"))
    a = threading.Thread(target=lambda: results.update(a=start()))
    a.start()
    assert entered.wait(5)
    b = threading.Thread(target=lambda: results.update(b=rm.record("message_board", fn)))
    b.start()
    b.join(0.3)
    assert b.is_alive()  # waiting on the in-flight fetch, not downloading
    release.set()
    a.join(5)
    b.join(5)
    assert len(calls) == 1
    assert results["b"].body.strip() == "hello"
