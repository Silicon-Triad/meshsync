# report

Report source (LaTeX) and figures for the CS-327 CEP submission.

Owner: all members. Related issues: #73 (skeleton and formal requirements), #74 (protocol,
deployment, transport: R1, R3, R4), #75 (capture, results, failure, tensions, sustainability:
R5, R6, R7, brief §6, §7).
Rules: [`AGENTS.md`](../AGENTS.md).

## Layout

| Path | Content |
| ---- | ------- |
| `main.tex` | Rubric title page, table of contents, opening block, section order, page-count check |
| `project.tex` | The only file with project strings: header, footer, opening-block text, switches (latex-report skill) |
| `preamble.tex` | Fonts, layout, headings, header/footer, TOC style, links and macros |
| `sections/00-rubric-placeholder.tex` | Shown only while `rubric.pdf` is missing |
| `sections/01-…10-*.tex` | One file per counted section, in the order the brief requires |
| `sections/11-references.tex` | References (not counted) |
| `appendices/A_run_guide.tex` | Appendix A: quick guide to running meshsync (not counted) |
| `appendices/B_code.tex` | Appendix B, code: repo link and commit hash (not counted) |
| `appendices/C_ai-use.tex` | Appendix C, AI-use declaration: summary of `docs/AI_USE_LOG.md` by area of work (last) |
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

- **Fonts:** Times New Roman, loaded by file name (never by family name: that makes Tectonic scan
  all system fonts, and one bad font crashes it). `preamble.tex` looks for lowercase `times.ttf`,
  `timesbd.ttf`, `timesi.ttf`, `timesbi.ttf` in `report/fonts/` (gitignored), then in
  `/usr/share/fonts/TTF/` (Arch: `ttf-ms-fonts`), and stops with an error if they are missing.
  With pdfLaTeX (Overleaf), `newtxtext` (a Times clone). `12pt` class option for the body;
  `titlesec` sets section headings to 14 pt; subsections stay 12 pt bold.
- **Page limit:** the rubric page is unnumbered; numbering starts at 1 on the table of contents.
  The footer reads `x of N`, where N excludes the rubric. The table of contents is not counted:
  after section 10 the build log prints `BODY PAGES: N (limit 10)` (first page of the opening
  block/section 1 to the end of section 10) and warns if N > 10. References and appendices
  start on a new page after that point.
- **Not set by the brief text we have:** paper size (A4) and margins (2.2 cm, as in the latex-report skill) are assumptions;
  confirm against the brief before submission.

## Page budget (suggestion only)

Set by #73; §2–§4 revised by #74 (the protocol spec needs more room for R1 precision), which
leaves 4.25 pages for §5–§10, to be split by #75.
Measured on the #74 build (XeLaTeX, 12 pt): §2 about 4.4, §3 about 1.2 (+0.4 for its figure),
§4 about 1.1 pages, so §2–§4 are over budget. Agreed: first collect all content, then trim and
rephrase once the whole report is compiled.

| § | Section | Pages |
| - | ------- | ----- |
| 1 | Introduction and option | 1 |
| 2 | Protocol specification (R1) | 2.5 |
| 3 | Architecture and deployment (R3) | 1.25 |
| 4 | Implementation notes, transport (R2, R4) | 1 |
| 5–10 | Capture (R5), results (R6), design tensions, sustainability, failure (R7), contributions | 4.25 (split by #75) |
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
fancyhdr, url, hyperref, tikz (libraries arrows.meta, positioning).

## Style and macros (`preamble.tex`)

House style of the latex-report skill (`references/style.md`): 2.2 cm margins, no paragraph indent
with a small gap between paragraphs, bold caption labels, a two-line header on each side (text in
`project.tex`: NED University / department at left, CEP / CS-327 at right), footer `CS-240-`,
`x of N`, `-39, -45, -47`, no header rule, plain (uncoloured) links. The brief fixes Times New
Roman, a 12 pt body and 14 pt section headings. The table of contents is page 1 of the numbering
and is not counted in the 10 pages.

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

`appendices/C_ai-use.tex` summarises `docs/AI_USE_LOG.md` by area of work (one row per area, with
issues, members, tool and model); the complete log stays in the repository and the appendix
points to it. It is written by hand: when the log gains entries, update the issue lists, the
entry count and the model note. Empty or placeholder cells in the log must be fixed in the log
itself (the member who did the work).

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
