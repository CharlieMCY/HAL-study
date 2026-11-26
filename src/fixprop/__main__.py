"""Command line: python -m fixprop <command>. fetch gets the data, all recomputes everything, check verifies,
review opens the human review page, packets writes the reading material for Claude Code."""

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
    c = sub.add_parser("packets", help="write the reading material, instructions and batch prompts for Claude Code")
    c.add_argument("--out", required=True, help="directory to write to")
    c.add_argument("--all", action="store_true", help="also write the pairs that have already been read")
    c.add_argument("--verify", metavar="RESULT_FILE", help="write nothing; check a reader's result file instead")
    c.add_argument("--batch", help="with --verify: also check that every pair of this batch is in the result file")
    c.add_argument("--collect", metavar="MODEL_ID", help="write nothing to <out>; check every batch's result file and copy "
                                                         "the judgments into judgments/, recording MODEL_ID as the reader")
    args = p.parse_args(argv)
    if args.cmd == "fetch":
        from . import fetch

        return fetch.run_verify(args.repo) if args.verify else fetch.run(args.repo)
    if args.cmd == "review":
        from . import review

        return review.serve(args.reviewer, args.port)
    if args.cmd == "packets":
        from . import packets

        if args.verify:
            return 1 if packets.verify(Path(args.out).resolve(), Path(args.verify).resolve(), args.batch) else 0
        if args.collect:
            return 1 if packets.collect(Path(args.out).resolve(), args.collect) else 0
        packets.write(Path(args.out).resolve(), args.all)
        return 0
    from . import pipeline

    return pipeline.run_all() if args.cmd == "all" else pipeline.check(args.keep)


if __name__ == "__main__":
    sys.exit(main())
