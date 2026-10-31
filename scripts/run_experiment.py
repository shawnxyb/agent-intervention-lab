"""Run one isolated experiment and seal P0 only without hidden evaluation."""

import argparse
import asyncio
import json

from common import ROOT
from intervention.controller import Run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        choices=[
            "outlines",
            "dirty_equals",
            "click",
            "dspy_8394",
            "dspy_8587",
            "click_2068",
            "datasets_3997",
            "dspy_8563",
            "chi_26",
        ],
        required=True,
    )
    parser.add_argument(
        "--policy", choices=["single", "static", "periodic", "adaptive"], required=True
    )
    parser.add_argument("--config", default=str(ROOT / "configs/pilot.json"))
    parser.add_argument("--kind", choices=["pilot", "formal"], default="pilot")
    parser.add_argument("--repeat", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--batch-id")
    args = parser.parse_args()
    config = json.loads(__import__("pathlib").Path(args.config).read_text())
    run = Run(args.task, args.policy, config, args.kind, args.repeat, args.batch_id)
    print(f"Run: {run.output}", flush=True)
    output = asyncio.run(run.execute())
    print(f"Candidates sealed: {output}", flush=True)


if __name__ == "__main__":
    main()
