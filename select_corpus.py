#!/usr/bin/env python3
"""
Filter the matched translations down to books usable for the alignment study
and draw a stratified sample.

A book is usable when it has a French, a German AND a Polish edition. Two levels:

  strict   For each language there is at least one edition that
             - contains only this novel (edition_type = single, not an anthology),
             - was matched with an exact original title (match_score >= 0.99) or by hand,
             and the original title comes from the record itself (not from name matching),
           and the English original comes from FictionDB (exact date and line known).
  relaxed  Any matched edition counts (anthologies too, fuzzy title matches above the
           matching threshold, character-name matches from name_match.py), and the original
           may come from Open Library.

For both levels the script also counts books where every chosen edition names its translator.

The sample is stratified by line x decade of the English edition (proportional allocation),
and within each stratum prefers books with known translators. For each sampled book it lists
one edition per language to buy: a single-novel edition with translator if possible, else the
earliest edition.

Usage:
  python select_corpus.py                       # 250 books from the strict set
  python select_corpus.py -n 300 --level relaxed
  python select_corpus.py --translators         # sample only books with all translators known
  python select_corpus.py --balanced            # spread evenly across lines (not proportional)

Inputs (default names): translations_all.csv, harlequin_lines.csv, english_additions.csv
Outputs: usable_books.csv (all usable books with their level), core_sample.csv (the sample)
"""
import argparse
import re
from pathlib import Path

import pandas as pd

LANGS = ["F", "G", "P"]


def year_of(s):
    m = re.search(r"(1[89]\d\d|20\d\d)", str(s or ""))
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--translations", default="translations_all.csv")
    ap.add_argument("--english", nargs="+", default=["harlequin_lines.csv", "english_additions.csv"])
    ap.add_argument("-n", type=int, default=250, help="sample size (default 250)")
    ap.add_argument("--level", choices=["strict", "relaxed"], default="strict")
    ap.add_argument("--translators", action="store_true", help="only books with all translators known")
    ap.add_argument("--balanced", action="store_true",
                    help="spread the sample evenly across lines (instead of proportional to availability)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--usable-out", default="usable_books.csv")
    ap.add_argument("-o", "--output", default="core_sample.csv")
    args = ap.parse_args()

    t = pd.read_csv(args.translations, dtype=str).fillna("")
    eng = pd.concat([pd.read_csv(p, dtype=str).fillna("") for p in args.english if Path(p).exists()],
                    ignore_index=True).drop_duplicates("book_id")
    eng["from_fictiondb"] = ~eng["line"].str.contains("Open Library", na=False)
    eng["year"] = eng["pub_date"].map(year_of)
    eng["decade"] = eng["year"].map(lambda y: f"{int(y) // 10 * 10}s" if pd.notna(y) and y else "unknown")

    score = pd.to_numeric(t["match_score"], errors="coerce")
    t["exact"] = (score >= 0.99) | (t["match_score"] == "manual")
    t["single"] = t["edition_type"] != "anthology"
    t["has_translator"] = t["translators"].str.strip() != ""
    t["year"] = pd.to_numeric(t["pub_year"], errors="coerce")
    t["name_match"] = t["source"].str.contains("name match", case=False, na=False)
    # strict: catalogue/publisher evidence only; character-name matches (98 % precision,
    # mostly e-book products) count at the relaxed level
    t["strict_ok"] = t["exact"] & t["single"] & ~t["name_match"]
    t["strict_tr_ok"] = t["strict_ok"] & t["has_translator"]

    # per book and language: is there any edition / a strict edition / one with translator?
    g = t.groupby(["book_id", "language_letter"])
    per = pd.DataFrame({
        "any": g.size() > 0,
        "strict": g["strict_ok"].any(),
        "tr_any": g["has_translator"].any(),
        "tr_strict": g["strict_tr_ok"].any(),
    }).unstack("language_letter").fillna(False)

    def all3(col):
        cols = [(col, L) for L in LANGS]
        if any(c not in per.columns for c in cols):
            return pd.Series(False, index=per.index)
        return per[cols].all(axis=1)

    books = pd.DataFrame(index=per.index)
    books["relaxed"] = all3("any")
    books["relaxed_tr"] = all3("tr_any")
    books["strict"] = all3("strict")
    books["strict_tr"] = all3("tr_strict")
    books = books.join(eng.set_index("book_id")[["title", "authors", "line", "pub_date", "decade", "from_fictiondb"]])
    books["from_fictiondb"] = books["from_fictiondb"].fillna(False).astype(bool)
    books["strict"] &= books["from_fictiondb"]
    books["strict_tr"] &= books["from_fictiondb"]

    print(f"Books in all three languages (relaxed): {int(books['relaxed'].sum()):,}"
          f"  - with all translators known: {int(books['relaxed_tr'].sum()):,}")
    print(f"Books in all three languages (strict):  {int(books['strict'].sum()):,}"
          f"  - with all translators known: {int(books['strict_tr'].sum()):,}")

    usable = books[books["relaxed"]].copy()
    usable["level"] = usable["strict"].map({True: "strict", False: "relaxed"})
    usable.reset_index().to_csv(args.usable_out, index=False, encoding="utf-8-sig")
    print(f"All usable books -> {args.usable_out}")

    # ---- pool and stratified sample ----
    flag = args.level + ("_tr" if args.translators else "")
    pool = books[books[flag]].copy()
    pool["all_tr"] = books.loc[pool.index, args.level + "_tr"]
    n = min(args.n, len(pool))
    if n < args.n:
        print(f"! only {len(pool)} books in the pool - sampling all of them")
    pool["stratum"] = pool["line"].fillna("?") + " | " + pool["decade"].fillna("?")
    def proportional(sizes, k):
        quota = sizes / sizes.sum() * k
        a = quota.astype(int)
        for s in (quota - a).sort_values(ascending=False).index[: k - a.sum()]:
            a[s] += 1
        return a

    if args.balanced:
        # equal share per line; lines with fewer books give their surplus to the others
        avail = pool["line"].fillna("?").value_counts()
        per_line = pd.Series(0, index=avail.index)
        left = n
        while left > 0:
            open_lines = per_line[per_line < avail].index
            share = max(1, left // len(open_lines))
            for ln in open_lines:
                add = min(share, avail[ln] - per_line[ln], left)
                per_line[ln] += add
                left -= add
                if left == 0:
                    break
        alloc = pd.concat([proportional(pool[pool["line"].fillna("?") == ln]["stratum"].value_counts(), int(k))
                           for ln, k in per_line.items() if k > 0])
    else:
        alloc = proportional(pool["stratum"].value_counts(), n)

    picked = []
    for s, k in alloc.items():
        if k == 0:
            continue
        sub = pool[pool["stratum"] == s].sample(frac=1, random_state=args.seed)
        sub = sub.sort_values("all_tr", ascending=False, kind="stable")   # translators known first
        picked.append(sub.head(k))
    sample = pd.concat(picked)

    # one edition per language to buy
    def best_edition(bid, L):
        e = t[(t["book_id"] == bid) & (t["language_letter"] == L)].copy()
        if args.level == "strict":
            e = e[e["strict_ok"]] if e["strict_ok"].any() else e
        e = e.sort_values(["single", "has_translator", "exact", "year"],
                          ascending=[False, False, False, True])
        return e.iloc[0]

    rows = []
    for bid, b in sample.iterrows():
        r = {"book_id": bid, "title": b["title"], "authors": b["authors"], "line": b["line"],
             "pub_date": b["pub_date"], "decade": b["decade"], "all_translators_known": bool(b["all_tr"])}
        for L in LANGS:
            e = best_edition(bid, L)
            r.update({f"{L}_id": e["translation_id"], f"{L}_title": e["translated_title"],
                      f"{L}_year": e["pub_year"], f"{L}_translators": e["translators"],
                      f"{L}_isbn": e["isbn"], f"{L}_series": e["series"],
                      f"{L}_edition_type": e["edition_type"]})
        rows.append(r)
    out = pd.DataFrame(rows).sort_values(["line", "pub_date"])
    out.to_csv(args.output, index=False, encoding="utf-8-sig")

    print(f"\nSample: {len(out)} books ({args.level}{', translators known' if args.translators else ''})"
          f"{', balanced across lines' if args.balanced else ''} -> {args.output}")
    print(f"  all translators known: {int(out['all_translators_known'].sum())}")
    print("  by line:\n" + out["line"].value_counts().to_string().replace("\n", "\n    ").join(["    ", ""]))
    print("  by decade:\n" + out["decade"].value_counts().sort_index().to_string().replace("\n", "\n    ").join(["    ", ""]))


if __name__ == "__main__":
    main()
