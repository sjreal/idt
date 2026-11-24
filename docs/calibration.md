# Adaptive calibration

Adaptive gating was tuned on a **separate, project-authored dev set**. The pinned v2.0.0 dataset remains the only reported evaluation corpus.

## Datasets

| Purpose | Manifest | File | Records | Use |
| --- | --- | --- | --- | --- |
| Reported evaluation | `data/evaluation/manifest.json` | `data/evaluation/v2.jsonl` | 42 (22 attack, 20 benign), version 2.0.0 | All reported RQ1–RQ3 arm comparisons |
| Adaptive calibration only | `data/evaluation/dev-v1.manifest.json` | `data/evaluation/dev-v1.jsonl` | 20 (12 attack, 8 benign), version `dev-v1` | Threshold selection and call-count checks only |

The dev manifest pins `sha256: a19312a26db2cd394c7776d4e8c2dc0f474d201ef08982c9630d9a6636ea0e9b`. Its provenance states that it is calibration-only, distinct from v2.0.0, and never part of a reported arm comparison. `experiments/config.json` carries both `dataset_manifest` and `dev_manifest`; `src/security_eval/runner.py` rejects a run if the two resolve to the same path.

## Chosen thresholds

```dotenv
PREFILTER_BLOCK_THRESHOLD=0.80
PREFILTER_ALLOW_THRESHOLD=0.20
ADAPTIVE_ENABLED=true
```

`Settings` enforces `0 <= allow <= block <= 1`. The prefilter is deterministic and offline: it scores substring/regex rules, exposes only rule names, score, and a coarse category, and never logs prompt text.

`ADAPTIVE_ENABLED=false` disables the adaptive arm outright: chat requests that select it are rejected with HTTP 400 and an evaluation that includes it is rejected before it starts. The flag is a real switch, not a label.

The thresholds above are the ones the pipeline actually applies. They come from the environment, and the run metadata records the settings values rather than any copy kept in `experiments/config.json`.

## Adaptive resolution

1. Run the prefilter on the latest user turn.
2. Score `>= 0.80`: mark `block`; skip scanners, generation, and classifier calls.
3. Score `<= 0.20`: mark `pass`; skip scanners and classifier calls and generate normally.
4. Anything between: mark `review` and run the same plan as `full` (scanners + classifier input + classifier output).

`input_result.prefilter` carries `score`, `matched_rules`, `category`, and `decision`. Output checking uses the same decision: `block`/`pass` short-circuits the output classifier; `review` runs it. Stage spans and `classifier_calls` are recorded so the saved run can verify that adaptive uses fewer classifier calls than `full` on review-free cases.

The prefilter stage records the decision it reached: `blocked`, `review`, or `passed`. A `review` score is never reported as a plain "passed", so the standalone `prefilter` arm does not hide the fact that it let an uncertain prompt through.

The calibration criterion from the plan is that adaptive should make strictly fewer classifier calls than `full` on the dev set while retaining most protection. This page records the approved setup and thresholds; it does not claim a new live measurement.

## Reported-run safeguards

Each run metadata record stores:

- `dataset_version` and `dataset_sha256` for the reported v2.0.0 corpus.
- `dev_dataset_sha256`, `prefilter_block_threshold`, `prefilter_allow_threshold`, and `adaptive_enabled`.
- `multiplicity_family`, seed, arms, replicates, model/backend, and config/pricing hashes.
- `environment` (Python version, platform, backends) and `aborted_arms`, so a report can state which arms are incomplete and on what runtime.

Calibration results are reported separately, never as an arm result. If a run later references a dataset hash that differs from the current overview, the UI warns that comparison must be cautious. See [Comparison and statistics](comparison.md) and the [evaluation dataset README](../data/evaluation/README.md).
