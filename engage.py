#!/usr/bin/env python3
"""Engage with Bluesky from the command line: likes, reposts, follows, mutes.

Examples:
    python engage.py like    at://did:plc:.../app.bsky.feed.post/3jz...
    python engage.py unlike  https://bsky.app/profile/handle/post/3jz...
    python engage.py repost  at://did:plc:.../app.bsky.feed.post/3jz...
    python engage.py unrepost at://did:plc:.../app.bsky.feed.post/3jz...
    python engage.py follow  @someone.bsky.social
    python engage.py unfollow @someone.bsky.social
    python engage.py mute    @someone.bsky.social
    python engage.py unmute  did:plc:abc...

    python engage.py like <uri> --dry-run   # show what would happen

Targets may be at:// URIs, bsky.app post/profile URLs, handles, or DIDs.
unlike/unrepost/unfollow accept EITHER the record URI returned when you acted
OR the original target (post URI/URL / handle) - your existing record is found
automatically via the repo.
"""

from __future__ import annotations

import argparse
import sys

from atproto import Client, exceptions

from bluesky_client import fetch_strong_ref, get_client, retry_transient

LIKE_COLLECTION = "app.bsky.feed.like"
REPOST_COLLECTION = "app.bsky.feed.repost"
FOLLOW_COLLECTION = "app.bsky.graph.follow"
POST_COLLECTION = "app.bsky.feed.post"
MAX_RECORD_PAGES = 5


def normalize_post_uri(client: Client, value: str) -> str:
    """Accept an at:// URI or a bsky.app post URL; return an at:// URI."""
    if value.startswith("at://"):
        return value
    parts = value.rstrip("/").split("/")
    if len(parts) >= 7 and parts[-2] == "post":
        rkey = parts[-1]
        actor = parts[-3]
        if not actor.startswith("did:"):
            try:
                actor = client.resolve_handle(actor).did
            except (exceptions.RequestErrorBase, exceptions.RequestException) as exc:
                sys.exit(f"Could not resolve handle '{actor}': {exc}")
        return f"at://{actor}/{POST_COLLECTION}/{rkey}"
    sys.exit(f"Not a post URI or bsky.app URL: {value}")


def normalize_did(client: Client, value: str) -> str:
    """Accept a DID, @handle or handle; return a DID."""
    if value.startswith("did:"):
        return value
    handle = value.removeprefix("@")
    try:
        return client.resolve_handle(handle).did
    except (exceptions.RequestErrorBase, exceptions.RequestException) as exc:
        sys.exit(f"Could not resolve handle '{handle}': {exc}")


def find_record_uri(client: Client, collection: str, subject: str) -> str | None:
    """Find your newest record in ``collection`` whose subject matches."""
    me = client.me.did
    cursor = None
    for _ in range(MAX_RECORD_PAGES):
        params: dict = {"repo": me, "collection": collection, "limit": 100, "reverse": True}
        if cursor:
            params["cursor"] = cursor
        resp = retry_transient(lambda: client.com.atproto.repo.list_records(params))
        for row in resp.records:
            subj = getattr(row.value, "subject", None)
            subj_uri = getattr(subj, "uri", None) or (subj if isinstance(subj, str) else None)
            if subj_uri == subject:
                return row.uri
        cursor = getattr(resp, "cursor", None)
        if not cursor:
            return None
    return None


def record_uri_for(value: str, collection: str) -> str | None:
    """Return value when it already is an at:// URI of the given collection."""
    if value.startswith("at://") and f"/{collection}/" in value:
        return value
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Like, repost, follow and mute on Bluesky.")
    parser.add_argument("action", choices=[
        "like", "unlike", "repost", "unrepost", "follow", "unfollow", "mute", "unmute",
    ])
    parser.add_argument("target", nargs="?",
                        help="Post URI/URL, handle, DID, or record URI (see module docstring)")
    parser.add_argument("--dry-run", action="store_true", help="Show the action without doing it")
    args = parser.parse_args()

    if not args.target:
        parser.error(f"'{args.action}' needs a target")
    if args.action in ("follow", "unfollow", "mute", "unmute") and "://" in args.target \
            and not args.target.startswith("at://"):
        parser.error(f"for {args.action} use a handle or DID, not a URL")

    client = get_client()

    if args.action in ("like", "repost"):
        post_uri = normalize_post_uri(client, args.target)
        if args.dry_run:
            print(f"[dry-run] Would {args.action} {post_uri}")
            return
        ref = fetch_strong_ref(client, post_uri)
        if args.action == "like":
            resp = retry_transient(lambda: client.like(ref.uri, ref.cid))
        else:
            resp = retry_transient(lambda: client.repost(ref.uri, ref.cid))
        print(f"{args.action.capitalize()} OK: {resp.uri}")

    elif args.action in ("unlike", "unrepost"):
        collection = LIKE_COLLECTION if args.action == "unlike" else REPOST_COLLECTION
        record_uri = record_uri_for(args.target, collection)
        if record_uri is None:
            post_uri = normalize_post_uri(client, args.target)
            record_uri = find_record_uri(client, collection, post_uri)
            if record_uri is None:
                sys.exit(f"No {collection.rsplit('.', 1)[-1]} record found for {post_uri}")
        if args.dry_run:
            print(f"[dry-run] Would {args.action} record {record_uri}")
            return
        remover = client.unlike if collection == LIKE_COLLECTION else client.unrepost
        ok = retry_transient(lambda: remover(record_uri))
        print(f"Un-{'like' if collection == LIKE_COLLECTION else 'repost'} OK: {record_uri}"
              if ok else "FAILED")

    elif args.action == "follow":
        subject = normalize_did(client, args.target)  # follow records need a DID
        if args.dry_run:
            print(f"[dry-run] Would follow {subject}")
            return
        resp = retry_transient(lambda: client.follow(subject))
        print(f"Follow OK: {resp.uri} (unfollow with this URI or the handle)")

    elif args.action == "unfollow":
        follow_uri = record_uri_for(args.target, FOLLOW_COLLECTION)
        if follow_uri is None:
            did = normalize_did(client, args.target)
            follow_uri = find_record_uri(client, FOLLOW_COLLECTION, did)
            if follow_uri is None:
                sys.exit(f"Not following {args.target}")
        if args.dry_run:
            print(f"[dry-run] Would unfollow record {follow_uri}")
            return
        ok = retry_transient(lambda: client.unfollow(follow_uri))
        print(f"Unfollow OK: {follow_uri}" if ok else "FAILED")

    elif args.action in ("mute", "unmute"):
        did = normalize_did(client, args.target)
        if args.dry_run:
            print(f"[dry-run] Would {args.action} {did}")
            return
        action = (client.app.bsky.graph.mute_actor if args.action == "mute"
                  else client.app.bsky.graph.unmute_actor)
        ok = retry_transient(lambda: action({"actor": did}))
        print(f"{args.action.capitalize()} OK: {did}" if ok else "FAILED")


if __name__ == "__main__":
    main()
