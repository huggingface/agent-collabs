import json

from fakes import seed_agent, seed_result


def seed_results(hub):
    seed_agent(hub, "agent-1")
    seed_agent(hub, "agent-2")
    a = seed_result(hub, "20260601-100000-000", "agent-1", 100.0)
    b = seed_result(hub, "20260601-110000-000", "agent-2", 120.0)
    c = seed_result(hub, "20260602-090000-000", "agent-1", 80.0, status="negative")
    hub.seed("results/verification_status.json", json.dumps({a: "valid", b: "invalid"}))
    return a, b, c


def test_list_backward_compatible_shape(env):
    a, b, c = seed_results(env.hub)
    data = env.client.get("/v1/results").json()
    assert data["count"] == 3 and data["matched"] == 3
    assert data["items"] == [c, b, a]


def test_status_and_verification_filters(env):
    a, b, c = seed_results(env.hub)
    assert env.client.get("/v1/results?status=negative").json()["items"] == [c]
    assert env.client.get("/v1/results?verification=valid").json()["items"] == [a]
    # c has no index entry → reads as pending
    assert env.client.get("/v1/results?verification=pending").json()["items"] == [c]
    assert env.client.get("/v1/results?verification=valid,invalid").json()["matched"] == 2


def test_bad_verification_param_is_400(env):
    r = env.client.get("/v1/results?verification=bogus")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_QUERY"


def test_expand_inlines_verification(env):
    a, b, _c = seed_results(env.hub)
    items = env.client.get("/v1/results?expand=true&order=asc").json()["items"]
    by_name = {i["filename"]: i for i in items}
    assert by_name[a]["verification"] == "valid"
    assert by_name[b]["verification"] == "invalid"
    assert by_name[a]["frontmatter"]["score"] == 100.0


def test_single_get_carries_verification_and_404s(env):
    a, _b, _c = seed_results(env.hub)
    r = env.client.get(f"/v1/results/{a}")
    assert r.status_code == 200
    assert r.json()["verification"] == "valid"
    assert env.client.get("/v1/results/nope.md").status_code == 404


def test_post_result_visible_immediately(env):
    seed_agent(env.hub, "agent-1")
    env.hub.seed(
        "results/run1.md",
        "---\nscore: 142.7\nmethod: vllm-fp8\nstatus: agent-run\ndescription: fast\n---\nnotes\n",
        bucket="test-org/test-agent-1",
    )
    r = env.client.post(
        "/v1/results",
        json={"source": "hf://buckets/test-org/test-agent-1/results/run1.md"},
    )
    assert r.status_code == 201
    filename = r.json()["filename"]
    data = env.client.get("/v1/results?expand=true&limit=1").json()
    assert data["items"][0]["filename"] == filename
    assert data["items"][0]["verification"] == "pending"
    # the verification index tracked the promotion
    index = json.loads(
        env.hub.buckets[env.settings.central_bucket]["results/verification_status.json"]
    )
    assert index[filename] == "pending"
