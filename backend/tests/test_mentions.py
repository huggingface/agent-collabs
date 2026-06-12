from app.mentions import extract_recipients


REG = {"agent-1", "agent-2", "agent-3"}


def extract(body="", refs=None, author="author", registered=REG, cap=10):
    return extract_recipients(
        body=body, refs=refs, author=author, registered=registered, cap=cap
    )


def test_mentions_in_order_of_appearance():
    assert extract("ping @agent-2 then @agent-1") == ["agent-2", "agent-1"]


def test_email_addresses_mention_nobody():
    assert extract("mail me at carlos@agent-1 or x.y@agent-2") == []


def test_punctuation_boundaries():
    assert extract("(@agent-1), @agent-2. and\n@agent-3!") == [
        "agent-1",
        "agent-2",
        "agent-3",
    ]


def test_author_never_a_recipient():
    assert extract("@agent-1 talking to myself", author="agent-1") == []


def test_unregistered_dropped():
    assert extract("@nobody @all @here @agent-1") == ["agent-1"]


def test_deduped():
    assert extract("@agent-1 again @agent-1") == ["agent-1"]


def test_cap_first_in_appearance_order():
    assert extract("@agent-1 @agent-2 @agent-3", cap=2) == ["agent-1", "agent-2"]


def test_uppercase_is_not_a_mention():
    assert extract("@Agent-1 looks wrong") == []


def test_human_handles_deliver_without_registration():
    assert extract("thanks @human-cmpatino!", registered=set()) == ["human-cmpatino"]


def test_bare_human_routes_nowhere():
    assert extract("any @human around?", registered=set()) == []


def test_refs_single_string():
    assert extract(refs="20260601-120000-000_agent-2.md") == ["agent-2"]


def test_refs_comma_separated_and_list():
    assert extract(refs="20260601-120000-000_agent-2.md, 20260602-000000-000_agent-1.md") == [
        "agent-2",
        "agent-1",
    ]
    assert extract(refs=["20260601-120000-000_agent-3.md"]) == ["agent-3"]


def test_refs_non_filename_tokens_ignored():
    assert extract(refs="agent-2") == []
    assert extract(refs=42) == []


def test_refs_author_with_invalid_charset_is_dropped():
    # A legacy filename whose author segment contains an underscore is not a
    # routable handle (the inbox read path validates with AGENT_ID_RE), so it
    # must never become a recipient — otherwise the copy is unreadable.
    assert extract(refs="20260608-212040_human-osanseviero_bae622f2.md", registered=set()) == []


def test_valid_human_handle_from_refs_delivers():
    assert extract(
        refs="20260101-000000-000_human-osanseviero.md", registered=set()
    ) == ["human-osanseviero"]


def test_mentions_then_refs_union():
    assert extract("@agent-3 builds on this", refs="20260601-120000-000_agent-1.md") == [
        "agent-3",
        "agent-1",
    ]
