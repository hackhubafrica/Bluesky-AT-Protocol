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
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from atproto import Client, models

from bluesky_client import build_link_facets, build_reply_ref, get_client, retry_transient

MAX_IMAGES = 4
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


def upload_images(client: Client, paths: list[str], alt: str) -> list[models.AppBskyEmbedImages.Image]:
    if len(paths) > MAX_IMAGES:
        sys.exit(f"Bluesky supports at most {MAX_IMAGES} images per post.")
    images = []
    for i, path in enumerate(paths):
        file = Path(path)
        if not file.is_file():
            sys.exit(f"Image not found: {path}")
        blob = client.upload_blob(file.read_bytes()).blob
        images.append(
            models.AppBskyEmbedImages.Image(alt=alt if i == 0 else "", image=blob)
        )
    return images


def publish(
    client: Client,
    text: str,
    embed: models.AppBskyEmbedImages.Main | None,
    reply_to: models.AppBskyFeedPost.ReplyRef | None,
    dry_run: bool,
) -> "models.CreateRecordResponse | None":
    if len(text) > WARN_GRAPHEMES:
        print(f"Warning: post is {len(text)} characters (Bluesky limit is {WARN_GRAPHEMES}).")

    facets = build_link_facets(text)

    if dry_run:
        print(f"[dry-run] Would post ({len(text)} chars): {text[:80]!r}")
        print(f"[dry-run] facets={len(facets) if facets else 0} "
              f"images={len(embed.images) if embed else 0} "
              f"reply={'yes' if reply_to else 'no'}")
        return None

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
    args = parser.parse_args()

    # --- Validate arguments -------------------------------------------------
    if not args.text and not args.thread:
        parser.error("provide --text or --thread")
    if args.text and args.thread:
        parser.error("--text and --thread are mutually exclusive")
    if args.thread and args.reply:
        parser.error("--thread posts are chained replies; --reply cannot be combined")

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

    ref = publish(client, args.text, embed, reply_ref, args.dry_run)
    if ref is not None:
        # Handy clickable link to the new post
        print(f"View: https://bsky.app/profile/{ref.uri.split('/')[2]}/post/{ref.uri.rsplit('/', 1)[-1]}")


if __name__ == "__main__":
    main()


