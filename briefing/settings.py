"""Your settings, read from settings.yaml in the workspace. Everything personal lives here, never in the code."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Topic(BaseModel):
    label: str
    brief: str = Field(description="what to search for, in plain words")
    days: int | None = Field(default=None, description="only items from the last N days (default: news.days)")
    context: str = Field(default="low", description="search breadth: low, medium or high")


class News(BaseModel):
    per_topic: int = 3
    max_items: int = 10
    days: int = 3
    topics: dict[str, Topic] = Field(default_factory=dict)


class Imap(BaseModel):
    name: str = Field(description="a label for this mailbox, e.g. 'work'")
    host: str
    port: int = 993
    user: str
    password_env: str = Field(description="the environment variable in .env that holds the (app) password")
    folders: list[str] = Field(default_factory=lambda: ["INBOX"])
    webmail: str | None = Field(default=None, description="optional link to open this mailbox in a browser")


class Mail(BaseModel):
    hours: int = Field(default=24, description="the longest look-back; normally the brief reads only mail since the last one")
    cap_per_account: int = 120
    allow_cloud_for_email: bool = Field(default=False, description="true lets a cloud model read your email (not recommended)")
    imap: list[Imap] = Field(default_factory=list)


class Delivery(BaseModel):
    telegram: bool = True        # used when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are in .env
    ntfy: bool = True            # used when NTFY_TOPIC is in .env
    whatsapp: bool = False       # CallMeBot (free, personal use): WHATSAPP_PHONE and CALLMEBOT_APIKEY in .env


class Settings(BaseModel):
    name: str = Field(default="", description="your first name, for the greeting")
    timezone: str = "Europe/Berlin"
    about_me: str = Field(default="", description="one paragraph: who you are and what matters to you now")
    watch: list[str] = Field(default_factory=list, description="organisations whose emails always count")
    calendar_days: int = 2
    mail: Mail = Field(default_factory=Mail)
    news: News = Field(default_factory=News)
    delivery: Delivery = Field(default_factory=Delivery)


def load(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
