"""Read-only Gmail and Google Calendar.

Setup once (README, "Gmail and Google Calendar"): a Google Cloud project with the Gmail API and Calendar API turned on,
an OAuth client of type "Desktop app" saved as <workspace>/google/client_secret.json, then per account:

    briefing google-link        # opens your browser; sign in; repeat for each account

Only the two READ-ONLY scopes below are ever requested: the briefing cannot send, delete, move or mark mail, and cannot
change your calendar. Tokens stay in <workspace>/google/<email>.json on your computer.
"""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"]
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"


class NotLinked(RuntimeError):
    pass


class Offline(RuntimeError):
    """Google could not be reached at all (no internet, computer just woke): the saved sign-in is fine."""


def short_error(account: str, e: Exception) -> str:
    if isinstance(e, Offline) or isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "offline"
    if isinstance(e, NotLinked):
        return f"{account}: {e}"
    return f"{account}: {type(e).__name__}: {str(e)[:120]}"


def summarise(errors: list[str], what: str) -> str | None:
    if not errors:
        return None
    rest = [x for x in errors if x != "offline"]
    off = len(errors) - len(rest)
    return " ".join(([f"No internet connection when the brief ran, so {what} could not be read; your sign-ins are fine."]
                     if off else []) + rest)


def link(folder: Path) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow
    secret = folder / "client_secret.json"
    if not secret.exists():
        print(f"Missing {secret}. Create a Desktop-app OAuth client in Google Cloud and save its JSON there "
              "(README, 'Gmail and Google Calendar').")
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  authorization_prompt_message="Opening Google sign-in in your browser ...",
                                  success_message="Linked read-only. You can close this tab.")
    if not creds.refresh_token:
        print("Google returned no refresh token. Remove the app's access at myaccount.google.com/permissions and link again.")
        return 1
    email = httpx.get(f"{GMAIL}/profile", headers={"Authorization": f"Bearer {creds.token}"}, timeout=20).json()["emailAddress"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{email}.json"
    path.write_text(creds.to_json())
    os.chmod(path, 0o600)
    print(f"Linked {email} (read-only Gmail and Calendar). Linked accounts: {', '.join(accounts(folder))}")
    return 0


def accounts(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.glob("*@*.json")) if folder.exists() else []


def unlink(folder: Path, email: str) -> bool:
    p = folder / f"{email}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def _token(folder: Path, email: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    path = folder / f"{email}.json"
    if not path.exists():
        raise NotLinked(f"{email} is not linked")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        from google.auth.exceptions import TransportError
        try:
            creds.refresh(Request())
        except TransportError as e:
            raise Offline(f"{email}: Google could not be reached") from e
        except Exception as e:  # noqa: BLE001  (revoked, or a 7-day token from a project still in Testing)
            raise NotLinked("Google refused the saved sign-in. Run `briefing google-link` again; if this repeats every "
                            "week, publish your Google Cloud app ('In production').") from e
        path.write_text(creds.to_json())
    return creds.token


class Client:
    """Thin read-only REST client for one account."""

    def __init__(self, folder: Path, email: str, http: httpx.Client | None = None, token: str | None = None):
        self.email = email
        self.http = http or httpx.Client(timeout=30)
        self.headers = {"Authorization": f"Bearer {token or _token(folder, email)}"}

    def get(self, url: str, **params) -> dict:
        r = self.http.get(url, headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    def recent_ids(self, hours: int = 24, limit: int = 120) -> list[dict]:
        q = f"newer_than:{max(1, round(hours / 24))}d -in:chats -in:sent -in:drafts"
        out, token = [], None
        while len(out) < limit:
            d = self.get(f"{GMAIL}/messages", q=q, maxResults=min(100, limit - len(out)), **({"pageToken": token} if token else {}))
            out += d.get("messages", [])
            token = d.get("nextPageToken")
            if not token:
                break
        return out

    def message(self, mid: str, full: bool) -> dict:
        if full:
            return self.get(f"{GMAIL}/messages/{mid}", format="full")
        return self.get(f"{GMAIL}/messages/{mid}", format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date", "List-Unsubscribe"])

    def events(self, start: datetime, end: datetime) -> list[dict]:
        cals = self.get(f"{CAL}/users/me/calendarList", minAccessRole="reader").get("items", [])
        out = []
        for c in cals:
            if not c.get("selected") and not c.get("primary"):
                continue
            items = self.get(f"{CAL}/calendars/{quote(c['id'], safe='')}/events", timeMin=start.isoformat(),
                             timeMax=end.isoformat(), singleEvents="true", orderBy="startTime", maxResults=50).get("items", [])
            for e in items:
                if e.get("status") == "cancelled":
                    continue
                out.append({"calendar": c.get("summary") or self.email, "title": e.get("summary") or "(no title)",
                            "start": e["start"].get("dateTime") or e["start"].get("date"),
                            "end": e["end"].get("dateTime") or e["end"].get("date"),
                            "all_day": "date" in e["start"], "where": e.get("location") or "", "link": e.get("htmlLink") or ""})
        return out


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def body_text(msg: dict, limit: int = 4000) -> str:
    """Plain text of a message: the text/plain part, else the HTML part without tags. Attachments are never read."""
    plain, htm = [], []

    def walk(p):
        mt = p.get("mimeType", "")
        if p.get("filename"):
            return
        data = p.get("body", {}).get("data")
        if data and mt == "text/plain":
            plain.append(_b64(data))
        elif data and mt == "text/html":
            htm.append(_b64(data))
        for c in p.get("parts", []) or []:
            walk(c)
    walk(msg.get("payload", {}))
    return clean_text("\n".join(plain) or html_to_text("\n".join(htm)), limit)


def html_to_text(h: str) -> str:
    return re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h))


def clean_text(t: str, limit: int = 4000) -> str:
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t ]+", " ", t)).strip()[:limit]


def mail_link(email: str, thread_id: str) -> str:
    return f"https://mail.google.com/mail/?authuser={email}#all/{thread_id}"


def day_window(tz: ZoneInfo, days: int = 1, day: datetime | None = None) -> tuple[datetime, datetime]:
    d = (day or datetime.now(tz)).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return d, d + timedelta(days=days)
