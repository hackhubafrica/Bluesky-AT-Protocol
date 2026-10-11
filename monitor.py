#!/usr/bin/env python3
"""Watch your notifications and auto-reply when your account is @mentioned.

Bluesky etiquette (official guidance): only interact (reply/like/repost) with
users who explicitly tag your bot — opt-in interaction only, or the account
may be flagged as spam. This tool follows that rule: it ONLY replies to
notifications whose reason is "mention" and that reference your own account.

Processed mention URIs are remembered in ``processed_mentions.json`` so a
mention is never answered twice.

Usage:
    python monitor.py            # poll every 60 seconds until Ctrl+C
    python monitor.py --once     # single poll, then exit (good for cron)
    python monitor.py --dry-run  # list mentions that would be replied to

Reply template supports {user} (mentioner's handle) and {text} (their post).
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from bluesky_client import build_reply_ref, get_client, retry_transient

STATE_PATH = Path(__file__).with_name("processed_mentions.json")
POLL_SECONDS = 60
DEFAULT_TEMPLATE = "Thanks for the mention, @{user}! 🤖 (automated reply via PyBsky)"


def load_processed() -> set[str]:
    if STATE_PATH.is_file():
        try:
            return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def save_processed(processed: set[str]) -> None:
    STATE_PATH.write_text(
        json.dumps(sorted(processed), indent=2) + "\n", encoding="utf-8"
    )


def get_notification_text(notification) -> str:
    """Best-effort extraction of the mentioning post's text."""
    record = getattr(notification, "record", None)
    return getattr(record, "text", "") or ""


def process_mentions(client, *, template: str, dry_run: bool) -> None:
    notifications = retry_transient(
        lambda: client.app.bsky.notification.list_notifications({"limit": 50})
    ).notifications
    processed = load_processed()
    my_did = client.me.did
    new_replies = 0

    for note in notifications:
        if note.reason != "mention" or note.uri in processed:
            continue
        if note.author.did == my_did:  # ignore our own posts
            continue

        mention_text = get_notification_text(note)
        reply_text = template.format(
            user=note.author.handle, text=mention_text[:100]
        )

        if dry_run:
            print(f"[dry-run] Would reply to @{note.author.handle}: {mention_text[:70]!r}")
            processed.add(note.uri)
            continue

        reply_ref = build_reply_ref(client, note.uri)
        ref = retry_transient(
            lambda: client.send_post(text=reply_text, reply_to=reply_ref)
        )
        processed.add(note.uri)
        new_replies += 1
        print(f"Replied to @{note.author.handle}: {ref.uri}")
        time.sleep(2)  # gentle pacing

    if not dry_run:
        save_processed(processed)
        if new_replies:
            client.app.bsky.notification.update_seen(
                {"seen_at": client.get_current_time_iso()}
            )
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
              f"checked, {new_replies} new repl{'y' if new_replies == 1 else 'ies'}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-reply to mentions of your account.")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--dry-run", action="store_true", help="List matches without replying")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE,
                        help="Reply text; {user} = handle, {text} = their post")
    args = parser.parse_args()

    client = get_client()
    print(f"Monitoring mentions of {client.me.handle} (only posts that tag this account).")

    process_mentions(client, template=args.template, dry_run=args.dry_run)
    if args.once:
        return

    print(f"Polling every {POLL_SECONDS}s. Ctrl+C to stop.")
    while True:
        try:
            time.sleep(POLL_SECONDS)
            process_mentions(client, template=args.template, dry_run=args.dry_run)
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break


if __name__ == "__main__":
    main()
