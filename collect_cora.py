#!/usr/bin/env python3
"""
Collect German Harlequin translations from the CORA Verlag shop (cora.de).

CORA is the German Harlequin publisher. Its product pages carry an impressum
with, for every story in the book:
    © 2019 by Harlequin Enterprises ULC
    Originaltitel: "The Rancher's Full House"
    Übersetzung: Stephanie Thoma-Kellner
This covers the magazine-style series (Bianca, Julia, Tiffany, Romana, ...) that
the German National Library often catalogues without the original title.

Steps
  1. Download the full product list (cora.de/products.json, 250 per page).
  2. Keep products tagged with an author from your series CSV.
  3. Open each kept product page and read the Originaltitel / Übersetzung blocks.
  4. Write one row per story to cora_editions.csv, in the "extra source" format
     that find_translations.py can merge (--extra cora_editions.csv).

Usage
  pip install requests beautifulsoup4
  python collect_cora.py harlequin_american_romance.csv
  python collect_cora.py harlequin_american_romance.csv --max-pages 3   # quick test

Note: the shop lists what CORA currently sells (print + e-book, incl. many
e-book reissues of older titles). Long out-of-print Hefte are not there.
Please keep the delay polite.
"""

import argparse
import difflib
import csv
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.cora.de"
HEADERS = {"User-Agent": "Mozilla/5.0 (research script; translation metadata)"}

EXTRA_COLS = ["language_letter", "authors", "original_title", "translated_title", "pub_date", "publisher",
              "place", "translators", "series", "isbn", "format", "copyright", "original_series",
              "first_edition", "first_edition_year", "source", "source_record_id", "url", "blurb"]
PRODUCT_COLS = ["handle", "title", "authors", "series", "pub_date", "isbn", "format", "n_stories_with_original",
                "original_titles", "blurb", "url"]


class Fetcher:
    def __init__(self, delay=1.0, cache_dir="cache_cora", retries=4):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay, self.retries = delay, retries
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)

    def get(self, url, use_cache=True):
        f = self.cache / (hashlib.sha1(url.encode()).hexdigest() + ".txt")
        if use_cache and f.exists():
            return f.read_text(encoding="utf-8")
        for attempt in range(1, self.retries + 1):
            try:
                r = self.s.get(url, timeout=60)
                if r.status_code == 404:
                    return ""
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                r.encoding = r.encoding or "utf-8"
                f.write_text(r.text, encoding="utf-8")
                time.sleep(self.delay)
                return r.text
            except requests.RequestException as e:
                wait = self.delay * 2 ** attempt
                print(f"  ! {e} - retry {attempt} in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
        print(f"  ! giving up on {url}", file=sys.stderr)
        return ""


def fold(s):
    """lowercase, no accents, single spaces - for comparing author names."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# --------------------------------------------------------------------------- #
# 1-2. Product list, filtered by author tags
# --------------------------------------------------------------------------- #
def all_products(f: Fetcher, max_pages, refresh):
    page = 1
    while page <= max_pages:
        txt = f.get(f"{BASE}/products.json?limit=250&page={page}", use_cache=not refresh)
        try:
            prods = json.loads(txt).get("products", []) if txt else []
        except json.JSONDecodeError:
            print(f"  ! page {page}: not JSON (blocked or changed?)", file=sys.stderr)
            break
        if not prods:
            break
        print(f"  product list page {page}: {len(prods)} products")
        yield from prods
        page += 1


# tags that look like a person's name ("Dani Collins"), not a series or genre ("Julia Extra")
NAME_TAG = re.compile(r"^[A-ZÄÖÜ][\w'’.\-]+(?: [A-ZÄÖÜ][\w'’.\-]+){1,3}$")
NOT_NAMES = {"Julia", "Bianca", "Tiffany", "Romana", "Baccara", "Historical", "Mira", "Cora", "Extra", "Exklusiv",
             "Band", "Collection", "Weekend", "Edition", "Saison", "Special", "Sommer", "Winter", "Liebesroman",
             "Liebesromane", "Bestseller", "Gold", "Premium", "Lords", "Ladies", "Kiss", "Serie", "Ärzte", "Arzt"}


def match_author(tag, authors, fuzzy_index):
    f = fold(tag)
    if f in authors:
        return authors[f]
    if len(f) >= 8:
        for key in fuzzy_index.get(f[:1], []):
            if abs(len(key) - len(f)) <= 2 and difflib.SequenceMatcher(None, f, key).ratio() >= 0.92:
                return authors[key]
    return None


def product_tags(p):
    tags = p.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return [t for t in tags if t]


# --------------------------------------------------------------------------- #
# 3. Product page -> stories
# --------------------------------------------------------------------------- #
Q_OPEN, Q_CLOSE = "\"'„“‚‘»›«‹", "\"'“”‘’«‹»›"
ORIG_RE = re.compile(r"Originaltitel\s*:\s*(.+)", re.I)
TRANS_RE = re.compile(r"Übersetzung\s*:\s*(.+)", re.I)
COPY_RE = re.compile(r"©\s*((?:19|20)\d\d)\s*(?:by\s+)?(.+)", re.I)
ORIG_SERIES_RE = re.compile(r"in der Reihe\s*:?\s*([A-Z][A-Z0-9 &'’.\-]{2,})", re.U)
FIRST_ED_RE = re.compile(r"Deutsche Erstausgabe\s+(?:in der Reihe\s*:?\s*)?(.+?)(?:\s+by\b|$)", re.I)
BAND_RE = re.compile(r"Band\s+(\d+)[^|]*?((?:19|20)\d\d)", re.I)
DATE_RE = re.compile(r"(?:Erscheinungs(?:tag|termin|datum)|Erscheint am|Veröffentlicht)\s*:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", re.I)
ISBN_RE = re.compile(r"\b(97[89]\d{10})\b")


def clean_title(t):
    t = re.split(r"\s+erschienen\s+bei\b|\s+Published by\b|\s+©", t, flags=re.I)[0]
    t = t.strip().strip(Q_OPEN + Q_CLOSE).strip()
    return re.sub(r"\s+", " ", t).rstrip(" .,;")


def parse_product_page(html):
    """Return (stories, page_info). stories: [{original_title, translators, copyright}]"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    # some shops put the whole impressum on one line separated by <br> or '|'
    lines = [l.strip() for l in re.split(r"\n|\|", text) if l.strip()]

    stories, last_copy = [], ""
    orig_idx = [i for i, l in enumerate(lines) if ORIG_RE.search(l)]
    for k, i in enumerate(orig_idx):
        # the story's block runs from its Originaltitel to the next Originaltitel
        end = orig_idx[k + 1] if k + 1 < len(orig_idx) else min(len(lines), i + 25)
        block = lines[i:end]
        for l in lines[max(0, i - 3):i + 1]:          # the © line just before the title
            c = COPY_RE.search(l)
            if c:
                last_copy = f"{c.group(1)} {c.group(2).strip()}"
        rest = ORIG_RE.search(lines[i]).group(1)
        tr_same = TRANS_RE.search(rest)
        title = clean_title(TRANS_RE.split(rest)[0] if tr_same else rest)
        translators = tr_same.group(1) if tr_same else ""
        orig_series = first_ed = first_year = ""
        joined = " | ".join(block)
        for l in block[1:]:
            if not translators:
                t2 = TRANS_RE.search(l)
                if t2:
                    translators = t2.group(1)
            if not orig_series and "Erstausgabe" not in l:
                m2 = ORIG_SERIES_RE.search(l)
                if m2:
                    orig_series = m2.group(1).strip().title()
        fe = FIRST_ED_RE.search(joined)
        if fe:
            first_ed = re.split(r"\s*\|\s*", fe.group(1))[0].strip(" :").title()
            b = BAND_RE.search(joined[fe.start():])
            if b:
                first_ed = f"{first_ed} {b.group(1)}".strip()
                first_year = b.group(2)
        translators = re.split(r"\s+©|\s+Originaltitel|\s*\|", translators)[0].strip().rstrip(".")
        if title:
            stories.append({"original_title": title, "translators": translators, "copyright": last_copy,
                            "original_series": orig_series, "first_edition": first_ed,
                            "first_edition_year": first_year})

    date = DATE_RE.search(text)
    isbn = ISBN_RE.search(text)
    return stories, {"pub_date": date.group(1) if date else "", "isbn": isbn.group(1) if isbn else ""}


def blurb_of(product, html):
    """German back-cover text: the shop's product description (body_html), else the page's meta description."""
    text = ""
    if product.get("body_html"):
        text = BeautifulSoup(product["body_html"], "html.parser").get_text(" ")
    if not text.strip() and html:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
        text = tag.get("content", "") if tag else ""
    return re.sub(r"\s+", " ", text).strip()[:4000]


def series_of(title, tags):
    m = re.match(r"^(.*?)\s+(?:Band|Bd\.|Nr\.)\s*(\d+)", title or "", re.I)
    if m:
        return f"{m.group(1).strip()} {m.group(2)}"
    for t in tags:
        if t.lower().startswith("reihe:"):
            return t.split(":", 1)[1].strip()
    return ""


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="series CSV (needs an 'authors' column)")
    ap.add_argument("-o", "--output", default="cora_editions.csv")
    ap.add_argument("--max-pages", type=int, default=400, help="max product-list pages (250 products each)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--refresh-list", action="store_true", help="re-download the product list (ignore cache)")
    ap.add_argument("--all", action="store_true",
                    help="read EVERY product page, not only those tagged with an author from the input table "
                         "(complete German picture; many more pages)")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8-sig") as fh:
        authors = {fold(a): a.strip() for row in csv.DictReader(fh)
                   for a in (row.get("authors") or "").split(";") if a.strip()}
    print(f"{len(authors)} series authors")
    fuzzy_index = {}
    for key in authors:
        fuzzy_index.setdefault(key[:1], []).append(key)

    f = Fetcher(delay=args.delay)
    kept, seen = [], set()
    n_total = 0
    for p in all_products(f, args.max_pages, args.refresh_list):
        n_total += 1
        tags = product_tags(p)
        hits = [a for a in (match_author(t, authors, fuzzy_index) for t in tags) if a]
        if args.all and not hits:
            hits = [t for t in tags if NAME_TAG.match(t) and not (set(t.split()) & NOT_NAMES)]
        if (hits or args.all) and p["handle"] not in seen:
            seen.add(p["handle"])
            kept.append((p, tags, list(dict.fromkeys(hits))))
    print(f"{n_total} products in the shop, {len(kept)} tagged with a series author")

    rows, products, no_impressum = [], [], 0
    for i, (p, tags, hits) in enumerate(kept, 1):
        url = f"{BASE}/products/{p['handle']}"
        html = f.get(url)
        stories, info = parse_product_page(html)
        blurb = blurb_of(p, html)
        if not stories:
            no_impressum += 1
        isbn = info["isbn"] or (ISBN_RE.search(p["handle"]).group(1) if ISBN_RE.search(p["handle"]) else "")
        pub_date = info["pub_date"] or (p.get("published_at") or "")[:10]
        for s in stories:
            names = list(hits)
            copy_name = re.sub(r"^\d{4}\s+", "", s["copyright"])
            if args.all and copy_name and "harlequin" not in copy_name.lower() and copy_name not in names:
                names.append(copy_name)
            rows.append({
                "language_letter": "G",
                "authors": "; ".join(names),
                "original_title": s["original_title"],
                "translated_title": p.get("title", ""),
                "pub_date": pub_date,
                "publisher": "CORA Verlag",
                "place": "Hamburg",
                "translators": s["translators"],
                "series": series_of(p.get("title", ""), tags),
                "isbn": isbn,
                "format": p.get("vendor", "") or p.get("product_type", ""),
                "copyright": s["copyright"],
                "original_series": s["original_series"],
                "first_edition": s["first_edition"],
                "first_edition_year": s["first_edition_year"],
                "source": "CORA",
                "source_record_id": f"cora:{p['handle']}",
                "url": url,
                "blurb": blurb,
            })
        # every product, also those WITHOUT an original title (candidates for later name matching)
        products.append({
            "handle": p["handle"], "title": p.get("title", ""), "authors": "; ".join(hits),
            "series": series_of(p.get("title", ""), tags), "pub_date": pub_date, "isbn": isbn,
            "format": p.get("vendor", "") or p.get("product_type", ""), "n_stories_with_original": len(stories),
            "original_titles": " | ".join(x["original_title"] for x in stories), "blurb": blurb, "url": url,
        })
        if i % 50 == 0 or i == len(kept):
            print(f"  [{i}/{len(kept)}] product pages read, {len(rows)} stories so far")

    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=EXTRA_COLS)
        w.writeheader()
        w.writerows(rows)
    products_path = str(Path(args.output).with_name(Path(args.output).stem + "_products.csv"))
    with open(products_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=PRODUCT_COLS)
        w.writeheader()
        w.writerows(products)
    with_blurb = sum(1 for x in products if x["blurb"])
    print(f"\nAll {len(products)} products with blurbs ({with_blurb} non-empty) -> {products_path}")
    print(f"{len(rows)} stories from {len(kept)} products -> {args.output}"
          f"  ({no_impressum} product pages had no Originaltitel block)")
    print(f"Next: python find_translations.py {args.input} --extra {args.output}")


if __name__ == "__main__":
    main()
