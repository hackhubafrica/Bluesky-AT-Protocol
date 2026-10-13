#!/usr/bin/env python3
"""Command-line posting tool for Bluesky.

Examples
--------
    # Plain text post
    python post.py --text "Hello from PyBsky!"

    # Text + image (up to 4 --image flags)
    python post.py --text "Check this out" --image photo.jpg --alt "Sunset over the bay"

    # Reply to a post
    python post.py --text "Great point!" --reply at://did:plc:abc/app.bsky.feed.post/3jz...

    # Multi-post thread (posts separated by blank lines in the file)
    python post.py --thread thread.txt

    # Validate credentials + build the post without publishing
    python post.py --text "test" --dry-run

    # Quote another post (at:// URI or bsky.app URL)
    python post.py --text "Great take!" --quote at://did:plc:abc/app.bsky.feed.post/3jz...

    # Post a video (mp4) with alt text
    python post.py --text "Watch this" --video clip.mp4 --video-alt "Waves at sunset"

    # Self-label the post as a bot (recommended by Bluesky for automation)
    python post.py --text "beep boop" --label-bot

    # Delete a post you own
    python post.py --delete at://did:plc:abc/app.bsky.feed.post/3jz...
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from atproto import Client, models

from bluesky_client import (
    build_link_facets,
    build_reply_ref,
    fetch_strong_ref,
    get_client,
    retry_transient,
    send_post_with_self_label,
    upload_images,
)

MAX_THREAD_POSTS = 30
# Bluesky posts are limited to 300 graphemes / 3000 bytes.
WARN_GRAPHEMES = 300
POST_DELAY_SECONDS = 2


def load_thread(path: str) -> list[str]:
    """Split a thread file into posts on blank lines."""
    raw = Path(path).read_text(encoding="utf-8")
    parts = [p.strip() for p in re.split(r"\n\s*\n", raw.strip()) if p.strip()]
    if not parts:
        sys.exit(f"Thread file is empty: {path}")
    if len(parts) > MAX_THREAD_POSTS:
        sys.exit(f"Thread too long: {len(parts)} posts (max {MAX_THREAD_POSTS}).")
    return parts


def publish(
    client: Client,
    text: str,
    embed,
    reply_to,
    dry_run: bool,
    label_bot: bool = False,
) -> "models.CreateRecordResponse | None":
    if len(text) > WARN_GRAPHEMES:
        print(f"Warning: post is {len(text)} characters (Bluesky limit is {WARN_GRAPHEMES}).")

    facets = build_link_facets(text)

    if dry_run:
        print(f"[dry-run] Would post ({len(text)} chars): {text[:80]!r}")
        print(f"[dry-run] facets={len(facets) if facets else 0} "
              f"embed={type(embed).__name__ if embed else 'none'} "
              f"reply={'yes' if reply_to else 'no'}"
              f"{' label=bot' if label_bot else ''}")
        return None

    if label_bot:
        ref = send_post_with_self_label(
            client, text, facets=facets, embed=embed, reply_to=reply_to, label="bot"
        )
    else:
        ref = retry_transient(
            lambda: client.send_post(text=text, facets=facets, embed=embed, reply_to=reply_to)
        )
    print(f"Posted! uri={ref.uri}")
    return ref


def main() -> None:
    parser = argparse.ArgumentParser(description="Post to Bluesky (AT Protocol).")
    parser.add_argument("--text", help="Post text (max 300 graphemes)")
    parser.add_argument("--image", action="append", default=[], metavar="PATH",
                        help="Image file to attach (repeatable, max 4)")
    parser.add_argument("--alt", default="", help="Alt text for the first image")
    parser.add_argument("--reply", metavar="AT_URI", help="AT URI of the post to reply to")
    parser.add_argument("--thread", metavar="FILE",
                        help="File with thread posts separated by blank lines")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log in and build the post, but do not publish")
    parser.add_argument("--quote", metavar="AT_URI",
                        help="AT URI or bsky.app URL of a post to quote")
    parser.add_argument("--video", metavar="PATH", help="Video file (mp4) to post instead of images")
    parser.add_argument("--video-alt", default="", help="Alt text for --video")
    parser.add_argument("--delete", metavar="AT_URI", help="Delete one of your posts by AT URI")
    parser.add_argument("--label-bot", action="store_true",
                        help="Self-label the post as 'bot' (recommended for automation)")
    args = parser.parse_args()

    # --- Validate arguments -------------------------------------------------
    if not args.text and not args.thread and not args.delete:
        parser.error("provide --text, --thread or --delete")
    if args.delete:
        if any([args.text, args.thread, args.reply, args.image, args.quote, args.video]):
            parser.error("--delete cannot be combined with other actions")
        client = get_client()
        if args.dry_run:
            print(f"[dry-run] Would delete {args.delete}")
            return
        ok = client.delete_post(args.delete)
        print(f"Deleted: {args.delete}" if ok else f"Delete FAILED for {args.delete}")
        return
    if args.text and args.thread:
        parser.error("--text and --thread are mutually exclusive")
    if args.thread and args.reply:
        parser.error("--thread posts are chained replies; --reply cannot be combined")
    if args.thread and (args.quote or args.video):
        parser.error("--thread cannot be combined with --quote/--video")
    if args.image and args.video:
        parser.error("--image and --video are mutually exclusive")
    if args.quote and (args.image or args.video):
        parser.error("--quote cannot be combined with --image/--video")
    if args.video and args.alt:
        parser.error("use --video-alt for videos, not --alt")

    client = get_client()  # logs in (validates the app password)

    # --- Thread -------------------------------------------------------------
    if args.thread:
        parts = load_thread(args.thread)
        if args.dry_run:
            print(f"[dry-run] Thread of {len(parts)} posts:")
            for i, part in enumerate(parts, 1):
                print(f"  {i}. {part[:70]!r}")
            return

        root_ref = None
        prev = None
        for i, part in enumerate(parts, 1):
            reply_to = None
            if prev is not None:
                if root_ref is None:
                    root_ref = models.ComAtprotoRepoStrongRef.Main(uri=prev.uri, cid=prev.cid)
                reply_to = models.AppBskyFeedPost.ReplyRef(
                    root=root_ref,
                    parent=models.ComAtprotoRepoStrongRef.Main(uri=prev.uri, cid=prev.cid),
                )
            ref = client.send_post(text=part, reply_to=reply_to)
            print(f"  [{i}/{len(parts)}] {ref.uri}")
            prev = ref
            if i < len(parts):
                time.sleep(POST_DELAY_SECONDS)
        print(f"Thread posted ({len(parts)} posts).")
        return

    # --- Single post --------------------------------------------------------
    reply_ref = None
    if args.reply:
        reply_ref = build_reply_ref(client, args.reply)

    embed = None
    if args.quote:
        quote_ref = fetch_strong_ref(client, args.quote)
        embed = models.AppBskyEmbedRecord.Main(record=quote_ref)

    if args.video:
        video_path = Path(args.video)
        if not video_path.is_file():
            sys.exit(f"Video not found: {args.video}")
        if args.dry_run:
            print(f"[dry-run] Would post video {args.video!r} "
                  f"({video_path.stat().st_size} bytes): {args.text[:60]!r}")
            return
        ref = retry_transient(
            lambda: client.send_video(
                text=args.text,
                video=video_path.read_bytes(),
                video_alt=args.video_alt or None,
                reply_to=reply_ref,
                facets=build_link_facets(args.text),
            )
        )
        print(f"Posted! uri={ref.uri}")
        return

    if args.image:
        if args.dry_run:
            for path in args.image:
                if not Path(path).is_file():
                    sys.exit(f"Image not found: {path}")
            print(f"[dry-run] Would upload {len(args.image)} image(s).")
        else:
            embed = models.AppBskyEmbedImages.Main(
                images=upload_images(client, args.image, args.alt)
            )

    ref = publish(client, args.text, embed, reply_ref, args.dry_run, label_bot=args.label_bot)
    if ref is not None:
        # Handy clickable link to the new post
        print(f"View: https://bsky.app/profile/{ref.uri.split('/')[2]}/post/{ref.uri.rsplit('/', 1)[-1]}")


if __name__ == "__main__":
    main()


