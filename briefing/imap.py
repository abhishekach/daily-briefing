"""Read-only IMAP for any other mailbox (Outlook.com, GMX, web.de, iCloud, Fastmail, your company's server).

Folders are opened READ-ONLY (EXAMINE) and messages fetched with BODY.PEEK, so nothing is marked as read, moved,
deleted or flagged. Attachments are never opened. Use an app password where your provider offers one; it is read from
the environment variable you name in settings.yaml (password_env), never from settings.yaml itself.

Without Gmail's labels, bulk mail is recognised by its headers: a List-Unsubscribe or 'Precedence: bulk/list' header
means a newsletter or automatic notice, which is counted but not read, unless the sender is on your `watch` list.
"""
from __future__ import annotations

import email
import imaplib
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from briefing.google import clean_text, html_to_text
from briefing.settings import Imap


def _dec(v: str | None) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:  # noqa: BLE001
        return v or ""


def body_text(msg: email.message.Message, limit: int = 4000) -> str:
    plain, htm = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_filename() or (part.get("Content-Disposition") or "").startswith("attachment"):
            continue
        ct = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if ct == "text/plain":
            plain.append(text)
        elif ct == "text/html":
            htm.append(text)
    return clean_text("\n".join(plain) or html_to_text("\n".join(htm)), limit)


def is_bulk(msg: email.message.Message, watch: list[str]) -> bool:
    frm = _dec(msg.get("From")).lower()
    if any(w.lower() in frm for w in watch if w.strip()):
        return False
    prec = (msg.get("Precedence") or "").lower()
    return bool(msg.get("List-Unsubscribe")) or prec in ("bulk", "list", "junk")


def fetch(box: Imap, hours: float, cap: int, watch: list[str], connect=imaplib.IMAP4_SSL) -> tuple[list[dict], dict]:
    """Messages received in the last `hours`. Returns (to_triage, counts)."""
    pw = os.environ.get(box.password_env, "")
    if not pw:
        raise RuntimeError(f"{box.password_env} is not set in .env")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out, counts = [], {"bulk": 0, "total": 0}
    conn = connect(box.host, box.port)
    try:
        conn.login(box.user, pw)
        for folder in box.folders:
            typ, _ = conn.select(folder, readonly=True)          # EXAMINE: read-only
            if typ != "OK":
                continue
            typ, data = conn.search(None, "SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y"))
            ids = (data[0] or b"").split()[-cap:] if typ == "OK" else []
            for mid in reversed(ids):
                typ, got = conn.fetch(mid, "(BODY.PEEK[])")        # PEEK: never sets the \\Seen flag
                if typ != "OK" or not got or not isinstance(got[0], tuple):
                    continue
                msg = email.message_from_bytes(got[0][1])
                try:
                    when = parsedate_to_datetime(msg.get("Date"))
                    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                except Exception:  # noqa: BLE001
                    when = since
                if when < since:
                    continue
                counts["total"] += 1
                if is_bulk(msg, watch):
                    counts["bulk"] += 1
                    continue
                mid_s = mid.decode() if isinstance(mid, bytes) else str(mid)
                out.append({"account": box.name, "id": f"{folder}:{mid_s}", "thread": msg.get("Message-ID") or mid_s,
                            "from": _dec(msg.get("From")), "subject": _dec(msg.get("Subject")) or "(no subject)",
                            "date": msg.get("Date") or "", "newsletter": False, "important": False,
                            "text": body_text(msg), "link": box.webmail or ""})
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return out, counts
