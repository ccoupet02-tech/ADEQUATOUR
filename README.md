# Adéquat Padel Tour – application de pilotage des événements

L'application est désormais autonome : **base Neon (PostgreSQL) + dashboard Streamlit + moteur d'automatisation**.
Notion n'est plus nécessaire ; l'ancien pipeline Notion (section « Pipeline Notion historique » plus bas) reste dans le dépôt
uniquement pour reprendre les données.

## Mise en route (15 minutes)

1. **Neon** : copiez la *connection string* du projet (elle commence par `postgresql://`).
2. **Streamlit Cloud** → votre application → *Settings → Secrets* (jamais dans le code ni dans une conversation) :
   ```toml
   APP_PASSWORD = "un-mot-de-passe-long"
   DATABASE_URL = "postgresql://...?sslmode=require"
   ```
3. Ouvrez l'application → page **Données** → « Charger les 14 événements connus et leurs 22 tâches », puis importez vos
   exports CSV de Notion (événements, tâches, participants, staff).
4. **GitHub** → *Settings → Secrets and variables → Actions* → ajoutez `DATABASE_URL` : le workflow `padel-engine`
   passe alors toutes les 15 minutes.

## Fonctionnalités

| Domaine | État |
|---|---|
| Base centrale (événements, personnes, inscriptions, tâches, staff, communications, journal, validations, exécutions) | ✅ terminé |
| Menu dynamique : les événements viennent de la base, aucun nom codé en dur | ✅ terminé |
| Fiche événement en 8 sections (vue d'ensemble, participants, staff, logistique, communications, automatisations, historique, bilan) | ✅ terminé |
| Accueil : événements proches, attention requise, e-mails en attente, actions du jour, erreurs, décisions | ✅ terminé |
| Historique des actions, Erreurs et synchronisations, Paramètres et connexions (états réels) | ✅ terminé |
| Moteur de workflow : exécutions tracées, erreurs isolées par événement, reprise, passage en « À valider » après 3 échecs, activation par workflow | ✅ terminé (mode aperçu) |
| Invitations et relances : aperçus, anti-doublons, relance annulée dès qu'une réponse est saisie | ✅ terminé (mode aperçu) |
| Import des exports CSV de Notion, rejouable | ✅ terminé |
| Poules, planning, PDF, t-shirts et repas | ✅ branché sur la base |
| Envoi réel des mails | ⏳ non branché : marquage « envoyé » manuel |
| Lecture des e-mails et des réponses (Outlook / Microsoft Graph) | ⏳ dépend des autorisations de votre DSI |
| Messages Teams | ⏳ idem |
| Typeform en direct | ⏳ code existant (`padel/typeform.py`) à rebrancher sur la base |
| Classification des réponses par Claude | ⏳ après Outlook |
| Comptes séparés par utilisateur | ❌ un seul mot de passe |

## Garde-fous

- Aucune connexion n'est affichée « OK » sans un appel réel de vérification ; Outlook, Teams et Claude sont signalés « non fonctionnels ».
- Le moteur **n'envoie rien** : il prépare des aperçus relus dans « Automatisations ».
- Une même communication n'est jamais préparée deux fois (clé d'idempotence en base, atomique).
- `python -m padel.engine --dry-run` montre ce qui serait préparé sans rien écrire.
- Les secrets ne sont que dans les Secrets Streamlit / GitHub ; `.env` et `data/` ne sont jamais versionnés.

## Commandes

```bash
pip install -r requirements-dev.txt
python -m padel.dbcli seed                          # jeu de départ
python -m padel.dbcli import-tasks taches.csv       # exports Notion
python -m padel.engine --dry-run                    # simulation du moteur
streamlit run streamlit_app.py                      # dashboard local (base SQLite locale si DATABASE_URL absente)
python -m pytest -q
```

---

# Pipeline Notion historique

Un seul outil en ligne de commande (Python 3.11+) qui fait tourner la logistique des événements autour de votre Notion :

| Besoin | Commande |
|---|---|
| Créer un événement (page, base Staff, 22 tâches avec offsets) | `create-event` |
| Compléter les tâches manquantes d'un ou de tous les événements | `sync-tasks` |
| Importer les inscrits Typeform (solo ou binôme, taille, niveau, type) | `sync-participants` |
| Importer un CSV de participants | `import-csv` |
| Calculer équipes, poules équilibrées, planning des terrains + PDF (planning, poules, feuilles d'arbitre, diplômes, étiquettes Avery L7160) | `documents` |
| T-shirts à commander (marge 10 %), repas, hôtels/transport du staff | `logistics` |
| Mails : invitation, relance, récap KAM, retour d'événement | `mail` |
| Retards et échéances imminentes (webhook Slack/Teams ou mail) | `digest` |
| Synthèse « J-x, inscrits, tâches en retard » sur la page de chaque événement | `refresh` |
| Tout cela chaque matin | `daily` |
| Repérer les incohérences de la to-do avant d'automatiser | `audit` |

## ⚠️ À lire avant la première utilisation

- **Ce code a été testé avec un faux Notion et un faux Typeform (39 tests), pas contre vos vrais comptes** : l'environnement où je l'ai écrit n'a pas accès à leurs API. Les noms de colonnes viennent de votre espace Notion réel (relevés le 14/10/2026), mais le premier passage réel peut révéler des détails. Commencez toujours **sans `--apply`**.
- **Garde-fous** : sans `--apply` rien n'est écrit dans Notion ; sans `--send` aucun mail ni notification ne part (les mails sont écrits en `.eml` dans `out/<événement>/mails/` pour relecture) ; `--send` exige `--apply`.
- Les mails ne sont **jamais** envoyés par `daily`. Ils partent uniquement via `mail <type> --apply --send`.

## Installation (10 minutes)

```bash
pip install -r requirements.txt
cp .env.example .env        # puis remplir
```

1. **Notion** : créez une intégration (notion.so/profile/integrations), copiez son token dans `NOTION_TOKEN`. Puis, dans Notion, ouvrez chaque base (ADEQUAT PADEL TOUR, STANDARD PADEL TOUR, Participants) → menu « … » → **Connexions** → ajoutez l'intégration. Les identifiants de base déjà présents dans `.env.example` sont ceux de votre espace.
2. **Typeform** (facultatif) : token personnel (typeform.com → Compte → Jetons personnels), puis dans `config.yaml` section `typeform.forms`, associez chaque événement à l'identifiant de son formulaire (la fin de l'URL du formulaire).
3. Vérifiez :

```bash
python -m padel list-events
python -m padel audit
python -m padel typeform-fields --event DIJON   # quelles questions sont reconnues ?
```

## Interface web (Streamlit, sans rien installer)

`streamlit_app.py` est une page web protégée par mot de passe qui appelle les mêmes commandes. La case « Écrire dans Notion » est décochée par défaut (simulation) et **aucun mail n'est jamais envoyé depuis l'interface** (aperçus `.eml` à télécharger).

1. Allez sur share.streamlit.io, connectez GitHub, puis **New app** : dépôt `ccoupet02-tech/ADEQUATOUR`, branche `main`, fichier `streamlit_app.py`.
2. **Advanced settings → Secrets** : collez le contenu de `.streamlit/secrets.toml.example` en remplaçant les valeurs (mot de passe, token Notion).
3. Mettez l'application en **privée** (Share → réservée aux personnes invitées) en plus du mot de passe.

## Utilisation courante

```bash
# Nouvel événement (simulation, puis réel)
python -m padel create-event LILLE 2027-01-21 --venue "Padel Club Lille" --dr "Prénom Nom"
python -m padel create-event LILLE 2027-01-21 --venue "Padel Club Lille" --dr "Prénom Nom" --apply

# Participants depuis Typeform
python -m padel sync-participants --event DIJON --apply

# J-10 : équipes, poules, planning, PDF, et écriture des poules dans Notion
python -m padel documents --event DIJON --courts 4 --write-poules --apply

# J-20 : invitations (relire les .eml, puis envoyer)
python -m padel mail invitation --event DIJON
python -m padel mail invitation --event DIJON --apply --send

# Chaque matin (voir .github/workflows/daily.yml pour l'automatiser gratuitement)
python -m padel daily --apply
```

Après un envoi réel, la tâche correspondante passe à « Done » (invitation, relance, récap KAM, retour) ; `documents --apply` passe « Définir les poules », « Points arbitres… » et « Imprimer étiquettes » à « In progress ». Une tâche déjà « Done » n'est jamais rétrogradée.

## Ce que le pipeline décide à votre place (à valider)

- **Équipes** : joueurs partageant la même valeur « Team » ; les autres sont appariés par niveau voisin ; un joueur seul reste en équipe incomplète (signalée).
- **Niveau** : un nombre en tête (« 3 - Confirmé ») ou un mot (débutant 1, intermédiaire 2, confirmé 3, expert 4) ; réglable dans `config.yaml` → `levels`.
- **Poules** : 4 équipes visées, jamais de poule de 2, répartition « serpentin » pour équilibrer les forces. Planning : 20 min/match, 4 terrains, une équipe évite de jouer deux créneaux de suite.
- **Qui joue le tournoi** : Type = Tournoi (ou vide) ; Initiation et Cocktail exclus ; « Refusée » et « Erreur mail » exclus.
- **Repas / t-shirts** : les invités « Cocktail » seuls sont exclus (hypothèse à confirmer). Tailles : seulement S à XXL, car la colonne Notion n'a pas de XS ; une réponse « XS » est laissée vide plutôt que remplacée.
- **ROI 6 mois** n'est pas dans le référentiel de tâches : c'est une tâche liée à plusieurs événements dans votre to-do, qui ne se répète pas par événement.

## Points de votre to-do à trancher (remontés par `audit`)

- « Demande devis » : 60 jours partout, **40 à Lille**. « Commande des t-shirt et goodies » : 20 jours partout, **15 à Orléans**. Le référentiel (`config.yaml` → `task_templates`) utilise 60 et 20.
- 116 tâches portent un suffixe « (1) », « (2) » : le pipeline les reconnaît, mais renommez-les.
- 3 lignes vides à supprimer ; PACA et PÉRIGUEUX n'ont aucune tâche (volontairement ignorés par `sync-tasks --all` car passés).

## Ce qui n'est pas automatisé (et pourquoi)

Réservations (hôtel, transport, repas, devis), groupe WhatsApp, acceptations Outlook (nécessiteraient les droits Microsoft Graph de votre DSI), lecture du PDF de devis. Le pipeline prépare les listes et les chiffres ; la décision et la réservation restent humaines.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Structure

`padel/` (code) · `config.yaml` (règles métier, schéma Notion, référentiel de tâches) · `templates/mail/*.j2` (textes des mails, modifiables) · `.env` (secrets, jamais versionné) · `out/` (PDF, CSV, .eml générés).

Si vous renommez une colonne dans Notion, modifiez-la uniquement dans `config.yaml`, section `notion`.
