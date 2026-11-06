# Item master golden outputs

`expected.json` holds the expected final cells for the 14 real invoices (rules page v0.3) that
`tests/test_master_golden.py` checks. Readable values, evidence and the assumed-values list are in tm8 doc
"Expected final output per real invoice" (01a1a88a-0301).

- Private values (invoice, supplier site, item, order, location) are `hmac:` HMAC-SHA256 digests under the private
  `golden.key`. Only public values (`""`, `Store (S)`, `Warehouse (W)`, `UAEV5`, `KSAV15`) appear as text.
- `confirm: true` means a reviewer confirms the cell (v0.3). The app may leave it blank or show the golden value,
  never another one.
- The invoices, the master workbook and the key never go into git. Set `INV_MASTER_GOLDEN_DIR` to run the cases;
  without it they skip.
- Every case passes since the rule fixes in PR #29, with no `xfail` marks. The expected values still wait for the
  business owner's sign-off (task "Confirm the expected outputs are right").
