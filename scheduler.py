#!/usr/bin/env python3
"""Queue-based scheduled posting for Bluesky.

Reads ``posts_queue.json`` every minute and publishes any item whose
``post_at`` timestamp is due.

Queue item format (UTC ISO-8601 timestamps):

    [
      {
        "text": "Good morning! ☀️",
        "post_at": "2026-10-12T09:00:00+00:00"
      },
      {
        "text": "Second post",
        "post_at": "2026-10-12T13:30:00+00:00"
      }
    ]

Usage:
    python scheduler.py          # run continuously (checks every minute)
    python scheduler.py --once   # single check pass, then exit (good for cron)
    python scheduler.py --dry-run  # show due posts without publishing

Items are updated in place: "status" becomes "posted" (with uri/posted_at)
or "failed" (with error). Pending items keep status "pending".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule

from bluesky_client import get_client, retry_transient

QUEUE_PATH = Path(__file__).with_name("posts_queue.json")
POST_DELAY_SECONDS = 2  # small pause between posts in a batch (rate-limit friendly)


def load_queue() -> list[dict]:
    if not QUEUE_PATH.is_file():
        sys.exit(f"Queue file not found: {QUEUE_PATH}")
    try:
        queue = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"Invalid JSON in {QUEUE_PATH.name}: {exc}")
    if not isinstance(queue, list):
        sys.exit(f"{QUEUE_PATH.name} must contain a JSON array of post objects.")
    return queue


def save_queue(queue: list[dict]) -> None:
    QUEUE_PATH.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_due(value: str, index: int) -> datetime:
    try:
        due = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(
            f"Queue item #{index}: invalid post_at '{value}' "
            "(use ISO-8601, e.g. 2026-10-12T09:00:00+00:00)"
        )
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)  # assume UTC when no offset given
    return due


def check_queue(client, *, dry_run: bool) -> None:
    """Publish every pending item whose post_at time has passed."""
    queue = load_queue()
    now = datetime.now(timezone.utc)
    changed = False
    due_count = 0

    for index, item in enumerate(queue):
        if item.get("status", "pending") != "pending":
            continue
        if not item.get("text") or not item.get("post_at"):
            print(f"Queue item #{index}: missing 'text' or 'post_at', skipping.")
            continue

        due = parse_due(item["post_at"], index)
        if due > now:
            continue

        due_count += 1
        if dry_run:
            print(f"[dry-run] Due ({item['post_at']}): {item['text'][:70]!r}")
            continue

        try:
            ref = retry_transient(lambda: client.send_post(text=item["text"]))
        except Exception as exc:  # keep the queue alive on transient errors
            item["status"] = "failed"
            item["error"] = str(exc)
            print(f"Queue item #{index} FAILED: {exc}")
        else:
            item["status"] = "posted"
            item["uri"] = ref.uri
            item["posted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"Queue item #{index} posted: {ref.uri}")
        changed = True
        time.sleep(POST_DELAY_SECONDS)

    if not due_count:
        print(f"[{now.strftime('%H:%M:%S')}] nothing due.")
    if changed:
        save_queue(queue)


def summarize() -> None:
    """Print pending items at startup so you know what is scheduled."""
    queue = load_queue()
    pending = [i for i in queue if i.get("status", "pending") == "pending"]
    if not pending:
        print(f"{QUEUE_PATH.name}: no pending posts.")
        return
    print(f"{QUEUE_PATH.name}: {len(pending)} pending post(s):")
    for item in pending:
        print(f"  - {item['post_at']}  {item['text'][:60]!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scheduled posting from posts_queue.json.")
    parser.add_argument("--once", action="store_true", help="Run a single check pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show due posts without publishing")
    args = parser.parse_args()

    client = get_client()  # logs in once; session is reused for the whole run
    summarize()

    check_queue(client, dry_run=args.dry_run)
    if args.once:
        return

    schedule.every(1).minutes.do(check_queue, client=client, dry_run=args.dry_run)
    print("Scheduler running (checks every minute). Ctrl+C to stop.")
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            print("\nScheduler stopped.")
            break


if __name__ == "__main__":
    main()
