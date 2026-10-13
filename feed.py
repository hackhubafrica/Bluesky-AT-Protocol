#!/usr/bin/env python3
"""Read Bluesky from the command line: timeline, search, profiles, author feeds.

Examples:
    python feed.py timeline --limit 10
    python feed.py search "atprotocol" --limit 10
    python feed.py search "cats" --author someone.bsky.social
    python feed.py author @someone.bsky.social --limit 5
    python feed.py profile someone.bsky.social
"""

from __future__ import annotations

import argparse

from bluesky_client import get_client, retry_transient

MAX_LIMIT = 100


def clean(text: str, width: int = 120) -> str:
    """Collapse whitespace and truncate for terminal display."""
    return " ".join((text or "").split())[:width]


def print_post(post, reason: str | None = None) -> None:
    if reason:
        print(f"  [{reason}]")
    likes = getattr(post, "like_count", 0) or 0
    reposts = getattr(post, "repost_count", 0) or 0
    replies = getattr(post, "reply_count", 0) or 0
    print(f"@{post.author.handle}: {clean(post.record.text)}")
    print(f"    likes={likes} reposts={reposts} replies={replies}  {post.uri}")


def repost_reason(item) -> str | None:
    reason = getattr(item, "reason", None)
    if reason is None:
        return None
    by = getattr(reason, "by", None)
    return f"reposted by @{by.handle}" if by is not None and hasattr(by, "handle") else "reposted"


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Bluesky: timeline, search, profiles, feeds.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_timeline = sub.add_parser("timeline", help="Your home timeline")
    p_timeline.add_argument("--limit", type=int, default=20)

    p_search = sub.add_parser("search", help="Search posts")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--author", help="Restrict results to one handle")

    p_author = sub.add_parser("author", help="Latest posts from an account")
    p_author.add_argument("handle")
    p_author.add_argument("--limit", type=int, default=10)

    p_profile = sub.add_parser("profile", help="Account summary")
    p_profile.add_argument("handle")

    args = parser.parse_args()
    if hasattr(args, "limit"):
        args.limit = min(max(args.limit, 1), MAX_LIMIT)

    client = get_client()

    if args.command == "timeline":
        resp = retry_transient(lambda: client.get_timeline(limit=args.limit))
        for item in resp.feed:
            print_post(item.post, reason=repost_reason(item))
        print(f"({len(resp.feed)} items)")

    elif args.command == "search":
        params: dict = {"q": args.query, "limit": args.limit}
        if args.author:
            params["author"] = args.author.removeprefix("@")
        resp = retry_transient(lambda: client.app.bsky.feed.search_posts(params))
        for post in resp.posts:
            print_post(post)
        print(f"({len(resp.posts)} posts)")

    elif args.command == "author":
        handle = args.handle.removeprefix("@")
        resp = retry_transient(lambda: client.get_author_feed(handle, limit=args.limit))
        for item in resp.feed:
            print_post(item.post, reason=repost_reason(item))
        print(f"({len(resp.feed)} items)")

    elif args.command == "profile":
        handle = args.handle.removeprefix("@")
        prof = retry_transient(lambda: client.get_profile(handle))
        print(f"@{prof.handle}  {prof.display_name or ''}")
        if prof.description:
            print(clean(prof.description, 300))
        print(f"posts={prof.posts_count} followers={prof.followers_count} "
              f"following={prof.follows_count}")


if __name__ == "__main__":
    main()
