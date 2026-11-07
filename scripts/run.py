#!/usr/bin/env python3
"""Command-line pipeline for the control hour.

  python3 scripts/run.py fetch DEV              # download a dataset from the organizers' API
  python3 scripts/run.py evaluate DEV           # evaluate every conversation of the group
  python3 scripts/run.py export DEV             # write results/dev/results.json
  python3 scripts/run.py all PROD --folder prod # fetch + evaluate + export in one go

Options: --force (re-evaluate already evaluated), --checklist chat|call,
         --provider anthropic|ollama|hybrid|mock, --workers 4, --folder dev|prod
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cqa import batch, config  # noqa: E402

config.load_env()


def progress(job):
    line = "  оценено %d/%d, ошибок %d, стоимость $%.3f" % (
        job["done"] + job["failed"], job["total"], job["failed"], job["cost_usd"])
    if sys.stdout.isatty():
        print("\r" + line, end="", flush=True)
    else:
        print(line, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["fetch", "evaluate", "export", "all"])
    ap.add_argument("group_id")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--checklist")
    ap.add_argument("--provider")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--folder", choices=["dev", "prod"])
    a = ap.parse_args()

    if a.command in ("fetch", "all"):
        r = batch.fetch_dataset(a.group_id)
        print("Скачано: %s, %d диалогов" % (r["group_id"], r["count"]))
    if a.command in ("evaluate", "all"):
        t = time.time()
        job = batch.run_batch(a.group_id, checklist_id=a.checklist, force=a.force, workers=a.workers,
                              provider=a.provider, on_progress=progress)
        print("\nГотово за %.0f с: %d оценено, %d ошибок, пропущено (уже оценены) %d" % (
            time.time() - t, job["done"], job["failed"], job["skipped"]))
        for e in job["errors"]:
            print("  !", e["conversation_id"], e["error"])
    if a.command in ("export", "all"):
        _, meta = batch.export_results(a.group_id, folder=a.folder)
        print("Записано %s: %d ok, %d error из %d диалогов" % (
            meta["path"], meta["ok_records"], meta["error_records"], meta["total_records"]))
        for pr in meta["problems"]:
            print("  ! ", pr)


if __name__ == "__main__":
    main()
