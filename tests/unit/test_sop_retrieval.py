import pytest

from engine.chat.tools import build_chat_tools
from engine.settings import SOP_PATH
from engine.sop.retriever import LocalSopRetriever, build_sop_retriever, parse_sop_clauses


def test_sop_has_three_numbered_clauses() -> None:
    clauses = parse_sop_clauses(SOP_PATH.read_text(encoding="utf-8"), SOP_PATH.name)
    assert [c.clause_id for c in clauses] == ["§1", "§2", "§3"]
    assert "4.0" in clauses[0].text
    assert "7.0" in clauses[1].text
    assert "0.65" in clauses[2].text


@pytest.mark.parametrize(
    ("query", "expected_first"),
    [
        ("temperature limit for fresh perishables", "§1"),
        ("port congestion at Long Beach", "§2"),
        ("high risk delay probability escalation", "§3"),
    ],
)
def test_local_retriever_ranks_the_right_clause_first(query: str, expected_first: str) -> None:
    assert LocalSopRetriever().search(query)[0].clause_id == expected_first


def test_no_match_returns_empty() -> None:
    assert LocalSopRetriever().search("zzzz qqqq") == []


def test_retriever_is_deterministic() -> None:
    retriever = LocalSopRetriever()
    assert retriever.search("divert cold storage") == retriever.search("divert cold storage")


def test_pinecone_backend_fails_clearly_not_at_import(settings) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotImplementedError, match="SOP_BACKEND=LOCAL"):
        build_sop_retriever(settings.model_copy(update={"sop_backend": "PINECONE"}))


def test_sop_tool_output_is_delimited_with_clause_ids(settings) -> None:  # type: ignore[no-untyped-def]
    tools = {t.name: t for t in build_chat_tools(settings)}
    output = tools["search_compliance_sop"].invoke({"query": "fresh perishables temperature"})
    assert output.startswith('<sop_clause id="§1"')
    assert output.rstrip().endswith("</sop_clause>")
