from datetime import date

import pytest

from padel.participants import normalize_size, normalize_type, parse_level, split_name
from padel.tasks import Task, classify, compute_deadline
from padel.typeform import title_matches
from padel.util import base_task_name, norm, norm_id


def test_norm_et_nom_de_tache():
    assert norm("  Réunion d’infos   STAFF ") == "reunion d'infos staff"
    assert base_task_name("Invitation (1)") == "Invitation"
    assert base_task_name("Retour/recap de l’événement (12)") == "Retour/recap de l’événement"
    assert norm_id("38A476D8-3937") == "38a476d83937"


def test_deadline_est_date_moins_offset():
    assert compute_deadline(date(2026, 10, 1), 60) == date(2026, 8, 2)   # « Demande devis » DIJON
    assert compute_deadline(date(2026, 10, 1), 5) == date(2026, 9, 26)   # « Outlook calandar »
    assert compute_deadline(date(2026, 10, 1), -1) == date(2026, 10, 2)  # après l'événement


def test_classement_retard_imminent_a_venir():
    ev, today = date(2026, 10, 1), date(2026, 9, 20)
    tasks = [Task("1", "A", None, 30, "Not started"),   # 1er sept -> retard
             Task("2", "B", None, 10, "Not started"),   # 21 sept -> imminent
             Task("3", "C", None, 3, "Not started"),    # 28 sept -> imminent (8 jours)
             Task("4", "D", None, 1, "Not started"),    # 30 sept -> plus tard
             Task("5", "E", None, 30, "Done"),
             Task("6", "", None, 30, "Not started"),    # ligne vide ignorée
             Task("7", "F", None, None, "Not started")]
    b = classify(tasks, ev, today, "Done", soon_days=9)   # imminent = échéance <= 29 sept
    assert [t.name for t, _ in b["late"]] == ["A"]
    assert [t.name for t, _ in b["soon"]] == ["B", "C"]
    assert [t.name for t, _ in b["later"]] == ["D"]
    assert [t.name for t, _ in b["done"]] == ["E"]
    assert [t.name for t, _ in b["undated"]] == ["F"]


def test_niveau(settings):
    assert parse_level("3 - Intermédiaire", settings) == 3
    assert parse_level("Confirmé", settings) == 3
    assert parse_level("débutant", settings) == 1
    assert parse_level("2,5", settings) == 2.5
    assert parse_level("", settings) is None
    assert parse_level("aucune idée", settings) is None


def test_taille_limitee_aux_options_notion():
    assert normalize_size("M") == "M"
    assert normalize_size("Taille XL") == "XL"
    assert normalize_size("xxl") == "XXL"
    assert normalize_size("XS") is None          # XS n'existe pas dans la colonne Size de Notion
    assert normalize_size("M ou L") is None      # ambigu -> on ne devine pas
    assert normalize_size(None) is None


def test_type(settings):
    assert normalize_type("Je participe au tournoi", settings) == "Tournoi"
    assert normalize_type("Initiation découverte", settings) == "Initiation"
    assert normalize_type("Cocktail uniquement", settings) == "Cocktail"
    assert normalize_type("peut-être", settings) is None


def test_decoupe_nom():
    assert split_name("Jean Dupont de la Tour") == ("Jean", "Dupont de la Tour")
    assert split_name("") == ("", "")


def test_reconnaissance_des_questions(settings):
    spec = settings.get("typeform", "fields")
    ok = lambda title, key: title_matches(title, spec[key])  # noqa: E731
    assert ok("Votre prénom", "prenom") and not ok("Prénom de votre coéquipier", "prenom")
    assert ok("Votre nom", "nom") and not ok("Votre prénom", "nom") and not ok("Nom de votre entreprise", "nom")
    assert ok("Adresse e-mail", "email") and not ok("E-mail de votre coéquipier", "email")
    assert ok("Nom et prénom de votre coéquipier", "partner_name")
    assert ok("E-mail de votre coéquipier", "partner_email")
    assert ok("Quel est votre niveau ?", "level")
    assert ok("Taille de t-shirt", "size")


@pytest.mark.parametrize("name", ["DIJON", "dijon", "Dijon "])
def test_find_event_insensible_a_la_casse(name):
    from padel.events import Event, find_event
    events = [Event("a", "DIJON", None), Event("b", "RENNES", None)]
    assert find_event(events, name).id == "a"
    assert find_event(events, "ren").id == "b"
    with pytest.raises(LookupError):
        find_event(events, "paris")
