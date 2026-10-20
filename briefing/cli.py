"""briefing: your calendar, the emails that need you and the news that matters, twice a day, privately.

  briefing init                      create settings.yaml and .env in this folder
  briefing check                     check settings, mailboxes, the local model and delivery, without spending anything
  briefing google-link               link a Gmail + Google Calendar account, read-only (repeat per account)
  briefing telegram-link             connect your Telegram bot
  briefing run                       make and send the brief now (morning before noon, afternoon after)
  briefing todos | briefing done 3   list open to-dos | mark to-do 3 done
  briefing schedule [--install]      run it every day at your times (macOS: installs itself; elsewhere: prints the line)
  briefing costs                     this month's model spend

Options for every command: --dir <workspace> (default: this folder, or $BRIEFING_HOME), --models <profile>.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from briefing import __version__, google as G, llm, notify, store
from briefing.workspace import Workspace

EXAMPLES = Path(__file__).parent / "examples"


def _setup(a) -> tuple[Workspace, object]:
    ws = Workspace.find(a.dir)
    s = ws.settings()
    llm.configure(ws.root, a.models, s.mail.allow_cloud_for_email)
    return ws, s


def cmd_init(a) -> int:
    ws = Workspace.find(a.dir)
    ws.root.mkdir(parents=True, exist_ok=True)
    for src, dst in (("settings.example.yaml", ws.settings_file), ("env.example", ws.env_file)):
        if dst.exists():
            print(f"  kept     {dst.name} (already there)")
        else:
            shutil.copy(EXAMPLES / src, dst)
            print(f"  created  {dst.name}")
    os.chmod(ws.env_file, 0o600)
    gi = ws.root / ".gitignore"
    if not gi.exists():
        gi.write_text(".env\nsettings.yaml\ngoogle/\nbriefs/\n.briefing/\n")
        print("  created  .gitignore (keeps your private files out of any git repository)")
    print("\nNext:\n  1. Edit settings.yaml: your name, a line about you, news topics, any IMAP mailbox\n"
          "  2. Put your search model key in .env, and start a local model (Ollama: `ollama pull qwen3:8b`)\n"
          "  3. briefing google-link (Gmail and Calendar), briefing telegram-link (optional)\n"
          "  4. briefing check, then briefing run")
    return 0


def cmd_check(a) -> int:
    ws, s = _setup(a)
    from briefing import mail as M
    rows = []
    gm = G.accounts(ws.google)
    rows.append(("Gmail / Google Calendar", bool(gm), ", ".join(gm) or "none linked (briefing google-link)"))
    for b in s.mail.imap:
        rows.append((f"IMAP {b.name}", bool(os.environ.get(b.password_env)), f"{b.user}@{b.host}" if os.environ.get(b.password_env)
                     else f"{b.password_env} missing in .env"))
    ok, why = M.local_ready(wait_s=5)
    rows.append(("Local model for email", ok, f"{llm.model_of('mail')}: {why}"))
    rows.append(("Email stays on this computer", llm.mail_is_private(), "yes" if llm.mail_is_private()
                 else "NO: a cloud model reads your email (allow_cloud_for_email is true)"))
    rows.append(("News topics", bool(s.news.topics), ", ".join(t.label for t in s.news.topics.values()) or "none"))
    rows.append(("Search model", True, llm.model_of("search")))
    chans = [n for n, on, env in (("Telegram", s.delivery.telegram, "TELEGRAM_CHAT_ID"), ("ntfy", s.delivery.ntfy, "NTFY_TOPIC"),
                                   ("WhatsApp", s.delivery.whatsapp, "CALLMEBOT_APIKEY")) if on and os.environ.get(env)]
    rows.append(("Phone delivery", bool(chans), ", ".join(chans) or "none: the brief is only saved and printed"))
    for name, good, detail in rows:
        print(f"  {'ok ' if good else '-- '} {name}: {detail}")
    return 0


def cmd_run(a) -> int:
    from briefing import brief as B
    ws, s = _setup(a)
    con = store.connect(ws.root)
    b = B.build(ws, s, con, news=not a.no_news, mail=not a.no_mail, edition=a.edition)
    page = B.save(ws, con, b, round(llm.S.run_cost, 4))
    t = B.text(b)
    print("\n" + t)
    if not a.no_notify:
        sent = notify.deliver("Morning brief" if b["edition"] == "morning" else "Afternoon update", t, B.short(b), s.delivery)
        print(f"\nSent to: {', '.join(k for k, v in sent.items() if v) or 'no phone channel set up'}"
              + (f"; failed: {', '.join(k for k, v in sent.items() if not v)}" if any(not v for v in sent.values()) else ""))
    for e in b["errors"]:
        print(f"note: {e}")
    print(f"Saved: {page}\nModel cost of this brief: ${llm.S.run_cost:.3f}")
    return 0


def cmd_todos(a) -> int:
    ws = Workspace.find(a.dir)
    con = store.connect(ws.root)
    items = store.todos(con, "open")
    for t in items:
        print(f"  #{t['id']:<4} {t['title']}" + (f"  (by {t['due']})" if t.get("due") else "") + (f"\n        {t['detail']}" if t.get("detail") else ""))
    print(f"{len(items)} open." if items else "Nothing open.")
    return 0


def cmd_done(a) -> int:
    ws = Workspace.find(a.dir)
    con = store.connect(ws.root)
    if store.set_todo(con, a.id, "open" if a.undo else "done"):
        print(f"To-do #{a.id} {'reopened' if a.undo else 'done'}.")
        return 0
    print(f"No to-do #{a.id}.")
    return 1


LABEL = "com.dailybriefing.{}"


def cmd_schedule(a) -> int:
    ws = Workspace.find(a.dir)
    exe = shutil.which("briefing") or f"{sys.executable} -m briefing"
    times = [t.strip() for t in a.times.split(",") if t.strip()]
    if sys.platform == "darwin" and a.install:
        d = Path.home() / "Library" / "LaunchAgents"
        d.mkdir(parents=True, exist_ok=True)
        for t in times:
            h, m = (int(x) for x in t.split(":"))
            label = LABEL.format(t.replace(":", ""))
            path = d / f"{label}.plist"
            plist = {"Label": label, "ProgramArguments": exe.split() + ["run", "--dir", str(ws.root)],
                     "StartCalendarInterval": {"Hour": h, "Minute": m}, "WorkingDirectory": str(ws.root),
                     "StandardOutPath": str(ws.root / ".briefing" / "run.log"), "StandardErrorPath": str(ws.root / ".briefing" / "run.log"),
                     "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}}
            (ws.root / ".briefing").mkdir(exist_ok=True)
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
            with open(path, "wb") as f:
                plistlib.dump(plist, f)
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True)
            print(f"  scheduled {t} daily ({path})")
        print("A run that falls while your Mac sleeps happens when it wakes. Remove with: briefing schedule --remove")
        return 0
    if sys.platform == "darwin" and a.remove:
        for p in (Path.home() / "Library" / "LaunchAgents").glob("com.dailybriefing.*.plist"):
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{p.stem}"], capture_output=True)
            p.unlink()
            print(f"  removed {p.name}")
        return 0
    print("Add these lines with `crontab -e` (Linux, or macOS without --install):")
    for t in times:
        h, m = t.split(":")
        print(f"  {int(m)} {int(h)} * * * cd {ws.root} && {exe} run >> {ws.root}/.briefing/run.log 2>&1")
    print("Windows: Task Scheduler -> Create Basic Task -> Daily -> start program: briefing, arguments: run --dir " + str(ws.root))
    return 0


def cmd_costs(a) -> int:
    _setup(a)
    print(f"This month: ${llm.month_spend():.3f}")
    for task, role, model, n, cost in llm.month_summary():
        print(f"  {task:<6} {role:<7} {model:<40} {n:>4} calls  ${cost or 0:.3f}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="briefing", description="A private daily briefing: calendar, email that needs you, news.")
    ap.add_argument("--version", action="version", version=f"daily-briefing {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", help="workspace folder (default: this folder, or $BRIEFING_HOME)")
    common.add_argument("--models", help="model profile from models.yaml (default: balanced, or $BRIEFING_MODELS)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", parents=[common], help="create settings.yaml and .env")
    sub.add_parser("check", parents=[common], help="check the setup without spending anything")
    sub.add_parser("google-link", parents=[common], help="link a Gmail and Google Calendar account (read-only)")
    gu = sub.add_parser("google-unlink", parents=[common], help="remove a linked Google account")
    gu.add_argument("email")
    sub.add_parser("telegram-link", parents=[common], help="connect your Telegram bot")
    r = sub.add_parser("run", parents=[common], help="make and send the brief now")
    r.add_argument("--edition", choices=["morning", "afternoon"], help="default: by the time of day")
    r.add_argument("--no-news", action="store_true", help="skip the web search (free)")
    r.add_argument("--no-mail", action="store_true")
    r.add_argument("--no-notify", action="store_true", help="save and print only")
    sub.add_parser("todos", parents=[common], help="list open to-dos")
    d = sub.add_parser("done", parents=[common], help="mark a to-do done")
    d.add_argument("id", type=int)
    d.add_argument("--undo", action="store_true", help="reopen it")
    sc = sub.add_parser("schedule", parents=[common], help="run it every day at set times")
    sc.add_argument("--times", default="07:30,17:30", help="comma-separated HH:MM (default 07:30,17:30)")
    sc.add_argument("--install", action="store_true", help="macOS: install it with launchd")
    sc.add_argument("--remove", action="store_true", help="macOS: remove the schedule")
    sub.add_parser("costs", parents=[common], help="this month's model spend")
    a = ap.parse_args(argv)
    handlers = {"init": cmd_init, "check": cmd_check, "run": cmd_run, "todos": cmd_todos, "done": cmd_done,
                "schedule": cmd_schedule, "costs": cmd_costs,
                "google-link": lambda a: G.link(Workspace.find(a.dir).google),
                "google-unlink": lambda a: 0 if G.unlink(Workspace.find(a.dir).google, a.email) else 1,
                "telegram-link": lambda a: notify.telegram_link(Workspace.find(a.dir).env_file)}
    try:
        return handlers[a.cmd](a)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
