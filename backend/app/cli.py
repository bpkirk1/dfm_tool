"""Command-line DFM run — useful for the acceptance test and CI.

    python -m app.cli --step ../examples/synthetic-sig-strip.stp --family stamping
"""
from __future__ import annotations

import argparse
import json

from . import config
from .diestrip import generate_strip_layout
from .extractors import extract_step
from .report import RunInputs, build_report
from .store import CriteriaStore


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a DFM check from the command line.")
    ap.add_argument("--step", help="Path to a STEP (.stp/.step) model")
    ap.add_argument("--pdf", help="Path to a 2D drawing PDF")
    ap.add_argument("--family", help="Process family (else auto-detect)")
    ap.add_argument("--part-name", help="Optional part name")
    ap.add_argument("--json", action="store_true", help="Print full JSON report")
    ap.add_argument(
        "--strip",
        action="store_true",
        help="Generate a first-pass progressive die strip layout (stamping)",
    )
    ap.add_argument("--flat", metavar="OUT.svg", help="Develop the flat pattern and write an SVG")
    ap.add_argument("--flat-dxf", metavar="OUT.dxf", help="Also write the developed blank as DXF")
    # Per-run display toggles (presentation only; mirror the web UI). They never
    # change verdicts, the score, or what is evaluated/stored.
    ap.add_argument("--no-manual", action="store_true", help="Hide 'needs manual check' items")
    ap.add_argument("--no-strip", action="store_true", help="Suppress die-layout suggestions")
    args = ap.parse_args()

    store = CriteriaStore(config.DB_PATH)
    if config.CRITERIA_SEED_PATH.exists():
        store.sync_from_yaml(config.CRITERIA_SEED_PATH)

    if args.strip:
        if args.no_strip:
            print("Die-layout suggestions are disabled (--no-strip); nothing to show.")
            return 0
        return _strip(store, args)
    if args.flat or args.flat_dxf:
        return _flat(store, args)

    report = build_report(
        store,
        RunInputs(
            step_path=args.step,
            pdf_path=args.pdf,
            family=args.family,
            part_name=args.part_name,
        ),
    )

    show_manual = not args.no_manual
    show_strip = not args.no_strip
    report["display_options"] = {"show_manual": show_manual, "show_strip": show_strip}

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    s = report["summary"]
    print(f"\nDFM report — {report['part_name']}")
    print(f"  family         : {report['family']}"
          f"{' (auto)' if report['family_auto_detected'] else ''}")
    print(f"  ruleset        : {report['ruleset_version']}")
    print(f"  readiness score: {s['readiness_score']}")
    print(f"  counts         : {s['counts']}")
    if report.get("geometry"):
        g = report["geometry"]
        print(f"  stock thickness: {g['stock_thickness_mm']} mm   bbox: {g['dimensions_mm']}")
    print("\n  rule                     verdict   limit            source")
    print("  " + "-" * 74)
    hidden_manual = 0
    for r in s["results"]:
        if not show_manual and r["verdict"] == "manual":
            hidden_manual += 1
            continue
        print(f"  {r['rule_id']:<24} {r['verdict']:<8} "
              f"{str(r['limit_detail']):<16} {r['source']}")
    if hidden_manual:
        print(f"  ({hidden_manual} manual-check item(s) hidden by --no-manual)")
    print()
    return 0


def _strip(store: CriteriaStore, args) -> int:
    criteria = store.get_criteria()
    family = args.family or "stamping"
    fam = criteria.family(family)
    geometry = extract_step(args.step) if args.step else None
    layout = generate_strip_layout(family, fam, geometry, criteria.meta.ruleset_version)

    print(f"\nFirst-pass strip layout — {family}")
    print(f"  pitch={layout.pitch_mm} mm  multi-up={layout.multi_up_pairs}  "
          f"stations={layout.station_count}  strip length={layout.strip_length_mm} mm")
    if layout.width_utilization_pct is not None:
        print(f"  strip width est={layout.strip_width_estimate_mm} mm  "
              f"width utilization={layout.width_utilization_pct}%")
    print("\n  #   type      operation")
    print("  " + "-" * 70)
    for s in layout.stations:
        angle = f"  [{s.target_angle_deg}deg {s.tolerance or ''}]" if s.target_angle_deg is not None else ""
        print(f"  {s.number:<3} {s.kind:<9} {s.operation}{angle}")
    if layout.review_items:
        print("\n  Needs engineer confirmation:")
        for r in layout.review_items:
            print(f"   - {r}")
    print()
    return 0


def _flat(store: CriteriaStore, args) -> int:
    from pathlib import Path

    from .flatpattern import analyze_flat, render_dxf, render_svg

    if not args.step:
        print("--flat requires --step <model.stp>")
        return 2
    criteria = store.get_criteria()
    family = args.family or "stamping"
    fam = criteria.family(family)
    result = analyze_flat(args.step, getattr(fam, "flat_pattern", None))
    fp = result.flat_pattern

    print(f"\nFlat pattern — {family}")
    print(f"  status         : {fp.status}")
    print(f"  developed bbox : {fp.developed_bbox_mm} mm")
    print(f"  bends developed: {fp.developed_bend_count}   K-factor={fp.k_factor_default}")
    if result.features.get("flat_min_web_mm") is not None:
        print(f"  min web        : {result.features['flat_min_web_mm']} mm")
    if result.features.get("flat_min_feature_to_edge_mm") is not None:
        print(f"  feat.-to-edge  : {result.features['flat_min_feature_to_edge_mm']} mm")
    for r in fp.reasons:
        print(f"   - {r}")

    limits = {
        "flat_min_web_mm": next(
            (float(x.limit) for x in fam.rules if x.parameter == "flat_min_web_mm"), None
        ),
        "flat_min_feature_to_edge_mm": next(
            (float(x.limit) for x in fam.rules if x.parameter == "flat_min_feature_to_edge_mm"),
            None,
        ),
    }
    if args.flat:
        Path(args.flat).write_text(render_svg(fp, result.details, limits), encoding="utf-8")
        print(f"  wrote SVG      : {args.flat}")
    if args.flat_dxf:
        dxf = render_dxf(fp)
        if dxf is None:
            print("  DXF skipped    : install the optional 'ezdxf' package for DXF export.")
        else:
            Path(args.flat_dxf).write_bytes(dxf)
            print(f"  wrote DXF      : {args.flat_dxf}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
