# Citation validation prompts

Reusable prompts for validating a citation CSV, `.bib`, and the compiled PDF against
authoritative sources. Hardened against every failure seen in practice: author-list
truncation (`and others`), LaTeX accent codes (`Dud\'\ik`), mangled/double-escaped LaTeX
titles (`Mem-{\alpha}`), HTML entities (`&amp;`), title/venue capitalization lost to
`change.case`, publisher junk, and invented fields (e.g. ICLR papers with `volume=2024`).

Fill in `<CSV_FILE>` / `<BIB_FILE>` / `<PDF_FILE>` (e.g. `forced_default_citations.csv`,
`custom.bib`, the Overleaf-compiled `main.pdf`).

Three checks, escalating:
- **Prompt 1** — CSV ↔ online (data capture is correct).
- **Prompt 2** — bib ↔ CSV ↔ online (the source files agree).
- **Prompt 3** — Overleaf PDF ↔ online (the FINAL decision: what renders matches the truth).

---

## Format convention (READ FIRST — prevents format-driven false results)

- **Source of truth = the authoritative online page** (arXiv abstract page, or the published
  venue: ACL Anthology / OpenReview / NeurIPS·PMLR·JMLR proceedings / publisher), plain Unicode.
- **CSV = human-readable Unicode**, in the *same form as the source* (`Dudík`, `Mem-α`, `&`).
  Compare CSV↔online exactly, character-for-character.
- **Author lists must be COMPLETE — every author from the byline, in exact order.** Never
  truncate to `et al.`, `and others`, or `Yang, An; et al.` — not in the CSV, the bib, or the
  rendered PDF. If a BibTeX style caps long lists (e.g. `acl_natbib.bst` prints "…and N others"),
  raise its `max.num.names` limit so all names appear.
- **`.bib` = LaTeX-safe** (special chars as math/commands: `$\alpha$`, `$\infty$`, `Dud{\'\i}k`;
  `\&`; double-brace `{{…}}` to protect title case from `change.case`; never raw Greek/symbol/CJK
  in text mode — it breaks pdfLaTeX).
- **Judge the `.bib` by what it RENDERS, never by lossy normalization** (do not lowercase or strip
  LaTeX/braces before comparing — that once hid `Mem-{\alpha}`).
- **The compiled PDF is the ground truth for "what renders"** and is the basis of the final check.

---

## Source priority — go to the PRIMARY source, skip the aggregators

Validate against the primary record, never an aggregator. For each entry, read in this order:

1. **The official venue page / its exported BibTeX:**
   - ACL / EMNLP / NAACL / COLING / TACL / Findings → **ACL Anthology** (aclanthology.org) — has a per-paper BibTeX
   - ICLR, TMLR, and recent venues → **OpenReview** (openreview.net)
   - NeurIPS → **proceedings.neurips.cc** ; ICML / AISTATS / (COLM via) → **proceedings.mlr.press** (PMLR)
   - JMLR → **jmlr.org** ; AAAI → **ojs.aaai.org** ; IEEE → **ieeexplore** ; ACM → **dl.acm.org** ;
     Springer / MIT Press / Elsevier → the **publisher page** ; books → the publisher
2. **The paper's own PDF first page / arXiv abstract page** — the printed byline, and the arXiv
   **"Comments"/journal-ref** (which usually announces the venue).

**Aggregators — OpenAlex, Semantic Scholar, CrossRef, Google Scholar, DBLP — may be used ONLY to
DISCOVER a candidate** (find the venue / DOI). Every value they return MUST be confirmed on the
primary source above. When an aggregator and the primary source disagree, **the primary source wins.**

Why (real aggregator failures observed): they index the arXiv **preprint** and hide the published
venue/year (ReAct shown as arXiv, not ICLR'23; RULER not COLM'24); **misparse** given-name-first /
Chinese names ("Feng Hu"→"Feng", "An Yang"→"An"); **drop/add** authors (Toolformer lost "Hambro");
match the **wrong record** (a 1995 Technometrics article for a Springer book); and **lowercase/strip**
titles ("MemAgent"→"Memagent", "LLM"→"llm"). Treat every aggregator field as a hint, not a fact.

---

## Prompt 1 — CSV ↔ online (2-way)

```
Validate <CSV_FILE> against authoritative online sources, line by line, every column.

For each row, open the paper's latest published venue page (ACL Anthology, OpenReview,
NeurIPS/PMLR/JMLR proceedings, publisher); use arXiv only if it is an unpublished preprint.
The CSV is human-readable Unicode and must be EXACTLY the source's own form.

FIRST, for every entry — ESPECIALLY anything cited as a preprint (`@article{…arXiv…}`,
`@misc`, "arXiv preprint …"): actively determine whether a PEER-REVIEWED PUBLISHED VERSION
exists. Check the arXiv abstract page's "Comments"/"Journal ref" field (it often says e.g.
"Published as a conference paper at COLM 2024"), DBLP (a conf/ or journals/ entry = published),
OpenReview (an Accept decision), and Semantic Scholar's venue. If a published version exists,
the citation MUST use that venue + year — arXiv is acceptable ONLY after you confirm none exists.
Do not trust the entry's self-declared type, and do not trust an aggregator's "primary" record
(OpenAlex/CrossRef often index the preprint).

Then verify every field: title, authors (every author, exact order), year, venue/journal/
booktitle, volume, number, pages, publisher, arXiv id, DOI/URL links, abstract.

- Character-for-character, case-sensitive. No normalizing away capitalization, accents,
  punctuation, whitespace. ("Memagent"/"llm" != "MemAgent"/"LLM".)
- Special chars = the real Unicode glyph: "Dudík" not "Dud\'\ik", "&" not "&amp;"/"\&".
  No LaTeX/escape codes, no HTML entities in any cell.
- Every author, in source order. "et al."/"and others"/truncation = FAIL.
- Venue = official name + capitalization. Publisher = real publisher only (no addresses,
  no archive like JSTOR). Fields absent at the source stay empty (ICLR/OpenReview has no
  volume/pages; an invented "volume=2024" is a FAIL). year = venue year (note, don't fail,
  an earlier arXiv-preprint year for the same work).
- A field passes ONLY if copied verbatim and in full. Flag EVERY difference; the source wins.
  Output per row: each field PASS/FAIL with both values.

Accuracy is the top priority — take as much time as you need; one row at a time.
```

---

## Prompt 2 — bib ↔ CSV ↔ online (3-way)

```
Cross-check every citation across three places — <BIB_FILE>, <CSV_FILE>, and the authoritative
online source — line by line, every field. A citation PASSES only if all three are exactly
AND completely the same on: title, authors (every author, exact order), year, venue/journal/
booktitle, volume, number, pages, publisher, arXiv id, links.

- VENUE EXISTENCE (check FIRST): for any entry cited as a preprint (`@article{…arXiv…}`, `@misc`,
  "arXiv preprint …"), verify whether a PEER-REVIEWED PUBLISHED version exists — arXiv
  "Comments"/journal-ref (e.g. "Published as a conference paper at COLM 2024"), DBLP, OpenReview,
  Semantic Scholar. If published, all three places MUST cite that venue + year, not arXiv; an
  arXiv-only citation of a published paper is a FAIL. Don't trust the self-declared preprint type
  or an aggregator's "primary" record (OpenAlex/CrossRef often index the preprint).
- Judge the bib by what it RENDERS, not its raw string: `Dud{\'\i}k` == `Dudík`, `$\alpha$` == α,
  `{{Title}}` == Title. But `Mem-$\{$$\backslash$alpha$\}$` renders literal "Mem-{\alpha}" and is
  WRONG. Never compare via lowercasing/stripping.
- Character-for-character, case-sensitive (rendered). Every author in order (no "and others").
  Real characters, no escape codes leaking (no `\&`, no `&amp;`). Official venue name+case.
  Real publisher only. Fields absent at the source stay empty.
- Flag EVERY mismatch among the three and say WHICH place is wrong + the correct value.
  Output per row: PASS, or the offending field(s) with all three values + the fix.

Accuracy is the top priority — take as much time as you need; one entry at a time.
```

---

## Prompt 3 — Overleaf PDF ↔ online (FINAL decision check)

```
Final decision check. Read the References section of the compiled Overleaf PDF (<PDF_FILE>)
and, for EACH rendered reference, compare it — exactly as it appears in the PDF — against the
authoritative online source (arXiv abstract page, or the published venue: ACL Anthology /
OpenReview / NeurIPS·PMLR·JMLR proceedings / publisher).

The PDF is what the reader sees; the online source is the truth. They MUST be identical.

For each reference verify, character-for-character, case-sensitive — as RENDERED:
- title — correct capitalization and real special characters ("Mem-α", "∞Bench", "Dudík").
  If the PDF shows raw LaTeX ("Mem-{\alpha}", "Dud\'\ik", "$\infty$Bench") or wrong case
  ("Memagent... llm"), FAIL.
- authors — every author, exact order, correct spelling/accents; no "et al."/truncation
  unless the source itself truncates.
- year, venue, volume, number, pages, publisher — exactly the source's values; a field the
  source lacks must NOT appear (no invented volume/pages).
- venue existence — if the PDF cites an arXiv preprint but the paper was actually PUBLISHED
  at a venue (check the arXiv "Comments"/journal-ref, DBLP, OpenReview), that is a FAIL: the
  reference must cite the published venue, not arXiv.

A citation PASSES ONLY if its rendered PDF entry is exactly the same as the online source.
For each FAIL give: the PDF text, the online source value, and the fix to make in <BIB_FILE>.
Output per reference: PASS / FAIL (+ details); end with the full list of FAILs.

This is the final gate: if the PDF matches the source, the citation is correct — regardless
of what the .bib or CSV look like internally. Accuracy is the top priority; one ref at a time.
```

---

## Lessons these prompts encode (why each rule exists)

| Rule | Real miss it prevents |
|---|---|
| check for a published venue, don't trust the preprint type | ReAct cited as arXiv 2022 was ICLR 2023; RULER as arXiv was COLM 2024 (arXiv "Comments" announce the venue) |
| final check is PDF↔online; judge bib by rendered output | `Mem-$\{$$\backslash$alpha$\}$` printed literal `Mem-{\alpha}`; a lossy normalizer called it a match |
| no case normalization | `change.case` printed `Memagent… llm`; lowercasing both sides hid it |
| real Unicode in CSV, no escape codes | CSV held `Dud\'\ik`, `K\"uttler`, `\&` |
| every author, no truncation | 8 rows ended in `…; others` (Google-Scholar truncation) |
| official venue capitalization + double-brace bib titles | venues sentence-cased; `change.case` lowercased unbraced titles |
| publisher = real publisher only | `MIT Press One Broadway, 12th Floor…`; `JSTOR` (archive, not publisher) |
| absent fields stay empty | ICLR papers carried fabricated `volume=2024` + page ranges |
| CSV Unicode = source form (Option A) | makes CSV↔source exact and an independent cross-check of the bib |
| bib LaTeX-safe, never raw Greek/symbol | Unicode `α`/`♫` in a `.bib` break pdfLaTeX in text mode |
