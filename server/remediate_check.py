"""Check remediation for every category: steps name the entity type and a concrete command or console action.

    python3 server/remediate_check.py            # lint runbooks, then run remediate.run per category (model calls)
    python3 server/remediate_check.py --static   # lint the runbooks only, no model calls

For each category it uses the newest stored report with that category from t/jobs/ (a real investigation), or a
small synthetic report when none exists. Rule for every immediate step: it names one of the runbook's entity types,
and it contains a concrete command (a backticked span with a space in it) or a console action.
Exit status 1 if any category fails.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import CATEGORIES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COMMAND = re.compile(r"`[^`]*\s[^`]*`")
PLACEHOLDER = re.compile(r"<[a-z_]+>")

SYNTHETIC: dict[str, dict[str, Any]] = {
    "memory": {"problem": "checkout-api pods OOMKilled and crash-looping after memory growth.",
               "root_cause": {"summary": "Heap grows without bound in checkout-api until containers are OOMKilled.",
                              "category": "memory", "entity": "checkout-api", "entity_type": "service",
                              "identifiers": {"version": "v2.3.0", "previous_version": "v2.2.4"}, "event_ids": []},
               "affected": {"service": "checkout-api", "hosts": ["cmp-c03-02"]}, "causal_chain": [], "confidence": 0.8},
    "unknown": {"problem": "inventory latency above SLO; cause not determined.",
                "root_cause": {"summary": "Not determined.", "category": "unknown", "entity": "unknown",
                               "entity_type": "unknown", "identifiers": {}, "event_ids": []},
                "affected": {"service": "inventory", "host": "cmp-d02-04"}, "causal_chain": [], "confidence": 0.1},
}


def entity_types(runbook_text: str) -> list[str]:
    m = re.search(r"^Entity types:\s*(.+)$", runbook_text, re.MULTILINE)
    return [t.strip().lower() for t in m.group(1).split(",")] if m else []


def step_ok(step: str, types: list[str]) -> tuple[bool, bool]:
    import remediate
    return remediate.step_compliant(step, types)


def lint_runbooks() -> int:
    bad = 0
    for cat in CATEGORIES:
        path = ROOT / "runbooks" / f"{cat}.md"
        if not path.exists():
            print(f"  MISSING runbooks/{cat}.md")
            bad += 1
            continue
        text = path.read_text(encoding="utf-8")
        types = entity_types(text)
        body = re.search(r"## Immediate steps\n(.*?)\n## ", text, re.S)
        steps = [ln[2:] for ln in (body.group(1) if body else "").splitlines() if ln.startswith("- ")]
        fails = [s for s in steps if not all(step_ok(s, types))]
        for s in fails:
            print(f"  runbooks/{cat}.md step lacks entity type or command/console: {s[:100]}")
        bad += bool(fails) or not steps
    print(f"runbook lint: {len(CATEGORIES) - bad}/{len(CATEGORIES)} ok")
    return bad


def stored_reports() -> dict[str, tuple[str, dict[str, Any], dict[str, Any] | None]]:
    out: dict[str, tuple[str, dict[str, Any], dict[str, Any] | None]] = {}
    for f in sorted(glob.glob(str(ROOT / "t" / "jobs" / "*.json")), key=os.path.getmtime):
        try:
            j = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rep = j.get("report")
        if j.get("status") == "done" and rep:
            out[(rep.get("root_cause") or {}).get("category", "unknown")] = (Path(f).stem, rep, j.get("ticket"))
    return out


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--static", action="store_true", help="lint the runbooks only")
    ap.add_argument("--category", action="append", help="only these categories")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every step")
    args = ap.parse_args()

    bad = lint_runbooks()
    if args.static:
        return 1 if bad else 0

    import remediate
    from tracelog import Trace

    stored = stored_reports()
    cats = [c for c in CATEGORIES if not args.category or c in args.category]
    print(f"\n{'category':<12} {'report':<22} {'source':<8} {'imm':>3} {'ver':>3} {'prev':>4} {'type+cmd':>9} "
          f"{'specific':>8} {'unfilled':>8}  result")
    for cat in cats:
        origin, rep, ticket = stored.get(cat) or ("synthetic", SYNTHETIC.get(cat), None)
        if rep is None:
            print(f"{cat:<12} no stored or synthetic report")
            bad += 1
            continue
        out = remediate.run(rep, Trace(), ticket)
        types = [t.lower() for t in out.get("entity_types") or []]
        checks = [step_ok(s, types) for s in out["immediate"]]
        all_ok = sum(1 for t, c in checks if t and c)
        vals = [v.lower() for v in remediate.placeholder_values(rep, ticket).values() if len(v) > 2]
        specific = any(v in s.lower() for s in out["immediate"] for v in vals)
        unfilled = sorted({m for s in out["immediate"] + out["verify"] for m in PLACEHOLDER.findall(s)})
        ok = bool(out["immediate"] and out["verify"] and out["prevent"] and out["rollback_risk"]) and \
            all_ok == len(out["immediate"]) and out["runbook"] == f"runbooks/{cat}.md"
        bad += not ok
        print(f"{cat:<12} {origin[:22]:<22} {out['source']:<8} {len(out['immediate']):>3} {len(out['verify']):>3} "
              f"{len(out['prevent']):>4} {all_ok:>4}/{len(out['immediate']):<4} {'yes' if specific else 'no':>8} "
              f"{len(unfilled):>8}  {'ok' if ok else 'FAIL'}" + (f"  ({out.get('note')})" if out.get("note") else ""))
        if args.verbose or not ok:
            for s, (t, c) in zip(out["immediate"], checks):
                print(f"      {'  ' if t and c else '!!'} {s[:160]}")
            if unfilled:
                print(f"      unfilled placeholders: {', '.join(unfilled)}")
    print("\nall categories produce compliant steps" if not bad else f"\n{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
