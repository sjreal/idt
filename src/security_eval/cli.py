"""Command-line client for starting and monitoring a local evaluation run."""

import argparse
import os
import sys
import time

import httpx

ARMS = ["off", "prefilter", "scanners", "input", "output", "full", "adaptive"]
HARD_MAX_REQUESTS = int(os.getenv("EVALUATION_HARD_MAX_REQUESTS", "300"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url", default=os.getenv("EVALUATION_API_URL", "http://localhost:8000")
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-limit", type=int, default=None)
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=["off", "scanners", "full"])
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help=(
            "Explicit budget override. The server caps it at "
            f"EVALUATION_HARD_MAX_REQUESTS ({HARD_MAX_REQUESTS})."
        ),
    )
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()

    if args.max_requests is not None and args.max_requests > HARD_MAX_REQUESTS:
        parser.error(
            f"--max-requests {args.max_requests} exceeds the hard ceiling "
            f"{HARD_MAX_REQUESTS}; lower it or raise EVALUATION_HARD_MAX_REQUESTS explicitly."
        )
    return args


def main() -> int:
    args = parse_args()
    base_url = args.api_url.rstrip("/")
    payload = {
        "arms": args.arms,
        "seed": args.seed,
        "replicates": args.replicates,
    }
    if args.sample_limit is not None:
        payload["sample_limit"] = args.sample_limit
    if args.max_requests is not None:
        payload["max_requests"] = args.max_requests

    try:
        with httpx.Client(timeout=30) as client:
            summary = client.get(f"{base_url}/api/evaluations/summary")
            summary.raise_for_status()
            cases = int(summary.json()["total"])
            planned = cases if args.sample_limit is None else min(cases, args.sample_limit)
            planned *= len(args.arms) * args.replicates
            print(f"Planned generation requests: {planned}")
            print(f"Budget override: {args.max_requests or 'server default'}")

            response = client.post(f"{base_url}/api/evaluations/runs", json=payload)
            if response.status_code >= 400:
                print(f"Could not start evaluation: {response.text}", file=sys.stderr)
                return 1
            run = response.json()
            run_id = run["id"]
            total = int(run["planned_requests"])
            print(
                f"Started evaluation {run_id}: {total} case/arm requests "
                f"(cap {run['max_requests']}, ceiling {run['hard_max_requests']})"
            )

            while True:
                current = client.get(f"{base_url}/api/evaluations/runs/{run_id}")
                current.raise_for_status()
                state = current.json()
                status = state["status"]
                print(
                    f"\r{status}: {state['results_written']}/{total} results",
                    end="\n" if status not in {"queued", "running"} else "",
                    flush=True,
                )
                if status not in {"queued", "running"}:
                    if state.get("error"):
                        print(f"Run error: {state['error']}", file=sys.stderr)
                    if state.get("partial"):
                        print(
                            "WARNING: partial budget-aborted run; "
                            "excluded from headline comparisons.",
                            file=sys.stderr,
                        )
                    print(f"Results: {base_url}/api/evaluations/runs/{run_id}")
                    print(f"CSV: {base_url}/api/evaluations/runs/{run_id}/export.csv")
                    print(f"Report: {base_url}/api/evaluations/runs/{run_id}/report.html")
                    return 0 if status == "completed" else 1
                time.sleep(max(args.poll_seconds, 0.25))
    except httpx.HTTPError as exc:
        print(f"Evaluation API request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
