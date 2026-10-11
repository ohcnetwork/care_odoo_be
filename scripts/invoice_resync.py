"""
Resync Care invoices to Odoo through Care's invoice-resync endpoint.

Reads the invoice numbers from a CSV's "invoice" column (e.g. a Metabase export), sends them to Care
in batches, writes every result to a CSV and prints a summary. It's a dry run unless --apply is given.

    python3 invoice_resync.py invoices.csv --care https://care.example.org --username <superuser>
    python3 invoice_resync.py invoices.csv --care https://care.example.org --username <superuser> --apply

The password is asked for, or read from CARE_PASSWORD. Only Python's standard library is needed.
"""

import argparse
import csv
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

BATCH_SIZE = 10  # the endpoint's limit per request
COLUMNS = [
    "invoice",
    "status",
    "invoice_date",
    "journal",
    "lines_changed",
    "care_total",
    "old_untaxed",
    "new_untaxed",
    "old_tax",
    "new_tax",
    "old_total",
    "new_total",
    "old_payment_state",
    "new_payment_state",
    "changes",
]


def post(url, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


class Care:
    def __init__(self, url, username, password):
        self.url, self.username, self.password = url.rstrip("/"), username, password
        self.token = None

    def login(self):
        body = {"username": self.username, "password": self.password}
        self.token = post(f"{self.url}/api/v1/auth/login/", body)["access"]

    def resync(self, numbers, apply):
        body = {"invoice_numbers": numbers, "dry_run": not apply}
        try:
            return post(f"{self.url}/api/care_odoo/invoice-resync/", body, self.token)["results"]
        except urllib.error.HTTPError as error:
            # Tokens last 10 minutes, and Care answers an expired one with a 403
            if error.code not in (401, 403):
                raise
        self.login()
        return post(f"{self.url}/api/care_odoo/invoice-resync/", body, self.token)["results"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", help='a CSV with the invoice numbers in an "invoice" column')
    parser.add_argument("--care", required=True, help="Care's API URL")
    parser.add_argument("--username", required=True, help="a Care superuser")
    parser.add_argument("--apply", action="store_true", help="save the corrections (default: dry run)")
    parser.add_argument("--out", help="the results CSV (default: resync_dry_run.csv or resync_applied.csv)")
    args = parser.parse_args()

    with open(args.csv, newline="", encoding="utf-8-sig") as file:
        numbers = list(dict.fromkeys(row["invoice"] for row in csv.DictReader(file)))
    password = os.environ.get("CARE_PASSWORD") or getpass.getpass(f"Care password for {args.username}: ")
    care = Care(args.care, args.username, password)
    try:
        care.login()
    except urllib.error.HTTPError as error:
        sys.exit(f"Couldn't log in to Care: HTTP {error.code} {error.read().decode()[:300]}")

    results = []
    for start in range(0, len(numbers), BATCH_SIZE):
        batch = numbers[start : start + BATCH_SIZE]
        try:
            results += care.resync(batch, args.apply)
        except (urllib.error.URLError, TimeoutError) as error:  # report the batch and carry on
            detail = f"HTTP {error.code} {error.read().decode()[:300]}" if hasattr(error, "code") else str(error)
            if args.apply:
                # A request that timed out may still have been applied in Odoo
                detail += " (run a dry run for these invoices to check)"
            results += [{"invoice": number, "status": f"error: {detail}"} for number in batch]
        print(f"{min(start + BATCH_SIZE, len(numbers))}/{len(numbers)} invoices", end="\r", file=sys.stderr)

    # Care lists the invoices it couldn't send first, so put the results back in the CSV's order
    position = {number: index for index, number in enumerate(numbers)}
    results.sort(key=lambda result: position.get(result["invoice"], len(numbers)))
    out = args.out or ("resync_applied.csv" if args.apply else "resync_dry_run.csv")
    with open(out, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    print(f"{len(results)} invoices, results in {out}")
    print("Statuses:", dict(Counter(result["status"].split(":")[0] for result in results)))
    partial = [
        r["invoice"] for r in results if (r.get("old_payment_state"), r.get("new_payment_state")) == ("paid", "partial")
    ]
    print("Paid -> partial (follow up by hand):", ", ".join(partial) or "none")
    for result in results:
        if not result["status"].startswith(("would update", "updated", "in sync")):
            print(f"  {result['invoice']}: {result['status']}")


if __name__ == "__main__":
    main()
