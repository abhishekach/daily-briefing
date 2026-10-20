"""Delivery to your phone. Every channel set up in .env (and switched on in settings.yaml) gets the brief; all are free.

  Telegram   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   (`briefing telegram-link` fills in the chat id)
  ntfy       NTFY_TOPIC (+ NTFY_SERVER for your own server)   free app; a long random topic name works as a password
  WhatsApp   WHATSAPP_PHONE + CALLMEBOT_APIKEY        (free CallMeBot gateway, personal use, one-way, short messages)

Best effort: a failed delivery never fails the brief; the brief is always saved as a web page and printed.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx

API = "https://api.telegram.org/bot{token}/{method}"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def chunks(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


def telegram(text: str, client: httpx.Client | None = None) -> bool | None:
    token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return None
    c = client or httpx.Client(timeout=20)
    ok = True
    for part in chunks(text, 3900):
        try:
            r = c.post(API.format(token=token, method="sendMessage"), json={"chat_id": chat, "text": part, "disable_web_page_preview": True})
            ok = ok and r.status_code == 200
        except httpx.HTTPError as e:
            print(f"telegram: {e}", file=sys.stderr)
            ok = False
    return ok


def ntfy(title: str, text: str, client: httpx.Client | None = None) -> bool | None:
    topic = _env("NTFY_TOPIC")
    if not topic:
        return None
    server = (_env("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    c = client or httpx.Client(timeout=20)
    ok = True
    parts = chunks(text, 3500)
    for i, part in enumerate(parts):
        head = title if len(parts) == 1 else f"{title} ({i + 1}/{len(parts)})"
        try:
            r = c.post(f"{server}/{topic}", content=part.encode("utf-8"), headers={"Title": head.encode("utf-8").decode("latin-1", "ignore")})
            ok = ok and r.status_code == 200
        except httpx.HTTPError as e:
            print(f"ntfy: {e}", file=sys.stderr)
            ok = False
    return ok


def whatsapp(text: str, client: httpx.Client | None = None) -> bool | None:
    phone, key = _env("WHATSAPP_PHONE"), _env("CALLMEBOT_APIKEY")
    if "apikey=" in key:
        key = key.split("apikey=")[1].split("&")[0]
    if not (phone and key):
        return None
    c = client or httpx.Client(timeout=30)
    ok = True
    for i, part in enumerate(chunks(text, 900)):
        if i:
            time.sleep(5)   # the free gateway refuses messages in quick succession
        try:
            r = c.get("https://api.callmebot.com/whatsapp.php", params={"phone": phone, "text": part, "apikey": key})
            good = r.status_code == 200 and "error" not in r.text.lower()[:300]
            if not good:
                print(f"whatsapp: HTTP {r.status_code}: {re.sub(r'<[^>]+>', ' ', r.text)[:200].strip()}", file=sys.stderr)
            ok = ok and good
        except httpx.HTTPError as e:
            print(f"whatsapp: {e}", file=sys.stderr)
            ok = False
    return ok


def deliver(title: str, text: str, short: str, delivery) -> dict[str, bool]:
    """Sends to every configured, switched-on channel. Returns {channel: delivered} for the channels that were tried."""
    res = {}
    for name, on, fn in (("telegram", delivery.telegram, lambda: telegram(text)), ("ntfy", delivery.ntfy, lambda: ntfy(title, text)),
                         ("whatsapp", delivery.whatsapp, lambda: whatsapp(short))):
        if on:
            got = fn()
            if got is not None:
                res[name] = got
    return res


def telegram_link(env_file: Path, wait_s: int = 180) -> int:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Add TELEGRAM_BOT_TOKEN=... to .env first (from @BotFather in Telegram), then run this again.")
        return 1
    c = httpx.Client(timeout=40)
    me = c.get(API.format(token=token, method="getMe")).json()
    if not me.get("ok"):
        print(f"Telegram rejected the token: {me.get('description')}")
        return 1
    print(f"Open Telegram and send any message to @{me['result']['username']} (waiting up to {wait_s // 60} minutes) ...", flush=True)
    offset, t0 = None, time.time()
    while time.time() - t0 < wait_s:
        upd = c.get(API.format(token=token, method="getUpdates"), params={"timeout": 25, **({"offset": offset} if offset else {})}).json().get("result", [])
        for u in upd:
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                lines = env_file.read_text().splitlines() if env_file.exists() else []
                lines = [ln for ln in lines if not ln.startswith("TELEGRAM_CHAT_ID=")] + [f"TELEGRAM_CHAT_ID={chat['id']}"]
                env_file.write_text("\n".join(lines) + "\n")
                os.chmod(env_file, 0o600)
                os.environ["TELEGRAM_CHAT_ID"] = str(chat["id"])
                telegram("Linked. Your daily briefing will arrive here.")
                print(f"Linked to chat {chat['id']}. Check Telegram for a test message.")
                return 0
    print("No message arrived. Run `briefing telegram-link` again and message the bot while it waits.")
    return 1
