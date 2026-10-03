#!/usr/bin/env python3
"""
Match German Cora blurbs to English FictionDB descriptions via character names.

Idea: translators change titles, but they almost never change the characters' names.
"Dr. Christos Moustakas" and "Naomi" in a German blurb point to the English book whose
description mentions Christos, Moustakas and Naomi.

How it works
  * English side: every capitalised word (>= 3 letters) in a FictionDB description that is not
    sentence-initial and not a common English word is a name candidate. Each name gets a weight
    (IDF): rare names ("Moustakas") count much more than frequent ones ("Texas", "Jack").
  * German side: Cora blurbs of anthologies are split into one segment per story
    ("TITEL von AUTOR  text ..."). German capitalises all nouns, but German nouns rarely occur
    as names in English descriptions, so only words that ALSO appear as names on the English
    side are used.
  * Score of an English book = sum of the weights of the names shared with the segment.
    If the author is known (segment "von AUTOR" or the product's author tag), only that
    author's books are candidates.
  * The result is accepted when the best score is high enough AND clearly ahead of the
    second-best book (margin).

Self-test
  Cora products WITH an "Originaltitel" are the test set: their true English book is known.
  The script reports how often the name match picks the right book (precision) and for how
  many segments it makes a prediction at all (coverage), for several thresholds. Then it applies
  the chosen threshold to the products WITHOUT an original title.

Usage
  python name_match.py harlequin_lines.csv cora_all_products.csv
  python name_match.py harlequin_lines.csv english_additions.csv cora_all_products.csv  (more English books)

Outputs
  name_match_test.csv      every test segment: predicted book, true book(s), score, correct?
  name_match_new.csv       predictions for products without original title (above threshold)
  name_match_extra.csv     the same in the --extra format for find_translations.py
"""
import argparse
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict

import pandas as pd

COMMON_EN = set("""
the and but for with from into when what where while will would could should this that these those
there their they them then than have has had his her hers him she he you your our who whom whose
which after before again against all any are because been being both each few more most other
some such only own same very can just now not nor off out over under until about above below
between through during also even ever every here how its let may might must never once one two
three four five six seven eight nine ten first second last next new old other another yes no
mr mrs ms miss dr sir lady lord king queen prince princess duke earl count countess baron
december january february march april june july august september october november
monday tuesday wednesday thursday friday saturday sunday christmas easter valentine
god heaven hell love life man woman men women girl boy baby child children family father mother
dad mom mum son daughter brother sister husband wife bride groom heart night day time year
book series harlequin silhouette mills boon romance presents desire intrigue special edition
american historical blaze temptation superromance moments intimate bestselling award usa today
times york new author novel story stories now available ebook paperback
can't won't don't didn't isn't wasn't aren't couldn't wouldn't shouldn't he's she's it's
""".split())

# frequent German capitalised words that also exist as English names / words
COMMON_DE = set("""
Doch Aber Und Als Wie Was Wer Wenn Dann Denn Nur Noch Auch Schon Sie Ihr Ihre Ihren Ihrem Er Sein Seine
Seinen Seinem Die Der Das Den Dem Des Ein Eine Einen Einem Mit Von Bei Nach Vor Für Auf Aus Bis
Als Damit Dass Plötzlich Endlich Gerade Nicht Kein Keine Mann Frau Liebe Herz Nacht Tag Baby Hochzeit
Boss Chef Doktor Dr Prinz Prinzessin Graf Gräfin Lord Lady Sir Earl Duke Herzog Scheich Milliardär Millionär
Weihnachten Ostern Amerika Australien England London Paris Rom Texas Italien Griechenland Spanien Montana
Band Roman Romane Collection Doppelband Sammelband Julia Bianca Baccara Tiffany Historical
""".split())

WORD_RE = re.compile(r"\b[A-ZÄÖÜ][a-zäöüßéèáàíóúñç'’]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?\b")
SEG_RE = re.compile(r"([A-ZÄÖÜ0-9][A-ZÄÖÜß0-9 ,.!?’'&:–\-…]{5,})\s+von\s+"
                    r"([A-ZÄÖÜ][A-ZÄÖÜ.'’\-]+(?:,?\s+[A-ZÄÖÜ][A-ZÄÖÜ.'’\-]+){1,3})(?=\s)")

# a story title in capitals (>= 3 words) followed by normal text
CAPS_RE = re.compile(r"(?:^|(?<=\s))((?:[A-ZÄÖÜ0-9][A-ZÄÖÜß0-9’'!?.,:–\-…]*\s+){2,}[A-ZÄÖÜ0-9][A-ZÄÖÜß0-9’'!?.,:–\-…]*)"
                     r"(?=\s+[\"„“»(]?[A-ZÄÖÜ][a-zäöüß])")


def fold(s):
    s = re.sub(r"[’‘`´']", "", str(s or ""))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_title(s):
    s = fold(s)
    s = re.sub(r"^(the|a|an) ", "", s)
    return s


def author_key(name):
    """'RUTTAN, AMY' / 'Amy Ruttan' -> 'ruttan'  (surname is enough to filter)."""
    name = str(name or "").strip()
    if "," in name:
        name = name.split(",")[0]
    else:
        name = name.split()[-1] if name.split() else ""
    return fold(name)


def english_names(text):
    """Capitalised, non-sentence-initial words that are not common English words."""
    out = set()
    for sent in re.split(r"(?<=[.!?…])\s+|\n", str(text or "")):
        words = sent.split()
        for i, w in enumerate(words):
            m = WORD_RE.fullmatch(w.strip("\"'“”‘’()[],.;:!?…—–-*"))
            if not m:
                continue
            tok = m.group(0)
            if i == 0 and tok.lower() in COMMON_EN:
                continue
            if tok.lower() in COMMON_EN or len(tok) < 3:
                continue
            out.add(fold(tok))
    return out


def german_tokens(text):
    return {fold(m.group(0)) for m in WORD_RE.finditer(str(text or ""))
            if m.group(0) not in COMMON_DE and len(m.group(0)) >= 3}


def segments(blurb, product_authors):
    """Split anthology blurbs into one segment per story: (de_title, author_key, text)."""
    blurb = str(blurb or "")
    hits = list(SEG_RE.finditer(blurb))
    if len(hits) >= 1:
        segs = []
        for i, m in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(blurb)
            segs.append((m.group(1).strip(), author_key(m.group(2)), blurb[m.end():end]))
        return segs
    auth = [a for a in str(product_authors or "").split(";") if a.strip()]
    akey = author_key(auth[0]) if len(auth) == 1 else ""
    # series packs: "MEINE WIDERSPENSTIGE PRINZESSIN Liebe? Nein. ...  DIE BRAUT DES SCHEICHS Als ..."
    caps = list(CAPS_RE.finditer(blurb))
    if len(caps) >= 2:
        return [(m.group(1).strip(), akey, blurb[m.end():caps[i + 1].start() if i + 1 < len(caps) else len(blurb)])
                for i, m in enumerate(caps)]
    return [("", akey, blurb)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="English table(s) ... cora_all_products.csv (last)")
    ap.add_argument("--min-score", type=float, default=None, help="accept threshold (default: chosen from the test)")
    ap.add_argument("--target-precision", type=float, default=0.98)
    args = ap.parse_args()
    *eng_files, prod_file = args.files

    eng = pd.concat([pd.read_csv(f, dtype=str).fillna("") for f in eng_files], ignore_index=True)
    eng = eng.drop_duplicates("book_id")
    if "description" not in eng.columns:
        sys.exit("The English table has no 'description' column - run the scraper with --details.")
    eng = eng[eng["description"].str.len() > 40].reset_index(drop=True)
    print(f"{len(eng):,} English books with a description")

    # ---- English name index ----
    names_of = [english_names(d) for d in eng["description"]]
    df = Counter(n for s in names_of for n in s)
    N = len(eng)
    idf = {n: math.log(N / c) for n, c in df.items()}
    index = defaultdict(list)
    for i, s in enumerate(names_of):
        for n in s:
            index[n].append(i)
    authors_of = [{author_key(a) for a in str(x).split(";") if a.strip()} for x in eng["authors"]]
    by_author = defaultdict(set)
    for i, aa in enumerate(authors_of):
        for a in aa:
            by_author[a].add(i)
    title_idx = defaultdict(set)
    for i, t in enumerate(eng["title"]):
        for part in str(t).split("//"):          # FictionDB lists 2-in-1 volumes as "A // B"
            title_idx[norm_title(part)].add(i)

    desc_words = [set(fold(d).split()) for d in eng["description"]]

    def same_story(i, j):
        """Two FictionDB entries for one novel (UK Mills & Boon vs. North American title):
        same author and nearly the same description."""
        if not (authors_of[i] & authors_of[j]):
            return False
        a, b = desc_words[i], desc_words[j]
        return len(a & b) / max(1, len(a | b)) >= 0.5

    def predict(text, akey):
        toks = german_tokens(text) & idf.keys()
        scores = defaultdict(float)
        shared = defaultdict(list)
        allowed = by_author.get(akey) if akey else None
        for t in toks:
            if df[t] > 300:          # far too common to identify a book
                continue
            for i in index[t]:
                if allowed is not None and i not in allowed:
                    continue
                scores[i] += idf[t]
                shared[i].append(t)
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        best, s1 = ranked[0]
        s2 = ranked[1][1] if len(ranked) > 1 else 0.0
        return {"idx": best, "score": s1, "margin": s1 - s2, "shared": shared[best],
                "author_filter": allowed is not None}

    prod = pd.read_csv(prod_file, dtype=str).fillna("")
    prod["n"] = pd.to_numeric(prod["n_stories_with_original"], errors="coerce").fillna(0).astype(int)

    # ---- self-test on products with known originals ----
    rows = []
    for _, p in prod[prod["n"] > 0].iterrows():
        truth = set()
        for ot in p["original_titles"].split(" | "):
            nt = norm_title(ot)
            truth |= title_idx.get(nt, set())
            if ":" in ot:                         # "Little Secrets: His Pregnant Secretary"
                truth |= title_idx.get(norm_title(ot.split(":", 1)[1]), set())
        if not truth:
            continue                     # original not among the English books -> cannot test
        prod_akeys = {author_key(a) for a in p["authors"].split(";") if a.strip()}
        segs = segments(p["blurb"], p["authors"])
        if len(segs) > p["n"]:
            continue                     # more stories than known originals: truth incomplete
        for de_title, akey, text in segs:
            # same title by another author is a different book: keep only the author's books
            keys = {akey} if akey else prod_akeys
            seg_truth = {i for i in truth if authors_of[i] & keys} if keys else truth
            if not seg_truth:
                continue                 # this story's original (e.g. a UK title) is not in FictionDB
            for use_author in (True, False):
                r = predict(text, akey if use_author else "")
                rows.append({
                    "handle": p["handle"], "de_title": de_title or p["title"], "author_key": akey,
                    "mode": "with author" if use_author else "names only",
                    "predicted_book_id": eng.at[r["idx"], "book_id"] if r else "",
                    "predicted_title": eng.at[r["idx"], "title"] if r else "",
                    "true_titles": p["original_titles"],
                    "score": round(r["score"], 2) if r else 0, "margin": round(r["margin"], 2) if r else 0,
                    "shared_names": " ".join(r["shared"]) if r else "",
                    "correct": bool(r and (r["idx"] in seg_truth or any(same_story(r["idx"], j) for j in seg_truth))),
                    "author_filter_used": bool(r and r["author_filter"]),
                })
    test = pd.DataFrame(rows)
    test.to_csv("name_match_test.csv", index=False, encoding="utf-8-sig")
    print(f"Self-test: {test['handle'].nunique():,} products / {len(test) // 2:,} story segments "
          f"whose English original is known\n")

    def table(d):
        out = []
        for thr in (0, 5, 10, 15):
            for mthr in (0, 3, 5, 8, 10):
                sel = d[(d["score"] >= thr) & (d["margin"] >= mthr) & (d["predicted_book_id"] != "")]
                if len(sel) == 0:
                    continue
                out.append({"min_score": thr, "min_margin": mthr, "predictions": len(sel),
                            "coverage": len(sel) / len(d), "precision": sel["correct"].mean()})
        return pd.DataFrame(out)

    chosen = {}
    for mode in ("with author", "names only"):
        d = test[test["mode"] == mode]
        tb = table(d)
        print(f"--- {mode} ---")
        print(tb.to_string(index=False, formatters={"coverage": "{:.0%}".format, "precision": "{:.0%}".format}))
        ok = tb[tb["precision"] >= args.target_precision].sort_values("coverage", ascending=False)
        if len(ok):
            chosen[mode] = ok.iloc[0]
            c = ok.iloc[0]
            print(f"=> best setting with precision >= {args.target_precision:.0%}: score >= {c.min_score}, "
                  f"margin >= {c.min_margin}: {c.coverage:.0%} coverage at {c.precision:.0%} precision\n")
        else:
            print(f"=> no setting reaches {args.target_precision:.0%} precision\n")

    # ---- apply to products without original title ----
    if not chosen:
        print("No reliable setting - nothing applied.")
        return
    new = []
    for _, p in prod[prod["n"] == 0].iterrows():
        for de_title, akey, text in segments(p["blurb"], p["authors"]):
            mode = "with author" if akey and akey in by_author else "names only"
            if mode not in chosen:
                continue
            c = chosen[mode]
            r = predict(text, akey if mode == "with author" else "")
            if not r or r["score"] < c.min_score or r["margin"] < c.min_margin:
                continue
            b = eng.iloc[r["idx"]]
            new.append({"handle": p["handle"], "de_product_title": p["title"], "de_story_title": de_title,
                        "book_id": b["book_id"], "original_title": b["title"], "authors": b["authors"],
                        "mode": mode, "score": round(r["score"], 2), "margin": round(r["margin"], 2),
                        "shared_names": " ".join(r["shared"]), "pub_date": p["pub_date"], "isbn": p["isbn"],
                        "series": p["series"], "format": p["format"], "url": p["url"]})
    new = pd.DataFrame(new)
    new.to_csv("name_match_new.csv", index=False, encoding="utf-8-sig")
    if len(new):
        extra = pd.DataFrame({
            "language_letter": "G", "authors": new["authors"], "original_title": new["original_title"],
            "translated_title": new["de_story_title"].where(new["de_story_title"] != "", new["de_product_title"]),
            "pub_date": new["pub_date"], "publisher": "CORA Verlag", "place": "Hamburg", "translators": "",
            "series": new["series"], "isbn": new["isbn"], "format": new["format"], "source": "CORA (name match)",
            "source_record_id": "cora:" + new["handle"], "url": new["url"]})
        extra.to_csv("name_match_extra.csv", index=False, encoding="utf-8-sig")
    print(f"Products without original title: {int((prod['n'] == 0).sum()):,}; "
          f"name match accepted for {len(new):,} story segments "
          f"({new['handle'].nunique() if len(new) else 0:,} products) -> name_match_new.csv / name_match_extra.csv")


if __name__ == "__main__":
    main()
