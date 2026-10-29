#!/usr/bin/env python3
"""Daily AI spend from the service's own log lines, for whoever is watching the bill.

Every extraction prints one `invoice_cost` line with its token counts and what they cost,
so spend can be followed from the logs without a session on the service or a billing
export. The same figures are in the audit records behind GET /api/costs.

Usage: scripts/spend.py [days] [--service inv-studio-api] [--region us-central1]
Needs gcloud with logging read access to the project.
"""
import json
import subprocess
import sys
from collections import defaultdict
from decimal import Decimal


def read(days, service, region):
    query = (f'resource.type="cloud_run_revision" resource.labels.service_name="{service}" '
             f'resource.labels.location="{region}" jsonPayload.event="invoice_cost"')
    out = subprocess.run(["gcloud", "logging", "read", query, f"--freshness={days}d",
                          "--format=json", "--limit=100000"], capture_output=True, text=True, check=True)
    for entry in json.loads(out.stdout or "[]"):
        payload = entry.get("jsonPayload") or {}
        yield str(entry.get("timestamp") or "")[:10], payload


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = dict(zip(sys.argv[1:], sys.argv[2:]))
    days = int(args[0]) if args else 30
    rows = defaultdict(lambda: {"invoices": 0, "calls": 0, "input": 0, "output": 0, "usd": Decimal(0)})
    for day, payload in read(days, flags.get("--service", "inv-studio-api"), flags.get("--region", "us-central1")):
        row = rows[day]
        row["invoices"] += 1
        row["calls"] += int(payload.get("calls") or 0)
        row["input"] += int(payload.get("input_tokens") or 0)
        row["output"] += int(payload.get("output_tokens") or 0)
        row["usd"] += Decimal(str(payload.get("usd") or 0))
    if not rows:
        print(f"no invoice_cost lines in the last {days} days")
        return
    print(f"{'day':12} {'invoices':>8} {'ai calls':>8} {'in tokens':>10} {'out tokens':>10} {'usd':>9}")
    total = Decimal(0)
    for day in sorted(rows, reverse=True):
        row = rows[day]
        total += row["usd"]
        print(f"{day:12} {row['invoices']:8} {row['calls']:8} {row['input']:10} {row['output']:10} {row['usd']:9.4f}")
    invoices = sum(row["invoices"] for row in rows.values())
    calls = sum(row["calls"] for row in rows.values())
    print(f"\n{invoices} invoices, {calls} AI calls ({calls / invoices:.0%} of invoices), ${total:.4f} over {len(rows)} days")
    if invoices:
        print(f"per invoice: ${total / invoices:.6f}")


main()
