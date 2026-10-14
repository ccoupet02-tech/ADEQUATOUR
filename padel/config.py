"""Chargement de la configuration : config.yaml (règles métier) + variables d'environnement (secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .notion import NotionClient, Schema
from .util import norm

PROJECT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class TaskTemplate:
    name: str
    category: str | None
    offset: int
    key: str | None = None


@dataclass
class Settings:
    raw: dict
    env: dict = field(default_factory=dict)
    base_dir: Path = PROJECT_DIR

    # -- accès génériques
    def get(self, *path, default=None):
        node = self.raw
        for p in path:
            if not isinstance(node, dict) or p not in node:
                return default
            node = node[p]
        return node

    def require_env(self, name: str) -> str:
        value = self.env.get(name)
        if not value:
            raise SystemExit(f"Variable d'environnement manquante : {name} (voir .env.example)")
        return value

    # -- Notion
    @property
    def events_db(self) -> str:
        return self.require_env("NOTION_EVENTS_DB")

    @property
    def tasks_db(self) -> str:
        return self.require_env("NOTION_TASKS_DB")

    @property
    def participants_db(self) -> str:
        return self.require_env("NOTION_PARTICIPANTS_DB")

    def schema(self, name: str) -> Schema:
        return Schema(self.raw["notion"][name])

    def notion_client(self, dry_run: bool = False) -> NotionClient:
        return NotionClient(self.require_env("NOTION_TOKEN"),
                            version=self.env.get("NOTION_VERSION") or "2022-06-28", dry_run=dry_run)

    def templates(self) -> list[TaskTemplate]:
        return [TaskTemplate(t["name"], t.get("category"), int(t["offset"]), t.get("key"))
                for t in self.raw["task_templates"]]

    def status(self, key: str) -> str:
        return self.raw["statuses"][key]

    # -- Typeform
    def typeform_client(self):
        from .typeform import TypeformClient
        return TypeformClient(self.require_env("TYPEFORM_TOKEN"),
                              base=self.env.get("TYPEFORM_API_BASE") or "https://api.typeform.com")

    def _by_event(self, section: str, event_name: str):
        table = {norm(k): v for k, v in (self.get("typeform", section, default={}) or {}).items()}
        return table.get(norm(event_name))

    def typeform_form(self, event_name: str) -> str | None:
        return self._by_event("forms", event_name)

    def typeform_link(self, event_name: str) -> str:
        return self._by_event("links", event_name) or ""

    @property
    def out_dir(self) -> Path:
        return Path(self.env.get("PADEL_OUT_DIR") or self.base_dir / "out")

    @property
    def brand(self) -> str:
        return self.get("brand", default="Adéquat Padel Tour")


def load_settings(config_path: str | Path | None = None, env: dict | None = None,
                  use_dotenv: bool = True) -> Settings:
    if env is None:
        if use_dotenv:
            from dotenv import load_dotenv
            load_dotenv(PROJECT_DIR / ".env")
        env = dict(os.environ)
    path = Path(config_path) if config_path else PROJECT_DIR / "config.yaml"
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Settings(raw=raw, env=env, base_dir=path.resolve().parent)
