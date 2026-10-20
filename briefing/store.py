"""Briefs, to-dos and what the briefing has already seen, in <workspace>/.briefing/briefing.db."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
create table if not exists briefs(
  key text primary key,          -- "YYYY-MM-DD am" or "YYYY-MM-DD pm"
  created text, data text, text_version text, html_path text, cost real
);
create table if not exists todos(
  id integer primary key autoincrement,
  source text,                   -- email / manual
  ref text unique,               -- email:<account>:<message id>; null for manual items
  title text, detail text, sender text, account text, link text,
  due text,                      -- YYYY-MM-DD or null
  priority integer default 2,    -- 1 high, 2 normal, 3 low
  status text default 'open',    -- open / done
  created text, done_at text
);
create table if not exists mail_seen(ref text primary key, account text, first_seen text, category text, subject text);
create table if not exists news_seen(url text primary key, title text, topic text, first_seen text, brief_key text);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(workspace: Path) -> sqlite3.Connection:
    p = workspace / ".briefing" / "briefing.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def last_brief_time(con, exclude_key: str) -> str | None:
    r = con.execute("select created from briefs where key != ? order by created desc limit 1", (exclude_key,)).fetchone()
    return r[0] if r else None


def add_todo(con, *, title: str, source: str, ref: str | None = None, detail: str = "", sender: str = "",
             account: str = "", link: str = "", due: str | None = None, priority: int = 2) -> int | None:
    """Adds an open to-do; one made from the same email is never added twice."""
    if ref and con.execute("select 1 from todos where ref=?", (ref,)).fetchone():
        return None
    cur = con.execute("insert into todos(source, ref, title, detail, sender, account, link, due, priority, status, created) "
                      "values (?,?,?,?,?,?,?,?,?,'open',?)",
                      (source, ref, title.strip()[:200], detail[:500], sender[:200], account, link, due, priority, now()))
    con.commit()
    return cur.lastrowid


def set_todo(con, tid: int, status: str) -> bool:
    if status not in ("open", "done"):
        raise ValueError(status)
    cur = con.execute("update todos set status=?, done_at=? where id=?", (status, now() if status == "done" else None, tid))
    con.commit()
    return cur.rowcount > 0


def todos(con, status: str = "open", limit: int = 200) -> list[dict]:
    if status == "open":
        q = ("select * from todos where status='open' order by case when due is null or due='' then 1 else 0 end, due, "
             "priority, created desc limit ?")
    else:
        q = "select * from todos where status='done' order by done_at desc limit ?"
    return [dict(r) for r in con.execute(q, (limit,))]


def save_brief(con, key: str, data: dict, text: str, html_path: str, cost: float) -> None:
    con.execute("insert into briefs(key, created, data, text_version, html_path, cost) values (?,?,?,?,?,?) "
                "on conflict(key) do update set created=excluded.created, data=excluded.data, "
                "text_version=excluded.text_version, html_path=excluded.html_path, cost=excluded.cost",
                (key, now(), json.dumps(data, ensure_ascii=False), text, html_path, cost))
    con.commit()
