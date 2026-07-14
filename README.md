# DFM & Design-Criteria Feedback Tool

Immediate, automated **DFM and design-criteria feedback** on stamped and molded
connector components, with a criteria store you can tune as supplier capability
feedback arrives. Local-first; runs on a Python toolchain.

> Status: **Phase 1** (immediate DFM feedback). See `cursor-dfm-tool-prompt.md`
> for the full multi-phase plan.

## Architecture (the rules that don't bend)

1. **Config-driven** — every DFM limit comes from `dfm-criteria.seed.yaml`. No
   magic numbers in code. Adding a rule = editing config.
2. **Versioned criteria** — the YAML is imported into a SQLite store; every
   change is a new version with author, timestamp, reason, and a rule-level diff.
3. **Provenance on every result** — each pass/flag/fail cites its rule id and the
   drawing note it came from.
4. **Process-family pluggable** — `stamping`, `molding`, and `cnc_machining`
   today; a new family is a config block + (optional) extractor, no engine change.
5. **Deterministic core** — the rule engine is plain comparisons. No LLM decides
   a verdict.
6. **Active vs proposed governance** — each rule (and family) carries a
   `status: active | proposed` (absent = active). Only `active` rules drive a
   verdict or the readiness score. `proposed` rules — mined from reference DFMs
   and awaiting a human sign-off — are surfaced in a separate, non-scoring
   "Proposed criteria" section of the report. Promote one by flipping its status
   to `active`. Rules may also carry `seen_count` / `evidence[]` provenance that
   the report shows as a "field-corroborated" badge.

```
dfm-criteria.seed.yaml      # single source of truth for rules
backend/
  app/
    models/    pydantic schema + YAML loader
    engine/    operators (lt/lte/gt/gte/eq/between/angle_tol) + evaluator
    extractors/ STEP geometry (lightweight), PDF notes (pdfplumber), family detect
    store/     versioned SQLite criteria store + CTF capability history
    report/    run orchestration -> report context
    templates/ server-rendered, printable report (Jinja2 + Tailwind)
    main.py    FastAPI app
    cli.py     command-line run
  tests/       operator / evaluator / extractor / seed-load tests
criteria/      SQLite store (dfm.sqlite) + version history
examples/      drawing + model fixtures (drop real files here)
uploads/       uploaded files per run
```

## Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell:  .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run the web app

```bash
cd backend
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Drop a PDF + STEP, pick the family (or auto-detect), and get a scored report you
can print / export to PDF.

## Run from the command line

```bash
cd backend
python -m app.cli --step ../examples/synthetic-sig-strip.stp --family stamping
```

## Tests

```bash
cd backend
pip install pytest
pytest -q
```

## Phase 1 scope & honesty about geometry

Phase 1 ships a **lightweight, dependency-free STEP reader** (bounding box +
stock thickness from the explicit vertices). Features that need a full B-rep
kernel — hole diameters, bend angles, wall thickness, draft — are reported as
**"needs manual check"** rather than guessed. The extractor interface is stable,
so an OpenCASCADE (`pythonocc-core`) implementation can be dropped in later
without touching the engine. A React/Vite frontend can likewise replace the
server-rendered views.

## Phase 3 — first-pass progressive die layout (stamping)

A deterministic, config-driven strip-layout generator (`app/diestrip/`) reads the
stamping family's `strip` + `forming` blocks and emits an ordered station
sequence (pilot → pierce/notch → form/idle … → coin → restrike → cutoff),
carrier/pilot scheme, strip length, and an across-feed width-utilization estimate.
The DFM bend data (angles, tolerances, springback) feeds the form stations.

```bash
# CLI
python -m app.cli --strip --step ../examples/<your-sig-strip>.stp
```

Web: open a stamping report and click **Generate strip layout**, or visit
`/strip?family=stamping`. JSON at `/api/strip?family=stamping&model=<file>`.

Tunable policy lives in `dfm-criteria.seed.yaml` under `stamping.strip.die_layout`
(carrier style/allowance, idle-between-forms, final restrike, lead operations).
Gaps that need a real B-rep extractor (true interference checks, flat-pattern
blank-area nesting) are surfaced as explicit review items, not guessed.

## Phase 7 — flat-pattern (developed blank) + "enough material" checks

`app/flatpattern/` develops a formed stamped part into its flat blank and runs
flat-state material checks, so the regularly-missed "not enough material in the
developed state" problem is caught before a design ever reaches a supplier.

**Honest by construction.** The unfolder is kernel-free: it recovers planar
patches + coaxial cylindrical bends from the STEP text (building on
`extractors/thickness.py`), then rotates each patch about its bend line into a
common plane, replacing every bend arc with its bend allowance
`BA = angle × (R_inside + K·t)`. `K` comes from config, never code. It only
reports `status: ok` when the topology is unambiguous (planar patches, simple
acyclic bends, each bend joining exactly two patches, no drawn/compound
surfaces). Anything else is `partial`/`unavailable` **with reasons**, and the
dependent checks return `manual` — never a silently-wrong flat pattern.

Flat checks are ordinary YAML rules (stamping family), so they flow through the
same engine, provenance and readiness score:

- `STMP-FLAT-MIN-WEB` — narrowest web between adjacent cutouts in the flat state
- `STMP-FLAT-FEATURE-TO-EDGE` — min cutout-to-blank-edge material
- `STMP-FLAT-CARRIER-CONNECTION` — blank-to-carrier tie width (manual w/o carrier ctx)
- `STMP-FLAT-OVERLAP` — unfolded patches must not overlap (blocker)

K-factor table and the flat minimums live under `stamping.flat_pattern`.

```bash
# CLI: develop + write supplier artifacts (DXF needs the optional ezdxf package)
python -m app.cli --step ../examples/<part>.stp --flat out.svg --flat-dxf out.dxf
```

Web: open a stamping report and click **View flat pattern**, or visit
`/flat?family=stamping&model=<file>`. JSON at `/api/flat`; supplier exports at
`/flat.svg`, `/flat.png`, `/flat.dxf`. When a flat pattern is `ok`, the strip
layout uses its real developed blank width for utilization instead of the
formed-part bounding box.

## Phase 2 (next)

- Criteria editor UI over the versioned store; CTF/SPC capability import that
  flags rules where supplier capability differs from drawing tolerance.

## Changelog

### 0.2.0 — hardening pass

- Input validation on all write paths: uploads are filename-sanitized (no path
  traversal), extension-allowlisted, and size-capped (`DFM_MAX_UPLOAD_MB`, default
  50); `GET /api/criteria/diff` returns 404 (not 500) for unknown versions;
  `POST /api/ctf` validates against Pydantic models.
- Scoring constants (`marginal_fraction`, `severity_weight`, `verdict_credit`)
  moved into `meta.scoring` in the YAML — verdicts/score are now fully
  config-driven, with in-code fallbacks for older files.
- 3D-viewer marker pinning decoupled from rule ids via an optional `marker:` tag
  (renaming a rule no longer breaks markers).
- SQLite writes serialized with a re-entrant lock (thread-safe under the
  FastAPI threadpool); version diff now reports `status`/`capability` changes.
- New test coverage for the thickness extractor, criteria store, and API layer;
  malformed STEP records are skipped instead of aborting analysis. Repo cleanup
  (removed stray DB + archived seed duplicates).

### 0.1.0 — initial DFM feedback + flat-pattern module.
