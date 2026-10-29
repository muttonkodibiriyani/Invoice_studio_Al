"""The target-sheet check reports what the sheet is, and never names a cause it did not find.

A read with no item lines writes no Details rows. The join check then has nothing to join, which is
a gap the empty cells already report: calling it a failed join made every invoice on a fresh install
(no owner catalog, no mapping tables imported) show "Target check failed"."""
from app import target_check as tc
from tests.conftest import bearer

TEXT = ("INVOICE DEMO-2026-001\nDate 2031-03-03\nOrder No 4500123456\nLocation 1234\n"
        "Net 100.00\nVAT 5.00\nWidget 2 x 50.00\n")


def field(value, original=None):
    return {"value": value, "evidence": [{"source": "Invoice page 1", "reference": "page 1 box 2",
                                          "original": original if original is not None else value}]}


def view(lines=()):
    return {"fields": {"number": field("DEMO-2026-001"), "site": field("4500123456"), "po": field("4500123456"),
                       "location": field("1234"), "location_type": {"value": "", "reason": "No location master"},
                       "date": field("2031-03-03"), "net": field("100.00"), "tax": field("5.00"),
                       "taxCode": {"value": "", "reason": "No owner VAT code table"},
                       "currency": {"value": "AED"}},
            "lines": list(lines)}


def line(number=1):
    return {"line": number, "cells": {"Item": field("12345"), "Unit Cost": field("50.00"),
                                      "Quantity": field("2"), "Unit Tax Code": {"value": "", "reason": "Not found"}}}


def checked(lines=()):
    return tc.check_view(view(lines), tc.Sources(text=TEXT))


def status_of(result, name):
    return next(check["status"] for check in result["checks"] if check["check"] == name)


def test_a_read_with_no_item_lines_does_not_report_a_broken_target_sheet():
    result = checked()
    assert status_of(result, "joins") == tc.SKIPPED
    assert "no Details rows" in next(c["detail"] for c in result["checks"] if c["check"] == "joins")
    assert "joins" not in result["failed"], "the empty cells already report the missing lines"
    assert result["counts"][tc.EMPTY], "the gap is still reported, as the cells it actually is"


def test_the_rows_of_a_whole_read_still_have_to_join():
    result = checked([line()])
    assert status_of(result, "joins") == tc.PASS
    assert result["failed"] == []
    orphan = {"Header": [tc.TEMPLATE["Header"], [1, "DEMO-2026-001"]],
              "Tax_Breakdown": [tc.TEMPLATE["Tax_Breakdown"], [1, "S1", 100]],
              "Details": [tc.TEMPLATE["Details"], [2, 12345, None, 50, 2, "S1"]]}
    broken = tc.joins(orphan)[0]
    assert broken["status"] == tc.FAIL and "do not join" in broken["detail"]


def test_the_check_names_which_row_check_failed_and_no_values():
    result = checked([line()])
    assert result["failed"] == [], "this read adds up"
    partial = view([line()])
    partial["fields"]["net"] = field("999.00", "999.00")
    result = tc.check_view(partial, tc.Sources(text=TEXT + "999.00\n"))
    assert "lines_to_net" in result["failed"] and result["holds"]
    assert "lines do not sum to net" in result["summary"]
    assert not any(value in str(result["failed"]) for value in ("999", "DEMO-2026-001"))


def test_a_job_checked_before_the_failed_names_existed_still_loads_the_inbox(app, client, owner):
    """An upgrade must not need the stored checks re-run: /api/state serves every job, or none of them."""
    stored = {"version": 1, "checked_at": "2026-10-01T00:00:00+00:00", "transaction": 1, "upc": "empty",
              "counts": {"cells": 3, tc.VERIFIED: 1, tc.OWNER_RULE_EMPTY: 0, tc.EMPTY: 2, tc.NEEDS_CHECKING: 0},
              "metric": {"cells": 3}, "cells": [], "checks": [{"check": "joins", "status": tc.FAIL, "detail": "x"}],
              "holds": True, "summary": "Target sheet: 1 verified"}
    app.state.store.job("job-old", {"id": "job-old", "status": "review", "filename": "older.pdf",
                                    "created_at": "2026-10-01T00:00:00+00:00", "invoice": {}, "trace": [], "revision": 1,
                                    "rules": {"target_check": stored}})
    response = client.get("/api/state", headers=bearer(owner))
    assert response.status_code == 200, response.text
    check = next(job for job in response.json()["jobs"] if job["id"] == "job-old")["rules"]["target_check"]
    assert check["counts"][tc.EMPTY] == 2 and check["holds"] is True
    assert "failed" not in check, "the stored check has no such key; it is not invented here"
    assert not {"cells", "checks"} & set(check), "the inbox is still served the trimmed check"
