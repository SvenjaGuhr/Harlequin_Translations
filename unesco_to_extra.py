#!/usr/bin/env python3
"""
Convert the UNESCO Index Translationum export (open-data sample, 1978-2008) into the
extra-source format for find_translations.py --extra.

The export has no author column, so entries are matched to your series list by
ORIGINAL TITLE only, and kept only when
  * the translation is French, German or Polish,
  * the publisher is a romance publisher (Harlequin, Cora, Arlekin, Silhouette, Mira), and
  * it appeared 0-15 years after the English original.
Rows are marked "title-only match" - check them.

Usage
  python unesco_to_extra.py harlequin_lines.csv index_translationum.csv
  -> index_translationum_extra.csv
"""
import argparse
import ast
import csv
import re

import pandas as pd

from find_translations import norm

LANG = {"French": "F", "German": "G", "Polish": "P"}
ROMANCE = r"harlequin|cora|arlekin|silhouette|mira"
COLS = ["language_letter", "authors", "original_title", "translated_title", "pub_date", "publisher", "place",
        "translators", "series", "isbn", "format", "copyright", "source", "source_record_id", "url"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("series_csv")
    ap.add_argument("unesco_csv")
    ap.add_argument("-o", "--output", default="index_translationum_extra.csv")
    ap.add_argument("--max-gap", type=int, default=15)
    args = ap.parse_args()

    df = pd.read_csv(args.unesco_csv, sep=None, engine="python", encoding="utf-8-sig")
    df.columns = [c.lstrip("﻿") for c in df.columns]
    t = pd.DataFrame([{**x, "original_title": r["original_title"], "uuid": r["uuid"]}
                      for _, r in df.iterrows() for x in ast.literal_eval(r["translations"])])
    t = t[t["translated_title_language"].isin(LANG)].copy()
    t["n"] = t["original_title"].astype(str).str.replace(r"<([^>]*)>", r"\1", regex=True).map(norm)

    b = pd.read_csv(args.series_csv, encoding="utf-8-sig")
    b["n"] = b["title"].map(norm)
    b["y"] = pd.to_numeric(b["pub_date"].astype(str).str.extract(r"((?:19|20)\d\d)")[0], errors="coerce")

    j = t.merge(b[["n", "title", "authors", "y"]], on="n")
    j = j[(j["year"] - j["y"]).between(0, args.max_gap)
          & j["publisher_name"].fillna("").str.contains(ROMANCE, case=False)]

    clean = lambda s: re.sub(r"[<>]", "", str(s)).strip() if pd.notna(s) else ""

    def person(s):                       # UNESCO writes "Lamorlette M.-J." -> "M.-J. Lamorlette"
        if pd.isna(s):
            return ""
        p = str(s).split()
        return " ".join(p[1:] + p[:1]) if len(p) > 1 else str(s)

    rows = [{
        "language_letter": LANG[r["translated_title_language"]], "authors": r["authors"],
        "original_title": r["title"], "translated_title": clean(r["translated_title"]),
        "pub_date": str(int(r["year"])), "publisher": r["publisher_name"], "place": clean(r.get("place")),
        "translators": person(r.get("translator_name")), "series": "", "isbn": "", "format": "",
        "copyright": "title-only match (UNESCO sample has no author field)", "source": "UNESCO",
        "source_record_id": f"unesco:{r['uuid']}:{LANG[r['translated_title_language']]}{int(r['year'])}:{i}",
        "url": "",
    } for i, (_, r) in enumerate(j.iterrows())]

    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} plausible UNESCO editions -> {args.output}")


if __name__ == "__main__":
    main()
