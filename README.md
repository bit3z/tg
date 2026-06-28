# TG Relay

A personal Telegram relay served as a GitHub Page. Private, password-locked, Material You design.

## Architecture

```
GitHub Actions (cron / manual)
  └─ fetch_messages.py  ──→  data/*.enc (Fernet-encrypted)  ──→  GitHub Pages (index.html)
                                                         │
                                          User queues actions in UI
                                                         │
                                          Open queue → Sync/Dispatch → process_queue.yml
                                               └─ process_queue.py → Telegram
                                                  (then re-syncs the chats it touched)

Cleanup Downloaded Files workflow → cleanup_files.py → deletes data/files/* on demand
```

**Everything written to the repo is now Fernet-encrypted** (`UI_ENCRYPTION_KEY`, which must equal your
unlock password) — `chats.json.enc`, `meta.json.enc`, `files_index.json.enc`, and every file in
`data/files/`. Nothing plaintext is ever committed to the public repo. Filenames in `data/files/` are
opaque hashes — no chat/user/message IDs are visible from the filename alone.

## What's new in this version

- **Archived & pinned chats** are fetched and shown like in Telegram (a "Pinned" section up top,
  and an "Archived" filter chip that's hidden from the normal view by default).
- **Saved Messages** always appears, even if it has no unread messages.
- **Fixed:** messages going stale forever — the old code skipped any chat with 0 unread messages
  on every run after the first, so once you'd read something elsewhere it never updated again.
  Every chat is refreshed every run now.
- **Fixed:** quoted replies and pin/title-change/member-added notifications used to render as
  empty bubbles. Quoted replies now show the real quoted text; service messages show readable
  text ("📌 Pinned: …", "✏️ Changed the group name to …", etc).
- **Profile pictures** are downloaded, encrypted, and shown as real avatars throughout the UI.
- **Sending messages, downloading files, reacting, etc. now sync immediately** — `process_queue.py`
  re-fetches every chat an action touched right after running it, instead of waiting for the next
  scheduled fetch.
- **Join a chat / start a bot / message someone new** — the "New chat" modal (top bar) now has
  three modes.
- **Per-chat fetch count** — each chat's Info panel has a number box to set how many messages to
  fetch for that chat specifically.
- **Cleanup workflow** — a new "Clean up downloaded files" action (in the **⋮ more menu**) deletes
  everything in `data/files/`, with an option to keep profile pictures.
- **Ghost mode** — a toggle (also in the **⋮ more menu**) that stops downloading profile info/avatars
  and zeroes unread counts in the stored data. *Caveat:* Telegram's actual online/last-seen status is
  controlled by Telegram itself — no client-side trick fully guarantees invisibility.
- **Sync + Dispatch merged** — there's one queue/sync button now. Opening the queue and pressing the
  action button always syncs, dispatching any queued actions first if there are any.
- Downloaded Files and the new options above moved into the **⋮ more menu** in the top bar.

## Setup

### 1. Generate a Telegram session

This project uses **Pyrogram** (the actual code has always used Pyrogram, not Telethon —
the old README was out of date here). Generate a session string:

```bash
pip install pyrogram tgcrypto
python -c "
from pyrogram import Client
with Client('relay', api_id=YOUR_API_ID, api_hash='YOUR_API_HASH', in_memory=True) as app:
    print(app.export_session_string())
"
```

Get `API_ID` and `API_HASH` from https://my.telegram.org

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `TG_API_ID` | your Telegram API ID (number) |
| `TG_API_HASH` | your Telegram API hash |
| `TG_SESSION_STRING` | output of the script above |
| `DATA_ENCRYPTION_KEY` | any long random passphrase (python-pipeline key) |
| `UI_ENCRYPTION_KEY` | **must be exactly your unlock password** — the browser decrypts everything with this |
| `GH_DISPATCH_TOKEN` | a GitHub PAT with `repo`+`workflow` scope (same value you'll paste into the UI) |
| `MASTER_PASSWORD_HASH` | SHA-256 hex of your password — see step 3 |
| `GH_OWNER` | your GitHub username |
| `GH_REPO` | this repo's name |

### 3. Compute your password hash

```js
// In a browser console or Node:
const hash = async p => [...new Uint8Array(
  await crypto.subtle.digest('SHA-256', new TextEncoder().encode(p))
)].map(b=>b.toString(16).padStart(2,'0')).join('')

hash('your_password').then(console.log)
```

Put the result in the `MASTER_PASSWORD_HASH` secret. **`UI_ENCRYPTION_KEY` must be the plain
password itself** (not the hash) — it's the Fernet key the browser uses to decrypt everything,
so it has to match what you type into the lock screen.

### 4. Configure repo details in index.html

```js
const CFG = { dataBase: "./data", ghOwner:"", ghRepo:"", ghToken:"", pwHash:"" };
```

GitHub owner/repo/token are read from the encrypted `config.enc` written by the fetch workflow —
you'll be prompted to paste your GitHub token into the UI on first load.

### 5. Enable GitHub Pages

**Settings → Pages → Source: Deploy from a branch → `main` / `/ (root)`**

### 6. Run the first fetch manually

**Actions → Fetch Telegram Messages → Run workflow → Force full fetch: true**

---

## Data files (generated by Actions — all encrypted)

| File | Description | Key |
|---|---|---|
| `data/meta.json.enc` | Sync metadata, last run info, ghost mode state | UI key |
| `data/chats.json.enc` | Chat/message data served to the browser | UI key |
| `data/chats.enc` | Same data, kept for any local python tooling | data key |
| `data/files_index.json.enc` | Index of downloaded files (hashed filenames) | UI key |
| `data/files/*.enc` | Downloaded media/avatars, opaque filenames | UI key |
| `data/settings.enc` | Ghost mode + per-chat fetch-count overrides (python-only) | data key |
| `data/queue_results.json` | Results of last queue dispatch | plaintext (no PII) |

> The browser decrypts everything with the same key derived from your unlock password
> (`UI_ENCRYPTION_KEY`). The python-pipeline copies (`chats.enc`, `settings.enc`) use the separate
> `DATA_ENCRYPTION_KEY` and are never fetched by the browser.

---

## Queue action types

| Type | Required fields |
|---|---|
| `send_message` | `chat_id`, `text`, optionally `reply_to` (chat_id can be a username for a brand-new chat) |
| `start_bot` | `target` (bot username), optionally `payload` |
| `react` | `chat_id`, `msg_id`, `emoji` |
| `mark_read` | `chat_id` |
| `download_request` | `chat_id`, `msg_id`, `kind` (`file`/`profile_pic`/`all_profile_media`) |
| `pin_message` / `unpin_message` | `chat_id`, `msg_id` |
| `forward` | `from_chat_id`, `msg_id`, `to_chat_id` |
| `delete_message` | `chat_id`, `msg_id`, optionally `revoke` |
| `edit_message` | `chat_id`, `msg_id`, `text` |
| `bot_callback` | `chat_id`, `msg_id`, `callback_data` |
| `join_chat` | `target` (username / invite link / numeric ID) |
| `set_fetch_count` | `chat_id`, `count` |
| `set_ghost_mode` | `enabled` |

Every dispatch re-syncs whichever chats were touched, so results show up within about a minute
without waiting for the next scheduled fetch.

---

## Secrets summary

```
TG_API_ID               Telegram API ID
TG_API_HASH             Telegram API hash
TG_SESSION_STRING       Pyrogram session string
DATA_ENCRYPTION_KEY     passphrase for the python-pipeline copy
UI_ENCRYPTION_KEY       MUST equal your unlock password — browser decryption key
GH_DISPATCH_TOKEN       GitHub PAT (repo + workflow scopes) — also pasted into the UI at runtime
MASTER_PASSWORD_HASH    SHA-256 hex of your password
GH_OWNER / GH_REPO      your GitHub username / this repo's name
```

The GitHub token for workflow dispatch is **never committed to the repo** — paste it into the UI
at runtime (kept in memory only, cleared on refresh).
