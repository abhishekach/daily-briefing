"""Offline tests: fake Gmail, fake IMAP server, fake models, a made-up person. No keys, no network, no cost.

    pytest            (or: python tests/test_offline.py)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from briefing import brief as B, cli, imap as I, llm, mail as M, news as N, notify, store  # noqa: E402
from briefing.workspace import Workspace  # noqa: E402

for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "WHATSAPP_PHONE", "CALLMEBOT_APIKEY"):
    os.environ.pop(k, None)
NOW = datetime.now(timezone.utc)


def workspace(profile="anthropic") -> tuple[Workspace, object]:
    d = Path(tempfile.mkdtemp())
    assert cli.main(["init", "--dir", str(d)]) == 0
    ws = Workspace.find(str(d))
    s = ws.settings()
    llm.configure(ws.root, profile, s.mail.allow_cloud_for_email)
    return ws, s


def resp(text, urls=(), model="m"):
    msg = NS(content=text, provider_specific_fields={"web_search_results": [{"content": [{"url": u} for u in urls]}]} if urls else {},
             annotations=None)
    return NS(model=model, usage=NS(prompt_tokens=10, completion_tokens=5), choices=[NS(finish_reason="stop", message=msg)])


# ---------------------------------------------------------------- privacy
def test_privacy_guard():
    ws, s = workspace()
    assert llm.is_local("mail") and llm.mail_is_private()
    llm.S.roles["mail"] = {"model": "anthropic/claude-haiku-5-5"}
    try:
        llm.ask("mail", "x")
        raise AssertionError("a cloud model must never read email")
    except llm.PrivacyError:
        pass
    (ws.root / "models.yaml").write_text("profiles:\n  p:\n    search: {model: anthropic/x}\n    mail: {model: ollama_chat/q, backup: anthropic/y}\n")
    try:
        llm.configure(ws.root, "p")
        raise AssertionError("the mail role may not have a backup")
    except llm.PrivacyError:
        pass
    calls = []

    def fake(role, messages, temperature, max_tokens, response_format=None, fallback=True, extra=None):
        calls.append((role, fallback))
        return resp('{"category":"routine","summary":"s"}')
    (ws.root / "models.yaml").unlink()
    llm.configure(ws.root, "anthropic", allow_cloud_email=False)
    llm._complete = fake
    llm.ask("mail", "x", schema=M.Triage)
    assert calls == [("mail", False)], "mail: one local call, never a fallback"


# ---------------------------------------------------------------- Gmail
class FakeGmail:
    def __init__(self, msgs):
        self.msgs = msgs

    def recent_ids(self, hours, limit):
        return [{"id": k} for k in self.msgs]

    def message(self, mid, full):
        m = self.msgs[mid]
        heads = [{"name": "From", "value": m["from"]}, {"name": "Subject", "value": m["subject"]}, {"name": "Date", "value": "today"}]
        if m.get("unsub"):
            heads.append({"name": "List-Unsubscribe", "value": "<mailto:x>"})
        import base64
        body = base64.urlsafe_b64encode(m.get("body", "hello").encode()).decode()
        return {"id": mid, "threadId": "t" + mid, "labelIds": m["labels"], "internalDate": str(int(m.get("at", NOW).timestamp() * 1000)),
                "snippet": "snip", "payload": {"headers": heads, "mimeType": "text/plain", "body": {"data": body}}}


GMAIL = {
    "1": {"from": "Example Bank <noreply@bank.example>", "subject": "Please confirm your new address by 30 Oct", "labels": ["INBOX", "IMPORTANT"],
          "body": "Dear Sam, please confirm your new address in online banking by 30 October."},
    "2": {"from": "Shop <deals@shop.example>", "subject": "50% off", "labels": ["CATEGORY_PROMOTIONS"]},
    "3": {"from": "Tool <noreply@tool.example>", "subject": "Your weekly digest", "labels": ["CATEGORY_UPDATES"], "unsub": True},
    "4": {"from": "Old <a@b.example>", "subject": "Old mail", "labels": ["INBOX"], "at": NOW - timedelta(days=3)},
    "5": {"from": "Northwind Climate <hr@northwind.example>", "subject": "Interview invitation", "labels": ["INBOX", "CATEGORY_UPDATES"],
          "unsub": True, "body": "We would like to invite you to an interview."},
}


def test_gmail_fetch():
    ws, s = workspace()
    msgs, counts = M.fetch_gmail(ws.google, "sam@example.com", 24, 50, FakeGmail(GMAIL))
    assert [m["id"] for m in msgs] == ["1"], "promotions, bulk notifications and old mail are not read"
    assert counts["bulk"] == 2
    msgs, counts = M.fetch_gmail(ws.google, "sam@example.com", 24, 50, FakeGmail(GMAIL), watch=s.watch)
    assert [m["id"] for m in msgs] == ["1", "5"], "an interview invitation from a watched sender is never bulk"
    assert counts["promotions"] == 1 and counts["bulk"] == 1 and counts["total"] == 4
    assert "confirm your new address" in msgs[0]["text"] and msgs[1]["text"] == "snip", "update-tab mail: snippet only"
    assert msgs[0]["link"].startswith("https://mail.google.com/mail/?authuser=sam@example.com")


# ---------------------------------------------------------------- IMAP
class FakeIMAP:
    log: list = []

    def __init__(self, host, port):
        self.log.clear()

    def login(self, user, pw):
        assert pw == "app-pass"

    def select(self, folder, readonly=False):
        self.log.append(("select", folder, readonly))
        return "OK", [b"3"]

    def search(self, charset, *crit):
        return "OK", [b"1 2 3"]

    def fetch(self, mid, what):
        self.log.append(("fetch", what))
        msg = EmailMessage()
        msg["Date"] = NOW.strftime("%a, %d %b %Y %H:%M:%S +0000")
        if mid == b"1":
            msg["From"], msg["Subject"] = "Landlord <l@flat.example>", "Rent increase from December"
            msg.set_content("Your rent changes from 1 December. Please sign and return the form by 15 November.")
        elif mid == b"2":
            msg["From"], msg["Subject"], msg["List-Unsubscribe"] = "News <n@paper.example>", "Morning news", "<mailto:u>"
            msg.set_content("news")
        else:
            msg["From"], msg["Subject"], msg["List-Unsubscribe"] = "Finanzamt <post@finanzamt.example>", "Bescheid", "<mailto:u>"
            msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="bescheid.pdf")
            msg.set_content("Ihr Steuerbescheid ist da.") if False else None
        return "OK", [(b"1", msg.as_bytes())]

    def logout(self):
        pass


def test_imap_read_only():
    from briefing.settings import Imap
    os.environ["TEST_PW"] = "app-pass"
    box = Imap(name="home", host="imap.example", user="sam", password_env="TEST_PW")
    msgs, counts = I.fetch(box, 24, 50, ["Finanzamt"], connect=FakeIMAP)
    assert ("select", "INBOX", True) in FakeIMAP.log, "folders are opened read-only"
    assert all(w == "(BODY.PEEK[])" for k, w in [x for x in FakeIMAP.log if x[0] == "fetch"]), "never sets the Seen flag"
    subjects = [m["subject"] for m in msgs]
    assert "Rent increase from December" in subjects and "Morning news" not in subjects, "bulk mail counted, not read"
    assert "Bescheid" in subjects, "a watched sender is never treated as bulk"
    assert counts["bulk"] == 1
    fin = next(m for m in msgs if m["subject"] == "Bescheid")
    assert "%PDF" not in fin["text"], "attachments are never read"


# ---------------------------------------------------------------- the whole brief with fake models
TRIAGE = {
    "confirm your new address": {"category": "deadline", "what_to_do": "Confirm your new address in online banking",
                                 "summary": "Your bank asks you to confirm your address.", "due": "2026-10-30", "importance": 1,
                                 "related_to": "Example Bank"},
    "interview": {"category": "Application update", "what_to_do": "Reply with times for the interview",
                  "summary": "Northwind invites you to an interview.", "importance": "1", "related_to": "Someone Else"},
    "rent changes": {"category": "action_needed", "what_to_do": "Sign and return the rent form", "summary": "Rent changes.",
                     "due": "2026-11-15", "importance": 2},
}
NEWS_URLS = ["https://www.eu-climate.example/2026/10/new-reporting-rule", "https://jobs.example/posting/12345"]


def fake_complete(role, messages, temperature, max_tokens, response_format=None, fallback=True, extra=None):
    user = messages[-1]["content"]
    if role == "mail":
        assert "ABOUT THE READER" in user and "Northwind Climate" in user
        for key, ans in TRIAGE.items():
            if key in user.lower():
                return resp(json.dumps(ans))
        return resp("not json")       # unsortable: kept as 'unsorted'
    assert role == "search" and extra and "web_search_options" in extra
    user = user.split("TOPIC\n", 1)[1]
    if "climate rules" in user:
        items = [{"title": "EU adopts new reporting rule", "url": NEWS_URLS[0], "source": "EU", "published": NOW.date().isoformat(),
                  "why": "Your company must report from 2027."},
                 {"title": "Invented story", "url": "https://made-up.example/story", "source": "?", "why": "?"}]
        return resp(json.dumps({"items": items}), urls=NEWS_URLS)
    if "product managers" in user:
        return resp(json.dumps({"items": [{"title": "A home page", "url": "https://tool.example/", "source": "x", "why": "y"}]}),
                    urls=["https://tool.example/"])
    return resp(json.dumps({"items": [{"title": "Senior PM, remote", "url": NEWS_URLS[1], "source": "Jobs", "why": "Fits your search."}]}),
                urls=NEWS_URLS)


class FakeCal:
    def events(self, start, end):
        t = start.replace(hour=9).astimezone(timezone.utc).isoformat()
        tm = (start + timedelta(days=1)).replace(hour=10).astimezone(timezone.utc).isoformat()
        return [{"title": "Stand-up", "start": t, "end": t, "all_day": False, "where": "", "link": ""},
                {"title": "Dentist", "start": tm, "end": tm, "all_day": False, "where": "Praxis", "link": ""},
                {"title": "Stand-up", "start": t, "end": t, "all_day": False, "where": "", "link": ""}]


def test_brief_end_to_end():
    ws, s = workspace()
    (ws.google / "sam@example.com.json").parent.mkdir(parents=True, exist_ok=True)
    (ws.google / "sam@example.com.json").write_text("{}")
    os.environ["HOME_MAIL_PW"] = "app-pass"
    from briefing.settings import Imap
    s.mail.imap = [Imap(name="home", host="imap.example", user="sam", password_env="HOME_MAIL_PW")]
    llm._complete = fake_complete
    M.local_ready = lambda wait_s=45: (True, "running")
    con = store.connect(ws.root)
    logs = []
    fakes = {"calendar_client_for": lambda a: FakeCal(), "gmail_client_for": lambda a: FakeGmail(GMAIL), "imap_connect": FakeIMAP}
    b = B.build(ws, s, con, edition="morning", log=logs.append, **fakes)
    assert [e["title"] for e in b["calendar"]] == ["Stand-up", "Dentist"], "duplicates removed, today first"
    assert b["calendar"][0]["day"] == "today" and b["calendar"][1]["day"] == "tomorrow"
    m = b["mail"]
    assert [x["subject"] for x in m["needs_you"]][:2] == ["Please confirm your new address by 30 Oct", "Interview invitation"]
    assert m["needs_you"][1]["category"] == "application_update" and m["needs_you"][1]["related_to"] is None, \
        "labels normalised; a related_to outside the watch list is dropped"
    assert any(x["category"] == "unsorted" for x in m["info"]), "an email the model could not sort is kept, not guessed"
    assert "text" not in json.dumps(m["needs_you"][0]), "email bodies never leave the triage step"
    assert b["todos"]["added"] == 3 and b["todos"]["open"] == 3
    assert [x["url"] for x in b["news"]] == NEWS_URLS, "unverified links and home pages are dropped"
    t = B.text(b)
    assert t.startswith("Good morning, Sam.") and "NEEDS YOU (3)" in t and "→ Confirm your new address" in t and "(by 30 Oct)" in t
    assert "Dentist" not in t, "the morning brief lists today; tomorrow shows in the afternoon update"
    assert "promotions skipped" in t and "NEWS" in t
    page = B.save(ws, con, b, 0.0)
    h = page.read_text()
    assert "prefers-color-scheme:dark" in h and "Needs you (3)" in h and "briefing done" in h

    # the afternoon update: nothing repeats
    b2 = B.build(ws, s, con, edition="afternoon", log=logs.append, **fakes)
    assert b2["todos"]["added"] == 0, "a to-do from the same email is never added twice"
    assert b2["news"] == [], "news already shown is not shown again"
    assert "TOMORROW" in B.text(b2) and "Dentist" in B.text(b2)
    assert B.short(b2).startswith("Afternoon update") and len(B.short(b2)) < 300


def test_todos_cli_and_delivery():
    ws, s = workspace()
    con = store.connect(ws.root)
    tid = store.add_todo(con, title="Call the school", source="manual")
    assert cli.main(["done", str(tid), "--dir", str(ws.root)]) == 0
    assert store.todos(con, "open") == []
    assert cli.main(["done", str(tid), "--undo", "--dir", str(ws.root)]) == 0 and len(store.todos(con, "open")) == 1
    assert notify.deliver("t", "x", "s", s.delivery) == {}, "no channel configured: nothing is sent"
    sent = []
    os.environ.update(TELEGRAM_BOT_TOKEN="tok", TELEGRAM_CHAT_ID="42")
    c = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(json.loads(r.content)) or httpx.Response(200, json={"ok": True})))
    assert notify.telegram("line\n" * 2000, client=c) and len(sent) >= 3 and all(len(x["text"]) <= 3900 for x in sent)
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(k)
    assert ".env" in (ws.root / ".gitignore").read_text() and oct(ws.env_file.stat().st_mode)[-3:] == "600"


def test_schedule_prints_cron():
    import contextlib
    import io
    ws, _ = workspace()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(["schedule", "--times", "07:30,17:30", "--dir", str(ws.root)])
    if sys.platform != "darwin":
        assert "30 7 * * *" in out.getvalue() and "30 17 * * *" in out.getvalue()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name}: OK")
