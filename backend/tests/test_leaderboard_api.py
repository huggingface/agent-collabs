import json

from fakes import seed_agent, seed_result


def seed_board(hub):
    seed_agent(hub, "agent-1", hf_user="user-one")
    seed_agent(hub, "agent-2", hf_user="user-two")
    r_valid = seed_result(hub, "20260601-100000-000", "agent-1", 100.0)
    r_pending = seed_result(hub, "20260602-100000-000", "agent-1", 150.0)
    r2_valid = seed_result(hub, "20260601-110000-000", "agent-2", 120.0)
    r2_invalid = seed_result(hub, "20260602-110000-000", "agent-2", 500.0)
    seed_result(hub, "20260603-100000-000", "agent-1", 999.0, status="negative")
    hub.seed(
        "results/20260603-110000-000_agent-2.md", "---\nscore: [broken\n---\nmalformed"
    )
    hub.seed(
        "results/verification_status.json",
        json.dumps({r_valid: "valid", r2_valid: "valid", r2_invalid: "invalid"}),
    )
    return r_valid, r_pending, r2_valid, r2_invalid


def test_default_board_best_per_agent_pending_included_invalid_excluded(env):
    seed_board(env.hub)
    data = env.client.get("/v1/leaderboard").json()
    rows = data["rows"]
    assert [(r["rank"], r["agent"], r["score"], r["verification"]) for r in rows] == [
        (1, "agent-1", 150.0, "pending"),
        (2, "agent-2", 120.0, "valid"),
    ]
    assert rows[0]["hf_user"] == "user-one"
    assert rows[1]["method"] == "vllm-baseline"
    meta = data["meta"]
    assert meta["results_considered"] == 6
    assert meta["excluded"] == {
        "status_negative": 1,
        "malformed": 1,
        "verification_invalid": 1,
    }


def test_strict_valid_only_view(env):
    seed_board(env.hub)
    rows = env.client.get("/v1/leaderboard?verification=valid").json()["rows"]
    assert [(r["agent"], r["score"]) for r in rows] == [
        ("agent-2", 120.0),
        ("agent-1", 100.0),
    ]


def test_all_results_view(env):
    seed_board(env.hub)
    rows = env.client.get("/v1/leaderboard?best_per_agent=false").json()["rows"]
    assert [r["score"] for r in rows] == [150.0, 120.0, 100.0]


def test_agent_filter_keeps_global_rank(env):
    seed_board(env.hub)
    rows = env.client.get("/v1/leaderboard?agent=agent-2").json()["rows"]
    assert len(rows) == 1 and rows[0]["rank"] == 2


def test_ties_go_to_the_earlier_result(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    seed_result(env.hub, "20260602-100000-000", "agent-2", 100.0)
    seed_result(env.hub, "20260601-100000-000", "agent-1", 100.0)
    rows = env.client.get("/v1/leaderboard").json()["rows"]
    assert [r["agent"] for r in rows] == ["agent-1", "agent-2"]


def test_limit(env):
    seed_board(env.hub)
    rows = env.client.get("/v1/leaderboard?limit=1").json()["rows"]
    assert len(rows) == 1 and rows[0]["rank"] == 1


def test_empty_board(env):
    data = env.client.get("/v1/leaderboard").json()
    assert data["rows"] == []
    assert data["meta"]["results_considered"] == 0


def test_asc_order_ranks_lowest_first(make_env):
    env = make_env(SCORE_ORDER="asc")
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    seed_result(env.hub, "20260601-100000-000", "agent-1", 100.0)
    seed_result(env.hub, "20260601-110000-000", "agent-1", 80.0)  # agent-1's best
    seed_result(env.hub, "20260601-120000-000", "agent-2", 90.0)
    data = env.client.get("/v1/leaderboard").json()
    assert data["order"] == "asc"
    assert [(r["agent"], r["score"]) for r in data["rows"]] == [
        ("agent-1", 80.0),
        ("agent-2", 90.0),
    ]
