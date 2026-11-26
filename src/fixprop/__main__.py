"""Command line: python -m fixprop <command>. fetch gets the data, all recomputes everything, check verifies,
read has a model read the pairs through the API, review opens the human review page."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m fixprop",
        description="Find bug fixes in embedded drivers that were not carried over to copies of the same code.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")
    c = sub.add_parser("fetch", help="fetch the 40 upstream repositories into data/repos/ (network, about 750 MB; "
                                     "repositories already present are left alone)")
    c.add_argument("--repo", help="fetch only this repository, e.g. apache/nuttx; or 'rust' / 'rtos' for one group")
    c.add_argument("--verify", action="store_true", help="only check that the local data is the data the results were "
                                                         "computed from (offline)")
    sub.add_parser("all", help="recompute all results and replace benchmark/ (offline, 1-2 minutes)")
    c = sub.add_parser("check", help="verify the data, verify benchmark/ is unmodified, recompute and compare byte for byte")
    c.add_argument("--keep", action="store_true", help="keep the recomputed copy")
    c = sub.add_parser("review", help="open the local human review page; verdicts go to judgments/review/<name>.jsonl")
    c.add_argument("--reviewer", required=True, help="reviewer name: letters, digits, underscore and hyphen only")
    c.add_argument("--port", type=int, default=8765)
    c = sub.add_parser("read", help="have a model read the pairs through the OpenRouter API, one conversation per pair, "
                                    "keeping every request and response (the key comes from OPENROUTER_API_KEY or .env)")
    c.add_argument("--out", required=True, help="directory for transcripts and judgments, e.g. judgments")
    c.add_argument("--model", help="model to read with, as OpenRouter names it, e.g. anthropic/claude-opus-5.5")
    c.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max", "off"],
                   help="reasoning effort sent with every request; off sends none (default: medium)")
    c.add_argument("--max-tokens", type=int, default=32000, help="output limit of one response (default: 32000)")
    c.add_argument("--provider", default="anthropic", help="the only provider allowed to serve the requests; "
                                                           "'any' lets OpenRouter choose (default: anthropic)")
    c.add_argument("--ids", nargs="+", metavar="ID", help="read only these pairs")
    c.add_argument("--codebase", help="read only this codebase")
    c.add_argument("--kind", choices=["candidate", "diverged"], help="read only candidates, or only diverged pairs")
    c.add_argument("--limit", type=int, help="read at most this many pairs in this run")
    c.add_argument("--jobs", type=int, default=4, help="conversations running at the same time (default: 4)")
    c.add_argument("--max-cost", type=float, metavar="USD", help="start no new pair once this run has cost this much")
    c.add_argument("--redo", action="store_true", help="read again the pairs that already have an accepted transcript")
    c.add_argument("--collect", action="store_true", help="read nothing; build the judgment files from the transcripts")
    c.add_argument("--partial", action="store_true", help="with --collect: accept that some pairs have no transcript")
    c.add_argument("--request", metavar="ID", help="send nothing; print the first request of one pair (needs --model)")
    c.add_argument("--show", metavar="ID", help="read nothing; print the transcript of one pair")
    c.add_argument("--full", action="store_true", help="with --show: print long texts in full")
    c.add_argument("--usage", action="store_true", help="read nothing; print calls, tokens and cost per codebase")
    args = p.parse_args(argv)
    if args.cmd == "fetch":
        from . import fetch

        return fetch.run_verify(args.repo) if args.verify else fetch.run(args.repo)
    if args.cmd == "review":
        from . import review

        return review.serve(args.reviewer, args.port)
    if args.cmd == "read":
        from . import reader

        out = Path(args.out).resolve()
        if args.collect:
            return 1 if reader.collect(out, args.partial) else 0
        if args.show:
            return reader.show(out, args.show, args.full)
        if args.usage:
            return reader.usage(out)
        if not args.model:
            p.error("read needs --model (or one of --collect, --show, --usage)")
        effort, provider = None if args.effort == "off" else args.effort, None if args.provider == "any" else args.provider
        if args.request:
            return reader.first_request(args.request, args.model, effort, args.max_tokens, provider)
        return reader.run(out, args.model, effort=effort, max_tokens=args.max_tokens, provider=provider,
                          ids=args.ids, codebase=args.codebase, kind=args.kind, limit=args.limit, jobs=args.jobs,
                          max_cost=args.max_cost, redo=args.redo)
    from . import pipeline

    return pipeline.run_all() if args.cmd == "all" else pipeline.check(args.keep)


if __name__ == "__main__":
    sys.exit(main())
