"""Write a batch plan without starting sessions, containers, or evaluators."""

import argparse
from pathlib import Path

from common import save_json
from intervention.batch import plan
from intervention.protocol import PROTOCOL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["pilot", "formal"], required=True)
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
        default="outlines",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Do not overwrite an existing batch")
    samples = plan(args.kind, args.task)
    save_json(
        args.output,
        {
            "protocol_id": PROTOCOL,
            "kind": args.kind,
            "task": args.task,
            "batch_id": (args.task + "-" if args.kind == "pilot" else "formal-") + PROTOCOL,
            "expected_runs": len(samples),
            "preregistered": False,
            "plan": samples,
            "runs": [],
        },
    )


if __name__ == "__main__":
    main()
