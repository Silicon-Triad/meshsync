# report

Report source (LaTeX) and figures for the CS-327 CEP submission.

Owner: all members. Related issues: #73 (skeleton and formal requirements), #74 (protocol,
deployment, transport: R1, R3, R4), #75 (capture, results, failure, tensions, sustainability:
R5, R6, R7, brief §6, §7).
Rules: [`AGENTS.md`](../AGENTS.md).

## Layout

| Path | Content |
| ---- | ------- |
| `main.tex` | Rubric title page, section order, page-count check |
| `preamble.tex` | Fonts, layout, headings, header/footer, links and macros (style of our CS-326 SRS) |
| `sections/00-rubric-placeholder.tex` | Shown only while `rubric.pdf` is missing |
| `sections/01-…10-*.tex` | One file per counted section, in the order the brief requires |
| `sections/11-references.tex` | References (not counted) |
| `appendices/code.tex` | Code appendix: repo link and commit hash (not counted) |
| `appendices/ai-use.tex` | AI-use declaration, from `docs/AI_USE_LOG.md` |
| `figures/` | Figures made for the report (diagrams also load from `docs/diagrams/`, graphs from `analysis/out/`) |
| `rubric.pdf` | The filled-in rubric; **not committed** (gitignored), we attach it ourselves |

Each section file starts with a comment naming the issue that fills it, where its data lives,
and its suggested page budget. Unfilled content is marked `\TODO{...}` and shows in red bold.

## Formal requirements (course brief §9–§11 and rubric)

- Sections in this order: 1 Introduction and the option selected; 2 Protocol specification
  (R1); 3 System and deployment architecture, network diagram, IP addressing plan (R3);
  4 Implementation notes, transport layer justification (R2, R4); 5 Packet capture and
  encapsulation analysis (R5); 6 Performance measurement, results and graphs (R6); 7 Design
  tensions (brief §6); 8 Sustainability and efficiency (brief §7); 9 Behaviour under failure
  (R7); 10 Individual contributions table, signed by all members; 11 References.
  Appendices: code (repo link + commit hash), AI-use declaration.
- **At most 10 pages**, excluding the rubric title page, references and the code appendix.
- **Times New Roman, 12 pt body, 14 pt headings.**
- The **filled-in grading rubric is the title page**; there is no separate title page.
- The report includes the network diagram, IP addressing plan, protocol message formats and
  each host's configuration (placeholders marked "filled by #74").
- Figures and tables are labelled and numbered (`\caption` + `\label`).

How `main.tex` and `preamble.tex` enforce them:

- **Fonts:** with Tectonic/XeLaTeX/LuaLaTeX, `fontspec` loads TeX Gyre Termes (a Times clone)
  by file name. For real Times New Roman, install the font files (Arch: `ttf-ms-fonts`) and set
  `\realtimestrue` in `main.tex` (`\timesfontpath` gives their folder). Fonts are never looked
  up by name: that makes Tectonic scan all system fonts, and one bad font crashes it.
  With pdfLaTeX (Overleaf), `newtxtext` (a Times clone).
  `12pt` class option for the body; `titlesec` sets section headings to 14 pt; subsections
  stay 12 pt bold.
- **Page limit:** the rubric pages are unnumbered; numbering starts at 1 on the Introduction.
  After section 10 the build log prints `BODY PAGES: N (limit 10)` and warns if N > 10.
  References and appendices start on a new page after that point.
- **Not set by the brief text we have:** paper size (A4) and margins (1 in, as in the SRS) are assumptions;
  confirm against the brief before submission.

## Page budget (suggestion only)

| § | Section | Pages |
| - | ------- | ----- |
| 1 | Introduction and option | 1 |
| 2 | Protocol specification (R1) | 1.5 |
| 3 | Architecture and deployment (R3) | 1.5 |
| 4 | Implementation notes, transport (R2, R4) | 1 |
| 5 | Capture and encapsulation (R5) | 1 |
| 6 | Performance results and graphs (R6) | 1.5 |
| 7 | Design tensions | 0.75 |
| 8 | Sustainability | 0.5 |
| 9 | Behaviour under failure (R7) | 0.75 |
| 10 | Individual contributions | 0.5 |
| | **Total** | **10** |

## Building (optional local tool, not in CI)

LaTeX is not a project dependency. Use any of:

```bash
cd report && tectonic main.tex                 # Tectonic (XeTeX); fetches packages on first build
```

```bash
cd report && latexmk -xelatex main.tex         # TeX Live, XeLaTeX
```

```bash
cd report && latexmk -pdf main.tex             # TeX Live, pdfLaTeX (also what Overleaf uses)
```

Then check the page count in the log:

```bash
grep "BODY PAGES" report/main.log
```

Packages used (all in TeX Live and the Tectonic bundle): iftex, fontspec or newtx, geometry,
array, booktabs, longtable, tabularx, graphicx, pdfpages, xcolor, refcount, caption, titlesec,
fancyhdr, url, hyperref.

## Style and macros (`preamble.tex`)

Same look as our CS-326 SRS: 1 in margins, no paragraph indent with a small gap between
paragraphs, bold caption labels, a grey running header (`meshsync -- CS-327 CEP report` /
`Group G2`), coloured links. Differences from the SRS, because the brief fixes them: Times
New Roman instead of Latin Modern, 12 pt body instead of 11 pt, 14 pt section headings,
body-size captions and tables, and no table of contents or lists of figures/tables (they would
use pages from the 10-page budget).

| Macro | Use |
| ----- | --- |
| `\begin{reptable}[size]{cols} … \end{reptable}` | Numbered table that breaks across pages with a repeated header (the SRS `srstable`) |
| `\repfigure{file}{caption}{label}{maxheight}` | Numbered figure, shrunk to fit, never enlarged |
| `\placeholderfigure{todo}{caption}{label}` | Boxed slot until the real figure exists |
| `\code{…}` | Code, paths, message types |
| `\refentry{n}{text}`, `\refcite{n}` | IEEE reference list and citations, no BibTeX run |
| `\TODO{…}` | Red marker for anything not filled in yet |
| `repnote` environment | Indented italic note |
Build outputs (PDF, aux, log, …) are gitignored; never commit them.

## Updating the AI-use appendix

`appendices/ai-use.tex` has one row per row of `docs/AI_USE_LOG.md`. Empty or placeholder
cells in the log appear as `TODO`: fix them in the log (the member who did the work), then
update the matching row here. Rows added to the log later (for example #74, #75) must be added
here before submission.

## Submission checklist

- [ ] No `TODO` left in the PDF (search for "TODO"); `BODY PAGES` ≤ 10.
- [ ] Filled-in rubric at `report/rubric.pdf`, on top as the title page.
- [ ] Contributions table signed by all three members.
- [ ] Code appendix has the commit hash of the submitted `main`.
- [ ] AI-use appendix matches `docs/AI_USE_LOG.md`, every row complete.
- [ ] Printed copy.
- [ ] PDF named with the uploader's roll number without the hyphen, `CS24XXX.pdf`
      (e.g. `CS24045.pdf`), uploaded by **each member from their own account**.
- [ ] All members present at submission.
- [ ] Deadline: end of Week 13 (exact date TBD).
