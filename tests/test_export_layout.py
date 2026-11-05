"""R4 (Tharak, 5 Nov): the exported workbook's sheet names, column names and column order never change.
The expected layout is tests/fixtures/target_template_headers.json: the sheet names and row-1 headers of Tharak's
headers-only target template (tm8 file 01a1aab0-9dd8), independent of app.excel.HEADERS and app.target_check.TEMPLATE.
R2: the Header 'Supplier Site' cell holds the Item Master SUPPLIER SITE id, not the site name."""
import io
import json
from pathlib import Path

import openpyxl
import pytest

from app import target_check
from app.excel import rules_workbook
from app.matching import rules_view
from tests.test_latest_master import activate, run

OWNER_TEMPLATE = json.loads((Path(__file__).parent / "fixtures" / "target_template_headers.json").read_text())


@pytest.fixture
def exported(client, owner, app):
    """A synthetic invoice through the real path: Item Master upload, fine rules, rules view, export workbook."""
    activate(client, owner)
    view = rules_view(run(app.state.store), "", [])
    content, _ = rules_workbook([view], draft=True)
    return openpyxl.load_workbook(io.BytesIO(content))


def test_the_exported_workbook_keeps_the_owner_template_sheets_and_columns_exactly(exported):
    assert exported.sheetnames == list(OWNER_TEMPLATE)
    for name, columns in OWNER_TEMPLATE.items():
        sheet = exported[name]
        assert sheet.max_column == len(columns)
        assert [sheet.cell(1, n).value for n in range(1, len(columns) + 1)] == columns


def test_the_target_check_template_is_the_owner_template():
    assert target_check.TEMPLATE == OWNER_TEMPLATE


def test_the_header_supplier_site_is_the_master_supplier_site_id_not_its_name(exported):
    """The synthetic master row: SUPPLIER SITE 101, SUPPLIER_NAME ABC001RA4AED, SUPPLIER SITE NAME Synthetic Co."""
    assert exported["Header"].cell(2, OWNER_TEMPLATE["Header"].index("Supplier Site") + 1).value == 101
