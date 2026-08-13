from app.orchestrator.conversation import InMemoryConversationStore, Message


def test_history_capped_at_five():
    store = InMemoryConversationStore(history_size=5)
    for i in range(8):
        store.append("s1", Message(role="user", content=f"m{i}"))
    history = store.get_history("s1")
    assert len(history) == 5
    assert history[0].content == "m3"  # oldest evicted


def test_sessions_isolated():
    store = InMemoryConversationStore()
    store.append("a", Message(role="user", content="hello a"))
    assert store.get_history("b") == []


def test_ttl_purge():
    store = InMemoryConversationStore(ttl_min=0)
    store.append("old", Message(role="user", content="x"))
    import time

    time.sleep(0.01)
    assert store.purge_expired() == 1
    assert store.get_history("old") == []


# --------------------------------------------------------------- working set

from app.core.models import QueryResult


def _result(rows, columns=("customer_name", "revenue")) -> QueryResult:
    return QueryResult(
        columns=list(columns), rows=rows, row_count=len(rows), truncated=False,
        source="view", object_name="vw_test", elapsed_ms=5,
    )


def test_remembered_result_appears_in_working_set_most_recent_first():
    store = InMemoryConversationStore()
    store.remember_result("s1", _result([("A", 1.0)]), label="first question")
    store.remember_result("s1", _result([("B", 2.0)]), label="second question")

    working_set = store.working_set("s1")
    assert [w.label for w in working_set] == ["second question", "first question"]


def test_working_set_capped_and_evicts_oldest():
    store = InMemoryConversationStore(working_set_size=2)
    for i in range(4):
        store.remember_result("s1", _result([("x", float(i))]), label=f"q{i}")

    labels = [w.label for w in store.working_set("s1")]
    assert labels == ["q3", "q2"]


def test_empty_result_is_not_worth_a_working_set_slot():
    store = InMemoryConversationStore()
    store.remember_result("s1", _result([]), label="nothing found")
    assert store.working_set("s1") == []


def test_working_set_isolated_per_session():
    store = InMemoryConversationStore()
    store.remember_result("a", _result([("A", 1.0)]), label="q")
    assert store.working_set("b") == []


def test_working_set_survives_alongside_message_history():
    store = InMemoryConversationStore()
    store.append("s1", Message(role="user", content="top customers?"))
    store.remember_result("s1", _result([("A", 1.0)]), label="top customers?")
    store.append("s1", Message(role="assistant", content="Alpha leads."))

    assert len(store.get_history("s1")) == 2
    assert len(store.working_set("s1")) == 1


def test_ttl_purge_clears_the_working_set_too():
    store = InMemoryConversationStore(ttl_min=0)
    store.remember_result("old", _result([("A", 1.0)]), label="q")
    import time

    time.sleep(0.01)
    assert store.purge_expired() == 1
    assert store.working_set("old") == []
