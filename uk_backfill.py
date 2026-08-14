#!/usr/bin/env python3
"""
UK legislation.gov.uk historical backfill — count-only mode.

Fetches year feeds for UK Public General Acts (ukpga) and Statutory
Instruments (uksi) from 2016-2026, counts entries per year, and prints
a table. Nothing is inserted into the database.

Run: python3 uk_backfill.py

Note: year feeds may be paginated; counts shown are entries visible in
the first feed page. Add ?page=N to paginate if needed.
"""
import sys
import time
import xml.etree.ElementTree as ET

import requests

ATOM_NS = "http://www.w3.org/2005/Atom"
YEARS   = range(2016, 2027)
UA      = "CivilGate/1.0 (+https://civilgate.org)"


def count_year(year: int, doc_type: str) -> int:
    url = f"https://www.legislation.gov.uk/{doc_type}/{year}/data.feed"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        root = ET.fromstring(r.content)
        return len(root.findall(f"{{{ATOM_NS}}}entry"))
    except Exception as e:
        print(f"  Error {doc_type}/{year}: {e}", file=sys.stderr)
        return -1


def main() -> None:
    col_a = "Public General Acts (ukpga)"
    col_b = "Statutory Instruments (uksi)"
    print(f"\n{'Year':<6}  {col_a:>30}  {col_b:>28}")
    print("-" * 70)

    totals = [0, 0]
    for year in YEARS:
        ukpga = count_year(year, "ukpga")
        time.sleep(0.5)
        uksi = count_year(year, "uksi")
        time.sleep(0.5)
        flag = " !" if ukpga < 0 or uksi < 0 else ""
        ukpga_str = str(ukpga) if ukpga >= 0 else "err"
        uksi_str  = str(uksi)  if uksi  >= 0 else "err"
        print(f"{year:<6}  {ukpga_str:>30}  {uksi_str:>28}{flag}")
        if ukpga >= 0:
            totals[0] += ukpga
        if uksi >= 0:
            totals[1] += uksi

    print("-" * 70)
    print(f"{'Total':<6}  {totals[0]:>30}  {totals[1]:>28}")
    print()
    print("Counts are per feed page (not paginated). Decide how far back")
    print("to go, then call fetch_legislation_gov_uk() or run pipeline.py.")


if __name__ == "__main__":
    main()
