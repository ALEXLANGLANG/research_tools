# citation_tools

Build a clean, source-verified citation table from a BibTeX file, and validate citations
against authoritative sources. No dependencies — Python 3.8+ standard library only.

## `pull_citations.py`

Parses a `.bib` and, for each entry, looks up canonical metadata + abstract online, then
writes a CSV. Sources are tried in order of reliability and the result is title-guarded
(a source is accepted only if its title matches the bib entry, so a stale/wrong arXiv DOI
can't silently bind to the wrong paper):

```
OpenAlex (arXiv DOI) → CrossRef (DOI) → OpenAlex (title) → arXiv API (id)
→ arXiv (title) → Semantic Scholar (title)
```

Per-entry JSON is cached under `--cache`, so runs are resumable (re-running skips cached
entries unless `--refresh`).

### Usage

```bash
export CITATION_MAILTO="you@example.com"   # OpenAlex/CrossRef "polite pool" contact

python3 pull_citations.py \
  --bib path/to/refs.bib \
  --out path/to/citations.csv \
  --cache path/to/cache_dir \
  [--overrides overrides.json]   # manual fills merged over the result (survive re-runs)
  [--only key1,key2]             # (re)fetch just these cite keys
  [--refresh]                    # ignore cache, refetch
  [--build-only]                 # skip fetching, just rebuild the CSV from cache + overrides
```

### Output columns

`cite_key, entry_type, title, authors, year, journal, booktitle, volume, number, pages,
publisher, bib_organization, arxiv_id, url, venue, abstract, Category_related_work,
Organization, arXiv_link, latest_venue_link, _found, _source`

- Raw bib fields (`journal`…`url`) are taken verbatim from the `.bib`; LaTeX accents are
  decoded to Unicode (`Dud{\'\i}k` → `Dudík`), HTML entities and `\emph{}` are cleaned.
- `Organization` = author affiliations (from OpenAlex institutions); distinct from
  `bib_organization` (the bibtex `organization` field, e.g. `PMLR`).
- `Category_related_work` is left blank for you to fill (a human/LLM judgement column).
- `_found` / `_source` are diagnostics — useful for spotting hallucinated/uncited entries.

### Manual overrides

OpenAlex coverage is sparse for very recent preprints and some venue records. Put manual
fixes in a JSON keyed by cite_key; they are merged over the built row and survive re-runs:

```json
{
  "smith2024example": {
    "Organization": "Stanford University; Google DeepMind",
    "Category_related_work": "Retrieval-augmented LMs",
    "abstract": "..."
  }
}
```

See `overrides.example.json`.

## `validation_prompts.md`

Three escalating prompts (for you or an LLM) to validate citations against authoritative
sources, hardened against the failure modes that bite real bibliographies (author-list
truncation, LaTeX accent codes, mangled/double-escaped titles like `Mem-{\alpha}`, HTML
entities, lost capitalization, publisher junk, invented `volume`/`pages`):

- **Prompt 1** — CSV ↔ online (data capture is correct).
- **Prompt 2** — bib ↔ CSV ↔ online (the source files agree).
- **Prompt 3** — compiled PDF ↔ online (the final decision: what *renders* matches the truth).

Key principle baked in: compare by what **renders**, never by lossy normalization
(lowercasing/stripping LaTeX), and keep the `.bib` LaTeX-safe while the CSV stays
human-readable Unicode.
