"""Workbook adapter: edit only this file when the owner's column labels change.

Keys are the canonical fields consumed by fine_rules_tables. Values are accepted
Excel headings (trimmed, case-insensitive). Required fields must appear even on an
empty sheet. Optional fields may be absent. Add aliases, never guess by position;
two columns resolving to the same field are an error. See docs/MAPPING_TABLES.md.
"""

SHEETS = {
    "VAT CODES": {
        "required": ("VAT_TYPE", "VAT_CODE", "VAT_REGION_NAME", "VAT_RATE", "ACTIVE_DATE"),
        "headers": {
            "VAT_TYPE": ("VAT_TYPE",), "VAT_CODE": ("VAT_CODE",),
            "VAT_REGION_NAME": ("VAT_REGION_NAME",), "VAT_RATE": ("VAT_RATE",),
            "ACTIVE_DATE": ("ACTIVE_DATE",),
        },
    },
    "LOCATIONS": {
        "required": ("LOCATION", "WH/STORE", "COUNTRY"),
        "headers": {
            "LOCATION": ("LOCATION",), "WH/STORE": ("WH/STORE",), "COUNTRY": ("COUNTRY",),
            "ENTITY AND CURRENCY": ("ENTITY AND CURRENCY", "ENTITY AND CURENCY"),
        },
    },
    "SUPPLIER SITES": {
        "required": ("SUPPLIER SITE", "STATUS DESCRIPTION", "CURRENCY"),
        "headers": {
            "SUPPLIER SITE": ("SUPPLIER SITE",), "STATUS DESCRIPTION": ("STATUS DESCRIPTION",),
            "CURRENCY": ("CURRENCY",), "SUPPLIER CODE": ("SUPPLIER CODE",),
            "SUPPLIER NAME": ("SUPPLIER NAME",), "SUPPLIER SITE NAME": ("SUPPLIER SITE NAME",),
        },
    },
}
