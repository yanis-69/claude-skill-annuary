# Skills Hub

Annuaire web local des skills Claude (`SKILL.md`) publiés sur GitHub : recherche plein texte, filtres, tris, page détaillée par skill, et un crawl incrémental qui ne retraite que ce qui a changé.

- Web : FastAPI + Jinja2 + HTMX (aucun framework JS, `htmx.min.js` servi en local)
- Base : SQLite (`data/skills.db`), recherche plein texte FTS5
- Enrichissement (résumé FR, avantages, catégorie, tags) : Claude Code CLI, Codex CLI, ou heuristique gratuite

## Installation

Prérequis : Python 3.10 ou plus récent, Git.

Windows (PowerShell) :

```powershell
git clone https://github.com/yanis-69/claude-skill-annuray.git
cd claude-skill-annuray
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```

macOS / Linux :

```bash
git clone https://github.com/yanis-69/claude-skill-annuray.git
cd claude-skill-annuray
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Ensuite :

1. Renseigner `GITHUB_TOKEN` dans `.env` (voir ci-dessous).
2. Remplir la base : `.venv\Scripts\python scripts\crawl.py --limit 20 --backend heuristic`
3. Lancer le site : `.venv\Scripts\python scripts\serve.py`, puis ouvrir http://localhost:8000

Sous macOS / Linux, remplacer `.venv\Scripts\python` par `.venv/bin/python` et `\` par `/` dans les commandes de ce document.

La base `data/skills.db` n'est pas versionnée : chaque installation la construit avec son propre crawl.

## Configuration (`.env`)

| Variable | Rôle |
|---|---|
| `GITHUB_TOKEN` | Jeton GitHub fine-grained, accès en lecture aux dépôts publics. Sans jeton : 60 requêtes/heure et pas de recherche de code, donc une découverte très partielle. |
| `ENRICH_BACKEND` | `auto` (défaut), `claude`, `codex` ou `heuristic`. `auto` essaie `claude`, puis `codex`, puis `heuristic`. |
| `DB_PATH` | Chemin de la base (défaut `data/skills.db`). |
| `ENRICH_CONCURRENCY` | Sous-processus CLI en parallèle (défaut 3). |
| `CLAUDE_MODEL` | Modèle passé à `claude --model` (défaut `haiku`). |
| `MAX_SKILLS_PER_REPO` | Skills traités par dépôt et par passage (défaut 1000) ; le reste est repris au crawl suivant. |

Créer le jeton : GitHub → Settings → Developer settings → Fine-grained tokens → « Public repositories (read-only) ».

## Commandes

```powershell
# Premier essai : 20 dépôts, sans consommer de crédits
.venv\Scripts\python scripts\crawl.py --limit 20 --backend heuristic

# Crawl complet incrémental (découverte + mise à jour)
.venv\Scripts\python scripts\crawl.py

# Remplacer les résumés heuristiques par ceux de Claude, 5 skills à la fois
.venv\Scripts\python scripts\crawl.py --enrich-only --limit 5 --backend claude

# Serveur web : http://localhost:8000
.venv\Scripts\python scripts\serve.py

# Tests
.venv\Scripts\python -m pytest
```

Options de `scripts/crawl.py` :

| Option | Effet |
|---|---|
| `--limit N` | Nombre maximal de dépôts (ou de skills avec `--enrich-only`). |
| `--full` | Ignore les ETag et les SHA : tout est retéléchargé et ré-enrichi. |
| `--discover-only` | Remplit la file des dépôts à crawler, sans les traiter. |
| `--no-discover` | Saute la découverte. |
| `--discover-minutes MIN` | Temps accordé à la recherche de code (défaut 20, `0` = sans limite). Elle reprend au crawl suivant là où elle s'est arrêtée. |
| `--repo OWNER/NOM` | Crawle uniquement ce dépôt (répétable). |
| `--enrich-only` | Ré-enrichit les skills restés en `heuristic` ou sans mode d'emploi. |
| `--backend` | `auto`, `claude`, `codex`, `heuristic`. |

Dépôts à inclure d'office : un `owner/repo` par ligne dans `data/seeds.txt`.

## Fonctionnement du crawl incrémental

Pour chaque dépôt :

1. `GET /repos/{dépôt}` avec `If-None-Match`. Réponse 304 : rien à faire.
2. `pushed_at` inchangé : seules les métadonnées (étoiles…) sont mises à jour.
3. Sinon lecture de l'arbre Git. `tree_sha` identique : arrêt.
4. Sinon comparaison du blob SHA de chaque `SKILL.md` avec la base : nouveau → ajouté, SHA modifié → re-parsé et ré-enrichi, SHA identique → ignoré, disparu → marqué `removed` (jamais supprimé).

L'ETag et le `tree_sha` ne sont mémorisés que si tout le dépôt a été traité : un crawl interrompu reprend au passage suivant. Les dépôts découverts mais pas encore crawlés attendent dans la table `candidates`, puis les dépôts connus sont revus du plus anciennement vérifié au plus récent.

Le contenu des `SKILL.md` est téléchargé depuis `raw.githubusercontent.com`, hors quota API. La base ne stocke ni README ni corps de `SKILL.md` : uniquement les SHA, les métadonnées et le résumé.

Sources de découverte (`skillshub/discover.py`) : seeds, recherche de dépôts par topics, listes « awesome », marketplaces de plugins (`.claude-plugin/marketplace.json`), et recherche de code `filename:SKILL.md` découpée par tranches de taille pour contourner le plafond de 1000 résultats. Les deux dernières exigent un jeton.

La recherche de code est limitée par GitHub à 10 requêtes par minute : couvrir toutes les tailles de fichier prend plusieurs heures. Chaque crawl y consacre donc `--discover-minutes`, enregistre les dépôts trouvés au fur et à mesure, mémorise où il s'est arrêté, puis passe au crawl. Les crawls suivants continuent la découverte.

## Enrichissement

Le modèle reçoit le frontmatter, les 4000 premiers caractères du `SKILL.md` et la description du dépôt. Il doit répondre par un JSON strict `{summary_fr, advantages, category, tags, compat, language, usage_fr, example}`, avec des listes fermées de catégories et de tags (voir `skillshub/enrich.py`). JSON invalide : un nouvel essai, puis repli heuristique.

- `claude` : `claude -p --output-format json --model haiku --max-turns 1`, prompt sur stdin.
- `codex` : `codex exec --sandbox read-only -o <fichier> -`, prompt sur stdin.
- `heuristic` : description du frontmatter comme résumé, tags par mots-clés.

Chaque skill reçoit aussi un mode d'emploi (`usage`) et, si possible, un exemple (`example`), stockés en base et affichés sur sa page. Sans modèle, ils sont extraits du `SKILL.md` (`skillshub/usage.py`) : commande `/nom`, phrase « Use when… » de la description, section Usage ou Quick start, puis bloc de code d'une section Examples ou phrases de déclenchement citées. Un modèle qui omet ces champs est complété de la même façon.

Un `SKILL.md` est un contenu non fiable. Les CLI sont donc lancés dans un dossier temporaire vide, sans outils (Claude) ou en lecture seule (Codex), et le prompt demande d'ignorer toute instruction présente dans le fichier.

## Application web

| Route | Contenu |
|---|---|
| `/` | Recherche, filtres, tris, pagination. HTMX met à jour les résultats et l'URL reste partageable. |
| `/skill/{id}` | Résumé, avantages, mode d'emploi et exemple, tags, compatibilité, étoiles, dates, taille en tokens, installation suggérée, autres skills du dépôt. |
| `/repo/{owner}/{nom}` | Tous les skills d'un dépôt. |
| `/stats` | Skills par catégorie et par tag, top auteurs, dernier crawl. |
| `/api/skills` | La même recherche en JSON (mêmes paramètres, plus `per_page`). |

Le tri « tendance » compare les étoiles actuelles au relevé d'il y a 30 jours (table `star_snapshots`, un point par crawl) : il devient utile après quelques semaines de crawls réguliers.

## Planification (facultatif)

Crawl quotidien à 6 h avec le Planificateur de tâches Windows :

```powershell
schtasks /Create /TN "SkillsHubCrawl" /SC DAILY /ST 06:00 /TR "cmd /c cd /d C:\chemin\vers\claude-skill-annuray && .venv\Scripts\python.exe scripts\crawl.py >> data\crawl.log 2>&1"
```

Suppression : `schtasks /Delete /TN "SkillsHubCrawl" /F`.

## Arborescence

```
skillshub/
  config.py     lecture du .env, chemins
  db.py         schéma, migrations, requêtes (filtres, tris, FTS5)
  github.py     client httpx : ETag, quota, retries
  discover.py   sources de découverte
  parse.py      SKILL.md, marketplace.json, arbre Git
  enrich.py     backends claude / codex / heuristic
  usage.py      mode d'emploi et exemple extraits du SKILL.md
  crawl.py      orchestration incrémentale
  web/          app FastAPI, templates Jinja2, CSS, htmx
scripts/        crawl.py, serve.py
tests/          pytest
data/           skills.db, seeds.txt
```
