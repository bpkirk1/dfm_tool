"""CLI for the supplier-feedback miner: ``python -m app.mining <cmd> ...``.

Subcommands:
  extract     Parse a supplier folder -> findings-<supplier>.jsonl (Stage 1)
  consolidate Group findings-*.jsonl -> consolidated.md + consolidated.json (Stage 2)
  emit        Emit proposed-rules.yaml + ctf-entries.json + REVIEW_QUEUE.md (Stage 3)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import consolidate as consolidate_mod
from . import extract as extract_mod
from . import emit as emit_mod
from .model import write_jsonl

_DEFAULT_OUT = Path("new_suggestions/mining")


def _cmd_extract(args) -> int:
    findings, unparsed = extract_mod.extract_folder(args.folder, supplier=args.supplier)
    out_dir = Path(args.out or _DEFAULT_OUT)
    # Split by supplier so ids stay grouped (findings-<supplier>.jsonl).
    by_supplier: dict[str, list] = {}
    for f in findings:
        by_supplier.setdefault(f.supplier, []).append(f)
    written = []
    for supplier, group in sorted(by_supplier.items()):
        path = out_dir / f"findings-{supplier.lower()}.jsonl"
        write_jsonl(group, path)
        written.append((path, len(group)))

    print(f"\nStage 1 extract — {args.folder}")
    for path, n in written:
        print(f"  wrote {path} ({n} findings)")
    by_process: dict[str, int] = {}
    by_conf: dict[str, int] = {}
    for f in findings:
        by_process[f.process] = by_process.get(f.process, 0) + 1
        by_conf[f.confidence] = by_conf.get(f.confidence, 0) + 1
    print(f"  by process   : {by_process}")
    print(f"  by confidence: {by_conf}")
    if unparsed:
        print(f"  unparsed ({len(unparsed)}):")
        for u in unparsed:
            print(f"   - {u}")
    return 0


def _cmd_consolidate(args) -> int:
    folder = Path(args.folder or _DEFAULT_OUT)
    files = [Path(p) for p in args.files] if args.files else consolidate_mod.find_findings_files(folder)
    if not files:
        print(f"  no findings-*.jsonl found under {folder}")
        return 2
    findings = consolidate_mod.load_findings(files)
    con = consolidate_mod.build_consolidation(findings)
    out_dir = Path(args.out or folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "consolidated.auto.md"
    js = out_dir / "consolidated.auto.json"
    md.write_text(consolidate_mod.render_markdown(con), encoding="utf-8")
    js.write_text(json.dumps(con.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nStage 2 consolidate — {len(findings)} findings from {len(files)} file(s)")
    print(f"  rule candidates: {len(con.rule_candidates)}")
    print(f"  capabilities   : {len(con.capabilities)}")
    print(f"  image-only     : {len(con.image_only)}")
    print(f"  wrote {md}")
    print(f"  wrote {js}")
    return 0


def _cmd_emit(args) -> int:
    folder = Path(args.folder or _DEFAULT_OUT)
    files = [Path(p) for p in args.files] if args.files else consolidate_mod.find_findings_files(folder)
    if not files:
        print(f"  no findings-*.jsonl found under {folder}")
        return 2
    findings = consolidate_mod.load_findings(files)
    con = consolidate_mod.build_consolidation(findings)
    out_dir = Path(args.out or folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    rules = out_dir / "proposed-rules.auto.yaml"
    ctf = out_dir / "ctf-entries.auto.json"
    queue = out_dir / "REVIEW_QUEUE.auto.md"
    rules.write_text(emit_mod.emit_proposed_rules(con), encoding="utf-8")
    ctf.write_text(emit_mod.emit_ctf_entries(con), encoding="utf-8")
    queue.write_text(emit_mod.emit_review_queue(con), encoding="utf-8")
    print("\nStage 3 emit (all status: proposed — human approval required)")
    print(f"  wrote {rules} ({len(con.rule_candidates)} rules)")
    print(f"  wrote {ctf} ({len(con.capabilities)} CTF entries)")
    print(f"  wrote {queue}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.mining", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="Stage 1: parse a supplier folder into findings")
    pe.add_argument("folder", help="Folder of supplier docs (.pptx/.xlsx/.eml/.pdf)")
    pe.add_argument("--supplier", help="Force a supplier name (else auto-detected)")
    pe.add_argument("--out", help=f"Output dir (default {_DEFAULT_OUT})")
    pe.set_defaults(func=_cmd_extract)

    pc = sub.add_parser("consolidate", help="Stage 2: group findings into candidates")
    pc.add_argument("--folder", help=f"Findings dir (default {_DEFAULT_OUT})")
    pc.add_argument("--files", nargs="*", help="Explicit findings-*.jsonl paths")
    pc.add_argument("--out", help="Output dir (default = findings dir)")
    pc.set_defaults(func=_cmd_consolidate)

    pm = sub.add_parser("emit", help="Stage 3: emit proposed rules + CTF + review queue")
    pm.add_argument("--folder", help=f"Findings dir (default {_DEFAULT_OUT})")
    pm.add_argument("--files", nargs="*", help="Explicit findings-*.jsonl paths")
    pm.add_argument("--out", help="Output dir (default = findings dir)")
    pm.set_defaults(func=_cmd_emit)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
