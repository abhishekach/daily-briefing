"""The brief: calendar, the emails that need you (as to-dos), open to-dos, and news that matters to you.

Two editions: the morning brief covers today (and the night's mail); the afternoon update shows what is still ahead
today, tomorrow's calendar, and only what arrived since the morning. Read-only everywhere: never sends, deletes, moves
or marks email, never changes the calendar, never contacts anyone.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from briefing import google as G, store
from briefing.mail import ACTION
from briefing.settings import Settings
from briefing.workspace import Workspace

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_day(d: str | None) -> str:
    if not d:
        return ""
    try:
        x = date.fromisoformat(d[:10])
        return f"{x.day} {x.strftime('%b')}"
    except ValueError:
        return d


def edition_now(tz: ZoneInfo, edition: str | None = None) -> str:
    return edition or ("morning" if datetime.now(tz).hour < 12 else "afternoon")


# ---------------------------------------------------------------- parts
def calendar_part(ws: Workspace, s: Settings, tz: ZoneInfo, upcoming_only: bool, client_for=None) -> tuple[list[dict], str | None]:
    accts = G.accounts(ws.google)
    if not accts:
        return [], None
    start, end = G.day_window(tz, days=s.calendar_days)
    today = start.date()
    out, errs = [], []
    for a in accts:
        try:
            c = client_for(a) if client_for else G.Client(ws.google, a)
            for e in c.events(start, end):
                st = e["start"]
                d = date.fromisoformat(st[:10])
                e["day"] = "today" if d == today else "tomorrow" if d == today + timedelta(days=1) else _fmt_day(st)
                e["time"] = "all day" if e["all_day"] else datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone(tz).strftime("%H:%M")
                if upcoming_only and e["day"] == "today" and not e["all_day"]:
                    if datetime.fromisoformat((e["end"] or st).replace("Z", "+00:00")) < datetime.now(timezone.utc):
                        continue
                out.append(e)
        except Exception as e:  # noqa: BLE001
            errs.append(G.short_error(a, e))
    seen, uniq = set(), []
    for e in sorted(out, key=lambda e: (e["start"][:10], e["start"])):
        k = (e["title"], e["start"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq, G.summarise(errs, "your calendar")


def mail_part(ws: Workspace, s: Settings, con, hours: float, log=print, gmail_client_for=None, imap_connect=None) -> dict:
    from briefing import imap as I, mail as M
    gm = G.accounts(ws.google)
    res = {"status": "ok", "note": "", "accounts": gm + [b.name for b in s.mail.imap], "needs_you": [], "info": [], "counts": {}}
    if not res["accounts"]:
        return {**res, "status": "not set up", "note": "No mailbox set up yet: run `briefing google-link` or add an IMAP mailbox to settings.yaml."}
    ok, why = M.local_ready()
    if not ok:
        return {**res, "status": "skipped", "note": f"Email skipped: {why}. Email is never sent to a cloud model."}
    msgs, counts, errs = [], {}, []

    def add(c):
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
    for a in gm:
        try:
            m, c = M.fetch_gmail(ws.google, a, hours, s.mail.cap_per_account, gmail_client_for(a) if gmail_client_for else None, s.watch)
            msgs += m
            add(c)
        except Exception as e:  # noqa: BLE001
            errs.append(G.short_error(a, e))
    for box in s.mail.imap:
        try:
            m, c = I.fetch(box, hours, s.mail.cap_per_account, s.watch, **({"connect": imap_connect} if imap_connect else {}))
            msgs += m
            add(c)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{box.name}: {type(e).__name__}: {str(e)[:120]}")
    log(f"  {len(msgs)} email(s) to sort; {counts.get('promotions', 0) + counts.get('social', 0)} promotions/social and "
        f"{counts.get('bulk', 0)} bulk messages counted, not read")
    M.triage(msgs, s, log)
    for m in msgs:
        m["sender"] = M.sender_name(m["from"])
        cat = m.get("category")
        counts[cat] = counts.get(cat, 0) + 1
        if cat in ACTION:
            res["needs_you"].append(m)
        elif cat in ("important_info", "unsorted"):
            res["info"].append(m)
        con.execute("insert or ignore into mail_seen(ref, account, first_seen, category, subject) values (?,?,?,?,?)",
                    (f"email:{m['account']}:{m['id']}", m["account"], store.now(), cat, m["subject"][:200]))
    con.commit()
    res["needs_you"].sort(key=lambda m: (m.get("importance", 2), m.get("due") or "9999"))
    res["counts"] = counts
    if errs:
        res["note"] = G.summarise(errs, "your email") if all(x == "offline" for x in errs) else \
            "Some mail could not be read: " + "; ".join(errs)
    keep = ("account", "id", "sender", "subject", "category", "what_to_do", "summary", "due", "importance", "related_to", "link")
    res["needs_you"] = [{k: m.get(k) for k in keep} for m in res["needs_you"]]
    res["info"] = [{k: m.get(k) for k in keep} for m in res["info"]][:10]
    return res


def todos_from_mail(con, needs: list[dict]) -> int:
    n = 0
    for m in needs:
        tid = store.add_todo(con, title=m.get("what_to_do") or m.get("summary") or m["subject"], source="email",
                             ref=f"email:{m['account']}:{m['id']}", detail=f"{m['sender']}: {m['subject']}", sender=m["sender"],
                             account=m["account"], link=m.get("link") or "", due=m.get("due"), priority=int(m.get("importance") or 2))
        n += tid is not None
    return n


# ---------------------------------------------------------------- build
def build(ws: Workspace, s: Settings, con, news: bool = True, mail: bool = True, edition: str | None = None, log=print, **fakes) -> dict:
    tz = ZoneInfo(s.timezone)
    ed = edition_now(tz, edition)
    day = datetime.now(tz).date().isoformat()
    key = f"{day} {'am' if ed == 'morning' else 'pm'}"
    b = {"day": day, "key": key, "edition": ed, "name": s.name, "generated": datetime.now(tz).isoformat(timespec="minutes"), "errors": []}
    last = store.last_brief_time(con, key)
    hours = float(s.mail.hours)
    if last:
        gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600 + 0.25
        hours = max(1.0, min(hours, gap))
    b["mail_window_hours"] = round(hours, 1)
    log(f"{ed.capitalize()} brief: calendar ...")
    b["calendar"], b["calendar_error"] = calendar_part(ws, s, tz, upcoming_only=ed != "morning", client_for=fakes.get("calendar_client_for"))
    if mail:
        log(f"Email of the last {hours:.1f} h (read only by your local model) ...")
        b["mail"] = mail_part(ws, s, con, hours, log, fakes.get("gmail_client_for"), fakes.get("imap_connect"))
    else:
        b["mail"] = {"status": "skipped", "note": "Email not checked in this run.", "accounts": [], "needs_you": [], "info": [], "counts": {}}
    added = todos_from_mail(con, b["mail"]["needs_you"])
    b["news"] = []
    if news and s.news.topics:
        from briefing import news as N
        log("News (web search) ...")
        items, errs = N.collect(s, con, key, log)
        b["news"] = [{**x, "label": s.news.topics[x["topic"]].label} for x in items]
        b["errors"] += errs
    open_t = store.todos(con, "open")
    wk = (date.fromisoformat(day) + timedelta(days=7)).isoformat()
    b["todos"] = {"open": len(open_t), "added": added, "due_week": sum(1 for t in open_t if t.get("due") and t["due"] <= wk),
                  "items": open_t[:15]}
    return b


# ---------------------------------------------------------------- output
def text(b: dict) -> str:
    d = date.fromisoformat(b["day"])
    morning = b["edition"] == "morning"
    who = f", {b['name']}" if b.get("name") else ""
    L = [f"{'Good morning' if morning else 'Afternoon update'}{who}. {WD[d.weekday()]} {d.day} {d.strftime('%b')}"]
    today = [e for e in b["calendar"] if e["day"] == "today"]
    L.append("\nTODAY" if morning else "\nREST OF TODAY")
    L += [f"{e['time']}  {e['title']}" for e in today] or ([] if b.get("calendar_error") else ["Nothing in your calendar."])
    later = [e for e in b["calendar"] if e["day"] != "today"]
    if later and not morning:
        L.append("\nTOMORROW")
        L += [f"{e['time']}  {e['title']}" for e in later if e["day"] == "tomorrow"]
    if b.get("calendar_error"):
        L.append(f"({b['calendar_error']})")
    m = b["mail"]
    if m["status"] != "ok":
        L.append(f"\nEMAIL\n{m['note']}")
    else:
        since = "since this morning" if not morning else "today"
        L.append(f"\nNEEDS YOU ({len(m['needs_you'])})" if m["needs_you"] else f"\nNEEDS YOU\nNothing new needs you from email {since}.")
        for x in m["needs_you"][:10]:
            due = f" (by {_fmt_day(x['due'])})" if x.get("due") else ""
            L.append(f"• {x['sender']}: {x['subject']}\n  → {x.get('what_to_do') or x.get('summary')}{due}")
        c = m["counts"]
        routine = c.get("routine", 0) + c.get("newsletter", 0) + c.get("bulk", 0)
        promo = c.get("promotions", 0) + c.get("social", 0) + c.get("promotion", 0)
        extra = [x for x in (f"{len(m['info'])} worth reading" if m["info"] else "", f"{routine} routine" if routine else "",
                             f"{promo} promotions skipped" if promo else "") if x]
        if extra:
            L.append("Also: " + ", ".join(extra) + ".")
        if m.get("note"):
            L.append(f"({m['note']})")
    t = b["todos"]
    L.append(f"\nTO-DO: {t['open']} open" + (f", {t['due_week']} due this week" if t["due_week"] else "")
             + (f" ({t['added']} new from email)" if t["added"] else ""))
    if b["news"]:
        L.append("\nNEWS")
        L += [f"• {x['label']}: {x['title']}\n  {x['url']}" for x in b["news"]]
    return "\n".join(L)


def short(b: dict) -> str:
    """For channels that refuse long messages (WhatsApp): the headline numbers only."""
    m, t = b["mail"], b["todos"]
    first = next((e for e in b["calendar"] if e["day"] == "today"), None)
    return (f"{'Morning brief' if b['edition'] == 'morning' else 'Afternoon update'} {b['day']}: "
            f"{len([e for e in b['calendar'] if e['day'] == 'today'])} event(s) today"
            + (f", first {first['time']} {first['title'][:40]}" if first else "")
            + f"; {len(m['needs_you'])} email(s) need you; {t['open']} to-dos open; {len(b['news'])} news.")


CSS = """:root{--bg:#fbfaf7;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e0d8;--accent:#2f5d8a;--chip:#efece4}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--ink:#ecebe6;--muted:#a3a29b;--line:#2f2e2a;--accent:#8fb5de;--chip:#24231f}}
body{background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;max-width:720px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:1.5rem;margin:0}h2{font-size:.78rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);padding-bottom:4px;margin:28px 0 8px}
ul{list-style:none;padding:0;margin:0}li{padding:8px 0;border-bottom:1px solid var(--line)}li:last-child{border-bottom:0}
.m{color:var(--muted);font-size:.85rem}.t{font-variant-numeric:tabular-nums;color:var(--muted);display:inline-block;min-width:4.5em}
a{color:var(--accent)}.chip{display:inline-block;background:var(--chip);border-radius:99px;padding:1px 8px;font-size:.75rem;margin-left:6px}
.p1{color:#b4472e}"""


def html_page(b: dict) -> str:
    e = html.escape
    d = date.fromisoformat(b["day"])
    title = "Morning brief" if b["edition"] == "morning" else "Afternoon update"
    p = [f"<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{title} {b['day']}</title><style>{CSS}</style>",
         f"<h1>{title}</h1><div class='m'>{WD[d.weekday()]} {d.day} {d.strftime('%B %Y')} · made {e(b['generated'][11:16])}</div>"]
    p.append("<h2>Calendar</h2><ul>" + ("".join(f"<li><span class='t'>{e(x['time'])}</span> {e(x['title'])}<span class='chip'>{e(x['day'])}</span>"
                                                 f"{(' <span class=m>' + e(x['where']) + '</span>') if x.get('where') else ''}</li>" for x in b["calendar"])
                                         or f"<li class='m'>{e(b.get('calendar_error') or 'Nothing in your calendar.')}</li>") + "</ul>")
    m = b["mail"]
    p.append(f"<h2>Needs you ({len(m['needs_you'])})</h2><ul>" + ("".join(
        f"<li><b class='{'p1' if x.get('importance') == 1 else ''}'>{e(x.get('what_to_do') or x.get('summary') or '')}</b>"
        f"{(' <span class=chip>by ' + e(_fmt_day(x['due'])) + '</span>') if x.get('due') else ''}"
        f"<div class='m'>{e(x['sender'])}: " + (f"<a href='{e(x['link'])}'>{e(x['subject'])}</a>" if x.get("link") else e(x["subject"]))
        + "</div></li>" for x in m["needs_you"]) or f"<li class='m'>{e(m['note'] or 'Nothing needs you from email.')}</li>") + "</ul>")
    if m.get("info"):
        p.append("<h2>Worth reading</h2><ul>" + "".join(f"<li>{e(x.get('summary') or '')}<div class='m'>{e(x['sender'])}: "
                                                         f"{e(x['subject'])}</div></li>" for x in m["info"]) + "</ul>")
    t = b["todos"]
    p.append(f"<h2>To-do ({t['open']} open)</h2><ul>" + ("".join(
        f"<li><span class='t'>#{x['id']}</span> {e(x['title'])}{(' <span class=chip>by ' + e(_fmt_day(x['due'])) + '</span>') if x.get('due') else ''}"
        f"<div class='m'>{e(x.get('detail') or '')}</div></li>" for x in t["items"]) or "<li class='m'>Nothing open.</li>")
             + "</ul><div class='m'>Mark one done: <code>briefing done &lt;number&gt;</code></div>")
    p.append("<h2>News</h2><ul>" + ("".join(f"<li><a href='{e(x['url'])}'>{e(x['title'])}</a><span class='chip'>{e(x['label'])}</span>"
                                            f"<div class='m'>{e(x['source'])}{(' · ' + e(x['published'])) if x.get('published') else ''}</div>"
                                            f"<div>{e(x['why'])}</div></li>" for x in b["news"]) or "<li class='m'>None this time.</li>") + "</ul>")
    if b.get("errors"):
        p.append("<h2>Notes</h2><ul>" + "".join(f"<li class='m'>{e(x)}</li>" for x in b["errors"]) + "</ul>")
    return "".join(p) + "</html>"


def save(ws: Workspace, con, b: dict, cost: float) -> Path:
    ws.briefs.mkdir(parents=True, exist_ok=True)
    page = ws.briefs / f"{b['key'].replace(' ', '-')}.html"
    page.write_text(html_page(b), encoding="utf-8")
    store.save_brief(con, b["key"], b, text(b), str(page), cost)
    return page
