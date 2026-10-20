"""The workspace: one folder holding your settings, sign-ins, briefs and to-dos. Nothing leaves it except the calls you
configure (web search for news; email only to your local model)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from briefing import settings as S


@dataclass
class Workspace:
    root: Path

    @classmethod
    def find(cls, path: str | None = None) -> "Workspace":
        root = Path(path or os.environ.get("BRIEFING_HOME") or Path.cwd()).expanduser().resolve()
        try:
            from dotenv import load_dotenv
            load_dotenv(root / ".env", override=False)
        except ImportError:
            pass
        return cls(root)

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.yaml"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def google(self) -> Path:
        return self.root / "google"

    @property
    def briefs(self) -> Path:
        return self.root / "briefs"

    def settings(self) -> S.Settings:
        return S.load(self.settings_file)
