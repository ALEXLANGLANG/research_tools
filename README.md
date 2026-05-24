# research_tools

A small collection of standalone research utilities (Python 3.8+, standard library only).

## [`citation_tools/`](citation_tools/)

Build a source-verified citation CSV from a BibTeX file, and validate citations against
authoritative online sources.

- **`pull_citations.py`** — `.bib` → canonical metadata + abstracts (OpenAlex / CrossRef /
  arXiv / Semantic Scholar) → CSV, with a resumable cache and a manual-overrides layer.
- **`validation_prompts.md`** — three escalating prompts to validate citations
  (CSV ↔ online, bib ↔ CSV ↔ online, and compiled-PDF ↔ online).

See [`citation_tools/README.md`](citation_tools/README.md) for usage.
