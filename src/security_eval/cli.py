"""Command-line client for starting and monitoring a local evaluation run."""

import argparse
import os
import sys
import time

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url", default=os.getenv("EVALUATION_API_URL", "http://localhost:8000")
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-limit", type=int, default=None)
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=["off", "scanners", "full"],
        default=["off", "scanners", "full"],
    )
    parser.add_argument("--poll-seconds", type=float, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.api_url.rstrip("/")
    payload = {"arms": args.arms, "seed": args.seed}
    if args.sample_limit is not None:
        payload["sample_limit"] = args.sample_limit

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(f"{base_url}/api/evaluations/runs", json=payload)
            if response.status_code >= 400:
                print(f"Could not start evaluation: {response.text}", file=sys.stderr)
                return 1
            run = response.json()
            run_id = run["id"]
            total = int(run["sample_count"]) * len(run["arms"])
            print(f"Started evaluation {run_id}: {total} case/arm requests")

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
                    print(f"Results: {base_url}/api/evaluations/runs/{run_id}")
                    print(f"CSV: {base_url}/api/evaluations/runs/{run_id}/export.csv")
                    return 0 if status == "completed" else 1
                time.sleep(max(args.poll_seconds, 0.25))
    except httpx.HTTPError as exc:
        print(f"Evaluation API request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
