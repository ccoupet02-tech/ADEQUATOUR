"""Client Notion minimal (requests) + conversion valeurs Python <-> propriétés Notion.

Toutes les écritures respectent `dry_run` : rien n'est envoyé, tout est journalisé.
"""
from __future__ import annotations

import itertools
import logging
import time

import requests

from .util import norm

log = logging.getLogger("padel.notion")

API_ROOT = "https://api.notion.com/v1"
MAX_TEXT = 1900  # limite Notion : 2000 caractères par objet rich_text


class NotionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- propriétés
def rich(text) -> list[dict]:
    text = "" if text is None else str(text)
    chunks = [text[i:i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def make_prop(ptype: str, value):
    """Valeur Python -> payload de propriété Notion (None => propriété ignorée)."""
    if value is None:
        return None
    if ptype in ("title", "rich_text"):
        return {ptype: rich(value)}
    if ptype == "number":
        return {"number": value}
    if ptype == "date":
        iso = value.isoformat() if hasattr(value, "isoformat") else str(value)
        return {"date": {"start": iso}}
    if ptype in ("select", "status"):
        return {ptype: ({"name": str(value)} if value != "" else None)}
    if ptype == "multi_select":
        return {"multi_select": [{"name": str(v)} for v in value]}
    if ptype in ("email", "phone_number", "url"):
        return {ptype: (str(value) if value != "" else None)}
    if ptype == "checkbox":
        return {"checkbox": bool(value)}
    if ptype == "relation":
        return {"relation": [{"id": i} for i in value]}
    raise ValueError(f"Type de propriété non géré : {ptype}")


def read_prop(prop: dict | None):
    """Propriété Notion (réponse API) -> valeur Python simple."""
    if not prop:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return "".join(r.get("plain_text", "") for r in v or [])
    if t in ("number", "checkbox", "email", "phone_number", "url", "created_time"):
        return v
    if t in ("select", "status"):
        return v["name"] if v else None
    if t == "multi_select":
        return [o["name"] for o in v or []]
    if t == "date":
        return v["start"] if v else None
    if t == "relation":
        return [r["id"] for r in v or []]
    if t == "people":
        return [p.get("name") for p in v or []]
    if t == "formula":
        if not v:
            return None
        ft = v.get("type")
        return read_prop({"type": ft, ft: v.get(ft)})
    if t == "rollup":
        if not v:
            return None
        rt = v.get("type")
        if rt == "array":
            return [read_prop(item) for item in v.get("array", [])]
        return read_prop({"type": rt, rt: v.get(rt)})
    return None


class Schema:
    """Table de correspondance clé logique -> (nom de propriété Notion, type)."""

    def __init__(self, fields: dict):
        self.fields = fields

    def prop(self, key: str) -> str:
        return self.fields[key]["prop"]

    def ptype(self, key: str) -> str:
        return self.fields[key]["type"]

    def build(self, values: dict, skip_empty: bool = True) -> dict:
        out = {}
        for key, value in values.items():
            if key not in self.fields:
                raise KeyError(f"Clé inconnue dans le schéma : {key}")
            if value is None or (skip_empty and (value == "" or value == [])):
                continue
            out[self.prop(key)] = make_prop(self.ptype(key), value)
        return out

    def read(self, page: dict, key: str):
        return read_prop(page.get("properties", {}).get(self.prop(key)))

    def filter(self, key: str, op: str, value) -> dict:
        return {"property": self.prop(key), self.ptype(key): {op: value}}


# --------------------------------------------------------------------------- blocs
def heading(text: str, level: int = 2) -> dict:
    t = f"heading_{level}"
    return {"object": "block", "type": t, t: {"rich_text": rich(text)}}


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(text)}}


def callout(text: str, emoji: str = "💡") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rich(text), "icon": {"type": "emoji", "emoji": emoji}}}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def block_text(block: dict) -> str:
    inner = block.get(block.get("type", ""), {}) or {}
    return "".join(r.get("plain_text", "") for r in inner.get("rich_text", []))


# --------------------------------------------------------------------------- client
class NotionClient:
    def __init__(self, token: str, version: str = "2022-06-28", dry_run: bool = False,
                 min_interval: float = 0.35, session: requests.Session | None = None):
        self.token = token
        self.version = version
        self.dry_run = dry_run
        self.min_interval = min_interval
        self.session = session or requests.Session()
        self._last = 0.0
        self._fake = itertools.count(1)

    # -- bas niveau
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Notion-Version": self.version,
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str, json=None, params=None) -> dict:
        url = API_ROOT + path
        for attempt in range(6):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)  # Notion : ~3 requêtes/seconde en moyenne
            self._last = time.monotonic()
            resp = self.session.request(method, url, headers=self._headers(), json=json,
                                        params=params, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                delay = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("Notion %s %s -> %s, nouvelle tentative dans %.1fs",
                            method, path, resp.status_code, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise NotionError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise NotionError(f"{method} {path}: échec après plusieurs tentatives")

    def _dry(self, what: str, **info) -> dict:
        log.info("[dry-run] %s %s", what, info or "")
        return {"id": f"dry-run-{next(self._fake)}", "object": "page", "properties": {}, "url": ""}

    # -- lecture
    def query_database(self, database_id: str, filter: dict | None = None) -> list[dict]:
        results, cursor = [], None
        while True:
            body: dict = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{database_id}/query", json=body)
            results.extend(data["results"])
            if not data.get("has_more"):
                return results
            cursor = data["next_cursor"]

    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def list_children(self, block_id: str) -> list[dict]:
        results, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._request("GET", f"/blocks/{block_id}/children", params=params)
            results.extend(data["results"])
            if not data.get("has_more"):
                return results
            cursor = data["next_cursor"]

    def find_child_database(self, page_id: str, title_prefix: str = "Staff") -> str | None:
        """Base posée directement sur la page, ou dans une sous-page 'Staff'."""
        children = self.list_children(page_id)
        for b in children:
            if b.get("type") == "child_database" and \
                    norm(b["child_database"].get("title", "")).startswith(norm(title_prefix)):
                return b["id"]
        for b in children:
            if b.get("type") == "child_page" and \
                    norm(b["child_page"].get("title", "")) == norm(title_prefix):
                for sub in self.list_children(b["id"]):
                    if sub.get("type") == "child_database":
                        return sub["id"]
        return None

    # -- écriture
    def create_page(self, parent: dict, properties: dict, children: list | None = None) -> dict:
        if self.dry_run:
            return self._dry("create_page", parent=parent, props=list(properties))
        body = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children[:100]
        return self._request("POST", "/pages", json=body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        if self.dry_run:
            return self._dry("update_page", page=page_id, props=list(properties))
        return self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def append_children(self, block_id: str, children: list[dict]) -> None:
        if self.dry_run:
            self._dry("append_children", block=block_id, n=len(children))
            return
        for i in range(0, len(children), 100):
            self._request("PATCH", f"/blocks/{block_id}/children",
                          json={"children": children[i:i + 100]})

    def update_block(self, block_id: str, payload: dict) -> dict:
        if self.dry_run:
            return self._dry("update_block", block=block_id)
        return self._request("PATCH", f"/blocks/{block_id}", json=payload)

    def create_database(self, parent_page_id: str, title: str, properties: dict,
                        inline: bool = True) -> dict:
        if self.dry_run:
            return self._dry("create_database", title=title)
        body = {"parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": properties, "is_inline": inline}
        return self._request("POST", "/databases", json=body)
