from __future__ import annotations

from compliance_assistant.answer import NO_SOURCES, answer_question, check_citations, format_sources, render
from compliance_assistant.index import search
from compliance_assistant.llm import FakeModel
from test_index import build


def test_check_citations_flags_unknown_numbers_and_missing_citations():
    assert check_citations("A [1] and B [2, 3] and C [1-2].", 3) == ([1, 2, 3], [])
    cited, warns = check_citations("See [1] and [7].", 3)
    assert cited == [1] and "7" in warns[0]
    assert check_citations("No cites here.", 3)[1] == ["The answer cites no sources."]


def test_answer_uses_sources_and_renders_citations(tmp_path):
    hits = search(build(tmp_path), "report misconduct")
    model = FakeModel("Report within 14 days [1].")
    ans = answer_question("When do I report misconduct?", hits, model)
    system, user = model.calls[0]
    assert "ONLY from the numbered SOURCES" in system
    assert "[1] A Notice (v2)" in user and "14 days" in user
    out = render(ans)
    assert "Sources cited:" in out and "Notice, para 4" in out and "Not legal advice" in out


def test_no_hits_means_no_model_call():
    model = FakeModel()
    ans = answer_question("anything", [], model)
    assert ans.text == NO_SOURCES and model.calls == []


def test_email_mode_uses_email_prompt(tmp_path):
    hits = search(build(tmp_path), "report misconduct")
    model = FakeModel("Dear [Client name], ... [1]")
    answer_question("Client asks about timing", hits, model, mode="email", notes="keep it short")
    assert "draft emails" in model.calls[0][0] and "keep it short" in model.calls[0][1]


def test_superseded_sources_are_labelled(tmp_path):
    hits = search(build(tmp_path), "misconduct", include_superseded=True)
    assert "[SUPERSEDED]" in format_sources(hits)
