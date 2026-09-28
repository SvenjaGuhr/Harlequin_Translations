#!/usr/bin/env python3
"""
Harvest ALL Harlequin translations from the national libraries by PUBLISHER,
independent of any author or series list.

  German  (DNB, SRU)       publishers: Cora, Harlequin, Mira
  French  (BnF, SRU)       publisher : Harlequin
  Polish  (BN Data API)    publishers: Harlequin, Arlekin

Each library record becomes one row per original title it names (anthologies ->
several rows with the same source_record_id), in the extra-source format that
find_translations.py --extra reads. Records without an original title are kept
too (original_title empty) so they can go through the candidate review.

Large result sets are split by publication year so no query hits a server limit.
Everything is cached (cache_harvest/), so an interrupted run resumes.

Usage
  pip install requests
  python harvest_by_publisher.py                       # all three languages
  python harvest_by_publisher.py --langs P             # Polish only (quick test)
  python harvest_by_publisher.py --from-year 1980 --to-year 2026

Output: harvest_F.csv, harvest_G.csv, harvest_P.csv
"""
import argparse
import csv
import datetime
import json
import re
from urllib.parse import urlencode

from find_translations import (Fetcher, LANGS, clean, marc_from_bn_json, norm, parse_marc21, parse_unimarc,
                               person, sru_records, strip_marks)

COLS = ["language_letter", "authors", "original_title", "translated_title", "pub_date", "publisher", "place",
        "translators", "series", "isbn", "format", "copyright", "source", "source_record_id", "url"]

DNB_PUBLISHERS = ["Cora", "Harlequin", "Mira"]
BNF_PUBLISHERS = ["Harlequin"]
BN_PUBLISHERS = ["Harlequin", "Arlekin"]
PAGE = 100


# --------------------------------------------------------------------------- #
# author of a record ("Brown, Sandra (1948- )" -> "Sandra Brown")
# --------------------------------------------------------------------------- #
def tidy_name(n):
    n = re.sub(r"\(.*?\)|\[.*?\]", "", strip_marks(n or ""))
    n = re.sub(r"\b(1[89]|20)\d\d\s*-\s*((1[89]|20)\d\d)?", "", n)
    return person(clean(n))


def author_marc21(rec):
    a = rec.first("100", "a")
    if a:
        return tidy_name(a)
    for _, _, sf in rec.dfs("700"):               # first added author that is not a translator
        d = {}
        for c, v in sf:
            d.setdefault(c, []).append(v)
        roles = " ".join(d.get("4", []) + d.get("e", [])).lower()
        if "a" in d and not any(r in roles for r in ("trl", "übers", "tłum", "transl")):
            return tidy_name(d["a"][0])
    return ""


def author_unimarc(rec):
    for tag in ("700", "701"):
        for _, _, sf in rec.dfs(tag):
            d = {}
            for c, v in sf:
                d.setdefault(c, []).append(v)
            if "730" in d.get("4", []):              # translator
                continue
            name = " ".join(d.get("b", []) + d.get("a", []))
            if name.strip():
                return tidy_name(name)
    return ""


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def sru_all(f, base, params_for, parse, label, years):
    """Run an SRU query; split by year if the full set is large. Yields (Marc, parsed)."""
    def run(query):
        start, total = 1, None
        while True:
            xml = f.get(base + "?" + urlencode(params_for(query, start)))
            recs, n = sru_records(xml)
            total = n if total is None else total
            for r in recs:
                yield r, parse(r)
            start += PAGE
            if not recs or start > total:
                break

    head = f.get(base + "?" + urlencode(params_for(label["all"], 1, 1)))
    total = sru_records(head)[1]
    print(f"  {label['name']}: {total:,} records")
    if total <= 9000:
        yield from run(label["all"])
        return
    for y in years:                                   # split to stay below server paging limits
        yield from run(label["year"](y))


def harvest_dnb(f, years):
    base = "https://services.dnb.de/sru/dnb"
    def params(q, start, n=PAGE):
        return {"version": "1.1", "operation": "searchRetrieve", "query": q,
                "recordSchema": "MARC21-xml", "maximumRecords": n, "startRecord": start}
    for pub in DNB_PUBLISHERS:
        label = {"name": f"DNB publisher={pub}", "all": f'vlg="{pub}"',
                 "year": lambda y, p=pub: f'vlg="{p}" and jhr={y}'}
        for rec, r in sru_all(f, base, params, lambda m: parse_marc21(m, "DNB"), label, years):
            yield "G", rec, r, author_marc21(rec)


def harvest_bnf(f, years):
    base = "https://catalogue.bnf.fr/api/SRU"
    def params(q, start, n=PAGE):
        return {"version": "1.2", "operation": "searchRetrieve", "query": q,
                "recordSchema": "unimarcxchange", "maximumRecords": n, "startRecord": start}
    for pub in BNF_PUBLISHERS:
        label = {"name": f"BnF publisher={pub}", "all": f'bib.publisher all "{pub}"',
                 "year": lambda y, p=pub: f'bib.publisher all "{p}" and bib.date all "{y}"'}
        for rec, r in sru_all(f, base, params, parse_unimarc, label, years):
            yield "F", rec, r, author_unimarc(rec)


def harvest_bn(f, years):
    for pub in BN_PUBLISHERS:
        url = "https://data.bn.org.pl/api/institutions/bibs.json?" + urlencode(
            {"publisher": pub, "language": "polski", "limit": 100})
        n = 0
        while url:
            txt = f.get(url)
            try:
                data = json.loads(txt) if txt else {}
            except json.JSONDecodeError:
                break
            for b in data.get("bibs", []):
                if b.get("marc"):
                    rec = marc_from_bn_json(b["marc"])
                    n += 1
                    yield "P", rec, parse_marc21(rec, "BN"), author_marc21(rec)
            url = data.get("nextPage") if data.get("bibs") else None
        print(f"  BN publisher={pub}: {n:,} records")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--langs", nargs="+", default=["F", "G", "P"], choices=["F", "G", "P"])
    ap.add_argument("--from-year", type=int, default=1970)
    ap.add_argument("--to-year", type=int, default=datetime.date.today().year)
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    f = Fetcher(delay=args.delay, cache_dir="cache_harvest")
    years = range(args.from_year, args.to_year + 1)
    sources = {"G": harvest_dnb, "F": harvest_bnf, "P": harvest_bn}

    for L in args.langs:
        print(f"\n{LANGS[L]['name']}:")
        rows, seen, stats = [], set(), {"records": 0, "in_language": 0, "with_original": 0}
        for lang, rec, r, author in sources[L](f, years):
            if r["record_id"] in seen:
                continue
            seen.add(r["record_id"])
            stats["records"] += 1
            if rec.is_serial() or re.search(r"(19|20)\d\d\s*-\s*$", r["pub_date"] or ""):
                stats["serials"] = stats.get("serials", 0) + 1
                continue                                   # series/periodical record, not a book
            if r["langs"] and not (r["langs"] & LANGS[L]["codes"]):
                continue
            if r["orig_lang"] and "eng" not in r["orig_lang"]:
                continue
            stats["in_language"] += 1
            # one row per distinct work: "Hazards of the heart, 1993" and "Hazards of the heart"
            # are the same title written twice, not two works
            by_norm = {}
            for o in r["original_titles"]:
                if o and norm(o) and norm(o) not in by_norm:
                    by_norm[norm(o)] = re.sub(r",?\s*(19|20)\d\d\s*$", "", o).strip(" ,")
            originals = list(by_norm.values()) or [""]
            if originals != [""]:
                stats["with_original"] += 1
            for o in originals:
                rows.append({
                    "language_letter": L, "authors": author, "original_title": o,
                    "translated_title": r["translated_title"], "pub_date": r["pub_date"],
                    "publisher": r["publisher"], "place": r["place"], "translators": r["translators"],
                    "series": r["series"], "isbn": r["isbn"], "format": "",
                    "copyright": r["responsibility"], "source": r["source"],
                    "source_record_id": r["record_id"], "url": "",
                })
        out = f"harvest_{L}.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
        print(f"  -> {out}: {stats['records']:,} records ({stats.get('serials', 0):,} series records skipped), "
              f"{stats['in_language']:,} translations from English "
              f"or unknown, {stats['with_original']:,} name an original title ({len(rows):,} rows)")


if __name__ == "__main__":
    main()
