"""
fetch_messages.py

Pulls dialogs/messages from Telegram (via Pyrogram) and writes encrypted
data files for the static frontend to consume. Shared logic lives in
tg_common.py so process_queue.py's post-action sync uses the exact same
(bug-fixed) code path.

Fixes/features vs the previous version:
  - Archived chats are fetched (folder_id=1) and tagged `archived: true`.
  - Pinned chats are tagged `pinned: true` (dialog.is_pinned).
  - "Saved Messages" is always fetched explicitly, even with 0 unread.
  - FIXED: chats with unread_count == 0 used to be skipped forever after
    the first run, which is why messages went permanently stale once read
    elsewhere. Every dialog is refreshed every run now.
  - FIXED: get_chat_history() always asks Telegram for replies=0, so quoted
    replies rendered as an empty box. We batch-fetch real reply previews.
  - Service messages (pinned/title/photo/members/video-chat events) get
    readable text instead of an empty bubble.
  - Profile photos are downloaded, encrypted, and indexed.
  - Per-chat fetch-count overrides and "ghost mode" come from
    data/settings.enc (written by process_queue.py from the UI).
  - All browser-facing data (chats, meta, files index) is Fernet-encrypted
    with UI_ENCRYPTION_KEY — nothing plaintext is shipped to the public repo.
"""

import os
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from pyrogram import Client

import tg_common as tc

API_ID         = int(os.environ["TG_API_ID"])
API_HASH       = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_SESSION_STRING"].strip()
RAW_KEY        = os.environ["DATA_KEY"]
UI_KEY         = os.environ["UI_ENCRYPTION_KEY"]
FORCE          = os.environ.get("FORCE_FULL", "false").lower() == "true"
INIT_N         = int(os.environ.get("DEFAULT_FETCH_COUNT",  "20"))
UPD_N          = int(os.environ.get("DEFAULT_UPDATE_COUNT", "50"))

GH_DISPATCH_TOKEN    = os.environ["GH_DISPATCH_TOKEN"]
MASTER_PASSWORD_HASH = os.environ["MASTER_PASSWORD_HASH"]
GH_OWNER             = os.environ["GH_OWNER"]
GH_REPO              = os.environ["GH_REPO"]

DATA_DIR  = Path("data")
FILES_DIR = DATA_DIR / "files"
DATA_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(exist_ok=True)

META_FILE       = DATA_DIR / "meta.json.enc"
CHATS_ENC       = DATA_DIR / "chats.enc"
CHATS_BROWSER   = DATA_DIR / "chats.json.enc"
CONFIG_ENC      = DATA_DIR / "config.enc"
FILES_INDEX_ENC = DATA_DIR / "files_index.json.enc"
SETTINGS_ENC    = DATA_DIR / "settings.enc"

for _legacy in ("chats.json", "files_index.json", "meta.json"):
    p = DATA_DIR / _legacy
    if p.exists():
        p.unlink()

F    = tc.fernet_for(RAW_KEY)
F_ui = tc.fernet_for(UI_KEY)

SETTINGS        = tc.decrypt_or_default(SETTINGS_ENC, F, {})
GHOST_MODE      = bool(SETTINGS.get("ghost_mode", False))
FETCH_OVERRIDES = SETTINGS.get("fetch_overrides", {})


async def main():
    meta = tc.decrypt_or_default(META_FILE, F_ui, {})
    first_run = FORCE or not meta.get("initialized", False)
    print(f"first_run={first_run}, ghost_mode={GHOST_MODE}")

    app = Client(
        name="relay", api_id=API_ID, api_hash=API_HASH,
        session_string=SESSION_STRING, in_memory=True,
    )

    async with app:
        me = await app.get_me()
        my_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        meta["me"] = {"id": me.id, "name": my_name, "username": me.username}
        print(f"✓ Connected as {my_name}")

        chats_data = tc.decrypt_or_default(CHATS_ENC, F, {}) if not first_run else {}
        files_index = tc.decrypt_or_default(FILES_INDEX_ENC, F_ui, {})

        action_log = []
        seen_chat_ids = set()

        for folder_id, archived in ((0, False), (1, True)):
            async for dialog in tc.iter_dialogs(app, folder_id=folder_id):
                chat_id = dialog.chat.id
                key = str(chat_id)
                seen_chat_ids.add(chat_id)
                override = FETCH_OVERRIDES.get(key)
                fetch_n = override if override else (INIT_N if first_run else UPD_N)
                entry = await tc.sync_chat(
                    app, chat_id, chats_data=chats_data, files_index=files_index,
                    files_dir=FILES_DIR, f_ui=F_ui, fetch_n=fetch_n,
                    archived=archived, pinned=bool(dialog.is_pinned),
                    unread=dialog.unread_messages_count or 0,
                    ghost_mode=GHOST_MODE, me_id=me.id,
                )
                if entry:
                    tag = (' [archived]' if archived else '') + (' [pinned]' if dialog.is_pinned else '')
                    print(f"  ↳ {entry['name']}{tag}: {len(entry['messages'])} messages")
                    action_log.append({"chat": entry["name"], "fetched": len(entry['messages'])})

        if me.id not in seen_chat_ids:
            override = FETCH_OVERRIDES.get(str(me.id))
            fetch_n = override if override else (INIT_N if first_run else UPD_N)
            entry = await tc.sync_chat(
                app, me.id, chats_data=chats_data, files_index=files_index,
                files_dir=FILES_DIR, f_ui=F_ui, fetch_n=fetch_n,
                archived=False, pinned=False, unread=0,
                ghost_mode=GHOST_MODE, me_id=me.id,
            )
            if entry:
                print(f"  ↳ {entry['name']}: {len(entry['messages'])} messages")
                action_log.append({"chat": entry["name"], "fetched": len(entry['messages'])})

        CHATS_ENC.write_bytes(tc.encrypt(chats_data, F))
        CHATS_BROWSER.write_bytes(tc.encrypt(chats_data, F_ui))
        FILES_INDEX_ENC.write_bytes(tc.encrypt(files_index, F_ui))

        config_payload = {
            "gh_token":      GH_DISPATCH_TOKEN,
            "password_hash": MASTER_PASSWORD_HASH,
            "gh_owner":      GH_OWNER,
            "gh_repo":       GH_REPO,
        }
        CONFIG_ENC.write_bytes(tc.encrypt(config_payload, F_ui))

        meta["initialized"] = True
        meta["last_sync"]   = datetime.now(timezone.utc).isoformat()
        meta["sync_log"]    = action_log
        meta["total_chats"] = len(chats_data)
        meta["ghost_mode"]  = GHOST_MODE
        META_FILE.write_bytes(tc.encrypt(meta, F_ui))
        print(f"✓ Done — {len(chats_data)} chats written (encrypted)")


if __name__ == "__main__":
    asyncio.run(main())
