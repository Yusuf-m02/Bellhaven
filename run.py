#!/usr/bin/env python3
"""Bellhaven CRM sync - command line entry point.

    python run.py fetch                 # pull website + CRM into data/snapshot
    python run.py fetch --offline       # re-parse the cached pages in data/raw
    python run.py pipeline              # snapshot -> proposals (no network)
    python run.py review                # local approval app on :8787
    python run.py apply                 # push approved proposals to the CRM
    python run.py apply --dry-run       # show the API calls, send nothing
    python run.py daily                 # fetch + pipeline (what cron runs)
    python run.py status                # counts by status and kind
    python run.py decide <fp> approve|reject [--note ...]   # headless decision
"""
from __future__ import annotations

import argparse
import json
import sys

from bellhaven import config, pipeline, store


def cmd_fetch(args) -> int:
    out = pipeline.stage_fetch(live=not args.offline)
    print(json.dumps(out, indent=2))
    return 0


def cmd_pipeline(args) -> int:
    st = store.Store()
    counts = pipeline.stage_pipeline(st)
    print(json.dumps(counts, indent=2))
    print("\nstatus:", json.dumps(st.counts()))
    return 0


def cmd_daily(args) -> int:
    print(json.dumps(pipeline.stage_fetch(live=not args.offline), indent=2))
    return cmd_pipeline(args)


def cmd_apply(args) -> int:
    st = store.Store()
    summary = pipeline.stage_apply(st, dry_run=args.dry_run, plan_out=args.plan_out)
    if args.dry_run or args.plan_out:
        print(json.dumps(summary["calls"], indent=2))
        print(f"\n{len(summary['calls'])} call(s) across {summary['approved']} "
              f"approved proposal(s). Nothing was sent.")
    else:
        print(json.dumps({k: v for k, v in summary.items() if k != "calls"}, indent=2))
    return 0


def cmd_ingest(args) -> int:
    print(json.dumps(pipeline.ingest_apply_results(store.Store(), args.results), indent=2))
    return 0


def cmd_review(args) -> int:
    from bellhaven import review_app
    review_app.serve(args.host or config.REVIEW_HOST, args.port or config.REVIEW_PORT)
    return 0


def cmd_status(args) -> int:
    st = store.Store()
    counts = st.counts()
    print("proposals by status:", json.dumps(counts, indent=2))
    by_kind: dict[str, dict[str, int]] = {}
    for p in st.list_proposals():
        by_kind.setdefault(p["kind"], {}).setdefault(p["status"], 0)
        by_kind[p["kind"]][p["status"]] += 1
    print("by kind:", json.dumps(by_kind, indent=2))
    return 0


def cmd_decide(args) -> int:
    st = store.Store()
    decision = {"approve": "approved", "reject": "rejected"}[args.decision]
    st.decide(args.fingerprint, decision, by=args.by, note=args.note or "")
    print(f"{args.fingerprint} -> {decision}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch"); p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("pipeline"); p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("daily"); p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("apply")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--plan-out", help="write resolved API calls to this JSON file")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("ingest-results", help="record results of a plan applied elsewhere")
    p.add_argument("results"); p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("review")
    p.add_argument("--host"); p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)

    p = sub.add_parser("decide")
    p.add_argument("fingerprint"); p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--by", default="cli"); p.add_argument("--note")
    p.set_defaults(func=cmd_decide)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
