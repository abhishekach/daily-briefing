"""One door to every model.

    from briefing.llm import ask
    found = ask("search", prompt, system=..., schema=Found, web=True, meta=meta)   # web search; meta["search_urls"]
    t = ask("mail", prompt, system=..., schema=Triage)                            # email: a LOCAL model only

- Code names a ROLE, never a model: `search` (web search for news) and `mail` (sorting email). Roles map to models in
  models.yaml (a profile chosen with BRIEFING_MODELS, default "balanced").
- PRIVACY: the `mail` role must point at a local model (Ollama, LM Studio). Code checks this before every call and
  refuses otherwise, unless settings.yaml says `allow_cloud_for_email: true`. There is never a cloud fallback for mail.
- With `schema`, the answer must be JSON matching the Pydantic model; it is validated, retried once with the error,
  then the backup model is tried; a call that never validates fails loudly.
- `web=True` turns on the provider's web search. The URLs the search really returned are put in meta["search_urls"],
  so callers keep only links that appeared in the results. Gemini answers in two steps (grounded notes, then JSON
  restricted to the grounding sources) because one grounded step with a strict schema was unreliable.
- Every call is logged with its cost to <workspace>/.briefing/ledger.sqlite, and BRIEFING_MONTHLY_BUDGET_USD
  (default $10) stops paid calls once the month's spend reaches it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
ROLES_NEEDED = ("search", "mail")
LOCAL_PREFIXES = ("ollama/", "ollama_chat/", "lm_studio/")
NATIVE_PREFIXES = ("openai/", "gemini/")
SCHEMA_MIN_TOKENS = 2500
LOCAL_MIN_TOKENS = 4096
LOCAL_EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}   # Qwen-style local models: no thinking for sorting
THINK_RE = re.compile(r"<think>.*?</think>", re.S)
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com/grounding-api-redirect",)


class BudgetExceeded(RuntimeError):
    pass


class PrivacyError(RuntimeError):
    pass


class _State:
    workspace: Path = Path.cwd()
    roles: dict[str, dict] = {}
    router = None
    run_cost: float = 0.0
    allow_cloud_email: bool = False
    native_refused: set = set()
    resolved: dict = {}


S = _State()


# ---------------------------------------------------------------- configuration
def load_profile(workspace: Path, name: str | None = None) -> dict[str, dict]:
    own = workspace / "models.yaml"
    path = own if own.exists() else Path(__file__).with_name("models.yaml")
    cfg = yaml.safe_load(path.read_text())
    name = name or os.environ.get("BRIEFING_MODELS") or "balanced"
    profiles = cfg.get("profiles") or {}
    if name not in profiles:
        raise ValueError(f"Model profile '{name}' not found in {path}. Choose one of: {', '.join(profiles)}")
    roles = profiles[name]
    missing = [r for r in ROLES_NEEDED if r not in roles]
    if missing:
        raise ValueError(f"Profile '{name}' in {path} has no model for: {', '.join(missing)}")
    return roles


def configure(workspace: Path, profile: str | None = None, allow_cloud_email: bool = False) -> None:
    S.workspace = Path(workspace)
    S.roles = load_profile(S.workspace, profile)
    S.allow_cloud_email = allow_cloud_email
    S.router = None
    if S.roles["mail"].get("backup"):
        raise PrivacyError("The mail role must not have a backup model: email is never sent anywhere else.")


def model_of(role: str) -> str:
    return str((S.roles.get(role) or {}).get("model", ""))


def is_local(role: str) -> bool:
    return model_of(role).startswith(LOCAL_PREFIXES)


def mail_is_private() -> bool:
    return is_local("mail")


def _router():
    if S.router is None:
        import litellm
        from litellm import Router
        litellm.suppress_debug_info = True
        litellm.drop_params = True
        for n in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            logging.getLogger(n).setLevel(logging.CRITICAL)
        model_list, fallbacks = [], []
        for role, r in S.roles.items():
            params = {"model": r["model"], **{k: v for k, v in r.items() if k in ("api_base", "reasoning_effort")}}
            if r["model"].startswith("lm_studio/"):
                params.setdefault("api_key", "lm-studio")
            model_list.append({"model_name": role, "litellm_params": params})
            if r.get("backup") and role != "mail":
                model_list.append({"model_name": f"{role}-backup", "litellm_params": {"model": r["backup"]}})
                fallbacks.append({role: [f"{role}-backup"]})
        S.router = Router(model_list=model_list, fallbacks=fallbacks, num_retries=1, timeout=180)
    return S.router


# ---------------------------------------------------------------- cost ledger
def _db() -> sqlite3.Connection:
    p = S.workspace / ".briefing" / "ledger.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=10)
    con.execute("create table if not exists calls(ts text, task text, role text, model text, in_tok integer, "
                "out_tok integer, cost_usd real, ms integer, ok integer, note text)")
    return con


def month_spend() -> float:
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    with _db() as con:
        (v,) = con.execute("select coalesce(sum(cost_usd),0) from calls where ts >= ?", (start,)).fetchone()
    return float(v)


def month_summary() -> list[tuple]:
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    with _db() as con:
        return con.execute("select task, role, model, count(*), sum(cost_usd) from calls where ts >= ? "
                           "group by task, role, model order by sum(cost_usd) desc", (start,)).fetchall()


def _log(task, role, model, in_tok, out_tok, cost, ms, ok, note="") -> None:
    S.run_cost += cost
    try:
        with _db() as con:
            con.execute("insert into calls values (?,?,?,?,?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"), task, role, model,
                         in_tok, out_tok, cost, ms, int(ok), note[:500]))
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------- web search sources
def resolve_redirect(u: str) -> str:
    """Gemini's Google Search grounding hands out redirect links; the real page is in the Location header."""
    if not any(h in u for h in REDIRECT_HOSTS):
        return u
    if u not in S.resolved:
        import httpx
        try:
            r = httpx.get(u, follow_redirects=False, timeout=5, headers={"User-Agent": "daily-briefing/0.1"})
            S.resolved[u] = r.headers.get("location") or u
        except Exception:  # noqa: BLE001
            S.resolved[u] = u
    return S.resolved[u]


def grounding_sources(resp) -> list[dict]:
    meta = getattr(resp, "vertex_ai_grounding_metadata", None) or (getattr(resp, "_hidden_params", {}) or {}).get(
        "vertex_ai_grounding_metadata") or []
    chunks = []
    for g in meta if isinstance(meta, list) else [meta]:
        for ch in (g or {}).get("groundingChunks") or []:
            w = ch.get("web") or {}
            if w.get("uri"):
                chunks.append(w)
    with ThreadPoolExecutor(max_workers=8) as ex:
        real = list(ex.map(lambda w: resolve_redirect(w["uri"]), chunks))
    return [{"redirect": w["uri"], "url": u, "title": w.get("title") or ""} for w, u in zip(chunks, real)]


def search_urls(resp) -> list[str]:
    """URLs a web search actually returned: Gemini grounding, Claude web search results and citations, OpenAI annotations."""
    out = [s["url"] for s in grounding_sources(resp)]
    try:
        msg = resp.choices[0].message
        psf = getattr(msg, "provider_specific_fields", None) or {}
    except (AttributeError, IndexError):
        return out
    for block in psf.get("web_search_results") or []:
        for r in (block.get("content") if isinstance(block, dict) else None) or []:
            if isinstance(r, dict) and r.get("url"):
                out.append(r["url"])
    for c in psf.get("citations") or []:
        if isinstance(c, dict) and c.get("url"):
            out.append(c["url"])
    for a in getattr(msg, "annotations", None) or []:
        a = a if isinstance(a, dict) else getattr(a, "model_dump", lambda: {})()
        u = (a.get("url_citation") or {}).get("url") or a.get("url")
        if u:
            out.append(u)
    return out


def _host(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").lower()).split("/")[0]


def fix_urls(obj, sources: list[dict]) -> None:
    """Replaces Google redirect links in a parsed answer with the real pages."""
    by_redirect = {s["redirect"]: s["url"] for s in sources}
    if not by_redirect:
        return

    def walk(o):
        if isinstance(o, BaseModel):
            for name in type(o).model_fields:
                v = getattr(o, name)
                if isinstance(v, str) and name in ("url", "link") and v in by_redirect:
                    setattr(o, name, by_redirect[v])
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)


# ---------------------------------------------------------------- calls
def _complete(role: str, messages: list, temperature: float, max_tokens: int, response_format=None, fallback: bool = True,
              extra: dict | None = None):
    kw = {"response_format": response_format} if response_format is not None else {}
    if not fallback:
        kw["fallbacks"] = []
    if is_local(role):
        kw["extra_body"] = LOCAL_EXTRA
        max_tokens = max(max_tokens, LOCAL_MIN_TOKENS)
    return _router().completion(model=role, messages=messages, temperature=temperature, max_tokens=max_tokens, **kw,
                                **(extra or {}))


def _cost(resp) -> float:
    try:
        import litellm
        return float(litellm.completion_cost(completion_response=resp) or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _json_from(text: str) -> str:
    text = text.strip()
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fences:
        return fences[-1].strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return text[start:]


def _schema_text(schema) -> str:
    return "\n\nReply with JSON only, no prose, matching this JSON schema:\n" + json.dumps(schema.model_json_schema())


def _guard(role: str) -> None:
    if role == "mail" and not is_local("mail") and not S.allow_cloud_email:
        raise PrivacyError(f"The mail role points at a cloud model ({model_of('mail')}). Email is only sorted by a local "
                           "model unless settings.yaml says allow_cloud_for_email: true.")


def _gemini_search(role, prompt, *, system, schema, task, max_tokens, temperature, meta):
    m1: dict = {}
    notes = ask(role, prompt + "\n\nAnswer in plain text first: for every item give its title, date, the key facts and "
                "the site it came from.", system=system, task=task, max_tokens=max(max_tokens, 3000),
                temperature=temperature, web=True, meta=m1)
    srcs = m1.get("sources", [])
    lines = "\n".join(f"[{i}] {x['url']} ({x['title']})" for i, x in enumerate(srcs, 1)) or "(none)"
    obj = ask(role, f"{prompt}\n\nRESEARCH NOTES (from a Google search just now)\n{notes}\n\nSOURCES (the only URLs you may "
              f"use)\n{lines}\n\nReturn the JSON answer. Copy every URL exactly from SOURCES; leave out any item that has no "
              "matching source.", system=system, schema=schema, task=task, max_tokens=max_tokens, temperature=temperature)
    fix_urls(obj, srcs)
    if meta is not None:
        meta.setdefault("search_urls", []).extend(x["url"] for x in srcs)
    return obj


def ask(role: str, prompt: str, *, system: str | None = None, schema: type[T] | None = None, task: str = "brief",
        max_tokens: int = 2000, temperature: float = 0.2, web: bool = False, meta: dict | None = None) -> str | T:
    if role not in S.roles:
        raise ValueError(f"Unknown role '{role}'. Known: {', '.join(sorted(S.roles)) or 'none (configure first)'}")
    _guard(role)
    if web and schema is not None and model_of(role).startswith("gemini/"):
        return _gemini_search(role, prompt, system=system, schema=schema, task=task, max_tokens=max_tokens,
                              temperature=temperature, meta=meta)
    local = is_local(role)
    budget = float(os.environ.get("BRIEFING_MONTHLY_BUDGET_USD", "10"))
    if not local and month_spend() >= budget:
        raise BudgetExceeded(f"This month's model spend has reached the ${budget:.2f} cap (BRIEFING_MONTHLY_BUDGET_USD)")
    extra = {"web_search_options": {"search_context_size": "medium"}} if web else None
    sys_text = system or "Use only the information given."
    fallback_ok = role != "mail"
    if schema is None:
        plan = [("plain", fallback_ok)]
    elif local:
        plan = [("prompt", False)]
    else:
        max_tokens = max(max_tokens, SCHEMA_MIN_TOKENS)
        native = (model_of(role).startswith(NATIVE_PREFIXES) and not web and (role, schema.__name__) not in S.native_refused)
        plan = ([("native", False)] if native else []) + [("prompt", False)] + ([("prompt", True)] if fallback_ok else [])

    last: Exception | None = None
    for mode, fallback in plan:
        messages = [{"role": "system", "content": sys_text + (_schema_text(schema) if mode == "prompt" else "")},
                    {"role": "user", "content": prompt + ("\n\n/no_think" if local else "")}]
        mt = max_tokens
        for attempt in (1, 2):
            t0 = time.time()
            try:
                resp = _complete(role, messages, temperature, mt, schema if mode == "native" else None, fallback, extra)
            except Exception as e:  # noqa: BLE001
                _log(task, role, model_of(role), 0, 0, 0.0, int((time.time() - t0) * 1000), False,
                     f"{mode}: {type(e).__name__}: {e}")
                if mode == "native" and type(e).__name__ == "BadRequestError":
                    S.native_refused.add((role, schema.__name__))
                last = e
                break
            ms = int((time.time() - t0) * 1000)
            used = getattr(resp, "model", None) or model_of(role)
            cost = 0.0 if local else _cost(resp)
            u = resp.usage
            text = THINK_RE.sub("", resp.choices[0].message.content or "").strip()
            finish = getattr(resp.choices[0], "finish_reason", None)
            if meta is not None and web:
                meta.setdefault("search_urls", []).extend(search_urls(resp))
                srcs = grounding_sources(resp)
                if srcs:
                    meta.setdefault("sources", []).extend(srcs)
            if schema is None:
                _log(task, role, used, u.prompt_tokens, u.completion_tokens, cost, ms, True)
                return text
            try:
                obj = schema.model_validate_json(_json_from(text))
                fix_urls(obj, grounding_sources(resp))
                _log(task, role, used, u.prompt_tokens, u.completion_tokens, cost, ms, True, mode)
                return obj
            except ValidationError as e:
                _log(task, role, used, u.prompt_tokens, u.completion_tokens, cost, ms, False,
                     f"{mode}: finish={finish} schema: {str(e)[:300]}")
                last = e
                if finish == "length" or not text:
                    mt *= 2
                    continue
                if attempt == 1:
                    messages = messages + [{"role": "assistant", "content": text},
                                           {"role": "user", "content": f"That did not match the schema: {e}. "
                                                                       "Reply again with valid JSON only."}]
    assert last is not None
    raise last
