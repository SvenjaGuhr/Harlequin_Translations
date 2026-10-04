#!/usr/bin/env python3
"""
Filter the matched translations down to books usable for the alignment study
and draw a stratified sample.

Two periods, because there were no Harlequin translations into Polish before 1991:

  early   English edition before --split-year (default 1991). The units are EN-FR and EN-DE
          pairs: the book needs a French and/or a German edition published before the split
          year (a contemporaneous translation). Books with both form EN-FR-DE triples and are
          preferred. A later Polish translation is not required (add it with --early-polish).
  late    English edition from the split year on. The book needs a French, a German AND a
          Polish edition (EN-FR-DE-PL quadruples).

Two levels:

  strict   Every required edition contains only this novel (no anthology), was matched with an
           exact original title (match_score >= 0.99) or by hand, and the original title comes
           from the record itself (not from character-name matching); the English original comes
           from FictionDB (exact date and line known).
  relaxed  Any matched edition counts (anthologies, title variants, name matches), and the
           original may come from Open Library.

Sampling: the sample is spread evenly across the decades of the English edition (decades with
too few books pass their surplus on), and within each decade evenly across lines (--lines
proportional to follow availability instead). Within each line x decade cell, early books with
both French and German come first, then books whose translators are all named.

For each book one edition per language is listed for acquisition: in the early period the
earliest edition before the split year; in the late period a single-novel edition naming its
translator if possible (--prefer earliest for the first translation instead).

Usage:
  python select_corpus.py                          # 250 books, strict, split 1991
  python select_corpus.py -n 300 --level relaxed
  python select_corpus.py --decades proportional   # follow availability over time
  python select_corpus.py --prefer earliest        # first translations also in the late period
  python select_corpus.py --split-year 0           # no split: FR-DE-PL for every book
  python select_corpus.py --min-year 1960          # include older English originals (default 1970)

Inputs (default names): translations_all.csv, harlequin_lines.csv, english_additions.csv
Outputs: usable_books.csv (all usable books, period, level, languages), core_sample.csv (the sample)
"""
import argparse
import re
from pathlib import Path

import pandas as pd

LANGS = ["F", "G", "P"]


def year_of(s):
    m = re.search(r"(1[89]\d\d|20\d\d)", str(s or ""))
    return int(m.group(1)) if m else None


def proportional(sizes, k):
    sizes = sizes[sizes > 0]
    if k <= 0 or sizes.empty:
        return pd.Series(0, index=sizes.index, dtype=int)
    quota = sizes / sizes.sum() * k
    a = quota.astype(int)
    for s in (quota - a).sort_values(ascending=False).index[: k - a.sum()]:
        a[s] += 1
    return a


def even(avail, k):
    """Spread k evenly over groups; groups with fewer items pass their surplus on."""
    avail = avail[avail > 0]
    got = pd.Series(0, index=avail.index, dtype=int)
    left = min(k, int(avail.sum()))
    while left > 0:
        open_ = got[got < avail].index
        share = max(1, left // len(open_))
        for g in open_:
            add = int(min(share, avail[g] - got[g], left))
            got[g] += add
            left -= add
            if left == 0:
                break
    return got


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--translations", default="translations_all.csv")
    ap.add_argument("--english", nargs="+", default=["harlequin_lines.csv", "english_additions.csv"])
    ap.add_argument("-n", type=int, default=250, help="sample size in books (default 250)")
    ap.add_argument("--level", choices=["strict", "relaxed"], default="strict")
    ap.add_argument("--split-year", type=int, default=1991,
                    help="first year with Polish translations (default 1991); 0 = no split")
    ap.add_argument("--min-year", type=int, default=1970,
                    help="earliest English edition year (default 1970: the French and German Harlequin "
                         "programmes start in the late 1970s; older backlist titles would have decades of lag)")
    ap.add_argument("--early-polish", action="store_true",
                    help="also list a (later) Polish edition for early books where one exists")
    ap.add_argument("--decades", choices=["even", "proportional"], default="even")
    ap.add_argument("--lines", choices=["even", "proportional"], default="even")
    ap.add_argument("--prefer", choices=["translator", "earliest"], default="translator",
                    help="edition choice in the late period (early period: always earliest)")
    ap.add_argument("--translators", action="store_true", help="only books whose translators are all named")
    ap.add_argument("--balanced", action="store_true", help=argparse.SUPPRESS)   # old name, now default
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--usable-out", default="usable_books.csv")
    ap.add_argument("-o", "--output", default="core_sample.csv")
    args = ap.parse_args()
    split = args.split_year or 0

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
    # date of the translation: where the publisher states the first edition (CORA imprints:
    # "Deutsche Erstausgabe ... 1987"), use it instead of the date of a later reissue / e-book
    if "first_edition_year" in t.columns:
        fe = pd.to_numeric(t["first_edition_year"], errors="coerce")
        t["year"] = t["year"].where(fe.isna() | (fe >= t["year"]), fe)
    t["name_match"] = t["source"].str.contains("name match", case=False, na=False)
    # strict: catalogue/publisher evidence only; character-name matches (98 % precision,
    # mostly e-book products) count at the relaxed level
    t["strict_ok"] = t["exact"] & t["single"] & ~t["name_match"]
    t["early_ed"] = t["year"] < split if split else False
    t["any_ok"] = True

    def flags(col):
        """book x language: is there an edition satisfying col (and, for early, before the split)?"""
        g = t.groupby(["book_id", "language_letter"])
        out = {}
        for name, mask in [("all", t[col]), ("early", t[col] & t["early_ed"]),
                           ("tr", t[col] & t["has_translator"]),
                           ("early_tr", t[col] & t["early_ed"] & t["has_translator"])]:
            out[name] = mask.groupby([t["book_id"], t["language_letter"]]).any()
        df = pd.DataFrame(out).unstack("language_letter").fillna(False)
        for k in ("all", "early", "tr", "early_tr"):
            for L in LANGS:
                if (k, L) not in df.columns:
                    df[(k, L)] = False
        return df

    books = eng.set_index("book_id")[["title", "authors", "line", "pub_date", "year", "decade", "from_fictiondb"]]
    books = books[books.index.isin(t["book_id"]) & (books["year"].fillna(0) >= args.min_year)].copy()
    books["period"] = books["year"].map(lambda y: "early" if split and pd.notna(y) and y < split else "late")

    for level, col in [("strict", "strict_ok"), ("relaxed", "any_ok")]:
        f = flags(col).reindex(books.index).fillna(False)
        early = books["period"] == "early"
        late = ~early
        # languages available for the study unit
        langs_early = (f[("early", "F")].map({True: "F", False: ""}) + f[("early", "G")].map({True: "G", False: ""}))
        ok_late = f[("all", "F")] & f[("all", "G")] & f[("all", "P")]
        ok_early = f[("early", "F")] | f[("early", "G")]
        ok = (early & ok_early) | (late & ok_late)
        tr_late = f[("tr", "F")] & f[("tr", "G")] & f[("tr", "P")]
        tr_early = ((~f[("early", "F")] | f[("early_tr", "F")]) & (~f[("early", "G")] | f[("early_tr", "G")]))
        books[level] = ok
        books[level + "_tr"] = ok & ((early & tr_early) | (late & tr_late))
        books[level + "_langs"] = langs_early.where(early, "FGP").where(ok, "")
        if level == "strict":
            books["strict"] &= books["from_fictiondb"]
            books["strict_tr"] &= books["from_fictiondb"]
        books[level + "_has_P"] = f[("all", "P")]

    # ---- report ----
    e, l = books["period"] == "early", books["period"] == "late"
    for level in ("strict", "relaxed"):
        lg = books[level + "_langs"]
        print(f"[{level}]")
        if split:
            ok = e & books[level]
            print(f"  before {split} (EN-FR / EN-DE): {int(ok.sum()):,} books"
                  f"  - FR+DE {int((ok & (lg == 'FG')).sum()):,}, FR only {int((ok & (lg == 'F')).sum()):,},"
                  f" DE only {int((ok & (lg == 'G')).sum()):,}")
        print(f"  {'from ' + str(split) if split else 'all years'} (EN-FR-DE-PL): {int((l & books[level]).sum()):,} books"
              f"  - translators all named: {int((l & books[level + '_tr']).sum()):,}")

    usable = books[books["relaxed"]].copy()
    usable["level"] = usable["strict"].map({True: "strict", False: "relaxed"})
    usable["languages"] = usable["strict_langs"].where(usable["strict"], usable["relaxed_langs"])
    usable = usable[["title", "authors", "line", "pub_date", "decade", "period", "level", "languages"]]
    usable.reset_index().to_csv(args.usable_out, index=False, encoding="utf-8-sig")
    print(f"All usable books -> {args.usable_out}")

    # ---- pool and stratified sample ----
    flag = args.level + ("_tr" if args.translators else "")
    pool = books[books[flag]].copy()
    pool["all_tr"] = books.loc[pool.index, args.level + "_tr"]
    pool["n_langs"] = pool[args.level + "_langs"].str.len()
    n = min(args.n, len(pool))
    if n < args.n:
        print(f"! only {len(pool)} books in the pool - sampling all of them")
    pool["line"] = pool["line"].fillna("?")

    dec_avail = pool["decade"].value_counts()
    dec_alloc = even(dec_avail, n) if args.decades == "even" else proportional(dec_avail, n)
    picked = []
    for dec, k in dec_alloc.items():
        sub = pool[pool["decade"] == dec]
        line_avail = sub["line"].value_counts()
        line_alloc = even(line_avail, int(k)) if args.lines == "even" else proportional(line_avail, int(k))
        for ln, kk in line_alloc.items():
            if kk == 0:
                continue
            cell = sub[sub["line"] == ln].sample(frac=1, random_state=args.seed)
            cell = cell.sort_values(["n_langs", "all_tr"], ascending=False, kind="stable")
            picked.append(cell.head(int(kk)))
    sample = pd.concat(picked)

    # ---- one edition per language to acquire ----
    def best_edition(bid, L, early_period):
        e = t[(t["book_id"] == bid) & (t["language_letter"] == L)].copy()
        if early_period and L != "P":
            e = e[e["early_ed"]]
        if args.level == "strict" and e["strict_ok"].any():
            e = e[e["strict_ok"]]
        if e.empty:
            return None
        if early_period or args.prefer == "earliest":
            e = e.sort_values(["single", "year", "has_translator"], ascending=[False, True, False])
        else:
            e = e.sort_values(["single", "has_translator", "exact", "year"], ascending=[False, False, False, True])
        return e.iloc[0]

    rows = []
    for bid, b in sample.iterrows():
        early_period = b["period"] == "early"
        r = {"book_id": bid, "title": b["title"], "authors": b["authors"], "line": b["line"],
             "pub_date": b["pub_date"], "decade": b["decade"], "period": b["period"],
             "languages": b[args.level + "_langs"], "all_translators_known": bool(b["all_tr"])}
        for L in LANGS:
            e = None
            if L in r["languages"] or (L == "P" and early_period and args.early_polish and b[args.level + "_has_P"]):
                e = best_edition(bid, L, early_period)
            if L == "P" and early_period and e is not None:
                r["languages"] += "P"
            fields = {"id": "translation_id", "title": "translated_title", "year": "pub_year",
                      "translators": "translators", "isbn": "isbn", "series": "series", "edition_type": "edition_type"}
            for k, c in fields.items():
                r[f"{L}_{k}"] = "" if e is None else e[c]
        rows.append(r)
    out = pd.DataFrame(rows).sort_values(["period", "line", "pub_date"])
    out.to_csv(args.output, index=False, encoding="utf-8-sig")

    vols = len(out) + sum((out[f"{L}_id"] != "").sum() for L in LANGS)
    print(f"\nSample: {len(out)} books, {vols} volumes incl. English "
          f"({args.level}{', translators named' if args.translators else ''}; decades {args.decades}, lines {args.lines})"
          f" -> {args.output}")
    if split:
        el = out.loc[out["period"] == "early", "languages"].str.replace("P", "")
        print(f"  before {split}: {len(el)} books "
              f"(FR+DE {int((el == 'FG').sum())}, FR only {int((el == 'F').sum())}, DE only {int((el == 'G').sum())})"
              f"; from {split}: {int((out['period'] == 'late').sum())} books (FR+DE+PL)")
    print(f"  all translators named: {int(out['all_translators_known'].sum())}")
    print("  by decade:\n" + out["decade"].value_counts().sort_index().to_string().replace("\n", "\n    ").join(["    ", ""]))
    print("  by line:\n" + out["line"].value_counts().to_string().replace("\n", "\n    ").join(["    ", ""]))


if __name__ == "__main__":
    main()
