"""Staff : lecture d'un Google Sheet partagé par lien, synchronisation, compteurs, modèle."""
from datetime import date

import pytest

from padel import staff_sheet, store
from padel.config import load_settings
from padel.db import init_db, make_engine
from padel.staff_template import staff_template_xlsx

CSV = ('ADÉQUAT PADEL TOUR · STAFF BORDEAUX,,,,\n"19/11/2026 — Merci",,,,\n,,,,\n'
       'Nom,Prénom,Taille polo,Déjeuner avant l’événement ?,"Rôle (cf onglet ""ROLE"")"\n'
       'Martin,Julie,M,Oui,Field lead\nDurand,Paul,l,non,Reception\nRoy,Ana,XL,oui,Field lead (préférence)\n,,,,\nVidal,Sam,?,,\n')


@pytest.fixture()
def env(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    init_db(eng)
    with eng.begin() as cx:
        eid, _ = store.upsert_event(cx, "BORDEAUX", date=date(2026, 11, 19))
    return eng, eid


def test_url_parsing_and_export_url():
    u = "https://docs.google.com/spreadsheets/d/19f4Addk_BATn2_2ftuLQNAW2-QmOXlnGhwN6dvnK-64/edit?usp=sharing"
    assert staff_sheet.parse_sheet_url(u) == ("19f4Addk_BATn2_2ftuLQNAW2-QmOXlnGhwN6dvnK-64", None)
    assert staff_sheet.export_url(u + "#gid=123").endswith("export?format=csv&gid=123")
    with pytest.raises(staff_sheet.SheetError):
        staff_sheet.parse_sheet_url("https://exemple.fr/x")


def test_parse_skips_banner_and_normalizes():
    members, warns = staff_sheet.parse_staff_csv(CSV)
    assert [m["nom"] for m in members] == ["Martin", "Durand", "Roy", "Vidal"]
    assert members[1]["shirt_size"] == "L" and members[1]["repas"] == "Non" and members[0]["repas"] == "Oui"
    assert any("taille" in w for w in warns)


def test_sync_adds_updates_removes_and_keeps_manual(env):
    eng, eid = env
    with eng.begin() as cx:
        cx.execute(staff_sheet.insert(staff_sheet.staff).values(event_id=eid, nom="Manuel", prenom="Marc", source=""))
        r = staff_sheet.sync_staff(cx, eid, CSV, dry_run=True)
        assert r["added"] == 4 and store.list_staff(cx, eid)[0]["nom"] == "Manuel" and len(store.list_staff(cx, eid)) == 1
        staff_sheet.sync_staff(cx, eid, CSV)
        assert {x["nom"] for x in store.list_staff(cx, eid)} == {"Manuel", "Martin", "Durand", "Roy", "Vidal"}
        again = staff_sheet.sync_staff(cx, eid, CSV)
        assert (again["added"], again["updated"], again["removed"]) == (0, 0, 0)
        newer = CSV.replace("Durand,Paul,l,non,Reception", "Durand,Paul,L,oui,Shuttle").replace("Vidal,Sam,?,,\n", "")
        r = staff_sheet.sync_staff(cx, eid, newer)
        assert (r["updated"], r["removed"]) == (1, 1)
        rows = {x["nom"]: x for x in store.list_staff(cx, eid)}
    assert rows["Durand"]["role"] == "Shuttle" and "Vidal" not in rows and "Manuel" in rows


def test_summary_counts_roles_lunch_sizes():
    roles = load_settings().get("staff", "roles")
    members, _ = staff_sheet.parse_staff_csv(CSV)
    sm = store.staff_summary(members, roles)
    assert sm["total"] == 4 and sm["lunch"] == 2 and sm["by_role"]["Field lead"] == 2 and sm["by_role"]["Reception"] == 1
    assert sm["unassigned"] == 1 and sm["sizes"]["M"] == 1


def test_html_response_means_not_shared(monkeypatch):
    class R:
        status_code, url, headers, content = 200, "https://accounts.google.com/ServiceLogin", {"content-type": "text/html"}, b""

    class S:
        def get(self, *a, **k):
            return R()
    with pytest.raises(staff_sheet.SheetError, match="Tous les utilisateurs"):
        staff_sheet.fetch_csv("https://docs.google.com/spreadsheets/d/abc/edit", S())


def test_template_has_three_tabs_and_headers():
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(staff_template_xlsx("Bordeaux", date(2026, 11, 19), "Salle", load_settings().get("staff", "roles"))))
    assert wb.sheetnames == ["STAFF", "ROLE", "SYNTHÈSE"]
    members, _ = staff_sheet.parse_staff_csv("\n".join(",".join(str(c or "") for c in row) for row in wb["STAFF"].iter_rows(max_row=6, values_only=True)))
    assert members == []                       # modèle vide : en-têtes reconnues, aucune personne


# ----------------------------------------------------------------------------- formulaire Typeform staff unique
from tests.test_signups import FakeTF  # noqa: E402


class StaffForm:
    form = {"fields": [
        {"id": "ev", "type": "dropdown", "title": "Quel événement ?"},
        {"id": "g", "type": "contact_info", "title": "Vos coordonnées", "properties": {"fields": [
            {"id": "fn", "type": "short_text", "title": "First name"}, {"id": "ln", "type": "short_text", "title": "Last name"}]}},
        {"id": "sz", "type": "multiple_choice", "title": "Taille polo"},
        {"id": "lu", "type": "multiple_choice", "title": "Déjeuner avant l’événement ?"},
        {"id": "ro", "type": "short_text", "title": "Rôle souhaité"}]}

    def __init__(self, responses):
        self.responses = responses

    def get_form(self, _):
        return self.form

    def iter_responses(self, _):
        yield from self.responses


def _r(when, event, first, last, size="M", lunch="Oui", role="Field lead"):
    t = lambda i, v: {"field": {"id": i}, "type": "text", "text": v}  # noqa: E731
    c = lambda i, v: {"field": {"id": i}, "type": "choice", "choice": {"label": v}}  # noqa: E731
    return {"submitted_at": when, "answers": [c("ev", event), t("fn", first), t("ln", last), c("sz", size), c("lu", lunch), t("ro", role)]}


def test_one_staff_form_routes_each_answer_to_its_event(env):
    eng, bdx = env
    with eng.begin() as cx:
        lyon, _ = store.upsert_event(cx, "LYON", date=date(2026, 12, 3))
        resp = [_r("2026-10-01T08:00:00Z", "Bordeaux", "Julie", "Martin"),
                _r("2026-10-02T08:00:00Z", "LYON", "Paul", "Durand", "L", "Non", "Reception"),
                _r("2026-10-03T08:00:00Z", "Bordeaux - 19 nov", "Ana", "Roy", "S"),
                _r("2026-10-04T08:00:00Z", "Bordeaux", "Julie", "Martin", "XL", "Non", "Shuttle"),   # réponse plus récente : elle remplace
                _r("2026-10-05T08:00:00Z", "Marseille", "Zoe", "Inconnue")]
        cache = {}
        for eid, name in ((bdx, "BORDEAUX"), (lyon, "LYON")):
            ev = store.get_event(cx, eid)
            r = staff_sheet.sync_staff_typeform(cx, ev, StaffForm(resp), "F1", cache=cache)
            assert r["unknown"] == ["Marseille"]
        b = {x["nom"]: x for x in store.list_staff(cx, bdx)}
        l = {x["nom"]: x for x in store.list_staff(cx, lyon)}
    assert set(b) == {"Martin", "Roy"} and b["Martin"]["role"] == "Shuttle" and b["Martin"]["shirt_size"] == "XL" and b["Martin"]["repas"] == "Non"
    assert set(l) == {"Durand"} and l["Durand"]["role"] == "Reception"


def test_staff_form_removal_and_manual_members_are_respected(env):
    eng, bdx = env
    with eng.begin() as cx:
        cx.execute(staff_sheet.insert(staff_sheet.staff).values(event_id=bdx, nom="Manuel", prenom="Marc", source=""))
        ev = store.get_event(cx, bdx)
        two = [_r("2026-10-01T08:00:00Z", "Bordeaux", "Julie", "Martin"), _r("2026-10-02T08:00:00Z", "Bordeaux", "Ana", "Roy")]
        staff_sheet.sync_staff_typeform(cx, ev, StaffForm(two), "F1")
        assert len(store.list_staff(cx, bdx)) == 3
        r = staff_sheet.sync_staff_typeform(cx, ev, StaffForm(two[:1]), "F1")     # Ana supprimée dans Typeform
        assert r["removed"] == 1
        assert {x["nom"] for x in store.list_staff(cx, bdx)} == {"Martin", "Manuel"}


def test_engine_runs_staff_typeform_only_with_token_and_form(env, monkeypatch):
    from padel import engine
    eng, bdx = env
    s = load_settings()
    resp = [_r("2026-10-01T08:00:00Z", "Bordeaux", "Julie", "Martin")]
    monkeypatch.setattr(type(s), "typeform_client", lambda self: StaffForm(resp))
    with eng.begin() as cx:
        store.set_setting(cx, "staff_typeform_id", "F1")
    engine.run(eng, s, today=date(2026, 10, 20))
    with eng.connect() as cx:
        assert store.list_staff(cx, bdx) == []                 # pas de jeton : rien n'est simulé
    s.env["TYPEFORM_TOKEN"] = "tfp_test"
    res = engine.run(eng, s, today=date(2026, 10, 20))
    with eng.connect() as cx:
        assert [x["nom"] for x in store.list_staff(cx, bdx)] == ["Martin"]
    assert res.errors == 0


def test_staff_diagnostic_has_no_personal_data():
    lines = "\n".join(staff_sheet.staff_typeform_diagnostic(StaffForm.form, [_r("2026-10-01T08:00:00Z", "Bordeaux", "Julie", "Martin")], ["BORDEAUX"]))
    assert "Julie" not in lines and "Martin" not in lines and "BORDEAUX" in lines and "event" in lines
