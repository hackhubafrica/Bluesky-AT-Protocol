"""Shared Bluesky (AT Protocol) client helpers for the PyBsky automation project.

Every tool in this project uses ``get_client()`` to obtain a logged-in
``atproto.Client`` (https://github.com/MarshalX/atproto).

Credentials are read from environment variables, optionally loaded from a
``.env`` file via python-dotenv.  NEVER commit your ``.env`` file.

Authentication uses an App Password (Settings -> Privacy and security ->
App Passwords on bsky.app), NOT your real account password.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from atproto import Client, Session, SessionEvent, models, exceptions
from dotenv import load_dotenv

DEFAULT_SERVICE = "https://bsky.social"

# Cached session tokens, re-used across runs (avoids createSession rate limits).
# Secret like .env: listed in .gitignore; delete the file to force a fresh login.
SESSION_PATH = Path(__file__).with_name(".bsky_session")

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

    @client.on_session_change
    def _persist_session(event: SessionEvent, session: Session) -> None:
        # Only the newest token pair is valid; persist it whenever it changes.
        if event in (SessionEvent.CREATE, SessionEvent.IMPORT, SessionEvent.REFRESH):
            try:
                SESSION_PATH.write_text(session.export(), encoding="utf-8")
            except OSError:
                pass  # never let session saving break a run

    # 1) Re-use a cached session when available.
    saved = None
    if SESSION_PATH.is_file():
        saved = SESSION_PATH.read_text(encoding="utf-8").strip() or None
    if saved:
        try:
            session = retry_transient(
                lambda: client.login(session_string=saved), attempts=3, delay=1.5
            )
            print(f"Session restored for {session.handle} (DID: {session.did})")
            return client
        except TRANSIENT_ERRORS:
            pass  # connection is down; password login below will surface it
        except (exceptions.RequestErrorBase, exceptions.RequestException, ValueError):
            SESSION_PATH.unlink(missing_ok=True)  # stale/revoked -> fresh login below

    # 2) Fresh login with the app password.
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


def fetch_strong_ref(client: Client, post_uri: str) -> models.ComAtprotoRepoStrongRef.Main:
    """Fetch a post and return its strong ref (uri + cid) — used for quote embeds."""
    try:
        resp = retry_transient(lambda: client.get_post_thread(uri=post_uri, depth=0))
    except (exceptions.RequestException, exceptions.RequestErrorBase) as exc:
        sys.exit(f"Could not fetch post {post_uri}: {type(exc).__name__}: {exc}")

    thread = resp.thread
    if not isinstance(thread, models.AppBskyFeedDefs.ThreadViewPost):
        sys.exit(f"Post unavailable (deleted or blocked): {post_uri}")
    return models.ComAtprotoRepoStrongRef.Main(uri=thread.post.uri, cid=thread.post.cid)


MAX_IMAGES = 4


def upload_images(
    client: Client, paths: list[str], alt: str = ""
) -> list[models.AppBskyEmbedImages.Image]:
    """Upload image files and return embed Image entries (max 4 per post)."""
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


def send_post_with_self_label(
    client: Client,
    text: str,
    *,
    facets=None,
    embed=None,
    reply_to=None,
    label: str = "bot",
) -> "models.CreateRecordResponse":
    """Create a post record carrying a self-label (e.g. 'bot') via createRecord.

    ``send_post()`` cannot set labels, so the record is built by hand. Bluesky
    recommends automated accounts self-label as ``bot``.
    """

    def raw(model) -> dict:
        # pydantic models serialize $type via the by_alias flag
        return json.loads(model.model_dump_json(by_alias=True, exclude_none=True))

    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": client.get_current_time_iso(),
        "labels": raw(
            models.ComAtprotoLabelDefs.SelfLabels(
                values=[models.ComAtprotoLabelDefs.SelfLabel(val=label)]
            )
        ),
    }
    if facets:
        record["facets"] = [raw(facet) for facet in facets]
    if embed is not None:
        record["embed"] = raw(embed)
    if reply_to is not None:
        record["reply"] = raw(reply_to)

    data = {
        "repo": client.me.did,
        "collection": "app.bsky.feed.post",
        "record": record,
    }
    return retry_transient(lambda: client.com.atproto.repo.create_record(data))
