# docs/diagrams

Network and protocol diagrams for the report (R3).

Owner: all members. Related issues: #74 (network diagram).
Rules: [`AGENTS.md`](../../AGENTS.md).

| File | What | Used in |
| ---- | ---- | ------- |
| `network-g2.tex` | 3-host deployment: hosts, IPs, interfaces, access point, UDP 47000 broadcast, TCP 47001 full mesh (arrows from the side that dialled). Values from `data/deploy/` (#67). | Report §3, Figure "Network diagram" |

Diagrams are TikZ fragments, `\input` by the report, so they need no tool beyond LaTeX and add
no dependency. GitHub does not render `.tex` files: to see a diagram, build the report
(`report/README.md`, "Building") and look at the PDF.
