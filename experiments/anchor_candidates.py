#!/usr/bin/env python3
"""
Link translations WITHOUT an original title to their English book using the ORDER of the
series, not the title (titles are transedited and cannot be trusted).

Idea
  Harlequin publishers abroad issued their collections in the same order as the English
  lines. Translations whose record DOES name the original are anchors:
      French "Collection Harlequin 433"  =  Harlequin Romance #2610
      French "Collection Harlequin 441"  =  Harlequin Romance #2625
  A record between two anchors in the same collection ("Collection Harlequin 437", author
  Anne Mather) must correspond to an English book between the two anchor books, in the
  same line - and by the same author. Often only one book fits.

Self-test
  Every anchor is hidden once and predicted from the others. The script reports how often
  a "unique" prediction is right, so you know how far to trust it before accepting anything.

Usage
  python anchor_candidates.py --lang F           # French (harvest_F.csv)
  python anchor_candidates.py --lang G           # German (harvest_G.csv, Cora series numbers)
  Options: --english harlequin_lines.csv english_additions.csv   --translations translations_all.csv
           --auto   pre-fill confirm=y for unique predictions (only if the self-test is good!)

Output
  anchored_review_<L>.csv  all suggestions, with a 'confirm' column (your marks are kept on re-runs)
  anchored_extra_<L>.csv   confirmed rows in --extra format -> pass to find_translations.py
"""
import argparse
import bisect
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

import difflib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root
from find_translations import CONFIRM_YES, fold, person

LANG_NAMES = {"F": "French", "G": "German", "P": "Polish"}


def split_series(s):
    """'Collection Harlequin 433' -> ('collection harlequin', 433); first series with a number."""
    for part in str(s or "").split(";"):
        m = re.match(r"^(.*?)[\s,.;:]*(?:n°|nr\.?|no\.?|band|bd\.?|vol\.?|t\.)?\s*(\d{1,5})\s*$", part.strip(), re.I)
        if m and m.group(1).strip():
            return fold(m.group(1)), int(m.group(2))
    return None, None


def year_of(s):
    m = re.search(r"(1[89]\d\d|20\d\d)", str(s or ""))
    return int(m.group(1)) if m else None


def english_memberships(row):
    """[(line, number)] from 'line'+'series_number' and 'all_lines' ('Harlequin Presents #12; ...')."""
    out = set()
    for m in re.finditer(r"([^;#]+?)\s*#\s*(\d+)", row.get("all_lines") or ""):
        out.add((m.group(1).strip(), int(m.group(2))))
    if row.get("line") and str(row.get("series_number") or "").split(".")[0].isdigit():
        out.add((row["line"], int(str(row["series_number"]).split(".")[0])))
    return list(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", required=True, choices=list(LANG_NAMES))
    ap.add_argument("--harvest", help="default: harvest_<lang>.csv")
    ap.add_argument("--english", nargs="+", default=["harlequin_lines.csv", "english_additions.csv"])
    ap.add_argument("--translations", default="translations_all.csv")
    ap.add_argument("--min-anchors", type=int, default=5, help="anchors needed per collection+line")
    ap.add_argument("--max-span", type=int, default=120,
                    help="max distance (in foreign series numbers) between the two surrounding anchors")
    ap.add_argument("--auto", action="store_true",
                    help="pre-fill confirm=y for unique predictions in collections that pass the self-test")
    ap.add_argument("--min-precision", type=float, default=0.95,
                    help="a collection counts as reliable if its unique predictions were right this often")
    ap.add_argument("--min-tests", type=int, default=20, help="...measured on at least this many hidden anchors")
    args = ap.parse_args()
    L = args.lang
    harvest = args.harvest or f"harvest_{L}.csv"
    review_path, extra_path = f"anchored_review_{L}.csv", f"anchored_extra_{L}.csv"

    # ---- English books: author -> [(line, number, book)] ------------------------------
    books, by_line = {}, defaultdict(dict)                  # by_line[line][number] = book_id
    author_books = defaultdict(list)
    for path in args.english:
        if not Path(path).exists():
            continue
        with open(path, encoding="utf-8-sig") as fh:
            for b in csv.DictReader(fh):
                if b["book_id"] in books:
                    continue
                books[b["book_id"]] = b
                for line, num in english_memberships(b):
                    by_line[line][num] = b["book_id"]
                    for a in (b.get("authors") or "").split(";"):
                        if a.strip():
                            author_books[fold(a)].append((line, num, b["book_id"]))
    author_keys = defaultdict(list)
    for k in author_books:
        author_keys[k[:1]].append(k)

    def find_author(name):
        f = fold(person(name or ""))
        if f in author_books:
            return f
        for k in author_keys.get(f[:1], []):
            if abs(len(k) - len(f)) <= 2 and difflib.SequenceMatcher(None, f, k).ratio() >= 0.92:
                return k
        return None

    # ---- translation records: one per record id ---------------------------------------
    recs = {}
    with open(harvest, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rid = r["source_record_id"]
            if rid not in recs:
                coll, num = split_series(r.get("series"))
                recs[rid] = {**r, "coll": coll, "num": num, "year": year_of(r.get("pub_date")),
                             "author_key": find_author((r.get("authors") or "").split(";")[0])}
    matched = {}
    with open(args.translations, encoding="utf-8-sig") as fh:
        for t in csv.DictReader(fh):
            if t.get("language_letter") == L and t.get("match_score") not in ("manual",):
                matched[t["source_record_id"]] = t["book_id"]

    # ---- anchors: (collection, line) -> sorted [(foreign_no, english_no, rid)] -----------
    anchors = defaultdict(list)
    for rid, bid in matched.items():
        r = recs.get(rid)
        if not r or r["num"] is None or bid not in books:
            continue
        for line, enum in english_memberships(books[bid]):
            anchors[(r["coll"], line)].append((r["num"], enum, rid))
    for k in anchors:
        anchors[k].sort()
    usable = {k: v for k, v in anchors.items() if len(v) >= args.min_anchors}
    lines_for_coll = defaultdict(list)
    for coll, line in usable:
        lines_for_coll[coll].append(line)
    print(f"{LANG_NAMES[L]}: {len(recs):,} records, {len(matched):,} already linked, "
          f"{len(usable)} collection/line pairs with >= {args.min_anchors} anchors")

    def predict(r, skip_rid=None):
        """Candidates [(score, line, eno, pred, lo, hi, book_id)] for record r."""
        out = []
        for line in lines_for_coll.get(r["coll"], []):
            pts = [(f, e) for f, e, rid in usable[(r["coll"], line)] if rid != skip_rid]
            fs = [f for f, _ in pts]
            i = bisect.bisect_left(fs, r["num"])
            if i == 0 or i >= len(pts):
                continue                                   # outside the anchored range
            (f1, e1), (f2, e2) = pts[i - 1], pts[i]
            if not (e1 <= e2) or f2 - f1 > args.max_span:  # non-monotonic or anchors too far apart
                continue
            pred = e1 + (r["num"] - f1) * (e2 - e1) / max(f2 - f1, 1)
            lo, hi = e1, e2
            for (l2, eno, bid) in author_books.get(r["author_key"], []):
                if l2 == line and lo <= eno <= hi:
                    out.append((1 - abs(eno - pred) / (hi - lo + 1), line, eno, round(pred), lo, hi, bid, f2 - f1))
        return sorted(out, key=lambda x: -x[0])

    # ---- self-test on the anchors, split by how far apart the surrounding anchors are ----
    def bucket(span):
        return "anchors <=10 apart" if span <= 10 else "anchors 11-40 apart" if span <= 40 else "anchors >40 apart"
    stats = defaultdict(lambda: [0, 0, 0, 0])          # tested, top right, unique, unique right
    per_coll = defaultdict(lambda: [0, 0])               # (coll, line) -> unique, unique right
    for (coll, line), pts in usable.items():
        for f, e, rid in pts:
            r = recs[rid]
            if not r["author_key"]:
                continue
            c = predict(r, skip_rid=rid)
            if not c:
                continue
            st = stats[bucket(c[0][7])]
            st[0] += 1
            st[1] += c[0][6] == matched[rid]
            if len({x[6] for x in c}) == 1:
                st[2] += 1
                st[3] += c[0][6] == matched[rid]
                pc = per_coll[(r["coll"], c[0][1])]
                pc[0] += 1
                pc[1] += c[0][6] == matched[rid]
    print("Self-test (each known link hidden once and predicted from the others):")
    for b in ("anchors <=10 apart", "anchors 11-40 apart", "anchors >40 apart"):
        t, top, u, ur = stats[b]
        if t:
            print(f"  {b:22} tested {t:6,} | best guess right {top / t:5.0%} | unique {u:6,}, right {ur / max(u, 1):6.1%}")

    reliable = {k for k, (u, ur) in per_coll.items() if u >= args.min_tests and ur / u >= args.min_precision}
    print(f"\nPer collection -> English line (unique predictions, >= {args.min_tests} tested):")
    ranked = sorted(((ur / u, u, k) for k, (u, ur) in per_coll.items() if u >= args.min_tests), reverse=True)
    for prec, u, (coll, line) in ranked[:25]:
        flag = "  RELIABLE" if (coll, line) in reliable else ""
        print(f"  {prec:6.1%} of {u:4}  {coll[:38]:38} -> {line}{flag}")
    print(f"{len(reliable)} collection/line pairs reach {args.min_precision:.0%}; --auto only accepts those.\n")

    # ---- predict for unlinked records ----------------------------------------------------
    previous = {}
    if Path(review_path).exists():
        with open(review_path, encoding="utf-8-sig") as fh:
            previous = {(r["source_record_id"], r["suggested_book_id"]): r["confirm"] for r in csv.DictReader(fh)}
    rows = []
    for rid, r in recs.items():
        if rid in matched or r["num"] is None or not r["author_key"]:
            continue
        c = predict(r)
        if not c:
            continue
        ids = list(dict.fromkeys(x[6] for x in c))
        for score, line, eno, pred, lo, hi, bid, span in c[:3]:
            b = books[bid]
            conf = "unique" if len(ids) == 1 else "several"
            good = (r["coll"], line) in reliable
            # your own marks from earlier runs win; otherwise --auto pre-fills unique predictions
            confirm = previous.get((rid, bid), "").strip() or ("y" if (args.auto and conf == "unique" and good) else "")
            rows.append({
                "confirm": confirm, "confidence": conf, "score": round(score, 3),
                "author": r.get("authors", ""), "collection": r.get("series", ""),
                "translated_title": r.get("translated_title", ""), "pub_date": r.get("pub_date", ""),
                "suggested_book_id": bid, "suggested_title": b["title"], "english_line": line,
                "english_number": eno, "predicted_number": pred, "window": f"{lo}-{hi}",
                "candidates": len(ids), "anchor_span": span,
                "collection_tested_precision": (f"{per_coll[(r['coll'], line)][1] / per_coll[(r['coll'], line)][0]:.0%}"
                                                if per_coll[(r["coll"], line)][0] else ""),
                "publisher": r.get("publisher", ""), "translators": r.get("translators", ""),
                "source": r.get("source", ""), "source_record_id": rid,
            })
    rows.sort(key=lambda x: (x["confidence"] != "unique", -x["score"]))
    cols = ["confirm", "confidence", "score", "author", "collection", "translated_title", "pub_date",
            "suggested_book_id", "suggested_title", "english_line", "english_number", "predicted_number",
            "window", "candidates", "anchor_span", "collection_tested_precision", "publisher", "translators", "source", "source_record_id"]
    with open(review_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # ---- confirmed rows -> extra-source file ---------------------------------------------
    ok = [x for x in rows if x["confirm"].strip().lower() in CONFIRM_YES]
    ecols = ["language_letter", "authors", "original_title", "translated_title", "pub_date", "publisher", "place",
             "translators", "series", "isbn", "format", "copyright", "source", "source_record_id", "url"]
    with open(extra_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=ecols)
        w.writeheader()
        for x in ok:
            b = books[x["suggested_book_id"]]
            w.writerow({"language_letter": L, "authors": b.get("authors", ""), "original_title": b["title"],
                        "translated_title": x["translated_title"], "pub_date": x["pub_date"],
                        "publisher": x["publisher"], "place": "", "translators": x["translators"],
                        "series": x["collection"], "isbn": "", "format": "",
                        "copyright": f"series-anchored ({x['english_line']} #{x['english_number']})",
                        "source": f"{x['source']} (series-anchored)", "source_record_id": x["source_record_id"],
                        "url": ""})
    n_unique = len({x["source_record_id"] for x in rows if x["confidence"] == "unique"})
    print(f"Suggestions for {len({x['source_record_id'] for x in rows}):,} unlinked records "
          f"({n_unique:,} unique) -> {review_path}")
    print(f"{len(ok):,} confirmed -> {extra_path}  (add it to find_translations.py --extra)")


if __name__ == "__main__":
    main()
