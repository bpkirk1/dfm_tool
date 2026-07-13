# BUILD PROMPT — DFM & Design-Criteria Feedback Tool (Stamping + Molding)

> Load this file into Cursor (Composer / Agent) as the project kickoff prompt.
> A companion file, `dfm-criteria.seed.yaml`, holds the starting rule set pulled
> from two real drawings. Build against that file; do not hardcode limits.

---

## 1. What we're building

An internal tool that gives connector engineers **immediate, automated DFM and
design-criteria feedback** on new stamped and molded components, and that lets
us **tune the criteria as supplier capability feedback arrives**. The long-term
goal is to extend it toward **automated stamping die (strip) layout**.

Build it as a local-first web app (single repo) that an engineer can run, drop a
drawing + 3D model into, and get a scored DFM report in seconds.

## 2. Who uses it and why

- **Design / product engineers** — check a part before release; see which
  criteria pass, flag, or fail, with the source note cited.
- **Tooling / die engineers** — see manufacturability risks early and, later,
  generate a first-pass strip layout.
- **Suppliers (indirect)** — their FAI/SPC capability feedback updates the
  criteria store so the rules reflect what can actually be held.

## 3. Reference inputs (already in the repo under `/examples`)

Two real, representative parts. Use them as the fixtures for every feature you
build and test.

| Family   | 2D drawing (PDF)        | 3D model (STEP AP214, mm) | Part |
|----------|-------------------------|---------------------------|------|
| Stamping | `P2331111500.pdf`       | `...sig-t-strip.stp`      | 0.080 mm Cu-alloy signal leadframe, multi-up 1–4 pair, formed spring beams |
| Molding  | `P2291551500.pdf`       | `...center-strip_asm.stp` | Insert-molded center plastic, multi-up 1–4 pair |

Key facts the tool must handle (already encoded in the seed YAML):

- **Ultra-thin stock (0.080 ±0.005 mm).** Classic *t*-multiple feature minimums
  (1·*t* holes, 1.5·*t* edges) are meaningless here — minimums are set by
  **supplier tooling capability**. Those rules are marked `supplier_adjustable`
  and must be confirmable at FAI. Do not bake in textbook *t*-multiples for thin
  precision stock; read the limit from config.
- **Multi-axis forming** (spring ramps, tabs, tube folds) with per-bend
  springback compensation and angle tolerances like 135° +8/−1, 30° +1/−2.
- **Strip / progression:** 20.00 mm pitch (15 progressions = 300.00 ±0.05 mm),
  multi-up 4 pairs, coining allowed for pitch/camber, selective Au plating,
  **no Sn on the carrier**.
- **Molding:** flash (0.10 at ejectors / 0.15 general / 0.05 specified), sink
  ≤0.05, draft ≤1° on posts, parting-line mismatch ≤0.03, gate vestige ≤0.05.
- **CTF + SPC:** ballooned dims are Critical-To-Function, measured in-process by
  SPC, with FAI feedback revising tolerances. The tool tracks CTF dims and their
  achieved capability (Cpk).

## 4. Hard architectural rules (do not violate)

1. **Config-driven rules.** All DFM limits come from `dfm-criteria.seed.yaml`
   (or a DB seeded from it). No magic numbers in code. Adding a rule = editing
   config, not editing the engine.
2. **Versioned criteria.** Every change to the criteria store is versioned with
   author, timestamp, and reason. An engineer can diff two ruleset versions and
   see what changed and why (e.g. "supplier raised min pierce from 0.15→0.18").
3. **Provenance on every result.** Each pass/flag/fail cites the rule id and the
   drawing note/source it came from. No unexplained verdicts.
4. **Process-family pluggable.** `stamping` and `molding` are two families today;
   adding a third (e.g. `etching`) is a config + a checker module, nothing more.
5. **Deterministic core, optional AI assist.** The rule engine is plain
   deterministic checks. Any LLM use (e.g. summarizing a report, parsing a messy
   drawing note) is an optional, clearly isolated layer — never the source of a
   pass/fail.

## 5. Recommended stack

- **Backend:** Python (FastAPI). Engineering ecosystem + good CAD libs.
- **Geometry:** parse STEP AP214 with `cadquery`/OpenCASCADE (`pythonocc-core`)
  or `steputils` for a lighter read. Extract: bounding box, stock thickness,
  hole/slot sizes, bend faces & angles, wall thickness (molding), draft angles.
- **2D drawings:** start by reading the PDF text layer (`pdfplumber`) to pull
  notes, tolerances, and ballooned CTF callouts; treat OCR/vision as a later
  enhancement. Many of these drawings carry a clean text layer.
- **Rules:** load YAML (`pydantic` models) → a small evaluator with the operator
  set (`lt/lte/gt/gte/eq/between/angle_tol`).
- **Frontend:** React + Vite, Tailwind. Clean, technical, printable report view.
- **Store:** SQLite to start (versioned criteria + CTF capability history);
  the seed YAML imports into it on first run.

## 6. Build in phases — ship Phase 1 first

### Phase 1 — Immediate DFM feedback (the core deliverable)
- Drop zone: upload a PDF + STEP, pick the process family (or auto-detect).
- Geometry extractor pulls measurable features from the STEP.
- Drawing parser pulls notes / tolerances / CTF balloons from the PDF.
- Rule engine evaluates features against the criteria for that family.
- **Report view:** readiness score, pass/flag/fail per rule with the cited
  source, and a list of features that couldn't be auto-measured (so the engineer
  knows what still needs a human check). Print / export to PDF.
- Run end-to-end on **both** example parts as the acceptance test.

### Phase 2 — Editable, versioned criteria + supplier loop
- Criteria editor UI that reads/writes the rule store with full version history.
- CTF capability tracking: enter/import SPC results (achieved value, Cpk, n) per
  ballooned dim; the tool flags rules where supplier capability is tighter or
  looser than the drawing tolerance.
- "What changed" diff between ruleset versions.

### Phase 3 — Toward automated stamping die layout
- From the leadframe model + strip params, generate a **first-pass strip layout**:
  station sequence (pierce → notch → form → restrike → cutoff), pitch (20 mm),
  carrier/pilot scheme, idle stations between interfering forms, multi-up nesting,
  and material-utilization estimate. Export the station table.
- This is where the DFM forming data (bend angles, springback, radii) feeds the
  die progression — keep the data model from Phase 1 ready for it.

## 7. Acceptance criteria (Phase 1)

- Both example parts load (PDF + STEP) without manual prep.
- The seed YAML drives every check; deleting a rule from YAML removes it from the
  report with no code change.
- Each result shows: rule id, measured value (or "needs manual check"), limit,
  verdict, severity, and cited source.
- A criteria value edited in YAML changes the next report's verdict.
- Report exports to a clean PDF an engineer can attach to a design review.

## 8. First tasks for the agent

1. Scaffold the repo (backend + frontend + `/examples` + `/criteria`).
2. Implement the pydantic model + evaluator for `dfm-criteria.seed.yaml` and unit-test the operators.
3. Build the STEP feature extractor; prove it reads stock thickness = 0.080 mm and the strip bounding box from the sig-strip model.
4. Build the PDF note/tolerance/CTF extractor against `P2331111500.pdf`.
5. Wire the report UI and run both example parts end-to-end.

Ask me before introducing any heavy dependency or cloud service — this should run
locally first.
