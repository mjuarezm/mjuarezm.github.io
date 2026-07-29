#!/usr/bin/env python3
"""Sync _bibliography/papers.bib with OpenAlex (openalex.org).

Replaces the earlier scholarly-based (Google Scholar) sync script. Google
Scholar has no official API, and scholarly's automatic CAPTCHA handling
turned out to be non-functional in practice: it silently runs a *headless*
browser (no window a human could ever use to solve anything) and polls for
up to a week hoping the CAPTCHA disappears on its own. Layered on top of
that were real library-version incompatibilities (bibtexparser needing a
newer pyparsing, httpx removing the `proxies` kwarg scholarly relies on).

OpenAlex is a free, actively maintained scholarly-metadata API - no key,
no scraping, no CAPTCHAs. One request fetches the whole publication list.
It aggregates Scholar/Crossref/PubMed/institutional repositories/etc., so
coverage is very close to Scholar's, though not always identical, and its
author-disambiguation isn't perfect (see KNOWN_INSTITUTION_IDS below).

Fields OpenAlex genuinely does well that Scholar/scholarly made painful:
  - Author names arrive as clean "Last, First" strings already - no name
    parsing needed.
  - `primary_location.pdf_url` / `open_access.oa_url` are direct file
    links (not search-result redirects) when available.
  - Venue names aren't truncated with "..." the way Scholar's compact
    profile listing truncates them.

Fields that still can't come from OpenAlex (same as before - see
_layouts/bib.html and _pages/publications.md): peer, pdf (confirmation),
code, blog, award. Handled the same way as the old script: best-effort
guess/scan, always shown for confirmation (interactive) or flagged with a
`% REVIEW:` comment above the entry (--non-interactive, e.g. cron).

This author's name has at least one confirmed false-positive merge in
OpenAlex (a 2002 remote-sensing paper by an unrelated "M. Juarez" at
Instituto Tecnológico de Toluca, and an entomology encyclopedia chapter
crediting "M. Patricia Juárez" among ~80 other authors) - both verified
by inspecting their actual authorships/institutions. New entries whose
authorships don't overlap with any institution in KNOWN_INSTITUTION_IDS
get flagged for review rather than silently trusted or silently dropped.

Usage:
    python3 scripts/sync_openalex.py                     # interactive
    python3 scripts/sync_openalex.py --dry-run            # preview only
    python3 scripts/sync_openalex.py --non-interactive    # for cron
    python3 scripts/sync_openalex.py --mailto you@example.com  # OpenAlex's
        "polite pool" gets you higher, more consistent rate limits; entirely
        optional, no account/signup involved.

Setup:
    pip install -r scripts/requirements.txt
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional

try:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
except ImportError:
    print("Missing dependency: bibtexparser. Run: pip install -r scripts/requirements.txt", file=sys.stderr)
    raise

try:
    import requests
except ImportError:
    requests = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BIB_PATH = REPO_ROOT / "_bibliography" / "papers.bib"
DEFAULT_PDF_CACHE_DIR = Path(__file__).resolve().parent / ".pdf_cache"
OPENALEX_API_BASE = "https://api.openalex.org"

# Found by searching https://api.openalex.org/authors?search=Marc%20Juarez
# and cross-checking works_count=43 against papers.bib's existing titles
# (Deep Fingerprinting, Encrypted DNS -> Privacy?, A Crack in the Bark,
# etc. all matched). Override with --author-id if this ever needs to
# change (e.g. OpenAlex re-splits/merges author records).
DEFAULT_AUTHOR_ID = "A5000247697"

# OpenAlex institution IDs confirmed from this site's own content (bio.md,
# teaching.md, existing papers.bib) as this author's real affiliations.
# The OpenAlex author record itself has at least two confirmed false
# merges from unrelated "Juarez"/"Juárez" name collisions (see module
# docstring), so this is used as a review flag, not an auto-filter.
KNOWN_INSTITUTION_IDS = {
    "I98677209": "University of Edinburgh",
    "I99464096": "KU Leuven",
    "I1174212": "University of Southern California",
    "I39327780": "iMinds",
    "I4210114974": "IMEC",
    "I196972281": "Imec the Netherlands",
}

# OpenAlex work types with no place in a publications bibliography.
SKIP_TYPES = {"dataset", "grant", "peer-review", "supplementary-materials"}

OPENALEX_TYPE_TO_BIBTEX = {
    "article": "article",
    "preprint": "misc",
    "posted-content": "misc",
    "proceedings-article": "inproceedings",
    "book-chapter": "incollection",
    "book": "book",
    "report": "techreport",
}

# Venue name substrings (lowercase) that count as peer-reviewed. Extend
# this list as you publish in new venues - it only needs to be a
# reasonable first guess, since the classification is always shown to
# you (interactively) or flagged for review (non-interactively).
PEER_VENUE_HINTS = [
    "usenix security", "usenix",
    "ieee symposium on security and privacy", "s&p", "oakland",
    "computer and communications security", " ccs", "ccs'", "ccs ",
    "network and distributed system security", "ndss",
    "privacy enhancing technologies", "pets", "popets",
    "european symposium on research in computer security", "esorics",
    "internet measurement conference", "imc",
    "workshop on privacy in the electronic society", "wpes",
    "aaai conference on artificial intelligence",
    "secure and trustworthy machine learning", "satml",
    "acm sigsac", "ieee s&p", "conext",
]
PREPRINT_HINTS = ["arxiv", "preprint", "corr abs", "repository"]

AWARD_PATTERNS = [
    r".{0,60}\bbest\s+(?:student\s+)?paper\s+award.{0,60}",
    r".{0,60}\boutstanding\s+paper.{0,60}",
    r".{0,60}\bdistinguished\s+paper.{0,60}",
    r".{0,60}\brunner[- ]up.{0,60}",
]

# ALL-CAPS tokens too generic to use as a venue badge on their own.
GENERIC_ACRONYMS = {"ACM", "IEEE", "USA", "THE", "AND", "FOR"}

# Field order used when writing new entries, matching the style of the
# most recently added entries in papers.bib.
FIELD_ORDER = [
    "booktitle", "journal", "author", "title", "publisher", "year",
    "abbr", "peer", "preprint", "pdf", "code", "blog", "blog2", "award", "award2",
]


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_title(title: str) -> str:
    title = re.sub(r"\$[^$]*\$", " ", title)  # strip inline LaTeX math, e.g. $\Rightarrow$
    title = re.sub(r"\\[a-zA-Z]+", " ", title)  # strip remaining LaTeX commands
    title = re.sub(r"[{}\\]", " ", title)
    title = strip_accents(title).lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


# --------------------------------------------------------------------------
# Existing bib parsing (for dedup + citation-key collisions)
# --------------------------------------------------------------------------

def load_existing_bib(bib_path: Path):
    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    with open(bib_path, encoding="utf-8") as f:
        db = bibtexparser.load(f, parser=parser)

    normalized_titles = set()
    existing_keys = set()
    for entry in db.entries:
        existing_keys.add(entry.get("ID", ""))
        title = entry.get("title", "")
        if title:
            normalized_titles.add(normalize_title(title))
    return normalized_titles, existing_keys


def is_duplicate(title: str, normalized_titles: set, threshold: float) -> bool:
    candidate = normalize_title(title)
    if not candidate:
        return False
    for existing in normalized_titles:
        if not existing:
            continue
        if difflib.SequenceMatcher(None, candidate, existing).ratio() >= threshold:
            return True
        shorter, longer = (candidate, existing) if len(candidate) <= len(existing) else (existing, candidate)
        if len(shorter) >= 12 and longer.startswith(shorter):
            return True
    return False


# --------------------------------------------------------------------------
# Citation key generation (FirstAuthorLastNameInitialsOfCoauthorsYY)
# --------------------------------------------------------------------------

def last_name_from_raw(raw_author_name: str) -> str:
    """OpenAlex's raw_author_name is usually 'Last, First'; fall back to the
    last whitespace-separated token if there's no comma."""
    if "," in raw_author_name:
        return raw_author_name.split(",", 1)[0].strip()
    parts = raw_author_name.strip().split()
    return parts[-1] if parts else raw_author_name


def last_name_letter(last_name: str) -> str:
    cleaned = strip_accents(last_name)
    cleaned = re.sub(r"[^A-Za-z]", "", cleaned)
    return cleaned[:1].upper()


def make_citation_key(raw_author_names: list[str], year: str, existing_keys: set) -> tuple[str, Optional[str]]:
    """Returns (key, colliding_base_key). colliding_base_key is set (to the
    same firstAuthorLastNameInitialsYY fingerprint that's returned as `key`
    itself when there's no collision) if that fingerprint already exists in
    papers.bib - a strong (not certain) signal this may be a differently-
    titled record for a paper that's already tracked (see build_new_entry)."""
    first_last = strip_accents(last_name_from_raw(raw_author_names[0]))
    first_last = re.sub(r"[^A-Za-z]", "", first_last).capitalize()
    initials = "".join(last_name_letter(last_name_from_raw(a)) for a in raw_author_names[1:])
    yy = str(year)[-2:]
    base_key = f"{first_last}{initials}{yy}"
    colliding_base_key = base_key if base_key in existing_keys else None

    key = base_key
    suffix = ord("a")
    while key in existing_keys:
        key = f"{base_key}{chr(suffix)}"
        suffix += 1
    return key, colliding_base_key


def format_authors_bibtex(raw_author_names: list[str]) -> str:
    return " and ".join(raw_author_names)


# --------------------------------------------------------------------------
# OpenAlex fetching
# --------------------------------------------------------------------------

def fetch_openalex_works(author_id: str, mailto: str, timeout: float = 30.0) -> list[dict]:
    if requests is None:
        print("Missing dependency: requests. Run: pip install -r scripts/requirements.txt", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching OpenAlex works for author_id={author_id} ...")
    params = {"filter": f"author.id:{author_id}", "per-page": 200}
    if mailto:
        params["mailto"] = mailto
    ua = f"mjuarezm-website-sync/1.0 (mailto:{mailto})" if mailto else "mjuarezm-website-sync/1.0"
    resp = requests.get(f"{OPENALEX_API_BASE}/works", params=params, headers={"User-Agent": ua}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    total = data["meta"]["count"]
    results = data["results"]
    if total > len(results):
        print(f"Note: OpenAlex reports {total} works but only fetched {len(results)} "
              "(pagination beyond one page isn't implemented - unlikely to matter at this scale).",
              file=sys.stderr)
    print(f"Found {len(results)} works listed on OpenAlex.")
    return results


# --------------------------------------------------------------------------
# Venue / peer-preprint / abbr derivation
# --------------------------------------------------------------------------

def extract_conference_name_from_raw(raw_source_name: Optional[str]) -> Optional[str]:
    """Institutional-repository citation strings often look like:
    'Lin, J & Juarez, M 2025, Title. in Proceedings of the 34th USENIX
    Security Symposium. pp. 7331-7348, ...' - pull out the readable venue
    name buried in that free text, since OpenAlex's structured venue field
    for this location may just say "Edinburgh Research Explorer"."""
    if not raw_source_name:
        return None
    m = re.search(r"\bin\s+(Proceedings of[^.]+?)\.", raw_source_name)
    if m:
        return m.group(1).strip()
    return None


def extract_pages_from_raw(raw_source_name: Optional[str]) -> Optional[str]:
    if not raw_source_name:
        return None
    m = re.search(r"\bpp\.\s*(\d+)\s*-\s*(\d+)", raw_source_name)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def gather_venue_info(work: dict) -> dict:
    """Returns dict with: venue (best display text), pages, is_peer_hint
    (bool - True if any location suggests a genuine, non-repository venue),
    and all raw_source_name/source-name text concatenated for keyword
    scanning (peer-review classification, abbr guessing)."""
    locations = [l for l in (work.get("locations") or []) if l]
    primary = work.get("primary_location") or {}
    if primary and primary not in locations:
        locations = [primary] + locations

    all_text_parts = []
    conference_from_raw = None
    pages = None
    is_peer_hint = False
    best_display_name = None

    for loc in locations:
        source = loc.get("source") or {}
        display_name = source.get("display_name")
        raw_name = loc.get("raw_source_name")
        if display_name:
            all_text_parts.append(display_name)
        if raw_name:
            all_text_parts.append(raw_name)
            if conference_from_raw is None:
                conference_from_raw = extract_conference_name_from_raw(raw_name)
            if pages is None:
                pages = extract_pages_from_raw(raw_name)
        if source.get("type") not in (None, "repository") and best_display_name is None:
            best_display_name = display_name
            is_peer_hint = True

    if best_display_name is None:
        best_display_name = conference_from_raw or (primary.get("source") or {}).get("display_name")

    biblio = work.get("biblio") or {}
    if pages is None and (biblio.get("first_page") or biblio.get("last_page")):
        pages = f"{biblio.get('first_page', '')}-{biblio.get('last_page', '')}".strip("-")

    return {
        "venue": best_display_name or "",
        "pages": pages,
        "is_peer_hint": is_peer_hint,
        "scan_text": " | ".join(all_text_parts),
    }


def guess_peer_or_preprint(venue_info: dict) -> tuple[str, bool]:
    if venue_info["is_peer_hint"]:
        return "peer", True
    lowered = venue_info["scan_text"].lower()
    for hint in PEER_VENUE_HINTS:
        if hint in lowered:
            return "peer", True
    for hint in PREPRINT_HINTS:
        if hint in lowered:
            return "preprint", True
    return "preprint", False


def guess_abbr(venue_text: str, year: str) -> Optional[str]:
    if not venue_text:
        return None
    for token in re.findall(r"\b[A-Z]{2,8}\b", venue_text):
        if token in GENERIC_ACRONYMS:
            continue
        return f"{token}'{str(year)[-2:]}" if year else token
    return None


def guess_entry_type(openalex_type: str, is_peer_hint: bool) -> str:
    mapped = OPENALEX_TYPE_TO_BIBTEX.get(openalex_type)
    if mapped:
        return mapped
    return "inproceedings" if is_peer_hint else "misc"


# --------------------------------------------------------------------------
# Best-effort PDF-derived hints (code link, award mention)
# --------------------------------------------------------------------------

def download_pdf(url: str, dest_dir: Path, timeout: int = 20) -> Optional[Path]:
    if requests is None or not url:
        return None
    m = re.match(r"https?://(?:www\.)?arxiv\.org/abs/(.+)", url)
    if m:
        url = f"https://arxiv.org/pdf/{m.group(1)}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (re.sub(r"[^A-Za-z0-9]+", "_", url)[-80:] + ".pdf")
    if dest_path.exists():
        return dest_path
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and ("pdf" in content_type.lower() or url.lower().endswith(".pdf")):
            dest_path.write_bytes(resp.content)
            return dest_path
    except requests.RequestException:
        pass
    return None


def extract_pdf_text(pdf_path: Path, max_pages: int = 8) -> str:
    if pdfplumber is None:
        return ""
    text_parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:max_pages]:
                text_parts.append(page.extract_text() or "")
    except Exception:
        return ""
    return "\n".join(text_parts)


def find_code_link(text: str) -> Optional[str]:
    m = re.search(r"https?://(?:www\.)?(?:github|gitlab|zenodo)\.(?:com|org)/\S+", text)
    if not m:
        return None
    return m.group(0).rstrip(".,)]}")


def find_award_mention(text: str) -> Optional[str]:
    for pattern in AWARD_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    return None


# --------------------------------------------------------------------------
# Interactive / non-interactive field resolution
# --------------------------------------------------------------------------

def prompt_field(label: str, candidate: Optional[str], interactive: bool) -> Optional[str]:
    if not interactive:
        return candidate
    shown = candidate if candidate else "(none found)"
    resp = input(f"    {label} [{shown}]: ").strip()
    if resp == "":
        return candidate
    if resp.lower() in ("-", "none", "skip"):
        return None
    return resp


def resolve_peer_preprint(venue_info: dict, interactive: bool) -> tuple[dict, list[str]]:
    guess_field, confident = guess_peer_or_preprint(venue_info)
    warnings = []
    if interactive:
        default_is_peer = guess_field == "peer"
        default_label = "y" if default_is_peer else "n"
        resp = input(
            f"    Is this peer-reviewed (not a preprint/workshop-only paper)? "
            f"[y/n, guess={default_label}]: "
        ).strip().lower()
        is_peer = default_is_peer if resp == "" else resp.startswith("y")
    else:
        is_peer = guess_field == "peer"
        if not confident:
            warnings.append(
                "peer/preprint classification is a low-confidence guess (venue not recognized) - verify manually"
            )
    return ({"peer": "true"} if is_peer else {"preprint": "true"}), warnings


def check_known_institution(work: dict, author_id: str) -> bool:
    """Returns True if any authorship on this work overlaps a known real
    affiliation. False is a signal (not proof) that this might be a
    different, misattributed person - see module docstring."""
    for authorship in work.get("authorships", []):
        for inst in authorship.get("institutions", []):
            inst_id = (inst.get("id") or "").rsplit("/", 1)[-1]
            if inst_id in KNOWN_INSTITUTION_IDS:
                return True
    return False


def build_new_entry(work: dict, existing_keys: set, args) -> Optional[tuple[str, str, dict, list[str]]]:
    title = (work.get("title") or work.get("display_name") or "").strip()
    year = str(work.get("publication_year", "")).strip()
    authorships = work.get("authorships", [])
    author_names = [a.get("raw_author_name") or a.get("author", {}).get("display_name", "") for a in authorships]
    author_names = [a for a in author_names if a]

    if not title or not author_names or not year:
        print("  Skipping: incomplete data from OpenAlex for this work (missing title/author/year).",
              file=sys.stderr)
        return None

    venue_info = gather_venue_info(work)
    author_field = format_authors_bibtex(author_names)
    key, colliding_base_key = make_citation_key(author_names, year, existing_keys)
    entry_type = guess_entry_type(work.get("type", ""), venue_info["is_peer_hint"])
    warnings: list[str] = []

    if not check_known_institution(work, DEFAULT_AUTHOR_ID):
        warnings.append(
            "no author institution on this work matches a known affiliation - this OpenAlex author "
            "record has at least one confirmed false merge with an unrelated person of a similar name; "
            "double-check this is really the right paper before keeping it"
        )
    if colliding_base_key:
        warnings.append(
            f"this entry's key would collide with an existing {colliding_base_key} "
            "in papers.bib (same first author + coauthor initials + year) - likely the same paper already "
            "tracked under a different/fuller title; verify before keeping this as a separate entry"
        )

    print(f"\n=== New paper: {title} ({year}) ===")
    print(f"  Authors: {author_field}")
    print(f"  Venue:   {venue_info['venue']}")
    print(f"  Key:     {key}")

    fields: dict = {}
    if entry_type == "article":
        fields["journal"] = venue_info["venue"]
    else:
        fields["booktitle"] = venue_info["venue"]
    fields["author"] = author_field
    fields["title"] = title
    fields["year"] = year
    if venue_info.get("pages"):
        fields["pages"] = venue_info["pages"]

    peer_fields, peer_warnings = resolve_peer_preprint(venue_info, args.interactive)
    fields.update(peer_fields)
    warnings.extend(peer_warnings)

    abbr_candidate = guess_abbr(venue_info["venue"], year)
    abbr = prompt_field("abbr (venue badge)", abbr_candidate, args.interactive)
    if not abbr and not args.interactive:
        warnings.append("no venue abbreviation (abbr) guessed - badge will be blank on the site")
    if abbr:
        fields["abbr"] = abbr

    pdf_candidate = (
        (work.get("primary_location") or {}).get("pdf_url")
        or (work.get("open_access") or {}).get("oa_url")
    )
    pdf_text = ""
    if pdf_candidate:
        pdf_path = download_pdf(pdf_candidate, args.pdf_cache_dir)
        if pdf_path:
            pdf_text = extract_pdf_text(pdf_path)

    pdf_value = prompt_field("pdf (URL or local filename under assets/pdf/)", pdf_candidate, args.interactive)
    if pdf_value:
        fields["pdf"] = pdf_value
    elif not args.interactive:
        warnings.append("no pdf link found")

    code_candidate = find_code_link(pdf_text) if pdf_text else None
    code_value = prompt_field("code (repo URL)", code_candidate, args.interactive)
    if code_value:
        fields["code"] = code_value
    elif not args.interactive:
        warnings.append("no code link found (checked PDF text only)")

    blog_value = prompt_field("blog (post URL)", None, args.interactive)
    if blog_value:
        fields["blog"] = blog_value
    elif not args.interactive:
        warnings.append("no blog link (not derivable from OpenAlex/PDF - fill in manually if one exists)")

    award_candidate = find_award_mention(pdf_text) if pdf_text else None
    award_value = prompt_field("award", award_candidate, args.interactive)
    if award_value:
        fields["award"] = award_value

    return entry_type, key, fields, warnings


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def format_bib_entry(entry_type: str, key: str, fields: dict, warnings: Optional[list[str]] = None) -> str:
    lines = []
    for w in warnings or []:
        lines.append(f"% REVIEW: {w}")
    ordered = [k for k in FIELD_ORDER if k in fields] + [k for k in fields if k not in FIELD_ORDER]
    lines.append(f"@{entry_type}{{{key},")
    for k in ordered:
        v = fields[k]
        if not v:
            continue
        lines.append(f"  {k} = {{{v}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def append_entries(bib_path: Path, entries: list[str]) -> None:
    with open(bib_path, "a", encoding="utf-8") as f:
        for entry_text in entries:
            f.write("\n" + entry_text)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--author-id", default=DEFAULT_AUTHOR_ID,
                         help="OpenAlex author id, e.g. A5000247697 (see module docstring for how this default was verified)")
    parser.add_argument("--mailto", default="", help="Email for OpenAlex's polite pool (optional, higher rate limits)")
    parser.add_argument("--bib", type=Path, default=DEFAULT_BIB_PATH, help="Path to papers.bib")
    parser.add_argument("--pdf-cache-dir", type=Path, default=DEFAULT_PDF_CACHE_DIR,
                         help="Where to cache downloaded PDFs for text scanning")
    parser.add_argument("--threshold", type=float, default=0.87,
                         help="Fuzzy title-match ratio above which an OpenAlex work is considered a duplicate")
    parser.add_argument("--limit", type=int, default=None, help="Only process this many new papers")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to papers.bib, just show what would happen")
    interactive_group = parser.add_mutually_exclusive_group()
    interactive_group.add_argument("--interactive", dest="interactive", action="store_true", default=None,
                                    help="Force interactive prompts (default when a terminal is attached)")
    interactive_group.add_argument("--non-interactive", dest="interactive", action="store_false",
                                    help="Never prompt (for cron); best-effort guesses only, flagged for review")
    args = parser.parse_args()

    if args.interactive is None:
        args.interactive = sys.stdin.isatty()

    if requests is None:
        print("Missing dependency: requests. Run: pip install -r scripts/requirements.txt", file=sys.stderr)
        sys.exit(1)
    if pdfplumber is None:
        print("Note: pdfplumber not installed - pdf/code/award hints from downloaded PDFs will be skipped.",
              file=sys.stderr)

    normalized_titles, existing_keys = load_existing_bib(args.bib)
    print(f"Loaded {len(normalized_titles)} existing entries from {args.bib}")

    works = fetch_openalex_works(args.author_id, args.mailto)

    new_entries_text = []
    review_notes = []
    processed = 0

    for work in works:
        if work.get("type") in SKIP_TYPES:
            continue
        title = work.get("title") or work.get("display_name") or ""
        if not title:
            continue
        if is_duplicate(title, normalized_titles, args.threshold):
            continue

        result = build_new_entry(work, existing_keys, args)
        if result is None:
            continue
        entry_type, key, fields, warnings = result
        existing_keys.add(key)
        normalized_titles.add(normalize_title(title))

        entry_text = format_bib_entry(entry_type, key, fields, warnings)
        new_entries_text.append(entry_text)
        if warnings:
            review_notes.append((key, warnings))

        processed += 1
        if args.limit and processed >= args.limit:
            break

    if not new_entries_text:
        print("\nNo new papers found - papers.bib is already in sync with OpenAlex.")
        return

    print(f"\n{len(new_entries_text)} new entries {'would be' if args.dry_run else 'will be'} added:\n")
    for entry_text in new_entries_text:
        print(entry_text)

    if review_notes:
        print("=" * 70)
        print("REVIEW NEEDED for the following new entries:")
        for key, warnings in review_notes:
            print(f"  {key}:")
            for w in warnings:
                print(f"    - {w}")
        print("=" * 70)

    if args.dry_run:
        print("\n--dry-run set: not writing to", args.bib)
        return

    append_entries(args.bib, new_entries_text)
    print(f"\nAppended {len(new_entries_text)} entries to {args.bib}")


if __name__ == "__main__":
    main()
