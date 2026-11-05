"""S1.2: one printed supplier name on two Item Master sites. The read holds it with the sites listed; the reviewer's
site pick, through run_invoice or the review endpoint, re-matches the lines inside that site. Synthetic data only."""
import pytest

from app.fine_rules import RowsSource, run_invoice
from app.models import Invoice, Line
from tests.conftest import bearer
from tests.test_latest_master import activate, master_bytes

TEXT = "Synthetic Co\nINVOICE\nRose red\nRose blue"


def two_sites(shared=False):
    # One SUPPLIER SITE NAME on sites 101 and 102; VPN-1 is under both, VPN-2 only under 101 unless shared.
    # Unshared, the items alone pick 101 (A2); shared, both sites hold every item and the reviewer picks.
    return RowsSource([
        {"VPN": "VPN-1", "ITEM_PARENT": "P1", "SUPPLIER": "101", "SUPPLIER_NAME": "ABC001RE4AED",
         "SUPPLIER SITE NAME": "Synthetic Co", "ITEM_DESC": "Rose red"},
        {"VPN": "VPN-1", "ITEM_PARENT": "P1", "SUPPLIER": "102", "SUPPLIER_NAME": "ABC001RD3AED",
         "SUPPLIER SITE NAME": "Synthetic Co", "ITEM_DESC": "Rose red"},
        {"VPN": "VPN-2", "ITEM_PARENT": "P2", "SUPPLIER": "101", "SUPPLIER_NAME": "ABC001RE4AED",
         "SUPPLIER SITE NAME": "Synthetic Co", "ITEM_DESC": "Rose blue"}] +
        [{"VPN": "VPN-2", "ITEM_PARENT": "P2", "SUPPLIER": "102", "SUPPLIER_NAME": "ABC001RD3AED",
          "SUPPLIER SITE NAME": "Synthetic Co", "ITEM_DESC": "Rose blue"}] * shared)


def invoice():
    return Invoice(supplier_name="Synthetic Co", lines=[Line(sku="VPN-1", description="Rose red"),
                                                        Line(sku="VPN-2", description="Rose blue")])


def held(result):
    return [e for e in result["exceptions"] if e["Rule ID"] == "SUP-003"]


def test_two_sites_are_held_with_the_sites_listed():
    result = run_invoice(invoice(), two_sites(shared=True), text_value=TEXT)
    assert result["header"]["Supplier Site"] == "" and [l["Item"] for l in result["lines"]] == ["", ""]
    assert [c["supplier_site"] for c in result["supplier_site_candidates"]] == ["101", "102"]
    assert [c["sites"][0]["site_name"] for c in result["supplier_site_candidates"]] == ["ABC001RE4AED", "ABC001RD3AED"]
    assert held(result) and held(result)[0]["Candidates / Evidence"] == "101, 102"


@pytest.mark.parametrize("site, items", [("101", ["P1", "P2"]), ("102", ["P1", ""])])
def test_the_pick_rematches_the_lines_inside_that_site(site, items):
    result = run_invoice(invoice(), two_sites(), text_value=TEXT, owner_supplier_site=site)
    assert result["header"]["Supplier Site"] == site
    assert [l["Item"] for l in result["lines"]] == items
    assert not held(result)
    trace = next(t for t in result["lineage"] if t["target"] == "Supplier Site")
    assert trace["rule"] == "OWNER-PICK" and trace["confidence"] == "Owner entry"
    # The candidates stay listed, so the review can show the pick and change it.
    assert [c["supplier_site"] for c in result["supplier_site_candidates"]] == ["101", "102"]


def test_a_pick_that_was_not_offered_is_ignored_and_said():
    result = run_invoice(invoice(), two_sites(shared=True), text_value=TEXT, owner_supplier_site="999")
    assert result["header"]["Supplier Site"] == "" and [l["Item"] for l in result["lines"]] == ["", ""]
    assert any(e["Rule ID"] == "OWNER-PICK" and "ignored" in e["Description"] for e in result["exceptions"])


def test_the_review_endpoint_takes_the_site_pick(client, owner, app):
    def edit(sheet):
        sheet.delete_rows(2, sheet.max_row)
        for item, vpn, parent, desc, name, site in [(11, "VPN-1", 100000001, "Rose red", "ABC001RE4AED", 101),
                                                    (12, "VPN-1", 100000001, "Rose red", "ABC001RD3AED", 102),
                                                    (13, "VPN-2", 100000002, "Rose blue", "ABC001RE4AED", 101),
                                                    (14, "VPN-2", 100000002, "Rose blue", "ABC001RD3AED", 102)]:
            sheet.append([item, None, parent, desc, vpn, "Rose", name, "Synthetic Co", site] + [None] * 8)
    activate(client, owner, master_bytes(edit))
    app.state.store.job("pick", {"id": "pick", "filename": "synthetic.txt", "status": "review", "revision": 1,
                                 "reviewed": False, "provenance": [], "trace": [], "text": TEXT,
                                 "invoice": invoice().model_dump(mode="json")})
    data = invoice().model_dump(mode="json")

    def review(revision, **pick):
        return client.post("/api/jobs/pick/review", headers=bearer(owner),
                           json={"invoice": data, "revision": revision, **pick})

    job = review(1).json()
    assert [c["supplier_site"] for c in job["rules"]["supplier_site_candidates"]] == ["101", "102"]
    assert review(2, supplier_site="999").status_code == 400
    job = review(2, supplier_site="101").json()
    assert job["owner_supplier_site"] == "101"
    assert job["rules"]["fields"]["site"]["value"] == "101"
    assert [l["cells"]["Item"]["value"] for l in job["rules"]["lines"]] == ["100000001", "100000002"]
    job = review(3, supplier_site="").json()
    assert "owner_supplier_site" not in job and job["rules"]["fields"]["site"]["value"] in ("", None)
