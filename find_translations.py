#!/usr/bin/env python3
"""
Find French, German and Polish translations of the books in the FictionDB
series CSV (produced by scrape_harlequin_american.py) by querying national
library catalogues, and write them to CSV with IDs like 6157_G, 6157a_F, 6157b_F.

Sources (all free, no API key):
  G  German  - Deutsche Nationalbibliothek SRU     https://services.dnb.de/sru/dnb   (MARC21-xml)
  F  French  - Bibliothèque nationale de France SRU https://catalogue.bnf.fr/api/SRU (UNIMARC)
  P  Polish  - Biblioteka Narodowa, BN Data API      https://data.bn.org.pl/api/     (MARC21 in JSON)

How matching works
  Translations carry a new title, so we cannot search by the English title.
  Instead, for every author in the CSV we download that author's records from
  each library, keep the ones in the target language, read the ORIGINAL title
  the library records (e.g. DNB 700$t / 240, BN 246 "Tyt. oryg.", BnF 454$t or
  "Trad. de" notes), and match it against that author's English titles.

ID scheme
  <book_id>_<L>              one edition in language L          e.g. 6157_P
  <book_id>a_<L>, b_<L>, ... several editions (a = earliest)   e.g. 6157a_G, 6157b_G

Output
  translations.csv                     one row per translated edition (long format)
  harlequin_with_translations.csv      your original table joined to the editions
                                       (originals without translations kept once)

Review loop for records that could not be matched automatically
  candidate_matches.csv lists, for every unmatched French/German/Polish record,
  up to 3 series books it could be (same author, published 0-15 years after
  the original, similar original title if one is recorded, romance publisher).
  1. Open candidate_matches.csv, put "y" in the 'confirm' column for correct rows.
  2. Save it as CSV (same file name) and run the script again.
  3. Confirmed rows are added to translations.csv and the merged table with
     proper IDs (match_score = "manual"). Your 'confirm' marks are kept on re-runs.

Extra sources
  --extra FILE.csv merges editions found elsewhere (e.g. cora_editions.csv from
  collect_cora.py). They go through the same matching, ID numbering and review.

Usage
  pip install requests
  python find_translations.py harlequin_american_romance.csv
  python find_translations.py harlequin_american_romance.csv --langs G P --limit-authors 10

Caveats
  * Coverage depends on the library. Magazine-style Romanhefte (Cora "Bianca",
    "Julia", ...) are often catalogued only as a series, without the original
    title, so some German translations will be missed.
  * Pen names / changed author names in translation will be missed.
  * Check rows with match_score < 1.0 by hand.
"""

import argparse
import csv
import difflib
import hashlib
import json
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (research script; translation metadata)"}

LANGS = {
    "G": {"name": "German", "codes": {"ger", "deu"}, "source": "DNB"},
    "F": {"name": "French", "codes": {"fre", "fra"}, "source": "BnF"},
    "P": {"name": "Polish", "codes": {"pol"}, "source": "BN"},
}


# --------------------------------------------------------------------------- #
# HTTP with cache
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, delay=1.0, cache_dir="cache_translations", retries=4):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay, self.retries = delay, retries
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)

    def get(self, url):
        f = self.cache / (hashlib.sha1(url.encode()).hexdigest() + ".txt")
        if f.exists():
            return f.read_text(encoding="utf-8")
        for attempt in range(1, self.retries + 1):
            try:
                r = self.s.get(url, timeout=60)
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


# --------------------------------------------------------------------------- #
# A tiny MARC model: list of (tag, ind1, ind2, [(code, value), ...] or str)
# --------------------------------------------------------------------------- #
class Marc:
    def __init__(self, fields, leader=""):
        self.fields = fields
        self.leader = leader or ""

    def is_serial(self):
        """Record for a series / periodical (e.g. 'Bianca / Extra'), not for a single book."""
        return len(self.leader) > 7 and self.leader[7] in "sib"

    def ctrl(self, tag):
        for t, _, _, v in self.fields:
            if t == tag and isinstance(v, str):
                return v
        return ""

    def dfs(self, tag):
        return [(i1, i2, sf) for t, i1, i2, sf in self.fields if t == tag and not isinstance(sf, str)]

    def sub(self, tag, code):
        return [v for _, _, sf in self.dfs(tag) for c, v in sf if c == code]

    def first(self, tag, code):
        vals = self.sub(tag, code)
        return vals[0] if vals else ""


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def marc_from_xml(elem):
    fields, leader = [], ""
    for ch in elem:
        n = _local(ch.tag)
        if n == "leader":
            leader = ch.text or ""
        elif n == "controlfield":
            fields.append((ch.get("tag"), "", "", (ch.text or "")))
        elif n == "datafield":
            sfs = [(s.get("code"), strip_marks(s.text).strip()) for s in ch if _local(s.tag) == "subfield"]
            fields.append((ch.get("tag"), ch.get("ind1", " "), ch.get("ind2", " "), sfs))
    return Marc(fields, leader)


def sru_records(xml_text):
    """Return (list[Marc], numberOfRecords) from an SRU response (namespace-agnostic)."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        return [], 0
    total = 0
    for e in root.iter():
        if _local(e.tag) == "numberOfRecords" and (e.text or "").strip().isdigit():
            total = int(e.text)
            break
    recs = [marc_from_xml(e) for e in root.iter()
            if _local(e.tag) == "record" and any(_local(c.tag) == "datafield" for c in e)]
    return recs, total


def marc_from_bn_json(m):
    fields = []
    for f in m.get("fields", []):
        for tag, val in f.items():
            if isinstance(val, str):
                fields.append((tag, "", "", val))
            else:
                sfs = [(c, strip_marks(str(v)).strip()) for sf in val.get("subfields", []) for c, v in sf.items()]
                fields.append((tag, val.get("ind1", " "), val.get("ind2", " "), sfs))
    return Marc(fields, m.get("leader", ""))


# --------------------------------------------------------------------------- #
# Record -> edition dict
# --------------------------------------------------------------------------- #
# Library non-sorting markers around leading articles ("Das", "Le", "The"):
#   DNB/MARC21: U+0098 ... U+009C   BnF/UNIMARC: U+0088 ... U+0089   some: <<Das>>
# plus any other invisible control characters.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\ufeff]")


def strip_marks(s):
    s = (s or "").replace("<<", "").replace(">>", "")
    return unicodedata.normalize("NFC", CONTROL_RE.sub("", s))


def clean(s):
    s = re.sub(r"\s+", " ", strip_marks(s)).strip()
    return s.strip(" /:;,.=").strip()


def year_of(s):
    m = re.search(r"(1[89]\d\d|20\d\d)", s or "")
    return m.group(1) if m else ""


TRANSLATOR_RE = re.compile(
    r"(?:aus dem [\w.]+(?:\s+[\w.]+)?\s+(?:übers(?:etzt|\.)?\s+)?von|übers(?:etzt|\.)?\s*(?:von|:)?|"
    r"przeł(?:ożył[a-z]*|\.)?|przekł(?:\.|ad)?|tłum(?:\.|aczenie|aczyła|aczył)?|"
    r"trad(?:uit|uction|\.)?(?:\s+de\s+l['’]\w+(?:\s+\([^)]*\))?)?\s*(?:par)?)\s*"
    r"(?:\[?z\s+ang(?:\.|ielskiego)?\]?\s*)?([^;\[\]]+)",
    re.I,
)


def fold(s):
    """lowercase, no accents, single spaces - for comparing author names."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def person(name):
    """'Sławińska, Krystyna' -> 'Krystyna Sławińska'."""
    name = clean(name)
    if name.count(",") == 1:
        last, first = [x.strip() for x in name.split(",")]
        if first and last:
            return f"{first} {last}"
    return name


def translators_from_statement(stmt):
    out = []
    for m in TRANSLATOR_RE.finditer(stmt or ""):
        for name in re.split(r"\s+(?:und|i|et|and)\s+|,\s*", m.group(1)):
            name = clean(name.replace("[", "").replace("]", ""))
            if name and len(name) > 2 and not re.search(r"\d", name):
                out.append(name)
    return out


ORIG_NOTE_RE = re.compile(
    r"(?:trad(?:uit|uction)?\.?\s+de(?:\s+l['’]\s*\w+(?:\s+\([^)]*\))?)?(?:\s+par\s+[^:]+)?|"
    r"titre\s+(?:de\s+l['’]\s*)?original(?:e)?|tit\.\s*orig\.?|"
    r"originaltitel|einheitssacht\.?|orig\.?\s*titel|"
    r"tyt(?:uł|ul)?\.?\s*oryg\w*\.?(?:\s*poszczeg\w*\.?\s*utw\w*\.?)?|"
    r"(?:original\s+)?title\s+of\s+(?:the\s+)?original|original\s+title|orig\.\s*title)"
    r"\s*[:.]?\s*",
    re.I,
)


def titles_from_note(note):
    """'Trad. de : "Tomorrow's promise"' -> ["Tomorrow's promise"];
    'Tyt. oryg. poszczeg. utworów: The Raider, 1994, Stranded, 1994 Deep cover' -> 3 titles"""
    out = []
    for m in ORIG_NOTE_RE.finditer(note or ""):
        rest = note[m.end():]
        q = (re.match(r'\s*«\s*(.+?)\s*»', rest) or re.match(r'\s*"\s*(.+?)\s*"', rest)
             or re.match(r'\s*[“„]\s*(.+?)\s*[”“]', rest))
        if q:
            parts = [q.group(1)]
        else:
            # stop at the end of the title sentence ("The three of us. Na stronie tytułowej ...")
            t = re.split(r"\s+[/;]\s+|\.\s+-|\.\s*$|\s+-\s+|\.\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż])", rest)[0]
            # several works: "The Raider, 1994, Stranded, 1994 Deep cover"
            parts = re.split(r",?\s*(?:19|20)\d\d\s*,?\s*", t) if re.search(r"(?:19|20)\d\d\s*,?\s*\S", t) else [t]
        for t in parts:
            t = clean(re.sub(r",?\s*(?:19|20)\d\d\s*$", "", t))
            if re.search(r"[A-Za-z]{2}", t):
                out.append(t)
    return out


POLISH_OR_GERMAN = re.compile(
    r"[ąćęłńśźżĄĆĘŁŃŚŹŻäöüßÄÖÜ]"                               # letters English titles never use
    r"|\b(?:w|z|ze|na|się|nie|dla|pod|nad|jak|czy|od|za|po|oraz|der|das|und|mit|ein|eine)\b"
    r"|\b\w*(?:rz|cz|szcz|cja|cje|ości|nych|owie)\b", re.I)


def plausibly_english(t):
    """Filter out Polish/German titles that libraries store next to the English original
    ("Nietypowa przysługa", "W imię miłości", "Korsarz"). French accents are allowed (Protégée, Café)."""
    return bool(t) and not POLISH_OR_GERMAN.search(t) and len(re.findall(r"[A-Za-z]", t)) >= 3


def parse_marc21(rec: Marc, source):
    """DNB (MARC21-xml) and BN (MARC21 JSON)."""
    langs = {v.lower() for v in rec.sub("041", "a")}
    f008 = rec.ctrl("008")
    if len(f008) >= 38:
        langs.add(f008[35:38].lower())
    orig_lang = ",".join(rec.sub("041", "h"))

    title = clean(" : ".join(x for x in [rec.first("245", "a"), rec.first("245", "b")] if x))
    title = re.sub(r"\s*/\s*$", "", title)
    resp = rec.first("245", "c")

    orig = []
    orig += rec.sub("240", "a")
    orig += rec.sub("246", "a")
    orig += rec.sub("700", "t")
    orig += rec.sub("765", "t")
    for note in rec.sub("500", "a") + rec.sub("246", "i"):
        orig += titles_from_note(note)
    # BN (only) writes "Polish title / English title" in 245
    if source == "BN" and "/" in rec.first("245", "a") + rec.first("245", "b"):
        orig.append((rec.first("245", "a") + " " + rec.first("245", "b")).split("/")[-1])

    trans = []
    for tag in ("700", "710"):
        for _, _, sf in rec.dfs(tag):
            d = defaultdict(list)
            for c, v in sf:
                d[c].append(v)
            roles = " ".join(d["4"] + d["e"]).lower()
            if "trl" in roles or "übers" in roles or "tłum" in roles or "transl" in roles:
                name = clean(re.sub(r"\(.*?\)", "", " ".join(d["a"])))
                if name:
                    trans.append(name)
    if not trans:
        trans = translators_from_statement(resp)

    pub = rec.dfs("264") or rec.dfs("260")
    place = publisher = date = ""
    for i1, i2, sf in pub:
        if rec.dfs("264") and i2 not in ("1", " "):
            continue
        d = defaultdict(list)
        for c, v in sf:
            d[c].append(v)
        place, publisher, date = clean(" ; ".join(d["a"])), clean(" ; ".join(d["b"])), clean(" ".join(d["c"]))
        break
    if not date and len(f008) >= 11:
        date = f008[7:11]

    series = "; ".join(clean(" ".join(v for c, v in sf if c in "av")) for _, _, sf in rec.dfs("490"))
    return {
        "record_id": rec.ctrl("001"),
        "langs": langs,
        "orig_lang": orig_lang,
        "translated_title": title,
        "original_titles": [clean(o) for o in orig if plausibly_english(clean(o))],
        "translators": "; ".join(dict.fromkeys(person(t) for t in trans)),
        "responsibility": clean(resp),
        "publisher": publisher,
        "place": place,
        "pub_date": date,
        "pub_year": year_of(date),
        "series": series,
        "isbn": "; ".join(dict.fromkeys(clean(i.split(" ")[0]) for i in rec.sub("020", "a"))),
        "source": source,
    }


def parse_unimarc(rec: Marc):
    """BnF (UNIMARC)."""
    langs = {v.lower() for v in rec.sub("101", "a")}
    title = clean(" : ".join(x for x in [rec.first("200", "a"), rec.first("200", "e")] if x))
    resp = " ; ".join(rec.sub("200", "f") + rec.sub("200", "g"))

    orig = rec.sub("454", "t") + rec.sub("500", "a") + rec.sub("510", "a")
    for tag in ("300", "304", "305", "311", "312", "314", "327"):
        for note in rec.sub(tag, "a"):
            orig += titles_from_note(note)

    trans = []
    for tag in ("701", "702"):
        for _, _, sf in rec.dfs(tag):
            d = defaultdict(list)
            for c, v in sf:
                d[c].append(v)
            if "730" in d["4"]:
                trans.append(clean(", ".join(x for x in [" ".join(d["a"]), " ".join(d["b"])] if x)))
    if not trans:
        trans = translators_from_statement(resp)

    pub = rec.dfs("214") or rec.dfs("210")
    place = publisher = date = ""
    if pub:
        d = defaultdict(list)
        for c, v in pub[0][2]:
            d[c].append(v)
        place, publisher, date = clean(" ; ".join(d["a"])), clean(" ; ".join(d["c"])), clean(" ".join(d["d"]))

    # UNIMARC 225: $a collection, $i sub-collection name, $h sub-collection number, $v volume number
    # "Harlequin. Série Désir ; 2" -> "Harlequin. Désir 2" (the number belongs to the sub-collection)
    def series_225(sf):
        d = defaultdict(list)
        for c, v in sf:
            d[c].append(clean(v))
        name = " ".join(d["a"])
        for part in d["h"] + d["i"]:
            if part and not part.isdigit():
                name += ". " + re.sub(r"^(?:s[ée]rie|collection)\s+", "", part, flags=re.I)
        num = " ".join(x for x in d["v"] + [h for h in d["h"] if h.isdigit()] if x)
        return clean(f"{name} {num}")
    series = "; ".join(series_225(sf) for _, _, sf in rec.dfs("225"))
    rid = rec.ctrl("003") or rec.ctrl("001")
    return {
        "record_id": rid,
        "langs": langs,
        "orig_lang": ",".join(rec.sub("101", "c")),
        "translated_title": title,
        "original_titles": [clean(o) for o in orig if plausibly_english(clean(o))],
        "translators": "; ".join(dict.fromkeys(person(t) for t in trans)),
        "responsibility": clean(resp),
        "publisher": publisher,
        "place": place,
        "pub_date": date,
        "pub_year": year_of(date),
        "series": series,
        "isbn": "; ".join(dict.fromkeys(rec.sub("010", "a"))),
        "source": "BnF",
    }


# --------------------------------------------------------------------------- #
# Library searches (by author)
# --------------------------------------------------------------------------- #
def name_forms(full):
    parts = full.split()
    if len(parts) < 2:
        return full, full
    return f"{parts[-1]}, {' '.join(parts[:-1])}", full   # "Brown, Sandra", "Sandra Brown"


def search_dnb(f: Fetcher, author, max_records):
    inverted, _ = name_forms(author)
    out, start = [], 1
    while start <= max_records:
        q = urlencode({"version": "1.1", "operation": "searchRetrieve",
                       "query": f'per="{inverted}"', "recordSchema": "MARC21-xml",
                       "maximumRecords": 100, "startRecord": start})
        recs, total = sru_records(f.get(f"https://services.dnb.de/sru/dnb?{q}"))
        out += [parse_marc21(r, "DNB") for r in recs]
        start += 100
        if not recs or start > total:
            break
    return out


def search_bnf(f: Fetcher, author, max_records):
    _, plain = name_forms(author)
    out, start = [], 1
    while start <= max_records:
        q = urlencode({"version": "1.2", "operation": "searchRetrieve",
                       "query": f'bib.author all "{plain}"', "recordSchema": "unimarcxchange",
                       "maximumRecords": 100, "startRecord": start})
        recs, total = sru_records(f.get(f"https://catalogue.bnf.fr/api/SRU?{q}"))
        out += [parse_unimarc(r) for r in recs]
        start += 100
        if not recs or start > total:
            break
    return out


def search_bn(f: Fetcher, author, max_records):
    inverted, _ = name_forms(author)
    url = "https://data.bn.org.pl/api/institutions/bibs.json?" + urlencode(
        {"author": inverted, "language": "polski", "limit": 100})
    out = []
    while url and len(out) < max_records:
        txt = f.get(url)
        try:
            data = json.loads(txt) if txt else {}
        except json.JSONDecodeError:
            break
        for b in data.get("bibs", []):
            if b.get("marc"):
                out.append(parse_marc21(marc_from_bn_json(b["marc"]), "BN"))
        url = data.get("nextPage") if data.get("bibs") else None
    return out


SEARCH = {"G": search_dnb, "F": search_bnf, "P": search_bn}


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def norm(t):
    t = strip_marks(t)
    t = re.split(r"\s+/{1,2}\s+|\s+;\s+|\s+=\s+", t)[0]              # drop "/ author", "; series", "= parallel"
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = re.sub(r"[’`´]", "'", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t.replace("'", ""))
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+(19|20)\d\d$", "", t)                          # trailing year
    t = re.sub(r"\s+(a )?novel$", "", t)
    t = re.sub(r"^(the|a|an) ", "", t)
    return t


MINOR_WORDS = {"a", "an", "the", "and", "of", "for", "to", "in", "on", "with", "s"}


def spelling_variant(x, y):
    """honour/honor, trouble/troubles, protege/protogee - but NOT billion/million, baby/bay."""
    if x[0] != y[0]:
        return False
    if x.startswith(y) or y.startswith(x):              # plural / possessive endings
        return abs(len(x) - len(y)) <= 2
    if abs(len(x) - len(y)) > 1 or min(len(x), len(y)) < 5:
        return False
    # edit distance <= 1
    i = 0
    while i < min(len(x), len(y)) and x[i] == y[i]:
        i += 1
    if len(x) == len(y):
        return x[i + 1:] == y[i + 1:]
    return (x[i + 1:] == y[i:]) if len(x) > len(y) else (x[i:] == y[i + 1:])


def compatible(a, b):
    """True if two normalised titles can be the same book: they may differ only in small
    words, spacing or spelling - never in a content word ("triplets" vs "quadruplets",
    "baby" vs "cowboy", "king" vs "thanksgiving")."""
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return True
    wa = [w for w in a.split() if w not in MINOR_WORDS and not w.isdigit()]
    wb = [w for w in b.split() if w not in MINOR_WORDS and not w.isdigit()]
    if not wa or not wb:
        return False
    rest_a, rest_b = [w for w in wa if w not in wb], [w for w in wb if w not in wa]
    if len(rest_a) != len(rest_b):
        # one side has extra content words; allow only a joined/split word ("cow boy" ~ "cowboy")
        return "".join(wa) == "".join(wb)
    for x in rest_a:                               # each differing word must be a spelling variant
        if not any(spelling_variant(x, y) for y in rest_b):
            return False
    return True


def best_match(orig_titles, candidates, threshold):
    """candidates: dict norm_title -> book row. Returns (row, score, best_guess_title)."""
    best, score = None, 0.0
    for o in orig_titles:
        n = norm(o)
        if not n:
            continue
        if n in candidates:
            return candidates[n], 1.0, candidates[n]["title"]
        for cn, row in candidates.items():
            s = difflib.SequenceMatcher(None, n, cn).ratio()
            # one title contained in the other: only for longer titles that make up most of
            # the other one ("texas wedding" must NOT match "a texas wedding vow")
            short, long_ = sorted((n, cn), key=lambda x: len(x.split()))
            if (len(short.split()) >= 3 and len(short.split()) / len(long_.split()) >= 0.6
                    and re.search(rf"\b{re.escape(short)}\b", long_)):
                s = max(s, 0.95)
            elif not compatible(n, cn):
                s = min(s, threshold - 0.001)       # similar-looking but a different book
            if s > score:
                best, score = row, s
    guess = best["title"] if best else ""
    return (best if score >= threshold else None), round(score, 3), guess


def works_in_edition(e):
    """Distinct original works named in a record (Cora: one row per story, so count per product)."""
    titles = {norm(t) for t in e.get("original_titles", []) if norm(t)}
    return max(len(titles), int(e.get("_stories_in_product", 0) or 0), 1)


def letters(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


# --------------------------------------------------------------------------- #
# Second pass: candidate matches for records the automatic step could not match
# --------------------------------------------------------------------------- #
ROMANCE_PUBLISHERS = re.compile(
    r"harlequin|arlekin|silhouette|mira|cora|bastei|kelter|pabel|moewig|zauberkreis|mills|boon|"
    r"harper\s*collins|j'ai lu|phantom press|awhe|amber|temptation|romance|romans|sonia", re.I)

CONFIRM_YES = {"y", "yes", "j", "ja", "x", "1", "true", "ok", "oui", "tak"}


def build_candidates(unmatched, by_author, max_gap, min_sim, top_n):
    """Suggest series books for unmatched records, scored 0-1, top_n per record."""
    out = []
    for u in unmatched:
        r = u["_rec"]
        rec_year = int(r["pub_year"]) if r["pub_year"].isdigit() else None
        romance_pub = bool(ROMANCE_PUBLISHERS.search(" ".join([r["publisher"], r["series"]])))
        series_books = list({b["book_id"]: b for b in by_author[u["author"]].values()}.values())
        titled = bool(r["original_titles"])
        cands = []
        for b in series_books:
            sim, ok = 0.0, False
            for o in r["original_titles"]:
                sim = max(sim, difflib.SequenceMatcher(None, norm(o), norm(b["title"])).ratio())
                ok = ok or compatible(norm(o), norm(b["title"]))
            if titled and not (ok and sim >= min_sim):
                continue                          # record names a different original work
            b_year = year_of(b.get("pub_date", ""))
            gap = (rec_year - int(b_year)) if (rec_year and b_year) else None
            if gap is not None and not (0 <= gap <= max_gap):
                continue                          # published before the original, or too late
            year_fit = 1 - gap / max_gap if gap is not None else 0.3
            if titled:     # same book, spelled differently: title evidence dominates
                score = 0.7 + 0.3 * sim
            else:          # no title in the record: only circumstantial evidence, max 'medium'
                # medium only if it is plausibly THIS book: romance publisher and either the
                # author has a single series title or the edition followed within 3 years
                strong = romance_pub and (len(series_books) == 1 or (gap is not None and gap <= 3))
                score = (0.5 if strong else 0.2) + 0.15 * year_fit + 0.04 * (len(series_books) == 1)
                score = min(score, 0.69)
            cands.append((round(score, 3), sim, gap, b))
        cands.sort(key=lambda c: -c[0])
        for score, sim, gap, b in cands[:top_n]:
            out.append({
                "confirm": "",
                # untitled records: 'medium' only if at most 2 series books are plausible at all
                "confidence": ("high" if score >= 0.7 else
                               "medium" if score >= 0.5 and len(cands) <= 2 else "low"),
                "candidate_score": score,
                "reason": u["reason"],
                "language_letter": u["language_letter"],
                "language": LANGS[u["language_letter"]]["name"],
                "author": u["author"],
                "suggested_book_id": b["book_id"],
                "suggested_original_title": b["title"],
                "original_pub_date": b.get("pub_date", ""),
                "translated_title": r["translated_title"],
                "pub_date": r["pub_date"],
                "pub_year": r["pub_year"],
                "years_after_original": "" if gap is None else gap,
                "publisher": r["publisher"],
                "place": r["place"],
                "translators": r["translators"],
                "series": r["series"],
                "isbn": r["isbn"],
                "title_similarity": round(sim, 3),
                "romance_publisher": "yes" if romance_pub else "",
                "candidates_for_this_record": len(cands),
                "original_title_in_record": " | ".join(r["original_titles"]),
                "responsibility": r["responsibility"],
                "source": r["source"],
                "source_record_id": r["record_id"],
            })
    return out


CAND_COLS = ["confirm", "confidence", "candidate_score", "reason", "language_letter", "language", "author",
             "suggested_book_id", "suggested_original_title", "original_pub_date", "translated_title",
             "pub_date", "pub_year", "years_after_original", "publisher", "place", "translators", "series",
             "isbn", "title_similarity", "romance_publisher", "candidates_for_this_record",
             "original_title_in_record", "responsibility", "source", "source_record_id"]


def cand_key(c):
    return (c["source_record_id"], c["suggested_book_id"], c["language_letter"])


def load_previous_candidates(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8-sig") as fh:
        return {cand_key(c): c for c in csv.DictReader(fh)}


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="+",
                    help="English book table(s): harlequin_lines.csv, optionally english_additions.csv")
    ap.add_argument("--langs", nargs="+", default=["F", "G", "P"], choices=list(LANGS))
    ap.add_argument("--threshold", type=float, default=0.88, help="fuzzy title match cut-off (0-1)")
    ap.add_argument("--max-records", type=int, default=1000, help="max catalogue records per author per library")
    ap.add_argument("--limit-authors", type=int, default=0, help="only process first N authors (testing)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("-o", "--output", default="translations.csv")
    ap.add_argument("--merged", default="harlequin_with_translations.csv")
    ap.add_argument("--unmatched", default="unmatched_records.csv",
                    help="target-language records that were NOT matched, with the reason")
    ap.add_argument("--candidates", default="candidate_matches.csv",
                    help="review file: suggested series books for unmatched records. Put y in 'confirm' "
                         "for correct ones; the next run adds them to the translation tables")
    ap.add_argument("--max-gap", type=int, default=15,
                    help="max years between original and translation for a candidate (default 15)")
    ap.add_argument("--min-sim", type=float, default=0.75,
                    help="records naming an original title less similar than this are not candidates")
    ap.add_argument("--top-n", type=int, default=3, help="max suggestions per unmatched record")
    ap.add_argument("--no-library", action="store_true",
                    help="skip the per-author library searches (use when harvest_*.csv are passed via --extra)")
    ap.add_argument("--to-resolve", default="originals_to_resolve.csv",
                    help="translations naming an English original that is not in the input table(s); "
                         "feed this to resolve_originals.py")
    ap.add_argument("--extra", nargs="*", default=[],
                    help="extra-source CSVs (e.g. cora_editions.csv) with columns language_letter, authors, "
                         "original_title, translated_title, pub_date, publisher, place, translators, series, "
                         "isbn, source, source_record_id")
    args = ap.parse_args()

    books, seen_ids = [], set()
    for path in args.input:
        with open(path, encoding="utf-8-sig") as fh:
            for b in csv.DictReader(fh):
                if b["book_id"] not in seen_ids:
                    seen_ids.add(b["book_id"])
                    books.append(b)
    print(f"{len(books):,} English books from {len(args.input)} table(s)")

    by_author = defaultdict(dict)       # author -> {norm title: row}
    for b in books:
        for a in [x.strip() for x in b["authors"].split(";") if x.strip()]:
            by_author[a][norm(b["title"])] = b
            # anthology stories "Volume: Story" - also match on either part alone
            if ":" in b["title"]:
                for part in b["title"].split(":", 1):
                    if len(norm(part).split()) >= 2:
                        by_author[a].setdefault(norm(part), b)
    authors = [] if args.no_library else list(by_author)
    if args.limit_authors:
        authors = authors[: args.limit_authors]

    # author names from other sources ("Gillan" typos, accents, "Brown, Sandra") -> table names
    fold_index = defaultdict(list)
    for a in by_author:
        fold_index[fold(a)[:1]].append((fold(a), a))

    def lookup_author(name):
        fn = fold(person(name))
        for key, a in fold_index.get(fn[:1], []):
            if key == fn:
                return a
        if len(fn) >= 8:
            for key, a in fold_index.get(fn[:1], []):
                if abs(len(key) - len(fn)) <= 2 and difflib.SequenceMatcher(None, fn, key).ratio() >= 0.92:
                    return a
        return None

    to_resolve = []

    f = Fetcher(delay=args.delay)
    found = defaultdict(list)          # (book_id, L) -> [edition]
    unmatched = []
    for i, author in enumerate(authors, 1):
        for L in args.langs:
            recs = SEARCH[L](f, author, args.max_records)
            n = in_lang = with_orig = 0
            for r in recs:
                if r["langs"] and not (r["langs"] & LANGS[L]["codes"]):
                    continue                     # clearly another language
                if r["orig_lang"] and "eng" not in r["orig_lang"]:
                    continue                     # translated from something else
                in_lang += 1
                if r["original_titles"]:
                    with_orig += 1
                row, score, guess = best_match(r["original_titles"], by_author[author], args.threshold)
                if not row:
                    if r["original_titles"]:
                        to_resolve.append({"author": author, "original_title": r["original_titles"][0],
                                           "language_letter": L, "translated_title": r["translated_title"],
                                           "pub_year": r["pub_year"], "publisher": r["publisher"],
                                           "reason": "title not in table"})
                    unmatched.append({
                        "author": author, "language_letter": L, "source": r["source"],
                        "source_record_id": r["record_id"], "translated_title": r["translated_title"],
                        "pub_year": r["pub_year"], "publisher": r["publisher"],
                        "original_title_in_record": " | ".join(r["original_titles"]),
                        "reason": "no original title in record" if not r["original_titles"] else "title did not match",
                        "closest_series_title": guess, "closest_score": score,
                        "responsibility": r["responsibility"],
                        "_rec": r,
                    })
                    continue
                key = (row["book_id"], L)
                if any(e["record_id"] == r["record_id"] for e in found[key]):
                    continue
                r.update(match_score=score, matched_original=row["title"], author=author)
                found[key].append(r)
                n += 1
            print(f"[{i}/{len(authors)}] {author:30s} {L}: {len(recs):4d} records | "
                  f"{in_lang:4d} in {LANGS[L]['name']:6s} | {with_orig:4d} name an original title | {n} matched")

    # extra sources (publisher shops, UNESCO Index Translationum, ...)
    for path in args.extra:
        with open(path, encoding="utf-8-sig") as fh:
            extra_rows = list(csv.DictReader(fh))
        per_record = defaultdict(int)
        for x in extra_rows:
            per_record[x.get("source_record_id", "")] += 1
        n = 0
        for x in extra_rows:
            L = (x.get("language_letter") or "").strip().upper()
            if L not in args.langs:
                continue
            r = {
                "record_id": x.get("source_record_id", ""), "langs": LANGS[L]["codes"], "orig_lang": "",
                "translated_title": clean(x.get("translated_title", "")),
                "original_titles": [clean(x.get("original_title", ""))] if x.get("original_title") else [],
                "translators": "; ".join(person(t) for t in re.split(r";|,\s+und\s+|\s+und\s+", x.get("translators", "")) if t.strip()),
                "responsibility": x.get("copyright", ""),
                "publisher": x.get("publisher", ""), "place": x.get("place", ""),
                "pub_date": x.get("pub_date", ""), "pub_year": year_of(x.get("pub_date", "")),
                "series": x.get("series", ""), "isbn": x.get("isbn", ""),
                "source": x.get("source", "") or Path(path).stem,
                "_stories_in_product": per_record[x.get("source_record_id", "")],
                "original_series": x.get("original_series", ""),
                "first_edition": x.get("first_edition", ""),
                "first_edition_year": x.get("first_edition_year", ""),
            }
            raw_authors = [a.strip() for a in (x.get("authors") or "").split(";") if a.strip()]
            tagged = list(dict.fromkeys(a for a in (lookup_author(n) for n in raw_authors) if a))
            results = [(a,) + best_match(r["original_titles"], by_author[a], args.threshold) for a in tagged]
            matched = [(a, row, score) for a, row, score, _ in results if row]
            if not matched and r["original_titles"] and raw_authors:
                to_resolve.append({"author": (tagged or raw_authors)[0], "original_title": r["original_titles"][0],
                                   "language_letter": L, "translated_title": r["translated_title"],
                                   "pub_year": r["pub_year"], "publisher": r["publisher"],
                                   "reason": "title not in table" if tagged else "author not in table"})
            if not matched:
                if results:   # report once, against the closest author
                    a, _, score, guess = max(results, key=lambda t: t[2])
                    unmatched.append({
                        "author": a, "language_letter": L, "source": r["source"],
                        "source_record_id": r["record_id"], "translated_title": r["translated_title"],
                        "pub_year": r["pub_year"], "publisher": r["publisher"],
                        "original_title_in_record": " | ".join(r["original_titles"]),
                        "reason": "no original title in record" if not r["original_titles"] else "title did not match",
                        "closest_series_title": guess, "closest_score": score,
                        "responsibility": r["responsibility"], "_rec": dict(r),
                    })
                continue
            author, row, score = max(matched, key=lambda t: t[2])
            key = (row["book_id"], L)
            if any(e["record_id"] == r["record_id"] or (r["isbn"] and e.get("isbn") == r["isbn"])
                   for e in found[key]):
                continue
            e = dict(r)
            e.update(match_score=score, matched_original=row["title"], author=author)
            found[key].append(e)
            n += 1
        print(f"Extra source {path}: {len(extra_rows)} rows, {n} translations matched")

    # second pass: candidates for unmatched records (keep earlier 'confirm' marks)
    previous = load_previous_candidates(args.candidates)
    candidates = build_candidates(unmatched, by_author, args.max_gap, args.min_sim, args.top_n)
    seen_keys = set()
    for c in candidates:
        seen_keys.add(cand_key(c))
        if cand_key(c) in previous:
            c["confirm"] = previous[cand_key(c)].get("confirm", "")
    # keep confirmed rows from earlier runs even if this run did not produce them
    # (e.g. run with --limit-authors or --langs)
    for k, c in previous.items():
        if k not in seen_keys and c.get("confirm", "").strip().lower() in CONFIRM_YES:
            candidates.append({col: c.get(col, "") for col in CAND_COLS})

    n_confirmed = 0
    for c in candidates:
        if c["confirm"].strip().lower() not in CONFIRM_YES:
            continue
        key = (c["suggested_book_id"], c["language_letter"])
        if any(e["record_id"] == c["source_record_id"] for e in found[key]):
            continue
        found[key].append({
            "record_id": c["source_record_id"], "matched_original": c["suggested_original_title"],
            "author": c["author"], "translated_title": c["translated_title"], "pub_date": c["pub_date"],
            "pub_year": str(c["pub_year"]), "publisher": c["publisher"], "place": c["place"],
            "translators": c["translators"], "series": c["series"], "isbn": c["isbn"],
            "source": c["source"],
            "original_titles": [t for t in c["original_title_in_record"].split(" | ") if t],
            "match_score": "manual",
        })
        n_confirmed += 1

    # assign IDs: 6157_G, or 6157a_G / 6157b_G sorted by year
    rows = []
    for (book_id, L), eds in found.items():
        eds.sort(key=lambda e: (e["pub_year"] or "9999", e["record_id"]))
        for k, e in enumerate(eds):
            tid = f"{book_id}_{L}" if len(eds) == 1 else f"{book_id}{letters(k)}_{L}"
            rows.append({
                "translation_id": tid,
                "book_id": book_id,
                "language_letter": L,
                "language": LANGS[L]["name"],
                "original_title": e["matched_original"],
                "author": e["author"],
                "translated_title": e["translated_title"],
                "pub_date": e["pub_date"],
                "pub_year": e["pub_year"],
                "publisher": e["publisher"],
                "place": e["place"],
                "translators": e["translators"],
                "series": e["series"],
                "isbn": e["isbn"],
                "source": e["source"],
                "source_record_id": e["record_id"],
                "original_title_in_record": " | ".join(e["original_titles"]),
                "match_score": e["match_score"],
                # several different original works in one translated volume = anthology edition
                "works_in_edition": works_in_edition(e),
                "edition_type": "anthology" if works_in_edition(e) > 1 else "single",
                # publisher data (Cora): first German edition, e.g. "Bianca 1808" (2011)
                "first_edition": e.get("first_edition", ""),
                "first_edition_year": e.get("first_edition_year", ""),
                "original_series": e.get("original_series", ""),
            })
    order = {b["book_id"]: i for i, b in enumerate(books)}
    rows.sort(key=lambda r: (order.get(r["book_id"], 1e9), r["language_letter"], r["translation_id"]))

    tcols = ["translation_id", "book_id", "language_letter", "language", "original_title", "author",
             "translated_title", "pub_date", "pub_year", "publisher", "place", "translators", "series",
             "isbn", "source", "source_record_id", "original_title_in_record", "match_score",
             "edition_type", "works_in_edition", "first_edition", "first_edition_year", "original_series"]
    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=tcols)
        w.writeheader()
        w.writerows(rows)

    # merged: original columns + translation columns (prefixed tr_)
    by_book = defaultdict(list)
    for r in rows:
        by_book[r["book_id"]].append(r)
    extra = [c for c in tcols if c not in ("book_id",)]
    book_cols = list(dict.fromkeys(k for b in books for k in b.keys()))   # union over all input tables
    mcols = book_cols + ["tr_" + c for c in extra]
    with open(args.merged, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=mcols)
        w.writeheader()
        for b in books:
            trs = by_book.get(b["book_id"]) or [None]
            for t in trs:
                out = dict(b)
                for c in extra:
                    out["tr_" + c] = t[c] if t else ""
                w.writerow(out)

    ucols = ["author", "language_letter", "source", "source_record_id", "translated_title", "pub_year",
             "publisher", "original_title_in_record", "reason", "closest_series_title", "closest_score",
             "responsibility"]
    with open(args.unmatched, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=ucols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(unmatched, key=lambda u: (u["author"], u["language_letter"], -u["closest_score"])))

    candidates.sort(key=lambda c: (c["author"], c["language_letter"], -float(c["candidate_score"] or 0)))
    with open(args.candidates, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CAND_COLS)
        w.writeheader()
        w.writerows(candidates)

    agg = {}
    for t in to_resolve:
        k = (fold(t["author"]), norm(t["original_title"]))
        if not k[1]:
            continue
        a = agg.setdefault(k, {**t, "languages": set(), "n_editions": 0})
        a["languages"].add(t["language_letter"])
        a["n_editions"] += 1
    with open(args.to_resolve, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["author", "original_title", "languages", "n_editions",
                                           "translated_title", "pub_year", "publisher", "reason"],
                           extrasaction="ignore")
        w.writeheader()
        for a in sorted(agg.values(), key=lambda a: (-a["n_editions"], a["author"])):
            w.writerow({**a, "languages": "".join(sorted(a["languages"]))})

    per_lang = defaultdict(int)
    for r in rows:
        per_lang[r["language"]] += 1
    print(f"\n{len(rows)} translated editions -> {args.output}  ({dict(per_lang)})")
    print(f"Merged table -> {args.merged}")
    print(f"English originals not in your table(s): {len(agg):,} -> {args.to_resolve} "
          f"(resolve with resolve_originals.py)")
    print(f"Unmatched target-language records (for checking) -> {args.unmatched}")
    by_conf = defaultdict(int)
    for c in candidates:
        by_conf[c["confidence"]] += 1
    print(f"Candidate matches to review -> {args.candidates}  ({dict(by_conf)}); "
          f"{n_confirmed} confirmed candidates included in the translation tables")


if __name__ == "__main__":
    main()
