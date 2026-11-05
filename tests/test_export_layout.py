"""R4 (Tharak, 5 Nov): the exported workbook's sheet names, column names and column order never change.
No owner template file is shipped, so the expected layout is the owner's target template written out here
(owner form 01a10c4d), independent of app.excel.HEADERS and app.target_check.TEMPLATE.
R2: the Header 'Supplier Site' cell holds the Item Master SUPPLIER SITE id, not the site name."""
import io

import openpyxl
import pytest

from app import target_check
from app.excel import rules_workbook
from app.matching import rules_view
from tests.test_latest_master import activate, run

OWNER_TEMPLATE = {
    "Header": ["Transaction Number", "Document", "Supplier Site", "Order No", "Location", "Location Type",
               "Document Date", "Total Cost Ex Tax", "Tax Amount", "Ref No. 1", "Ref No. 2", "Ref No. 3", "Comment"],
    "Tax_Breakdown": ["Transaction Number", "Tax Code", "Tax Basis"],
    "Details": ["Transaction Number", "Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code"],
}


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
