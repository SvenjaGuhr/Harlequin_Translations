#!/usr/bin/env python3
"""
Scrape several Harlequin / Silhouette lines from FictionDB into ONE metadata CSV.

Lines (FictionDB series pages, 200 books per page):
  american_romance   Harlequin American Romance            ~1,700 titles
  presents           Harlequin Presents                    ~4,000+
  romance            Harlequin Romance                     ~4,000+
  desire             Silhouette Desire                     ~2,000+
  special_edition    Silhouette / Harlequin Special Edition ~2,800+

A book that appears in several lines is kept ONCE (same FictionDB book_id); the
columns `line` (first line it was found in) and `all_lines` ("Harlequin Presents #12;
Harlequin Romance #3") record every membership.

Usage
  pip install requests beautifulsoup4
  python scrape_harlequin_lines.py                              # all five lines, list pages only
  python scrape_harlequin_lines.py --lines presents desire      # a subset
  python scrape_harlequin_lines.py --details                    # + each book's page (slow: ~1.5 s/book)

List pages give title, authors, series number, publication date and IDs. --details
adds rating, ISBN, publisher, pages, genres, description (about 6 h for ~15,000 books;
the cache lets you stop and resume).

Output: harlequin_lines.csv - use it as input for collect_cora.py and find_translations.py.
"""

import argparse
import csv
import hashlib
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.fictiondb.com"
LINES = {
    "american_romance": ("Harlequin American Romance", f"{BASE}/series/harlequin-american-romance~14031.htm"),
    "presents":         ("Harlequin Presents",         f"{BASE}/series/harlequin-presents~14060.htm"),
    "romance":          ("Harlequin Romance",          f"{BASE}/series/harlequin-romance~14065.htm"),
    "desire":           ("Silhouette Desire",          f"{BASE}/series/silhouette-desire~14137.htm"),
    "special_edition":  ("Special Edition",            f"{BASE}/series/silhouette-special-edition~14152.htm"),
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BOOK_RE = re.compile(r"/book/[^\"']*~(\d+)\.htm")
AUTHOR_RE = re.compile(r"/author/[^\"']*~(\d+)\.htm")
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(?:\d{1,2},?\s+)?\d{4}\b"
    r"|\b\d{4}-\d{2}(?:-\d{2})?\b"
)
RATING_RE = re.compile(r"(?<![\d.])([0-5]\.\d{1,2})(?![\d.])")


# --------------------------------------------------------------------------- #
# HTTP with retries + on-disk cache
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, delay: float, cache_dir: Path, retries: int = 4):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, url: str) -> str:
        key = hashlib.sha1(url.encode()).hexdigest() + ".html"
        cached = self.cache_dir / key
        if cached.exists():
            return cached.read_text(encoding="utf-8")

        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                resp.encoding = resp.encoding or "utf-8"
                html = resp.text
                cached.write_text(html, encoding="utf-8")
                time.sleep(self.delay)
                return html
            except requests.RequestException as exc:
                wait = self.delay * (2 ** attempt)
                print(f"  ! {url} failed ({exc}); retry {attempt}/{self.retries} in {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(wait)
        raise RuntimeError(f"Giving up on {url}")


# --------------------------------------------------------------------------- #
# Series list pages
# --------------------------------------------------------------------------- #
def page_url(series_url: str, n: int) -> str:
    return series_url if n == 1 else series_url.replace(".htm", f"~{n}.htm")


def total_pages(soup: BeautifulSoup) -> int:
    m = re.search(r"Page\s+\d+\s+of\s+(\d+)", soup.get_text(" ", strip=True))
    return int(m.group(1)) if m else 1


def parse_series_page(html: str) -> list[dict]:
    """Find each row: the smallest element holding exactly one book link plus an author link."""
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()

    for a in soup.find_all("a", href=BOOK_RE):
        book_id = BOOK_RE.search(a["href"]).group(1)
        if book_id in seen:
            continue

        # The row is the LARGEST ancestor that still holds only this one book.
        # (The nearest one is often a tiny wrapper around title + mobile author
        # link, which leaves out the number and date cells.)
        row = None
        for anc in a.parents:
            if anc.name in ("body", "html", "[document]"):
                break
            ids = {BOOK_RE.search(x["href"]).group(1) for x in anc.find_all("a", href=BOOK_RE)}
            if len(ids) != 1:
                break
            row = anc
        if row is None or not row.find("a", href=AUTHOR_RE):
            continue   # e.g. cover gallery tiles (no author link)

        # Authors (rows often repeat the author link for mobile layouts -> dedupe)
        authors, author_ids, author_urls = [], [], []
        for al in row.find_all("a", href=AUTHOR_RE):
            aid = AUTHOR_RE.search(al["href"]).group(1)
            if aid not in author_ids:
                author_ids.append(aid)
                authors.append(al.get_text(strip=True))
                author_urls.append(urljoin(BASE, al["href"]))

        # Series number: first cell / leading integer in the row
        number = ""
        first_cell = row.find(["td", "th"])
        candidates = [first_cell.get_text(strip=True)] if first_cell else []
        candidates.append(row.get_text(" ", strip=True).split(" ")[0])
        for c in candidates:
            if re.fullmatch(r"\d+(\.\d+)?", c or ""):
                number = c
                break

        row_text = row.get_text(" ", strip=True)
        date_m = DATE_RE.search(row_text)
        pub_date = date_m.group(0) if date_m else ""
        if not pub_date:
            t = row.find("time")
            if t:
                pub_date = t.get("datetime") or t.get_text(strip=True)
        if not pub_date:
            # year only ("1949"): look only AFTER the title, so a series number like 1995 is never
            # taken for a year, and only at a stand-alone 4-digit year
            after = row_text.split(a.get_text(" ", strip=True), 1)[-1]
            y = re.search(r"(?<![\d.#])((?:19|20)\d\d)(?![\d.])", after)
            if y:
                pub_date = y.group(1)
        # rating: look only after the date to avoid matching "1.5"-style series numbers
        rating = ""
        if date_m:
            r = RATING_RE.search(row_text[date_m.end():])
            rating = r.group(1) if r else ""

        seen.add(book_id)
        rows.append({
            "series_number": number,
            "title": a.get_text(strip=True),
            "authors": "; ".join(authors),
            "author_ids": "; ".join(author_ids),
            "pub_date": re.sub(r"\s+", " ", pub_date),
            "rating": rating,
            "book_id": book_id,
            "book_url": urljoin(BASE, a["href"]),
            "author_urls": "; ".join(author_urls),
        })
    return rows


def scrape_series(fetcher: Fetcher, line_name: str, series_url: str) -> list[dict]:
    first = fetcher.get(page_url(series_url, 1))
    n_pages = total_pages(BeautifulSoup(first, "html.parser"))
    print(f"{line_name}: {n_pages} page(s)")

    books, seen = [], set()
    for n in range(1, n_pages + 1):
        html = first if n == 1 else fetcher.get(page_url(series_url, n))
        page_books = parse_series_page(html)
        for b in page_books:
            b["line"] = line_name
        new = [b for b in page_books if b["book_id"] not in seen]
        seen.update(b["book_id"] for b in new)
        books.extend(new)
        print(f"  page {n}/{n_pages}: {len(new)} books (total {len(books)})")
    return books


# --------------------------------------------------------------------------- #
# Individual book pages (optional)
# --------------------------------------------------------------------------- #
def meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def section_lines(lines, start_label, stop_labels):
    try:
        i = lines.index(start_label)
    except ValueError:
        return []
    out = []
    for line in lines[i + 1:]:
        if line in stop_labels:
            break
        out.append(line)
    return out


def parse_book_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l]

    # "Book Details" block: label on one line, value on the next
    details = section_lines(lines, "Book Details", {"Continue the Series", "Track This Book"})
    info = {}
    for label in ("Published", "Main Genre", "Pages", "Rating"):
        if label in details:
            idx = details.index(label)
            if idx + 1 < len(details):
                info[label] = details[idx + 1]
    # labels and values can also come joined on one line: "Pages 253"
    for line in details:
        for label in ("Pages", "Rating", "Main Genre", "Published"):
            if label not in info and line.startswith(label + " "):
                info[label] = line[len(label) + 1:]
    pages = info.get("Pages", "")
    pages = pages if pages.isdigit() else ""
    # Rating: accept "4", "4.0", "4.25", "4.0 / 5", "4.0 (12 ratings)" etc.
    rating = ""
    rm = re.match(r"\s*([0-5](?:\.\d{1,2})?)\b", info.get("Rating", ""))
    if rm:
        rating = rm.group(1)
    if not rating:
        # fallback: schema.org markup, if present
        tag = soup.find(attrs={"itemprop": "ratingValue"})
        if tag:
            rating = (tag.get("content") or tag.get_text(strip=True)).strip()
    if not rating:
        m2 = re.search(r'"ratingValue"\s*:\s*"?([0-5](?:\.\d+)?)', html)
        if m2:
            rating = m2.group(1)

    # Subgenres (links to /genre/ under the "Subgenres" heading)
    subgenres = []
    h = soup.find(lambda t: t.name in ("h2", "h3", "h4") and t.get_text(strip=True) == "Subgenres")
    if h:
        for sib in h.find_all_next():
            if sib.name in ("h2", "h3", "h4") and sib is not h:
                break
            if sib.name == "a" and "/genre/" in sib.get("href", ""):
                name = sib.get_text(strip=True)
                if name not in subgenres:
                    subgenres.append(name)

    # Other series this book belongs to (excluding this one)
    other_series = []
    for a in soup.find_all("a", href=re.compile(r"/series/[^\"']*~\d+\.htm")):
        nxt = a.find_next(string=re.compile(r"Book\s+[\d.]+"))
        label = a.get_text(strip=True)
        num = re.search(r"Book\s+([\d.]+)", nxt).group(1) if nxt else ""
        entry = f"{label} #{num}" if num else label
        # only the association links (they're followed closely by "Book N")
        if num and entry not in other_series:
            other_series.append(entry)

    # First listed edition: "<Format> <Mon YYYY> <Publisher> ISBN13 ... ISBN10 ..."
    editions = " ".join(section_lines(lines, "Formats & Editions", {"Manage Tags", "Close"}))
    fmt = publisher = isbn10 = ""
    m = re.search(
        r"(Mass Market Paperback|Paperback|Trade Paperback|Hardcover|eBook|Audiobook)\s+"
        r"(?:[A-Z][a-z]{2} \d{4})\s+(.+?)\s+ISBN13\s+(\d{13})(?:\s+ISBN10\s+([\dX]{10}))?",
        editions,
    )
    if m:
        fmt, publisher, isbn10 = m.group(1), m.group(2).strip(), m.group(4) or ""

    # Full description from "About This Book", else og:description
    about = section_lines(lines, "About This Book", {"Series Placement", "Genres & Themes"})
    description = " ".join(about) or meta(soup, "og:description")

    return {
        "isbn13": meta(soup, "book:isbn", "og:isbn"),
        "isbn10": isbn10,
        "publisher": publisher,
        "format": fmt,
        "pages": pages,
        "rating_detail": rating,
        "published_detail": info.get("Published", ""),
        "main_genre": info.get("Main Genre", ""),
        "subgenres": "; ".join(subgenres),
        "other_series": "; ".join(other_series),
        "cover_url": meta(soup, "og:image"),
        "description": description,
    }


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="harlequin_lines.csv")
    ap.add_argument("--lines", nargs="+", default=list(LINES), choices=list(LINES),
                    help="which lines to scrape (default: all)")
    ap.add_argument("--details", action="store_true",
                    help="also fetch each book's page (needed for ratings, ISBN, publisher, etc.)")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N books")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--cache", default="cache", help="folder for cached HTML")
    args = ap.parse_args()

    fetcher = Fetcher(delay=args.delay, cache_dir=Path(args.cache))
    books, by_id, raw_numbers = [], {}, []
    for key in args.lines:
        name, url = LINES[key]
        for b in scrape_series(fetcher, name, url):
            if b["series_number"]:
                raw_numbers.append((name, b["series_number"]))   # before de-duplication
            tag = f"{name} #{b['series_number']}" if b["series_number"] else name
            if b["book_id"] in by_id:                   # already found in another line
                by_id[b["book_id"]]["all_lines"] += f"; {tag}"
                continue
            b["all_lines"] = tag
            by_id[b["book_id"]] = b
            books.append(b)
    # per-line flags: companion = no number in its line; anthology = number shared within the line
    from collections import Counter
    counts = Counter(raw_numbers)
    for b in books:
        b["is_companion"] = not b["series_number"]
        b["is_anthology"] = bool(b["series_number"]) and counts[(b["line"], b["series_number"])] > 1
    print(f"\n{len(books)} distinct books from {len(args.lines)} line(s); "
          f"{sum(';' in b['all_lines'] for b in books)} appear in more than one line")
    if args.limit:
        books = books[: args.limit]

    if args.details:
        for i, b in enumerate(books, 1):
            print(f"[{i}/{len(books)}] {b['title']}")
            try:
                d = parse_book_page(fetcher.get(b["book_url"]))
            except Exception as exc:  # keep going; leave blanks for this row
                print(f"  ! could not parse {b['book_url']}: {exc}", file=sys.stderr)
                d = {}
            if not b["rating"] and d.get("rating_detail"):
                b["rating"] = d["rating_detail"]
            d.pop("rating_detail", None)
            if not b["pub_date"] and d.get("published_detail"):
                b["pub_date"] = d["published_detail"]
            d.pop("published_detail", None)
            b.update(d)

    first = ["line", "series_number", "title", "authors", "pub_date", "all_lines", "is_companion", "is_anthology"]
    fields = first + [k for k in (books[0].keys() if books else []) if k not in first]
    # make sure every row has every column
    for b in books:
        for f in fields:
            b.setdefault(f, "")
    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(books)

    missing = sum(1 for b in books if not b.get("pub_date"))
    print(f"\nWrote {len(books)} rows to {args.output} ({missing} without pub_date)")
    if missing and not args.details:
        print("Tip: re-run with --details to fill missing dates from each book's page.")


if __name__ == "__main__":
    main()
