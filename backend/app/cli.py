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
    args = ap.parse_args()

    store = CriteriaStore(config.DB_PATH)
    if config.CRITERIA_SEED_PATH.exists():
        store.sync_from_yaml(config.CRITERIA_SEED_PATH)

    if args.strip:
        return _strip(store, args)

    report = build_report(
        store,
        RunInputs(
            step_path=args.step,
            pdf_path=args.pdf,
            family=args.family,
            part_name=args.part_name,
        ),
    )

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
    for r in s["results"]:
        print(f"  {r['rule_id']:<24} {r['verdict']:<8} "
              f"{str(r['limit_detail']):<16} {r['source']}")
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


if __name__ == "__main__":
    raise SystemExit(main())
