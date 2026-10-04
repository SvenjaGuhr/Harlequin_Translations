#!/usr/bin/env python3
"""
How are German Harlequin novels before 1991 catalogued in the DNB?

Hypothesis: CORA's newsstand series (Julia, Romana, Bianca, ...) were catalogued as
periodicals - one record per series - so individual issues and their originals are
missing. This script checks that in three ways:

  A  The series records that harvest_by_publisher.py skipped (from the cache, no new
     downloads): which series, from when, with which identifiers.  -> dnb_series_records.csv
  B  All DNB records of the publishers Cora / Harlequin / Mira per publication year
     (books and series records), to show where the catalogue begins.
  C  A direct search for issue-level records before 1991 by series name (Julia, Romana, ...)
     regardless of publisher, to see whether single issues were catalogued under
     another publisher name (e.g. Axel Springer).                       -> dnb_issue_search.csv

Run from the folder with the data and cache_harvest/ (after harvest_by_publisher.py):
  python experiments/explore_dnb_series.py                 # A + B from the cache, C online (~3 min)
  python experiments/explore_dnb_series.py --no-search     # A + B only

Result (October 2026): see README, "Negative result: German translations before 1991".
"""
import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root

from find_translations import Fetcher, parse_marc21, sru_records
from harvest_by_publisher import harvest_dnb

SERIES = ["Julia", "Romana", "Bianca", "Baccara", "Tiffany", "Cora-Bestseller", "Historical", "Mira"]
BASE = "https://services.dnb.de/sru/dnb"


def year(s):
    m = re.search(r"(19|20)\d\d", s or "")
    return int(m.group(0)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-search", action="store_true", help="skip part C (online search)")
    ap.add_argument("--from-year", type=int, default=1970)
    ap.add_argument("--to-year", type=int, default=1990)
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    # ---------- A + B: from the harvest cache ----------
    f = Fetcher(delay=args.delay, cache_dir="cache_harvest")
    years = range(1970, 2027)
    serial_rows, per_year, seen = [], Counter(), set()
    for _, rec, r, author in harvest_dnb(f, years):
        if r["record_id"] in seen:
            continue
        seen.add(r["record_id"])
        y = year(r["pub_date"])
        serial = rec.is_serial() or bool(re.search(r"(19|20)\d\d\s*-\s*$", r["pub_date"] or ""))
        per_year[(y, "series" if serial else "book")] += 1
        if serial:
            serial_rows.append({
                "record_id": r["record_id"], "title": r["translated_title"], "publisher": r["publisher"],
                "dates": r["pub_date"], "first_year": y, "issn": "; ".join(rec.sub("022", "a")),
                "frequency": "; ".join(rec.sub("310", "a")), "numbering": "; ".join(rec.sub("362", "a")),
                "leader_type": rec.leader[6:8] if len(rec.leader) > 7 else "",
            })
    with open("dnb_series_records.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(serial_rows[0].keys()) if serial_rows else ["record_id"])
        w.writeheader()
        w.writerows(sorted(serial_rows, key=lambda x: (x["first_year"] or 9999, x["title"])))

    print(f"A  Series records skipped by the harvest: {len(serial_rows)}  -> dnb_series_records.csv")
    for s in sorted(serial_rows, key=lambda x: (x["first_year"] or 9999))[:25]:
        print(f"   {s['dates'][:14]:14s} {s['title'][:55]:55s} {s['publisher'][:30]}")

    print("\nB  DNB records (publishers Cora / Harlequin / Mira) per publication year")
    print("   year   books  series records")
    for y in range(1970, 2001):
        b, s = per_year.get((y, "book"), 0), per_year.get((y, "series"), 0)
        if b or s:
            print(f"   {y}  {b:6d}  {s:6d}")
    later = sum(v for (y, k), v in per_year.items() if y and y > 2000 and k == "book")
    print(f"   2001-2026 books: {later:,}")

    if args.no_search:
        return

    # ---------- C: issue-level search by series name ----------
    print(f"\nC  Searching issue-level records {args.from_year}-{args.to_year} by series name (any publisher)")
    hits = []
    for s in SERIES:
        n_series, n_total = 0, 0
        for y in range(args.from_year, args.to_year + 1):
            q = f'tit="{s}" and jhr={y}'
            xml = f.get(BASE + "?" + urlencode({"version": "1.1", "operation": "searchRetrieve", "query": q,
                                                "recordSchema": "MARC21-xml", "maximumRecords": 100,
                                                "startRecord": 1}))
            recs, total = sru_records(xml)
            for rec in recs:
                r = parse_marc21(rec, "DNB")
                ser = r["series"] or ""
                # keep records that belong to the series (series statement or title starts with it)
                if not (re.search(rf"\b{re.escape(s)}\b", ser, re.I) or r["translated_title"].lower().startswith(s.lower())):
                    continue
                n_total += 1
                if rec.is_serial():
                    n_series += 1
                hits.append({"series_searched": s, "year": y, "title": r["translated_title"], "series": ser,
                             "publisher": r["publisher"], "original_titles": " | ".join(r["original_titles"]),
                             "is_series_record": rec.is_serial(), "record_id": r["record_id"]})
        print(f"   {s:16s} {n_total:5d} records in the series ({n_series} series-level)")
    with open("dnb_issue_search.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(hits[0].keys()) if hits else ["series_searched"])
        w.writeheader()
        w.writerows(hits)
    issues = [h for h in hits if not h["is_series_record"]]
    print(f"   Issue-level records found: {len(issues)}, of which naming an original title: "
          f"{sum(1 for h in issues if h['original_titles'])}  -> dnb_issue_search.csv")
    pubs = Counter(h["publisher"] for h in issues)
    if pubs:
        print("   Publishers of these records:", ", ".join(f"{p or '?'} ({n})" for p, n in pubs.most_common(6)))


if __name__ == "__main__":
    main()
