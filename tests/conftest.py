"""Faux client Notion en mémoire + fixtures : permet de tester les flux sans réseau."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from padel.config import load_settings  # noqa: E402
from padel.events import Event  # noqa: E402

ENV = {"NOTION_TOKEN": "x", "NOTION_EVENTS_DB": "db-events", "NOTION_TASKS_DB": "db-tasks",
       "NOTION_PARTICIPANTS_DB": "db-participants"}


def _with_plain(rich_list):
    return [{**r, "plain_text": r["text"]["content"]} for r in rich_list]


def to_response(payload: dict) -> dict:
    out = {}
    for name, val in payload.items():
        t = next(iter(val))
        data = val[t]
        if t in ("title", "rich_text"):
            data = _with_plain(data)
        out[name] = {"type": t, t: data}
    return out


class FakeNotion:
    dry_run = False

    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.blocks: dict[str, list[dict]] = {}
        self.n = 0
        self.writes = 0

    def _id(self, prefix="id"):
        self.n += 1
        return f"{prefix}-{self.n}"

    # -- lecture
    def query_database(self, database_id, filter=None):
        pages = [p for p in self.pages.values() if p["parent"].get("database_id") == database_id]
        if filter:
            prop = filter["property"]
            kind = next(k for k in filter if k != "property")
            op, value = next(iter(filter[kind].items()))
            keep = []
            for p in pages:
                cell = p["properties"].get(prop, {})
                if op == "contains" and kind == "relation":
                    if any(r["id"] == value for r in cell.get("relation", [])):
                        keep.append(p)
                elif op == "on_or_after" and kind == "date":
                    d = (cell.get("date") or {}).get("start")
                    if d and d >= value:
                        keep.append(p)
            pages = keep
        return pages

    def list_children(self, block_id):
        return list(self.blocks.get(block_id, []))

    def find_child_database(self, page_id, title_prefix="Staff"):
        for b in self.blocks.get(page_id, []):
            if b.get("type") == "child_database" and \
                    b["child_database"]["title"].lower().startswith(title_prefix.lower()):
                return b["id"]
        return None

    # -- écriture (en simulation : rien n'est enregistré, comme le vrai client)
    def _stub(self):
        return {"id": "dry-run", "url": "", "properties": {}}

    def create_page(self, parent, properties, children=None):
        if self.dry_run:
            return self._stub()
        pid = self._id("page")
        page = {"id": pid, "url": f"https://notion.so/{pid}", "parent": parent,
                "properties": to_response(properties)}
        self.pages[pid] = page
        self.writes += 1
        if children:
            self.append_children(pid, children)
        return page

    def update_page(self, page_id, properties):
        if self.dry_run:
            return self._stub()
        self.pages[page_id]["properties"].update(to_response(properties))
        self.writes += 1
        return self.pages[page_id]

    def append_children(self, block_id, children):
        if self.dry_run:
            return self._stub()
        for b in children:
            b = dict(b)
            inner = b.get(b["type"], {})
            if "rich_text" in inner:
                b[b["type"]] = {**inner, "rich_text": _with_plain(inner["rich_text"])}
            b["id"] = self._id("block")
            self.blocks.setdefault(block_id, []).append(b)
        self.writes += 1

    def update_block(self, block_id, payload):
        if self.dry_run:
            return self._stub()
        for blocks in self.blocks.values():
            for b in blocks:
                if b["id"] == block_id:
                    key = next(iter(payload))
                    b[key] = {**b.get(key, {}), "rich_text": _with_plain(payload[key]["rich_text"])}
        self.writes += 1

    def create_database(self, parent_page_id, title, properties, inline=True):
        if self.dry_run:
            return self._stub()
        dbid = self._id("db")
        self.blocks.setdefault(parent_page_id, []).append(
            {"id": dbid, "type": "child_database", "child_database": {"title": title}})
        self.writes += 1
        return {"id": dbid}


class FakeTypeform:
    def __init__(self, form: dict, responses: list[dict]):
        self.form, self.responses = form, responses

    def get_form(self, form_id):
        return self.form

    def iter_responses(self, form_id, since=None):
        yield from self.responses


@pytest.fixture
def settings():
    return load_settings(ROOT / "config.yaml", env=dict(ENV), use_dotenv=False)


@pytest.fixture
def notion():
    return FakeNotion()


@pytest.fixture
def event(notion, settings):
    """Un événement DIJON au 1er octobre 2026 dans la base de test."""
    page = notion.create_page({"database_id": "db-events"},
                              settings.schema("events").build({"name": "DIJON", "date": date(2026, 10, 1),
                                                                "venue": "Padel Club"}))
    return Event(id=page["id"], name="DIJON", date=date(2026, 10, 1), venue="Padel Club")
