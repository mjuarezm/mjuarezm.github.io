#!/usr/bin/env python3
"""Sync _bibliography/papers.bib with a public Google Scholar profile.

Fetches the publication list from a Google Scholar profile (via the
`scholarly` package), finds papers that aren't yet in papers.bib (matched
by fuzzy title comparison), and appends new BibTeX entries for them.

Google Scholar has no official API and actively rate-limits/blocks
automated access. This script is meant for occasional, manual runs
(e.g. every few weeks) - not tight loops or frequent cron jobs. If
`scholarly` starts raising errors about blocks/CAPTCHAs, wait a while
before retrying, or configure a proxy (see the `scholarly` docs for
`scholarly.use_proxy(...)`).

Some fields used on the site (peer, pdf, code, blog, award - see
_layouts/bib.html and _pages/publications.md) aren't part of a normal
BibTeX export and can't be reliably inferred. For each new paper the
script:
  - guesses `peer` vs `preprint` from the venue name against a
    hardcoded list of known peer-reviewed venues,
  - tries to find a PDF link and a code repo link (by downloading the
    PDF, if one is linked from Scholar, and scanning its text),
  - otherwise leaves the field for you to fill in.

Run with a terminal attached (interactive mode, the default) to review
and confirm/edit every guess before it's written. Run with
--non-interactive (e.g. from cron) to accept best-effort guesses
automatically; in that mode, every new entry is preceded by a
`% REVIEW: ...` comment listing what it could not determine
confidently, so nothing is silently wrong on the live site.

Usage:
    python3 scripts/sync_scholar.py                     # interactive
    python3 scripts/sync_scholar.py --dry-run            # preview only
    python3 scripts/sync_scholar.py --non-interactive     # for cron
    python3 scripts/sync_scholar.py --limit 3            # only process
                                                          # the first 3
                                                          # new papers found

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
    from bibtexparser.customization import splitname
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

try:
    from scholarly import scholarly
except ImportError:
    scholarly = None


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USER_ID = "Na1MX7EAAAAJ"
DEFAULT_BIB_PATH = REPO_ROOT / "_bibliography" / "papers.bib"
DEFAULT_PDF_CACHE_DIR = Path(__file__).resolve().parent / ".pdf_cache"

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
    "acm sigsac", "ieee s&p",
]
PREPRINT_HINTS = ["arxiv", "preprint", "corr abs"]

AWARD_PATTERNS = [
    r".{0,60}\bbest\s+(?:student\s+)?paper\s+award.{0,60}",
    r".{0,60}\boutstanding\s+paper.{0,60}",
    r".{0,60}\bdistinguished\s+paper.{0,60}",
    r".{0,60}\brunner[- ]up.{0,60}",
]

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
        # Scholar's profile listing truncates long titles/venues with "...",
        # which can drag the ratio below threshold. Treat a clean prefix
        # match (long enough to not be a coincidence) as a duplicate too.
        shorter, longer = (candidate, existing) if len(candidate) <= len(existing) else (existing, candidate)
        if len(shorter) >= 12 and longer.startswith(shorter):
            return True
    return False


# --------------------------------------------------------------------------
# Citation key generation (FirstAuthorLastNameInitialsOfCoauthorsYY)
# --------------------------------------------------------------------------

def last_name_letter(name_piece: str) -> str:
    cleaned = strip_accents(name_piece)
    cleaned = re.sub(r"[^A-Za-z]", "", cleaned)
    return cleaned[:1].upper()


def parse_author_names(author_names: list[str]) -> list[dict]:
    """author_names: list of 'Last, First' or 'First Last' strings, in author order."""
    return [splitname(a, strict_mode=False) for a in author_names]


def format_authors_bibtex(parsed_names: list[dict]) -> str:
    """Renders parsed names as bibtex's 'Last, First and Last, First...' convention."""
    parts = []
    for p in parsed_names:
        last = " ".join(p.get("last", []))
        first = " ".join(p.get("first", []))
        parts.append(f"{last}, {first}" if first else last)
    return " and ".join(parts)


def make_citation_key(parsed_names: list[dict], year: str, existing_keys: set) -> str:
    first_last = strip_accents(" ".join(parsed_names[0].get("last", [])))
    first_last = re.sub(r"[^A-Za-z]", "", first_last).capitalize()
    initials = "".join(last_name_letter(" ".join(p.get("last", []))) for p in parsed_names[1:])
    yy = str(year)[-2:]
    base_key = f"{first_last}{initials}{yy}"

    key = base_key
    suffix = ord("a")
    while key in existing_keys:
        key = f"{base_key}{chr(suffix)}"
        suffix += 1
    return key


# --------------------------------------------------------------------------
# peer / preprint classification
# --------------------------------------------------------------------------

def guess_peer_or_preprint(venue_text: str) -> tuple[str, bool]:
    """Returns (field_name, confident) where field_name is 'peer' or 'preprint'."""
    lowered = (venue_text or "").lower()
    for hint in PEER_VENUE_HINTS:
        if hint in lowered:
            return "peer", True
    for hint in PREPRINT_HINTS:
        if hint in lowered:
            return "preprint", True
    # No recognized venue: default to preprint (matches how this file
    # already treats unrecognized/workshop venues) but flag as unconfident.
    return "preprint", False


def guess_entry_type(venue_text: str) -> str:
    lowered = (venue_text or "").lower()
    if "arxiv" in lowered:
        return "misc"
    if "journal" in lowered or "transactions" in lowered:
        return "article"
    return "inproceedings"


# ALL-CAPS tokens too generic to use as a venue badge on their own.
GENERIC_ACRONYMS = {"ACM", "IEEE", "USA", "THE", "AND", "FOR", "USENIX'"}


def guess_abbr(venue_text: str, year: str) -> Optional[str]:
    """Looks for a standalone ALL-CAPS acronym (e.g. NDSS, CCS, USENIX) in the
    venue text. Returns None (rather than a bad guess) if nothing plausible
    is found - a missing badge is better than a nonsense one."""
    if not venue_text or "…" in venue_text:  # "..." truncation marker
        return None
    for token in re.findall(r"\b[A-Z]{2,8}\b", venue_text):
        if token in GENERIC_ACRONYMS:
            continue
        return f"{token}'{str(year)[-2:]}" if year else token
    return None


def enrich_venue_from_detail_page(author_pub_id: str, timeout: int = 20) -> dict:
    """Google Scholar's profile listing truncates long venue names with an
    ellipsis, but the citation's own detail page has the full text in a
    labeled field (e.g. "Book"/"Journal"/"Conference"). Only called when the
    compact citation looks truncated or missing, to limit extra requests."""
    if requests is None or not author_pub_id or ":" not in author_pub_id:
        return {}
    user_id, _, citation_id = author_pub_id.partition(":")
    url = (
        "https://scholar.google.com/citations?view_op=view_citation&hl=en"
        f"&user={user_id}&citation_for_view={user_id}:{citation_id}"
    )
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException:
        return {}
    if resp.status_code != 200:
        return {}
    html = resp.text
    labels = re.findall(r'class="gsc_oci_field">([^<]*)<', html)
    raw_values = re.findall(r'class="gsc_oci_value">(.*?)</div>', html, re.DOTALL)
    values = [re.sub(r"<[^>]+>", "", v).strip() for v in raw_values]
    return dict(zip(labels, values))


# --------------------------------------------------------------------------
# Best-effort PDF-derived hints (code link, award mention)
# --------------------------------------------------------------------------

def download_pdf(url: str, dest_dir: Path, timeout: int = 20) -> Optional[Path]:
    if requests is None or not url:
        return None
    # arxiv.org/abs/<id> is an HTML landing page; the actual PDF is at /pdf/<id>.
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
# Scholar fetching
# --------------------------------------------------------------------------

def fetch_scholar_publications(user_id: str, delay: float):
    if scholarly is None:
        print("Missing dependency: scholarly. Run: pip install -r scripts/requirements.txt", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching Scholar profile for user_id={user_id} ...")
    author = scholarly.search_author_id(user_id)
    author = scholarly.fill(author, sections=["publications"])
    total = len(author.get("publications", []))
    print(f"Found {total} publications listed on Scholar. Fetching details "
          f"(this can take a while and may be rate-limited by Scholar) ...")

    for i, pub_stub in enumerate(author["publications"], start=1):
        try:
            pub = scholarly.fill(pub_stub)
        except Exception as exc:  # scholarly's exceptions vary by version
            print(f"  [{i}/{total}] Failed to fetch publication details, skipping: {exc}", file=sys.stderr)
            continue
        yield pub
        time.sleep(delay)


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


def resolve_peer_preprint(venue_text: str, interactive: bool) -> tuple[dict, list[str]]:
    guess_field, confident = guess_peer_or_preprint(venue_text)
    warnings = []
    if interactive:
        default_is_peer = guess_field == "peer"
        default_label = "y" if default_is_peer else "n"
        resp = input(
            f"    Is this peer-reviewed (not a preprint/workshop-only paper)? "
            f"[y/n, guess={default_label}]: "
        ).strip().lower()
        if resp == "":
            is_peer = default_is_peer
        else:
            is_peer = resp.startswith("y")
    else:
        is_peer = guess_field == "peer"
        if not confident:
            warnings.append(
                "peer/preprint classification is a low-confidence guess (venue not recognized) - verify manually"
            )
    return ({"peer": "true"} if is_peer else {"preprint": "true"}), warnings


def build_new_entry(scholar_pub: dict, existing_keys: set, args) -> Optional[tuple[str, str, dict, list[str]]]:
    bib_info = scholar_pub.get("bib", {})

    title = (bib_info.get("title") or "").strip()
    year = str(bib_info.get("pub_year", "")).strip()
    author_field_raw = bib_info.get("author", "")
    author_names = [a.strip() for a in author_field_raw.split(" and ") if a.strip()]

    if not title or not author_names or not year:
        print("  Skipping: incomplete data from Scholar for this publication (missing title/author/year).",
              file=sys.stderr)
        return None

    citation = bib_info.get("citation", "") or ""
    venue = citation
    publisher = None
    if "…" in citation or not citation:
        # Scholar's compact profile listing truncates long venue names;
        # the citation's own detail page has the full text.
        enriched = enrich_venue_from_detail_page(scholar_pub.get("author_pub_id", ""))
        full_venue = enriched.get("Book") or enriched.get("Journal") or enriched.get("Conference") or enriched.get("Source")
        if full_venue:
            venue = full_venue
        publisher = enriched.get("Publisher")

    parsed_names = parse_author_names(author_names)
    author_field = format_authors_bibtex(parsed_names)
    key = make_citation_key(parsed_names, year, existing_keys)
    entry_type = guess_entry_type(venue)
    warnings: list[str] = []

    print(f"\n=== New paper: {title} ({year}) ===")
    print(f"  Authors: {author_field}")
    print(f"  Venue:   {venue}")
    print(f"  Key:     {key}")

    fields: dict = {}
    if entry_type == "article":
        fields["journal"] = venue
    else:
        fields["booktitle"] = venue
    fields["author"] = author_field
    fields["title"] = title
    if publisher:
        fields["publisher"] = publisher
    fields["year"] = year

    peer_fields, peer_warnings = resolve_peer_preprint(venue, args.interactive)
    fields.update(peer_fields)
    warnings.extend(peer_warnings)

    # abbr: short venue badge text, e.g. "USENIX'25"
    abbr_candidate = guess_abbr(venue, year)
    abbr = prompt_field("abbr (venue badge)", abbr_candidate, args.interactive)
    if not abbr and not args.interactive:
        warnings.append("no venue abbreviation (abbr) guessed - badge will be blank on the site")
    if abbr:
        fields["abbr"] = abbr

    # PDF / code / award: try to download+scan a linked PDF for hints.
    pdf_url_candidate = scholar_pub.get("eprint_url") or scholar_pub.get("pub_url")
    if pdf_url_candidate and "scholar.google.com" in pdf_url_candidate:
        # A Scholar-internal search/cluster link, not an actual paper link.
        pdf_url_candidate = None
    pdf_text = ""
    if pdf_url_candidate:
        pdf_path = download_pdf(pdf_url_candidate, args.pdf_cache_dir)
        if pdf_path:
            pdf_text = extract_pdf_text(pdf_path)

    pdf_value = prompt_field("pdf (URL or local filename under assets/pdf/)", pdf_url_candidate, args.interactive)
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
        warnings.append("no blog link (not derivable from Scholar/PDF - fill in manually if one exists)")

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
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="Google Scholar user id")
    parser.add_argument("--bib", type=Path, default=DEFAULT_BIB_PATH, help="Path to papers.bib")
    parser.add_argument("--pdf-cache-dir", type=Path, default=DEFAULT_PDF_CACHE_DIR,
                         help="Where to cache downloaded PDFs for text scanning")
    parser.add_argument("--threshold", type=float, default=0.87,
                         help="Fuzzy title-match ratio above which a Scholar entry is considered a duplicate")
    parser.add_argument("--delay", type=float, default=4.0, help="Seconds to wait between Scholar requests")
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

    if scholarly is None or requests is None:
        print("Missing dependencies. Run: pip install -r scripts/requirements.txt", file=sys.stderr)
        sys.exit(1)
    if pdfplumber is None:
        print("Note: pdfplumber not installed - pdf/code/award hints from downloaded PDFs will be skipped.",
              file=sys.stderr)

    normalized_titles, existing_keys = load_existing_bib(args.bib)
    print(f"Loaded {len(normalized_titles)} existing entries from {args.bib}")

    new_entries_text = []
    review_notes = []
    processed = 0

    for pub in fetch_scholar_publications(args.user_id, args.delay):
        title = pub.get("bib", {}).get("title", "")
        if not title:
            continue
        if is_duplicate(title, normalized_titles, args.threshold):
            continue

        result = build_new_entry(pub, existing_keys, args)
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
        print("\nNo new papers found - papers.bib is already in sync with Scholar.")
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
