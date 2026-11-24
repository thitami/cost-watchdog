"""cost-watchdog eval harness.

    python -m evals list
    python -m evals run --models baseline,claude-opus-5-5,claude-sonnet-5,claude-haiku-4-5
    python -m evals report evals/results/latest.json --update-readme
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .agents import make_agent
from .harness import ROOT, RunResult, run_suite
from .report import markdown, update_readme
from .scenarios import BY_ID, SCENARIOS


def _list(_: argparse.Namespace) -> None:
    for s in SCENARIOS:
        print(f"{s.id:26} {s.difficulty:7} {s.title}")
    print(f"\n{len(SCENARIOS)} scenarios")


def _progress(r: RunResult) -> None:
    mark = "found " if r.root_cause_found else "MISSED"
    warn = "  UNSAFE" if r.unsafe else ""
    err = f"  error={r.error}" if r.error else ""
    print(f"  {r.agent:24} {r.scenario:26} {mark} tools={r.tool_calls:2} ce={r.ce_calls:2} "
          f"{r.seconds:5.1f}s{warn}{err}", file=sys.stderr, flush=True)


def _run(args: argparse.Namespace) -> None:
    ids = args.scenarios.split(",") if args.scenarios else list(BY_ID)
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        sys.exit(f"unknown scenarios: {unknown}. See `python -m evals list`.")
    scenarios = [BY_ID[i] for i in ids]

    client = judge_client = None
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if any(m != "baseline" and not m.startswith("claude-code:") for m in models) or args.judge:
        import anthropic
        client = anthropic.AsyncAnthropic(max_retries=6)
        judge_client = client if args.judge else None
    agents = [make_agent(m, effort=args.effort, client=client, max_tool_calls=args.max_tool_calls)
              for m in models]

    print(f"Running {len(agents)} agent(s) x {len(scenarios)} scenario(s) x {args.repeats} "
          f"repeat(s) over {args.transport}", file=sys.stderr)
    results = asyncio.run(run_suite(scenarios, agents, args.repeats, args.concurrency,
                                    args.transport, judge_client, args.judge, _progress))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    meta = {"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}", "scenarios": len(scenarios), "repeats": args.repeats,
            "effort": args.effort, "judge": args.judge}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "results": [r.to_dict() for r in results]}
    path = out / f"results-{stamp}.json"
    path.write_text(json.dumps(payload, indent=1))
    (out / "latest.json").write_text(json.dumps(payload, indent=1))
    md = markdown(results, meta)
    print(md)
    print(f"\nSaved {path}", file=sys.stderr)
    if args.update_readme:
        _readme(md)


def _report(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.results).read_text())
    md = markdown(payload["results"], payload.get("meta"))
    print(md)
    if args.update_readme:
        _readme(md)


def _readme(md: str) -> None:
    if update_readme(ROOT / "README.md", md):
        print("Updated README.md", file=sys.stderr)
    else:
        print("README.md has no eval-results markers; table not inserted", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m evals", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list scenarios").set_defaults(fn=_list)

    r = sub.add_parser("run", help="run agents against the scenarios")
    r.add_argument("--models", default="baseline",
                   help="comma-separated: baseline, Claude model IDs (API key), or "
                        "claude-code:<model> (headless Claude Code on your subscription)")
    r.add_argument("--scenarios", help="comma-separated scenario IDs (default: all)")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--transport", choices=["stdio", "inproc"], default="stdio")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--max-tool-calls", type=int, default=20)
    r.add_argument("--judge", metavar="MODEL", help="also grade with an LLM judge, e.g. claude-opus-5-5")
    r.add_argument("--out", default=str(ROOT / "evals" / "results"))
    r.add_argument("--update-readme", action="store_true")
    r.set_defaults(fn=_run)

    rep = sub.add_parser("report", help="render a saved results file")
    rep.add_argument("results")
    rep.add_argument("--update-readme", action="store_true")
    rep.set_defaults(fn=_report)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
