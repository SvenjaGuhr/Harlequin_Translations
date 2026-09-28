#!/usr/bin/env python3
"""
Resolve English originals that translations point to but that are NOT in your
book tables (e.g. books from other Harlequin lines), using the Open Library
search API, and write them as extra English books.

A result is accepted only if
  * the title is the same book (same words; spelling variants allowed),
  * the author matches, and
  * at least one edition was published by a Harlequin-family imprint
    (Harlequin, Silhouette, Mills & Boon, Mira, HQN, Love Inspired, ...).
    Use --any-publisher to drop this condition.

Usage
  python resolve_originals.py originals_to_resolve.csv
  -> english_additions.csv   (same columns as harlequin_lines.csv; book_id = Open Library work id)

Then run find_translations.py again with BOTH tables:
  python find_translations.py harlequin_lines.csv english_additions.csv --no-library --extra ...
"""
import argparse
import csv
import difflib
import json
from urllib.parse import urlencode

from find_translations import Fetcher, compatible, fold, norm

HARLEQUIN = ("harlequin", "silhouette", "mills", "boon", "mira", "hqn", "love inspired", "kimani",
             "steeple hill", "worldwide", "carina", "luna", "spice", "red dress")
COLS = ["line", "series_number", "title", "authors", "pub_date", "all_lines", "is_companion", "is_anthology",
        "book_id", "book_url", "source_en", "en_publishers", "languages_translated", "n_translated_editions"]


def author_ok(wanted, names):
    w = fold(wanted)
    return any(fold(n) == w or difflib.SequenceMatcher(None, fold(n), w).ratio() >= 0.85 for n in names or [])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("to_resolve", help="originals_to_resolve.csv from find_translations.py")
    ap.add_argument("-o", "--output", default="english_additions.csv")
    ap.add_argument("--any-publisher", action="store_true", help="accept non-Harlequin publishers too")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0, help="only the first N rows (testing)")
    args = ap.parse_args()

    with open(args.to_resolve, encoding="utf-8-sig") as fh:
        todo = list(csv.DictReader(fh))
    if args.limit:
        todo = todo[: args.limit]

    f = Fetcher(delay=args.delay, cache_dir="cache_openlibrary")
    out, seen = [], set()
    stats = {"found": 0, "not_found": 0, "not_harlequin": 0}
    for i, t in enumerate(todo, 1):
        q = {"title": t["original_title"], "author": t["author"], "limit": 10,
             "fields": "key,title,author_name,first_publish_year,publisher"}
        txt = f.get("https://openlibrary.org/search.json?" + urlencode(q))
        try:
            docs = json.loads(txt).get("docs", []) if txt else []
        except json.JSONDecodeError:
            docs = []
        hit = next((d for d in docs if compatible(norm(d.get("title", "")), norm(t["original_title"]))
                    and author_ok(t["author"], d.get("author_name"))), None)
        if not hit:
            stats["not_found"] += 1
            continue
        pubs = [p for p in hit.get("publisher", []) if p]
        harlequin = [p for p in pubs if any(h in p.lower() for h in HARLEQUIN)]
        if not harlequin and not args.any_publisher:
            stats["not_harlequin"] += 1
            continue
        work = hit["key"].rsplit("/", 1)[-1]           # "/works/OL123W" -> "OL123W"
        if work in seen:
            continue
        seen.add(work)
        stats["found"] += 1
        out.append({
            "line": "Other Harlequin (via Open Library)", "series_number": "", "title": hit["title"],
            "authors": t["author"], "pub_date": str(hit.get("first_publish_year", "")),
            "all_lines": "", "is_companion": False, "is_anthology": False, "book_id": work,
            "book_url": f"https://openlibrary.org/works/{work}", "source_en": "Open Library",
            "en_publishers": "; ".join(dict.fromkeys(harlequin or pubs))[:300],
            "languages_translated": t.get("languages", ""), "n_translated_editions": t.get("n_editions", ""),
        })
        if i % 100 == 0:
            print(f"  [{i}/{len(todo)}] {stats}")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)
    print(f"\n{len(todo):,} originals checked: {stats['found']:,} Harlequin books found, "
          f"{stats['not_harlequin']:,} found but not Harlequin, {stats['not_found']:,} not found")
    print(f"-> {args.output}. Next: run find_translations.py with harlequin_lines.csv {args.output}")


if __name__ == "__main__":
    main()
