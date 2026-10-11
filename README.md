# PyBsky — Bluesky (AT Protocol) Automation

Beginner-friendly Python automation for Bluesky using the
[`atproto`](https://github.com/MarshalX/atproto) SDK with **App Password**
authentication — no OAuth flow, no API key approval, just handle + password.

## Features

| Tool | What it does |
|------|--------------|
| `post.py` | Post text, images (up to 4), replies, and multi-post threads from the CLI |
| `scheduler.py` | Publishes queued posts at scheduled times (`posts_queue.json`) |
| `monitor.py` | Polls notifications and auto-replies when your account is @mentioned |
| `bluesky_client.py` | Shared login + helpers (link facets, reply refs) |

## Step 1 — Get your App Password

1. Log in to **[bsky.app](https://bsky.app)** (web) or open the mobile app.
2. **Web:** click your avatar (bottom-left) → **Settings** → **Privacy and security** → **App Passwords**.
   **Mobile:** Settings (gear) → **Privacy and security** → **App Passwords**.
3. Click **Add app password**, name it (e.g. `pybsky-automation`), and copy the
   generated password — it looks like `xxxx-xxxx-xxxx-xxxx` and is shown only once.
4. Put it in `.env` (already git-ignored — never commit it).

App passwords can do everything your account can **except** destructive actions
(deleting/migrating the account) — that's why they're the recommended way to
authenticate bots. Tip: run automation from a dedicated bot account.

## Step 2 — Install

```bash
cd ~/Documents/Bluesky-AT-Protocol
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your handle + app password
```

## Step 3 — Post something

```bash
# Sanity check (logs in, builds the post, does NOT publish)
python post.py --text "hello" --dry-run

# Publish for real
python post.py --text "Hello from PyBsky! 🚀"

# With an image + alt text
python post.py --text "Sunset" --image photo.jpg --alt "Sunset over the bay"

# Reply to a post (pass its AT URI)
python post.py --text "Great point!" --reply at://did:plc:xxx/app.bsky.feed.post/3jz...

# Thread: separate posts with a blank line in a file
python post.py --thread thread.txt
```

## Step 4 — Schedule posts

Add items to `posts_queue.json` (UTC, ISO-8601):

```json
[
  { "text": "Good morning! ☀️", "post_at": "2026-10-12T09:00:00+00:00" },
  { "text": "Afternoon check-in", "post_at": "2026-10-12T13:30:00+00:00" }
]
```

```bash
python scheduler.py            # run continuously (checks every minute)
python scheduler.py --dry-run  # show what would post
python scheduler.py --once     # single pass — usable from system cron
```

Posted items are marked `"status": "posted"` with their `uri`; failures get
`"status": "failed"` plus the error, so nothing is ever posted twice.

## Step 5 — Auto-reply to mentions (optional)

```bash
python monitor.py --dry-run           # preview matches
python monitor.py                     # poll every 60s
python monitor.py --template "Hi {user}! Thanks for tagging me 🙌"
```

It replies **only** to posts that @mention your account (official Bluesky
guidance: bots should only interact when tagged, to avoid spam labels).
Replies already handled are stored in `processed_mentions.json`.

## Notes & limits

- **Rate limits (official):** writes cost *points* — **5,000/hour and 35,000/day**
  per account (CREATE = 3 pts, UPDATE = 2, DELETE = 1) ≈ up to ~1,666 creates/hour.
  General API calls: 3,000 per 5 min per IP. Logins (`createSession`): 30 per 5 min.
  See [bsky.network/docs/rate-limits](https://bsky.network/docs/rate-limits).
- The scheduler waits ~2s between posts in a batch; keep automation human-scaled
  to avoid spam labels (Bluesky's Community Guidelines apply to bots too).
- **Automatic retries:** every network call retries up to 3–5 times with backoff
  on transient errors (`NetworkError`, `InvokeTimeoutError`), so flaky
  connections won't lose a scheduled post.
- **Post limit:** 300 graphemes / 3000 bytes, max 4 images, threads max 30 posts.
- **Bare URLs** in your text are made clickable automatically via link facets.
- For production, prefer a system `cron`/`systemd` timer calling
  `python scheduler.py --once` over leaving a process running forever.

## Troubleshooting

| Error | Fix |
|-------|-----|
| `Authentication failed` | Check `BSKY_HANDLE` / `BSKY_APP_PASSWORD` in `.env`; regenerate the app password |
| `Missing credentials` | Copy `.env.example` → `.env` and fill it in |
| `Parent post is unavailable` | The `--reply` URI is wrong, deleted, or from a blocked account |
| Handle not found | Use a full handle like `pybsky.bsky.social` (a bare name gets `.bsky.social` appended automatically) |
