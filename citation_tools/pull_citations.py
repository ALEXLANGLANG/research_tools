"""Reusable citation puller.

One-shot pipeline: parse a BibTeX file -> look up canonical metadata + abstract
+ author affiliations online -> emit a CSV.

Sources, in order of reliability:
  OpenAlex (via arXiv DOI)  ->  CrossRef (via DOI)  ->  OpenAlex (title search)
  ->  arXiv API (id)  ->  arXiv API (title)  ->  Semantic Scholar (title search)

Per-entry results are cached to <cache>/<cite_key>.json so the run is fully
resumable: re-running skips entries already cached unless --refresh is passed.

Output columns (override with --columns is not supported; this is the schema
the forced-default / igmosaic CSVs use):
  cite_key, title, abstract, Category_related_work, authors, year,
  Organization, arXiv_link, latest_venue_link, _found, _source

`Category_related_work` is always left blank — it is a human/LLM judgement column.
`Organization` is auto-filled from OpenAlex institutions when available.
`_found` is yes/no (did any online source resolve the entry — useful for spotting
hallucinated citations), `_source` records which source won.

Usage:
  python3 pull_citations.py --bib path/to/refs.bib --out path/to/out.csv \
      --cache path/to/cachedir [--sleep 1.0] [--only KEY1,KEY2] [--refresh]

No external dependencies (stdlib only).
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

MAILTO = os.environ.get("CITATION_MAILTO", "your-email@example.com")  # OpenAlex/CrossRef polite-pool contact
UA = f"CitationPullerBot/1.0 (mailto:{MAILTO})"

HOST_TIMEOUT = {
    "export.arxiv.org": 12,
    "api.crossref.org": 15,
    "api.openalex.org": 15,
    "api.semanticscholar.org": 12,
}
DEFAULT_TIMEOUT = 15
HOST_MIN_DELAY = {
    "export.arxiv.org": 4.0,
    "api.crossref.org": 0.4,
    "api.openalex.org": 0.4,
    "api.semanticscholar.org": 3.5,
    "dblp.org": 3.0,
}
_LAST_HIT: dict[str, float] = {}

# Hosts that are preprint repositories rather than a "latest venue".
PREPRINT_HOSTS = ("arxiv.org", "arxiv", "preprint", "biorxiv", "ssrn", "researchsquare")


# ---------------------------------------------------------------- HTTP
def http_get(url: str, headers: dict | None = None, retries: int = 2) -> bytes:
    host = urllib.parse.urlparse(url).hostname or ""
    min_delay = HOST_MIN_DELAY.get(host, 0.3)
    timeout = HOST_TIMEOUT.get(host, DEFAULT_TIMEOUT)
    last = _LAST_HIT.get(host, 0.0)
    wait = (last + min_delay) - time.time()
    if wait > 0:
        time.sleep(wait)
    backoff = 3.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _LAST_HIT[host] = time.time()
                return resp.read()
        except urllib.error.HTTPError as e:
            _LAST_HIT[host] = time.time()
            if e.code == 404:
                raise
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                time.sleep(float(ra) if ra and ra.isdigit() else backoff)
                backoff *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            _LAST_HIT[host] = time.time()
            if attempt < retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise


# ---------------------------------------------------------------- helpers
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


def _sim(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _contain(a: str, b: str) -> float:
    """Overlap relative to the shorter title. Tolerates 'Title: subtitle' vs 'Title'
    while still rejecting unrelated works (near-zero overlap)."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


_LATEX_ACCENTS = {"'": "́", '"': "̈", "`": "̀", "^": "̂",
                  "~": "̃", "=": "̄", ".": "̇", "v": "̌",
                  "u": "̆", "H": "̋", "c": "̧"}
# \acc{\i}, \acc\i (dotless i/j) or \acc{c}, \acc c  ->  precomposed Unicode
_ACC_RE = re.compile(r"""\\(['"`^~=.vuHc])\{?(?:\\([ij])|([A-Za-z]))\}?""")


def _delatex(s: str) -> str:
    """Decode common LaTeX accent/escape commands in bib-derived text to Unicode, for
    CSV output. The .bib keeps the LaTeX (e.g. Dud{\\'\\i}k), which renders fine in the PDF;
    a CSV needs the real characters (Dudík). Leaves non-accent backslashes (e.g. $\\infty$) intact."""
    if not s or "\\" not in s:
        return s
    s = _ACC_RE.sub(lambda m: unicodedata.normalize("NFC", (m.group(2) or m.group(3)) + _LATEX_ACCENTS[m.group(1)]), s)
    for a, b in (("\\ss", "ß"), ("\\&", "&"), ("\\_", " "), ("\\%", "%"), ("\\#", "#")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


_TEXTCMD_RE = re.compile(r"\\(?:emph|textit|textbf|texttt|textsc|textrm|text|mathrm|mathbf)\{([^{}]*)\}")


def _clean_abstract(s: str) -> str:
    """Render abstract text to clean Unicode for the CSV: decode HTML entities (&amp;->&,
    &gt;->>), unwrap LaTeX text commands (\\emph{x}->x), strip protective braces and common
    escapes. Source abstracts (arXiv/OpenAlex) often carry this markup verbatim."""
    if not s:
        return s
    for _ in range(3):  # decode double-encoded entities (&amp;quot; -> &quot; -> ")
        s2 = html.unescape(s)
        if s2 == s:
            break
        s = s2
    for _ in range(3):  # nested text commands
        s2 = _TEXTCMD_RE.sub(r"\1", s)
        if s2 == s:
            break
        s = s2
    s = _delatex(s)                       # \& \_ \%, accents
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


def _invert_abstract(inv: dict) -> str:
    if not inv:
        return ""
    positions = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def is_bogus_abstract(s: str) -> bool:
    """Detect citation strings / placeholders posing as abstracts."""
    if not s:
        return True
    s = s.strip()
    if len(s) < 60:
        return True
    low = s.lower()
    if low.startswith(("abstract page for arxiv", "page not found", "loading")):
        return True
    if len(s) < 300:
        venue_hits = sum(k in low for k in ("proceedings", "conference", "journal", "transactions on", "workshop"))
        prose_hits = sum(k in low for k in (" we ", "this paper", "this work", "we propose", "we present",
                                            "we study", "we introduce", "this study", "in this", "our approach",
                                            "our method", "our model"))
        if venue_hits >= 1 and prose_hits == 0:
            return True
    return False


def _institutions_from_authorships(authorships: list) -> list[str]:
    seen, out = set(), []
    for a in authorships or []:
        for inst in (a.get("institutions") or []):
            name = (inst.get("display_name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _venue_link_from_openalex(work: dict, arxiv_id: str | None) -> str:
    """Pick the best non-preprint published location's link."""
    def host_of(url: str) -> str:
        return (urllib.parse.urlparse(url or "").hostname or "").lower()

    locs = []
    pl = work.get("primary_location")
    if pl:
        locs.append(pl)
    locs.extend(work.get("locations") or [])
    for loc in locs:
        src = (loc.get("source") or {})
        landing = loc.get("landing_page_url") or ""
        doi = loc.get("doi") or work.get("doi") or ""
        cand = doi or landing
        if not cand:
            continue
        src_name = (src.get("display_name") or "").lower()
        h = host_of(cand)
        if any(p in h for p in PREPRINT_HOSTS) or "arxiv" in src_name or "10.48550/arxiv" in cand.lower():
            continue
        return cand
    return ""


def _biblio_from_openalex(work: dict) -> dict:
    """volume / issue(number) / pages / work-type / publisher from an OpenAlex work."""
    b = work.get("biblio") or {}
    vol = str(b.get("volume") or "").strip()
    num = str(b.get("issue") or "").strip()
    fp, lp = str(b.get("first_page") or "").strip(), str(b.get("last_page") or "").strip()
    pages = f"{fp}-{lp}" if fp and lp else (fp or lp or "")
    src = (work.get("primary_location", {}) or {}).get("source", {}) or {}
    return {
        "volume": vol, "number": num, "pages": pages,
        "work_type": str(work.get("type") or "").strip(),
        "publisher": str(src.get("host_organization_name") or "").strip(),
    }


# ---------------------------------------------------------------- sources
ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}


def fetch_openalex_by_arxiv(arxiv_id: str) -> dict:
    doi = f"10.48550/arXiv.{arxiv_id}"
    url = f"https://api.openalex.org/works/https://doi.org/{doi}?mailto={MAILTO}"
    try:
        best = json.loads(http_get(url))
    except Exception as e:
        return {"source": "openalex_arxiv", "error": str(e)}
    if not best or not best.get("title"):
        return {"source": "openalex_arxiv", "error": "empty record"}
    return {
        "source": "openalex_arxiv",
        "title": best.get("title", ""),
        "authors": [(a.get("author", {}) or {}).get("display_name", "") for a in (best.get("authorships") or [])],
        "year": str(best.get("publication_year") or ""),
        "abstract": _invert_abstract(best.get("abstract_inverted_index") or {}),
        "arxiv_link": f"https://arxiv.org/abs/{arxiv_id}",
        "venue_link": _venue_link_from_openalex(best, arxiv_id),
        "journal": ((best.get("primary_location", {}) or {}).get("source", {}) or {}).get("display_name", "") or "",
        "institutions": _institutions_from_authorships(best.get("authorships")),
        **_biblio_from_openalex(best),
    }


def fetch_crossref(doi: str) -> dict:
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}"
    try:
        obj = json.loads(http_get(url)).get("message", {})
    except Exception as e:
        return {"source": "crossref", "error": str(e)}
    title = (obj.get("title") or [""])[0]
    abstract = re.sub(r"<[^>]+>", "", obj.get("abstract", "") or "").strip()
    issued = obj.get("issued", {}).get("date-parts", [[None]])[0]
    affs = []
    for a in (obj.get("author") or []):
        for aff in (a.get("affiliation") or []):
            n = (aff.get("name") or "").strip()
            if n and n not in affs:
                affs.append(n)
    return {
        "source": "crossref",
        "title": re.sub(r"\s+", " ", title).strip(),
        "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in (obj.get("author") or [])],
        "year": str(issued[0]) if issued and issued[0] else "",
        "abstract": re.sub(r"\s+", " ", abstract),
        "arxiv_link": "",
        "venue_link": obj.get("URL") or f"https://doi.org/{doi}",
        "journal": (obj.get("container-title") or [""])[0] or (obj.get("event", {}) or {}).get("name", ""),
        "institutions": affs,
        "volume": str(obj.get("volume") or "").strip(),
        "number": str(obj.get("issue") or "").strip(),
        "pages": str(obj.get("page") or "").strip(),
        "work_type": str(obj.get("type") or "").strip(),
        "publisher": str(obj.get("publisher") or "").strip(),
    }


def fetch_openalex_by_title(title: str) -> dict:
    url = f"https://api.openalex.org/works?search={urllib.parse.quote(title)}&per-page=10&mailto={MAILTO}"
    try:
        results = json.loads(http_get(url)).get("results", [])
    except Exception as e:
        return {"source": "openalex", "error": str(e)}
    if not results:
        return {"source": "openalex", "error": "no results"}
    target = _norm(title)
    best = max(results, key=lambda w: _sim(_norm(w.get("title", "") or ""), target))
    if _sim(_norm(best.get("title", "") or ""), target) < 0.6:
        return {"source": "openalex", "error": "low title similarity", "best_title": best.get("title", "")}
    doi = best.get("doi") or ""
    arxiv_link = ""
    m = re.search(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})", (doi or "").lower())
    for loc in ([best.get("primary_location")] if best.get("primary_location") else []) + (best.get("locations") or []):
        lp = (loc.get("landing_page_url") or "")
        mm = re.search(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})", lp.lower())
        if mm:
            arxiv_link = f"https://arxiv.org/abs/{mm.group(1)}"
            break
    if not arxiv_link and m:
        arxiv_link = f"https://arxiv.org/abs/{m.group(1)}"
    return {
        "source": "openalex",
        "title": best.get("title", "") or "",
        "authors": [(a.get("author", {}) or {}).get("display_name", "") for a in (best.get("authorships") or [])],
        "year": str(best.get("publication_year") or ""),
        "abstract": _invert_abstract(best.get("abstract_inverted_index") or {}),
        "arxiv_link": arxiv_link,
        "venue_link": _venue_link_from_openalex(best, None),
        "journal": ((best.get("primary_location", {}) or {}).get("source", {}) or {}).get("display_name", "") or "",
        "institutions": _institutions_from_authorships(best.get("authorships")),
        **_biblio_from_openalex(best),
    }


def fetch_arxiv(arxiv_id: str) -> dict:
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        root = ET.fromstring(http_get(url))
    except Exception as e:
        return {"source": "arxiv", "error": str(e)}
    entry = root.find("a:entry", ARXIV_NS)
    if entry is None:
        return {"source": "arxiv", "error": "no entry"}
    title = (entry.findtext("a:title", default="", namespaces=ARXIV_NS) or "").strip()
    summary = (entry.findtext("a:summary", default="", namespaces=ARXIV_NS) or "").strip()
    published = (entry.findtext("a:published", default="", namespaces=ARXIV_NS) or "").strip()
    authors = [a.findtext("a:name", default="", namespaces=ARXIV_NS).strip()
               for a in entry.findall("a:author", ARXIV_NS)]
    return {
        "source": "arxiv",
        "title": re.sub(r"\s+", " ", title),
        "authors": [a for a in authors if a],
        "year": published[:4] if published else "",
        "abstract": re.sub(r"\s+", " ", summary),
        "arxiv_link": f"https://arxiv.org/abs/{arxiv_id}",
        "venue_link": "",
        "journal": "",
        "institutions": [],
    }


# ---------------------------------------------------------------- published-venue detection
ARXIV_META_NS = {"arxiv": "http://arxiv.org/schemas/atom"}
# signals in an arXiv comment / journal_ref that the paper was published at a venue
_VENUE_RE = re.compile(
    r"\b(published|accepted|to appear|camera.?ready|proceedings of|in proceedings|"
    r"ICLR|NeurIPS|NIPS|ICML|ACL|EMNLP|NAACL|COLING|COLM|AAAI|IJCAI|CVPR|ICCV|ECCV|"
    r"KDD|SIGIR|WWW|TACL|JMLR|TMLR|TPAMI|Transactions on)\b", re.IGNORECASE)


def arxiv_venue_signals(arxiv_id: str) -> dict:
    """The arXiv entry's comment + journal_ref — these often announce the published
    venue, e.g. 'Published as a conference paper at COLM 2024'."""
    try:
        root = ET.fromstring(http_get(f"https://export.arxiv.org/api/query?id_list={arxiv_id}"))
        entry = root.find("a:entry", ARXIV_NS)
        if entry is None:
            return {}
        comment = (entry.findtext("arxiv:comment", default="", namespaces=ARXIV_META_NS) or "").strip()
        jref = (entry.findtext("arxiv:journal_ref", default="", namespaces=ARXIV_META_NS) or "").strip()
        return {"comment": re.sub(r"\s+", " ", comment), "journal_ref": re.sub(r"\s+", " ", jref)}
    except Exception:
        return {}


def dblp_venue(title: str) -> str:
    """Best-effort: DBLP's venue for the best title match. '' if CoRR (= arXiv), no match,
    or DBLP is unreachable (DBLP rate-limits hard, so failures are swallowed)."""
    try:
        data = json.loads(http_get(f"https://dblp.org/search/publ/api?q={urllib.parse.quote(title)}&format=json&h=5"))
    except Exception:
        return ""
    hits = (((data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
    target = _norm(title)
    best, bs = None, 0.0
    for h in hits:
        info = h.get("info", {})
        s = _sim(_norm(info.get("title", "") or ""), target)
        if s > bs:
            bs, best = s, info
    if not best or bs < 0.6:
        return ""
    venue = html.unescape(best.get("venue", "") or "")
    return "" if venue.strip().lower() in ("", "corr") else venue


def published_venue_hint(arxiv_id: str, title: str) -> str:
    """Return a non-empty hint iff the paper looks PUBLISHED at a venue (so an arXiv-only
    citation would be wrong). Combines the arXiv comment/journal_ref with a DBLP venue check."""
    parts = []
    if arxiv_id:
        sig = arxiv_venue_signals(arxiv_id)
        if sig.get("journal_ref"):
            parts.append(f"journal_ref={sig['journal_ref']}")
        elif sig.get("comment") and _VENUE_RE.search(sig["comment"]):
            parts.append(f"comment={sig['comment']}")
    dv = dblp_venue(title)
    if dv:
        parts.append(f"dblp={dv}")
    return " | ".join(parts)


def fetch_arxiv_by_title(title: str) -> dict:
    q = urllib.parse.quote(f'ti:"{title}"')
    url = f"http://export.arxiv.org/api/query?search_query={q}&max_results=5"
    try:
        root = ET.fromstring(http_get(url))
    except Exception as e:
        return {"source": "arxiv_title", "error": str(e)}
    entries = root.findall("a:entry", ARXIV_NS)
    if not entries:
        return {"source": "arxiv_title", "error": "no results"}
    target = _norm(title)
    best, best_score = None, 0.0
    for entry in entries:
        t = (entry.findtext("a:title", default="", namespaces=ARXIV_NS) or "").strip()
        s = _sim(_norm(t), target)
        if s > best_score:
            best_score, best = s, entry
    if best_score < 0.7:
        return {"source": "arxiv_title", "error": f"low similarity {best_score:.2f}"}
    title_a = re.sub(r"\s+", " ", best.findtext("a:title", default="", namespaces=ARXIV_NS).strip())
    summary = re.sub(r"\s+", " ", best.findtext("a:summary", default="", namespaces=ARXIV_NS).strip())
    published = best.findtext("a:published", default="", namespaces=ARXIV_NS).strip()
    aid_url = best.findtext("a:id", default="", namespaces=ARXIV_NS).strip()
    m = re.search(r"abs/([0-9]{4}\.[0-9]{4,5})", aid_url)
    return {
        "source": "arxiv_title",
        "title": title_a,
        "authors": [a.findtext("a:name", default="", namespaces=ARXIV_NS).strip()
                    for a in best.findall("a:author", ARXIV_NS)],
        "year": published[:4] if published else "",
        "abstract": summary,
        "arxiv_link": f"https://arxiv.org/abs/{m.group(1)}" if m else aid_url,
        "venue_link": "",
        "journal": "",
        "institutions": [],
    }


def fetch_s2_by_title(title: str) -> dict:
    url = (f"https://api.semanticscholar.org/graph/v1/paper/search?query={urllib.parse.quote(title)}&limit=5"
           f"&fields=title,authors,year,abstract,venue,externalIds,url")
    try:
        data = json.loads(http_get(url)).get("data", [])
    except Exception as e:
        return {"source": "s2", "error": str(e)}
    if not data:
        return {"source": "s2", "error": "no results"}
    target = _norm(title)
    best = max(data, key=lambda w: _sim(_norm(w.get("title") or ""), target))
    if _sim(_norm(best.get("title") or ""), target) < 0.6:
        return {"source": "s2", "error": "low title similarity", "best_title": best.get("title")}
    ext = best.get("externalIds") or {}
    arxiv_link = f"https://arxiv.org/abs/{ext['ArXiv']}" if ext.get("ArXiv") else ""
    venue_link = f"https://doi.org/{ext['DOI']}" if ext.get("DOI") else (best.get("url") or "")
    return {
        "source": "s2",
        "title": best.get("title") or "",
        "authors": [a.get("name", "") for a in (best.get("authors") or [])],
        "year": str(best.get("year") or ""),
        "abstract": best.get("abstract") or "",
        "arxiv_link": arxiv_link,
        "venue_link": venue_link,
        "journal": best.get("venue") or "",
        "institutions": [],
    }


# ---------------------------------------------------------------- bib parsing
ARXIV_RE = re.compile(r"arXiv[:\s]*([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)
ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)
URL_LATEX_RE = re.compile(r"\\url\s*\{([^}]+)\}")


def strip_braces(s: str) -> str:
    return re.sub(r"[{}]", "", s).strip()


def match_brace(text: str, open_idx: int) -> int:
    depth = 0
    for j in range(open_idx, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def split_top_level_entries(text: str):
    i, n = 0, len(text)
    while i < n:
        at = text.find("@", i)
        if at == -1:
            return
        brace = text.find("{", at)
        if brace == -1:
            return
        entry_type = text[at + 1:brace].strip().lower()
        if entry_type in {"comment", "preamble", "string"}:
            i = match_brace(text, brace) + 1
            continue
        end = match_brace(text, brace)
        if end == -1:
            return
        yield entry_type, text[brace + 1:end]
        i = end + 1


def parse_fields(body: str):
    depth, first_comma = 0, -1
    for j, c in enumerate(body):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "," and depth == 0:
            first_comma = j
            break
    if first_comma == -1:
        return body.strip(), {}
    bibkey = body[:first_comma].strip()
    rest = body[first_comma + 1:]
    fields, i = {}, 0
    while i < len(rest):
        while i < len(rest) and rest[i] in " \t\n\r,":
            i += 1
        if i >= len(rest):
            break
        m = re.match(r"([A-Za-z][A-Za-z0-9_\-]*)\s*=\s*", rest[i:])
        if not m:
            nxt = rest.find(",", i)
            if nxt == -1:
                break
            i = nxt + 1
            continue
        key = m.group(1).lower()
        i += m.end()
        if i >= len(rest):
            break
        c = rest[i]
        if c == "{":
            close = match_brace(rest, i)
            if close == -1:
                break
            val = rest[i + 1:close]
            i = close + 1
        elif c == '"':
            j, d = i + 1, 0
            while j < len(rest):
                ch = rest[j]
                if ch == "{":
                    d += 1
                elif ch == "}":
                    d -= 1
                elif ch == '"' and d == 0:
                    break
                j += 1
            val = rest[i + 1:j]
            i = j + 1
        else:
            j = i
            while j < len(rest) and rest[j] != ",":
                j += 1
            val = rest[i:j].strip()
            i = j
        fields[key] = val
    return bibkey, fields


def extract_arxiv_id(fields: dict):
    eprint = fields.get("eprint", "").strip()
    if re.match(r"^\d{4}\.\d{4,5}$", eprint):
        return eprint
    for src in (fields.get("journal", ""), fields.get("note", ""),
                fields.get("url", ""), fields.get("howpublished", "")):
        m = ARXIV_RE.search(src) or ARXIV_URL_RE.search(src)
        if m:
            return m.group(1)
    return None


def parse_bib(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    out = []
    for entry_type, body in split_top_level_entries(text):
        bibkey, fields = parse_fields(body)
        url = fields.get("url", "").strip()
        if not url:
            m = URL_LATEX_RE.search(fields.get("howpublished", "")) or re.search(
                r"https?://[^\s,}]+", fields.get("howpublished", ""))
            url = (m.group(1) if m and m.re is URL_LATEX_RE else (m.group(0) if m else "")).rstrip(".,;}") if m else ""
        out.append({
            "cite_key": bibkey,
            "entry_type": entry_type,
            "title": strip_braces(fields.get("title", "")),
            "authors_bib": fields.get("author", "").strip(),
            "venue_bib": strip_braces(fields.get("journal") or fields.get("booktitle") or fields.get("publisher") or ""),
            "year_bib": fields.get("year", "").strip(),
            "doi": fields.get("doi", "").strip(),
            "url": url,
            "arxiv_id": extract_arxiv_id(fields),
            # raw bibliographic fields, verbatim from the .bib (every field present on the paper)
            "journal": strip_braces(fields.get("journal", "")),
            "booktitle": strip_braces(fields.get("booktitle", "")),
            "volume": strip_braces(fields.get("volume", "")),
            "number": strip_braces(fields.get("number", "")),
            "pages": strip_braces(fields.get("pages", "")).replace("--", "-"),
            "publisher": strip_braces(fields.get("publisher", "")),
            "bib_organization": strip_braces(fields.get("organization", "")),
            "note": strip_braces(fields.get("note", "")),
        })
    return out


# ---------------------------------------------------------------- author formatting
def parse_bib_authors(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for p in re.split(r"\s+and\s+", raw.strip(), flags=re.IGNORECASE):
        p = re.sub(r"[{}]", "", p).strip().strip(",")
        if not p:
            continue
        if "," in p:
            last, first = [x.strip() for x in p.split(",", 1)]
            p = f"{first} {last}".strip()
        out.append(re.sub(r"\s+", " ", p))
    return out


def authors_bib_canonical(raw: str) -> str:
    out = []
    for a in parse_bib_authors(raw):
        parts = a.split()
        out.append(f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) >= 2 else a)
    return "; ".join(out)


# ---------------------------------------------------------------- driver
def fetch_entry(e: dict) -> dict:
    out = {"cite_key": e["cite_key"], "attempts": []}
    primary = None
    bib_title = e.get("title") or ""

    def try_source(r):
        """Accept a source only if it resolves a title that actually matches the
        bib title. OpenAlex occasionally mis-associates an arXiv DOI with an
        unrelated work, so a successful HTTP fetch is not enough — guard on title."""
        nonlocal primary
        out["attempts"].append(r)
        if not r or r.get("error") or not r.get("title"):
            return False
        if bib_title and _contain(_norm(r["title"]), _norm(bib_title)) < 0.6:
            r["error"] = f"title guard: '{r['title'][:60]}' != bib"
            return False
        primary = r
        return True

    if e.get("arxiv_id"):
        try_source(fetch_openalex_by_arxiv(e["arxiv_id"]))
    if primary is None and e.get("doi"):
        try_source(fetch_crossref(e["doi"]))
    if primary is None and e.get("title"):
        try_source(fetch_openalex_by_title(e["title"]))
    if primary is None and e.get("arxiv_id"):
        try_source(fetch_arxiv(e["arxiv_id"]))
    if primary is None and e.get("title"):
        try_source(fetch_arxiv_by_title(e["title"]))
    if primary is None and e.get("title"):
        try_source(fetch_s2_by_title(e["title"]))
    # Long "Title: verbose subtitle" entries break title search — retry on the pre-colon head.
    if primary is None and ":" in bib_title:
        head = bib_title.split(":", 1)[0].strip()
        if len(head.split()) >= 3:
            try_source(fetch_openalex_by_title(head))

    if primary is not None and primary.get("abstract") and is_bogus_abstract(primary["abstract"]):
        primary["abstract"] = ""

    # Backfill missing abstract / institutions / venue / biblio from a secondary OpenAlex title hit
    if primary is not None and e.get("title") and primary.get("source") not in ("openalex", "openalex_arxiv"):
        if (not primary.get("abstract") or not primary.get("institutions") or not primary.get("venue_link")
                or not primary.get("volume") or not primary.get("journal")):
            r = fetch_openalex_by_title(e["title"])
            out["attempts"].append(r)
            # guard: only backfill from a record whose title matches (avoid wrong-paper org/abstract)
            if r and not r.get("error") and _contain(_norm(r.get("title", "")), _norm(bib_title)) >= 0.6:
                if not primary.get("abstract") and r.get("abstract") and not is_bogus_abstract(r["abstract"]):
                    primary["abstract"] = r["abstract"]
                if not primary.get("institutions") and r.get("institutions"):
                    primary["institutions"] = r["institutions"]
                if not primary.get("venue_link") and r.get("venue_link"):
                    primary["venue_link"] = r["venue_link"]
                if not primary.get("arxiv_link") and r.get("arxiv_link"):
                    primary["arxiv_link"] = r["arxiv_link"]
                # biblio backfill (arXiv-sourced primaries carry no volume/pages)
                for f in ("volume", "number", "pages", "work_type", "publisher", "journal"):
                    if not primary.get(f) and r.get(f):
                        primary[f] = r[f]

    out["primary"] = primary or {"source": "none", "error": "all sources failed"}
    return out


def build_row(e: dict, src: dict) -> dict:
    found = bool(src) and not src.get("error") and src.get("source") != "none"
    title = (src.get("title") if found else "") or e.get("title") or ""
    abstract = src.get("abstract") or "" if found else ""
    if abstract and is_bogus_abstract(abstract):
        abstract = ""
    year = (src.get("year") if found else "") or e.get("year_bib") or ""
    org = "; ".join(src.get("institutions") or []) if found else ""
    arxiv_link = (src.get("arxiv_link") or "") if found else ""
    if not arxiv_link and e.get("arxiv_id"):
        arxiv_link = f"https://arxiv.org/abs/{e['arxiv_id']}"
    venue_link = (src.get("venue_link") or "") if found else ""
    if not venue_link and e.get("doi"):
        venue_link = f"https://doi.org/{e['doi']}"
    # Prefer the cited (bib) venue: for arXiv-hosted papers OpenAlex returns the
    # preprint location ("arXiv (Cornell University)"), not the published venue.
    venue = e.get("venue_bib") or (src.get("journal") if found else "") or ""
    return {
        "cite_key": e["cite_key"],
        "entry_type": e.get("entry_type", ""),
        "title": re.sub(r"\s+", " ", title).strip(),
        "authors": _delatex(authors_bib_canonical(e.get("authors_bib", ""))),
        "year": year,
        # bibliographic fields from the .bib (LaTeX accents/escapes decoded to Unicode)
        "journal": _delatex(e.get("journal", "")),
        "booktitle": _delatex(e.get("booktitle", "")),
        "volume": e.get("volume", ""),
        "number": e.get("number", ""),
        "pages": e.get("pages", ""),
        "publisher": _delatex(e.get("publisher", "")),
        "bib_organization": _delatex(e.get("bib_organization", "")),
        "arxiv_id": e.get("arxiv_id") or "",
        "url": e.get("url", ""),
        "venue": _delatex(re.sub(r"\s+", " ", venue).strip()),
        # enriched fields
        "abstract": _clean_abstract(abstract),
        "Category_related_work": "",
        "Organization": org,
        "arXiv_link": arxiv_link,
        "latest_venue_link": venue_link,
        "_found": "yes" if found else "no",
        "_source": src.get("source", "") if found else (src.get("error", "") or "none"),
        "published_venue_hint": "",  # filled by --check-venues
    }


COLS = ["cite_key", "entry_type", "title", "authors", "year",
        "journal", "booktitle", "volume", "number", "pages", "publisher", "bib_organization",
        "arxiv_id", "url", "venue",
        "abstract", "Category_related_work", "Organization", "arXiv_link", "latest_venue_link",
        "published_venue_hint", "_found", "_source"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bib", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--only", default="", help="comma-separated cite_keys to (re)fetch")
    ap.add_argument("--refresh", action="store_true", help="ignore cache, refetch all")
    ap.add_argument("--build-only", action="store_true", help="skip fetching, just rebuild CSV from cache")
    ap.add_argument("--overrides", default="", help="JSON {cite_key: {col: value}} merged over built rows (manual fills win)")
    ap.add_argument("--check-venues", action="store_true",
                    help="for arXiv-cited entries, check the arXiv comment/journal_ref + DBLP for a "
                         "published venue, fill published_venue_hint, and flag likely arXiv-instead-of-venue")
    args = ap.parse_args()

    bib_path = Path(args.bib).expanduser().resolve()
    cache_dir = Path(args.cache).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out).expanduser().resolve()

    entries = parse_bib(bib_path)
    only = set(s.strip() for s in args.only.split(",") if s.strip())
    print(f"Parsed {len(entries)} entries from {bib_path.name}")

    if not args.build_only:
        for idx, e in enumerate(entries, 1):
            key = e["cite_key"]
            if only and key not in only:
                continue
            cache_path = cache_dir / f"{key}.json"
            if cache_path.exists() and not args.refresh and not (only and key in only):
                print(f"[{idx}/{len(entries)}] {key}: cached")
                continue
            try:
                result = fetch_entry(e)
            except Exception as exc:  # noqa: BLE001
                result = {"cite_key": key, "primary": {"source": "exception", "error": str(exc)}}
            cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            p = result.get("primary", {})
            status = "OK" if not p.get("error") else f"ERR({p.get('error')})"
            print(f"[{idx}/{len(entries)}] {key}: {p.get('source', '?')} {status}")
            sys.stdout.flush()
            time.sleep(args.sleep)

    overrides = {}
    if args.overrides:
        op = Path(args.overrides).expanduser().resolve()
        if op.exists():
            overrides = json.loads(op.read_text())
            print(f"Loaded {len(overrides)} override(s) from {op.name}")

    rows, stats = [], {"found": 0, "missing": 0}
    for e in entries:
        cache_path = cache_dir / f"{e['cite_key']}.json"
        src = json.loads(cache_path.read_text()).get("primary", {}) if cache_path.exists() else {}
        row = build_row(e, src)
        ov = overrides.get(e["cite_key"])
        if ov:
            for col, val in ov.items():
                if col in COLS:
                    row[col] = val
            if any(row[c] for c in ("title", "abstract", "Organization")):
                row["_found"] = "yes"
                if not row["_source"] or row["_source"] in ("none", "all sources failed"):
                    row["_source"] = "manual"
        stats["found" if row["_found"] == "yes" else "missing"] += 1
        rows.append(row)

    venue_flags = []
    if args.check_venues:
        # entries the bib cites as a preprint (arXiv journal or @misc)
        preprint = [(e, r) for e, r in zip(entries, rows)
                    if e.get("journal", "").strip().lower().startswith("arxiv preprint")
                    or e.get("entry_type") == "misc"]
        print(f"\nChecking {len(preprint)} arXiv-cited entries for a published venue (arXiv comment/journal_ref + DBLP)...")
        for e, r in preprint:
            hint = published_venue_hint(e.get("arxiv_id") or "", e.get("title", ""))
            r["published_venue_hint"] = hint
            print(f"  {e['cite_key']}: {'PUBLISHED -> ' + hint if hint else 'preprint (no venue found)'}")
            sys.stdout.flush()
            if hint:
                venue_flags.append((e["cite_key"], hint))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows -> {out_path}")
    print(f"Found online: {stats['found']}  |  NOT found: {stats['missing']}")
    if stats["missing"]:
        print("NOT FOUND:", ", ".join(r["cite_key"] for r in rows if r["_found"] == "no"))
    if args.check_venues:
        if venue_flags:
            print(f"\n⚠ {len(venue_flags)} arXiv-cited entr{'y' if len(venue_flags)==1 else 'ies'} appear PUBLISHED "
                  "at a venue — cite the venue, not arXiv:")
            for k, h in venue_flags:
                print(f"  {k}: {h}")
        else:
            print("\nVenue check: no arXiv-cited entry appears published — all genuinely preprint/arXiv-only.")


if __name__ == "__main__":
    main()
