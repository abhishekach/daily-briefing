"""News: one web search per topic on the cheap model (public data only). A link is kept only if it appeared in the
search results, and nothing shown in an earlier brief is shown again."""
from __future__ import annotations

import re
from datetime import date, timedelta

from pydantic import BaseModel, Field

from briefing import llm, store
from briefing.settings import Settings


class Item(BaseModel):
    title: str
    url: str = Field(description="copied exactly from the search results; never constructed")
    source: str = Field(description="publisher or site name")
    published: str | None = Field(default=None, description="YYYY-MM-DD if known")
    why: str = Field(description="one plain sentence on why this matters to the reader specifically")


class Found(BaseModel):
    items: list[Item]


SYSTEM = """You find today's most useful news for one reader, described in ABOUT THE READER. Search the web for the topic
you are given. Report only items published within the window, each from a page that appeared in your search results,
with its URL copied exactly. Prefer official sources and reputable outlets. Skip opinion pieces, listicles, duplicates
and anything not useful to this reader. Write plainly: no hype words. Finish with the JSON answer only."""


def norm(u: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower())
    return re.sub(r"[#?].*$", "", u).rstrip("/")


TRACKING = {"gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid", "si"}


def canon(u: str | None) -> str:
    """The page a link points to: scheme, host case, a leading www., a trailing slash, #fragment and tracking
    parameters are ignored; other query parameters (often the page id) and the path's letter case are kept."""
    from urllib.parse import parse_qsl, urlencode, urlsplit
    u = (u or "").strip()
    if not u:
        return ""
    try:
        p = urlsplit(u if "://" in u else "https://" + u)
    except ValueError:
        return ""
    host = (p.hostname or "").lower().removeprefix("www.")
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
               if not k.lower().startswith("utm_") and k.lower() not in TRACKING)
    return f"{host}{p.path.rstrip('/')}" + (("?" + urlencode(q)) if q else "") if host else ""


def search(topic: str, brief: str, about: str, n: int, days: int, seen: set[str], log=print) -> list[dict]:
    meta: dict = {}
    since = (date.today() - timedelta(days=days)).isoformat()
    prompt = (f"ABOUT THE READER\n{about or 'not given'}\n\nTOPIC\n{brief}\n\nWINDOW\nPublished on or after {since} (today is "
              f"{date.today().isoformat()}).\n\nReturn at most {n} items, each a specific article, announcement or posting "
              "(never a home page).")
    res = llm.ask("search", prompt, system=SYSTEM, schema=Found, task="news", max_tokens=1800, web=True, meta=meta)
    urls = {canon(u) for u in meta.get("search_urls", [])}
    out, dropped = [], []
    for it in res.items[:n]:
        nu = norm(it.url)
        verified = bool(canon(it.url)) and canon(it.url) in urls   # the same page only: no prefix or look-alike links
        why = ("not in the search results" if not verified else
               "a home page, not an article" if "/" not in nu or len(nu.split("/", 1)[1]) < 4 else
               "already shown" if nu in seen else
               f"older than {days} days" if it.published and it.published < since else None)
        if why:
            dropped.append(f"{why}: {it.url}")
            continue
        seen.add(nu)
        out.append({**it.model_dump(), "topic": topic})
    log(f"  news {topic}: {len(res.items)} found, {len(out)} kept" + (f" ({len(dropped)} dropped)" if dropped else ""))
    return out


def collect(s: Settings, con, key: str, log=print) -> tuple[list[dict], list[str]]:
    """Returns (items, errors). Each topic is its own call, so one failure never empties the section."""
    N = s.news
    seen = {r[0] for r in con.execute("select url from news_seen where coalesce(brief_key,'') != ?", (key,))}
    items, errors = [], []
    for topic, t in N.topics.items():
        try:
            items += search(topic, t.brief, s.about_me, N.per_topic, t.days or N.days, seen, log)
        except llm.BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            errors.append(f"news ({t.label}): {type(e).__name__}: {str(e)[:120]}")
    items = items[: N.max_items]
    for it in items:
        con.execute("insert or ignore into news_seen(url, title, topic, first_seen, brief_key) values (?,?,?,?,?)",
                    (norm(it["url"]), it["title"], it["topic"], store.now(), key))
    con.commit()
    return items, errors
