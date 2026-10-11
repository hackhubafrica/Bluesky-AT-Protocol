"""Shared Bluesky (AT Protocol) client helpers for the PyBsky automation project.

Every tool in this project uses ``get_client()`` to obtain a logged-in
``atproto.Client`` (https://github.com/MarshalX/atproto).

Credentials are read from environment variables, optionally loaded from a
``.env`` file via python-dotenv.  NEVER commit your ``.env`` file.

Authentication uses an App Password (Settings -> Privacy and security ->
App Passwords on bsky.app), NOT your real account password.
"""

from __future__ import annotations

import os
import re
import sys
import time

from atproto import Client, models, exceptions
from dotenv import load_dotenv

DEFAULT_SERVICE = "https://bsky.social"

# Bare URLs become clickable via a "link" facet in the post record.
URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Transport-level hiccups worth retrying (seen on flaky connections).
TRANSIENT_ERRORS = (exceptions.NetworkError, exceptions.InvokeTimeoutError)


def retry_transient(fn, *, attempts: int = 3, delay: float = 2.0):
    """Call ``fn()``; retry with backoff when the network hiccups.

    Only NetworkError and InvokeTimeoutError are retried — real API errors
    (bad password, rate limit, invalid record) are raised immediately.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TRANSIENT_ERRORS:
            if attempt == attempts:
                raise
            time.sleep(delay * attempt)


def load_credentials() -> tuple[str, str, str]:
    """Return ``(service, handle, app_password)`` from env / .env file."""
    load_dotenv()
    service = (os.getenv("BSKY_SERVICE") or DEFAULT_SERVICE).rstrip("/")
    handle = (os.getenv("BSKY_HANDLE") or "").strip()
    password = (os.getenv("BSKY_APP_PASSWORD") or "").strip()

    if not handle or not password:
        sys.exit(
            "Missing credentials.\n"
            "Copy .env.example to .env and set BSKY_HANDLE and BSKY_APP_PASSWORD."
        )

    # Convenience: 'PyBsky' -> 'pybsky.bsky.social' (handles must be domains)
    if "." not in handle:
        handle = f"{handle}.bsky.social"

    return service, handle, password


def get_client(*, login: bool = True) -> Client:
    """Create (and optionally log in) an ``atproto.Client``.

    Exits the program with a readable error on bad credentials or network
    problems instead of printing a stack trace.
    """
    service, handle, password = load_credentials()
    client = Client(base_url=service)

    if not login:
        return client

    try:
        session = retry_transient(
            lambda: client.login(handle, password), attempts=5, delay=1.5
        )
    except TRANSIENT_ERRORS as exc:
        sys.exit(
            f"Network problem reaching {service} while logging in "
            f"({type(exc).__name__}). Check your connection and retry."
        )
    except exceptions.RequestErrorBase as exc:
        # 400/401-style API errors: bad handle, wrong password, not an app password
        sys.exit(
            f"Login rejected for '{handle}': {exc}\n"
            "Check BSKY_HANDLE and BSKY_APP_PASSWORD in .env\n"
            "(app passwords look like: xxxx-xxxx-xxxx-xxxx)."
        )
    except exceptions.RequestException as exc:
        sys.exit(f"Network error while logging in to {service}: {exc}")

    print(f"Logged in as {session.handle} (DID: {session.did})")
    return client


def build_link_facets(text: str) -> list[models.AppBskyRichtextFacet.Main] | None:
    """Create clickable-link facets for bare URLs inside ``text``.

    Bluesky stores link ranges as UTF-8 *byte* offsets, so every position is
    computed on the encoded bytes, not on Python string indices.
    """
    facets: list[models.AppBskyRichtextFacet.Main] = []
    for match in URL_RE.finditer(text):
        uri = match.group(0).rstrip(".,;:!?)]}")
        if not uri:
            continue
        byte_start = len(text[: match.start()].encode("utf-8"))
        byte_end = len(text[: match.start()].encode("utf-8")) + len(uri.encode("utf-8"))
        facets.append(
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byteStart=byte_start, byteEnd=byte_end
                ),
                features=[models.AppBskyRichtextFacet.Link(uri=uri)],
            )
        )
    return facets or None


def build_reply_ref(client: Client, parent_uri: str) -> models.AppBskyFeedPost.ReplyRef:
    """Build a ReplyRef (root + parent strong refs) for a post we reply to.

    Walks up the thread so replies in a thread stay attached to its root.
    """
    try:
        resp = retry_transient(
            lambda: client.get_post_thread(uri=parent_uri, depth=0)
        )
    except (exceptions.RequestException, exceptions.RequestErrorBase) as exc:
        sys.exit(f"Could not fetch parent post {parent_uri}: {type(exc).__name__}: {exc}")

    thread = resp.thread
    if not isinstance(thread, models.AppBskyFeedDefs.ThreadViewPost):
        sys.exit(f"Parent post is unavailable (deleted or blocked): {parent_uri}")

    parent = thread.post
    parent_ref = models.ComAtprotoRepoStrongRef.Main(uri=parent.uri, cid=parent.cid)

    record = parent.record
    reply = getattr(record, "reply", None)
    if reply is not None:
        root = models.ComAtprotoRepoStrongRef.Main(uri=reply.root.uri, cid=reply.root.cid)
    else:
        root = parent_ref

    return models.AppBskyFeedPost.ReplyRef(root=root, parent=parent_ref)
