from datetime import datetime

import pytest

from linkedin_mcp_server.posting.plan import (
    PostingError,
    PostingErrorCode,
    PostLedger,
    content_lines,
    date_field,
    plan_post,
    posting_at_label,
    time_field,
)

NOW = datetime(2026, 10, 3, 16, 0)


def _code(text, when=None):
    with pytest.raises(PostingError) as e:
        plan_post(text, when, now=NOW)
    return e.value.code


def test_schedule_uses_linkedins_own_wording():
    plan = plan_post("Hello\n\nhttps://example.com/x", "2026-10-06T08:30", now=NOW)
    assert plan.posting_at == "Tue, Oct 6, 8:30 AM"
    assert plan.preview()["links"] == ["https://example.com/x"]
    assert plan.preview()["when"] == "Scheduled: Posting at Tue, Oct 6, 8:30 AM"


@pytest.mark.parametrize(
    ("when", "label", "date", "clock"),
    [
        (datetime(2026, 11, 3, 0, 15), "Tue, Nov 3, 12:15 AM", "11/3/2026", "12:15 AM"),
        (datetime(2026, 10, 22, 12, 0), "Thu, Oct 22, 12:00 PM", "10/22/2026", "12:00 PM"),
        (datetime(2026, 10, 5, 17, 45), "Mon, Oct 5, 5:45 PM", "10/5/2026", "5:45 PM"),
    ],
)
def test_form_values(when, label, date, clock):
    assert posting_at_label(when) == label
    assert date_field(when) == date
    assert time_field(when) == clock


def test_immediate_post_has_no_schedule():
    plan = plan_post("  Hello  ", None, now=NOW)
    assert plan.text == "Hello" and plan.schedule is None
    assert plan.preview()["when"] == "Immediately"


@pytest.mark.parametrize(
    ("text", "when"),
    [
        ("", None),
        ("x" * 3001, None),
        ("ok", "tomorrow"),
        ("ok", "2026-10-06T08:31"),
        ("ok", "2026-10-03T16:15"),
        ("ok", "2027-02-01T09:00"),
        ("ok", "2026-10-06T08:30+01:00"),
    ],
)
def test_refusals(text, when):
    assert _code(text, when) is PostingErrorCode.VALIDATION_ERROR


def test_read_back_ignores_paragraph_spacing_only():
    typed = "One.\n\n\n\n\nTwo  words\n\n#tag"
    assert content_lines(typed) == content_lines("One.\n\nTwo words\n\n#tag")
    assert content_lines(typed) != content_lines("One.\n\nTwo words")


def test_ledger_finds_a_submitted_text(tmp_path):
    ledger = PostLedger(tmp_path)
    plan = plan_post("Hello\n\nworld", "2026-10-06T08:30", now=NOW)
    assert ledger.find(plan.fingerprint) is None
    ledger.record(plan, "scheduled")
    same = plan_post("Hello\n\n\nworld ", None, now=NOW)
    assert ledger.find(same.fingerprint)["status"] == "scheduled"


def test_refusal_result_says_whether_a_retry_is_safe():
    before = PostingError(PostingErrorCode.TEXT_MISMATCH, "x").to_result()
    after = PostingError(PostingErrorCode.COMPOSER_CHANGED, "x", submitted=True).to_result()
    assert before["retry_safe"] is True and before["posted"] is False
    assert after["retry_safe"] is False
