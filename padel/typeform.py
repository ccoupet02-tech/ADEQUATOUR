"""Client Typeform + reconnaissance automatique des questions + lecture des réponses."""
from __future__ import annotations

import logging
import time

import requests

from .util import tokens

log = logging.getLogger("padel.typeform")

MULTI = {"partner_name"}  # plusieurs questions possibles (prénom + nom) -> concaténées


class TypeformError(RuntimeError):
    pass


class TypeformClient:
    def __init__(self, token: str, base: str = "https://api.typeform.com",
                 session: requests.Session | None = None):
        self.token, self.base = token, base.rstrip("/")
        self.session = session or requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(5):
            resp = self.session.get(self.base + path, params=params, timeout=30,
                                    headers={"Authorization": f"Bearer {self.token}"})
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise TypeformError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise TypeformError(f"GET {path}: échec après plusieurs tentatives")

    def get_form(self, form_id: str) -> dict:
        return self._get(f"/forms/{form_id}")

    def iter_responses(self, form_id: str, since: str | None = None, page_size: int = 1000):
        before = None
        while True:
            params = {"page_size": page_size, "completed": "true"}
            if since:
                params["since"] = since
            if before:
                params["before"] = before
            items = self._get(f"/forms/{form_id}/responses", params).get("items", [])
            yield from items
            if len(items) < page_size:
                return
            before = items[-1]["token"]


def flatten_fields(fields: list[dict] | None) -> list[dict]:
    """Aplatit les questions, y compris les groupes et les blocs « coordonnées » (contact_info) qui contiennent des sous-questions."""
    out = []
    for f in fields or []:
        children = (f.get("properties") or {}).get("fields")
        if children:
            out.extend(flatten_fields(children))
        else:
            out.append({"id": f["id"], "ref": f.get("ref"), "title": f.get("title", ""),
                        "type": f.get("type")})
    return out


def _starts(toks: list[str], words: list[str]) -> bool:
    return any(t.startswith(w) for t in toks for w in words)


def title_matches(title: str, spec: dict) -> bool:
    toks = tokens(title)
    if not all(_starts(toks, [str(w) for w in group]) for group in spec.get("all_of", [])):
        return False
    return not _starts(toks, [str(w) for w in spec.get("none", [])])


def match_fields(fields: list[dict], matchers: dict, refs: dict | None = None) -> dict[str, list[str]]:
    """clé logique -> liste d'identifiants de questions Typeform."""
    refs = refs or {}
    mapping = {}
    for target, spec in matchers.items():
        if target in refs:
            mapping[target] = [f["id"] for f in fields if refs[target] in (f["id"], f["ref"])]
        else:
            mapping[target] = [f["id"] for f in fields if title_matches(f["title"], spec)]
    return mapping


def answer_value(answer: dict):
    t = answer.get("type")
    if t == "choice":
        c = answer.get("choice") or {}
        return c.get("label") or c.get("other")
    if t == "choices":
        c = answer.get("choices") or {}
        labels = list(c.get("labels") or []) + ([c["other"]] if c.get("other") else [])
        return ", ".join(labels)
    if t in ("text", "email", "phone_number", "url", "date", "file_url", "number", "boolean"):
        return answer.get(t)
    return None


def parse_response(response: dict, mapping: dict[str, list[str]]) -> dict:
    by_id = {a["field"]["id"]: answer_value(a) for a in response.get("answers", [])}
    out = {"submitted_at": response.get("submitted_at", ""),
           "response_id": response.get("response_id") or response.get("token", "")}
    for target, ids in mapping.items():
        values = [str(by_id[i]).strip() for i in ids if by_id.get(i) not in (None, "")]
        values = [v for v in values if v]
        out[target] = (" ".join(values) if target in MULTI else values[0]) if values else None
    return out
