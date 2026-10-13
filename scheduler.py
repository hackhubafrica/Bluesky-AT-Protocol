#!/usr/bin/env python3
"""Queue-based scheduled posting for Bluesky.

Reads ``posts_queue.json`` every minute and publishes any item whose
``post_at`` timestamp is due.

Queue item format (UTC ISO-8601 timestamps; "text" or "image" required):

    [
      {
        "text": "Good morning! ☀️",
        "post_at": "2026-10-14T09:00:00+00:00"
      },
      {
        "text": "With a photo and a link",
        "post_at": "2026-10-14T13:30:00+00:00",
        "image": "photo.jpg",
        "alt": "Sunset over the bay"
      },
      {
        "text": "Replying to my earlier post",
        "post_at": "2026-10-14T15:00:00+00:00",
        "reply": "at://did:plc:xxx/app.bsky.feed.post/3jz...",
        "image": ["pic1.jpg", "pic2.jpg"]
      }
    ]

"image" may be one path or a list of up to 4; "alt" applies to the first.

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

from atproto import models

from bluesky_client import (
    build_link_facets,
    build_reply_ref,
    get_client,
    retry_transient,
    upload_images,
)

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
        if not item.get("post_at") or (not item.get("text") and not item.get("image")):
            print(f"Queue item #{index}: needs 'post_at' and either 'text' or 'image'.")
            continue

        due = parse_due(item["post_at"], index)
        if due > now:
            continue

        due_count += 1
        text = item.get("text", "")
        image = item.get("image")
        image_paths = [image] if isinstance(image, str) else list(image or [])
        alt = item.get("alt", "")
        reply_uri = item.get("reply")

        if dry_run:
            extra = []
            if image_paths:
                extra.append(f"images={len(image_paths)}")
            if reply_uri:
                extra.append("reply=yes")
            suffix = (" " + " ".join(extra)) if extra else ""
            print(f"[dry-run] Due ({item['post_at']}): {text[:70]!r}{suffix}")
            continue

        try:
            embed = None
            reply_ref = None
            if image_paths:
                embed = models.AppBskyEmbedImages.Main(
                    images=upload_images(client, image_paths, alt)
                )
            if reply_uri:
                reply_ref = build_reply_ref(client, reply_uri)
            ref = retry_transient(
                lambda: client.send_post(
                    text=text, facets=build_link_facets(text), embed=embed, reply_to=reply_ref
                )
            )
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
