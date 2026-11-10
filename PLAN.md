# Plan — Skills Hub : annuaire web des skills Claude sur GitHub

## Contexte
Objectif : tester le modèle Fable en lui faisant construire une application web Python locale qui recense le plus possible de skills Claude publiés sur GitHub. On doit pouvoir rechercher, filtrer et trier ces skills. Chaque skill a une page détaillée (description + avantages clés). Un script de crawl incrémental met la base à jour : un skill déjà connu n'est retraité que s'il a changé.

Choix validés :
- Emplacement : `E:\CLAUDE\skills-hub` (code + base de données)
- Web : FastAPI + Jinja2 + HTMX (pas de framework JS)
- Enrichissement (description, avantages, catégories) : via **Claude Code CLI** (`claude -p`) et/ou **Codex CLI** (`codex exec`) pour consommer les crédits d'abonnement, avec un repli heuristique gratuit si aucun CLI n'est disponible.
- Base : **SQLite** (module `sqlite3` standard, aucun serveur, un seul fichier `data/skills.db`, recherche plein texte FTS5).

## Arborescence
```
E:\CLAUDE\skills-hub\
  pyproject.toml / requirements.txt   # fastapi, uvicorn, jinja2, httpx, python-dotenv, pyyaml
  .env.example                        # GITHUB_TOKEN=, ENRICH_BACKEND=claude|codex|heuristic, DB_PATH=
  data/skills.db
  skillshub/
    config.py        # lecture .env, chemins
    db.py            # schéma, migrations, requêtes (filtres/tri/FTS)
    github.py        # client httpx : REST + ETag, gestion rate limit, retries
    discover.py      # sources de découverte de repos
    parse.py         # lecture SKILL.md (frontmatter YAML + corps), marketplace.json
    enrich.py        # backends claude / codex / heuristic -> JSON normalisé
    crawl.py         # orchestration incrémentale (CLI)
    web/
      app.py         # routes FastAPI
      templates/     # base, index, _results (partiel HTMX), skill, repo, stats
      static/style.css
  scripts/
    crawl.py         # point d'entrée : python scripts/crawl.py [--full] [--limit N] [--enrich-only]
    serve.py         # uvicorn skillshub.web.app:app --reload
  tests/             # pytest : parse, db filtres, détection de changement (fixtures)
```

## Schéma SQLite (léger, pas de copie du contenu complet)
- **repos** : `id` (GitHub node id), `full_name`, `url`, `owner`, `owner_type` (user/org), `is_official` (org `anthropics`), `description`, `stars`, `forks`, `open_issues`, `license`, `topics` (JSON), `default_branch`, `created_at`, `pushed_at`, `archived`, `etag`, `tree_sha`, `last_checked_at`, `source` (comment découvert).
- **skills** : `id`, `repo_id`, `path` (dossier du skill), `name`, `frontmatter_description`, `summary` (FR, 2-4 phrases), `advantages` (JSON, 3-5 puces), `category` (principale), `skill_md_sha` (blob SHA = clé de détection de changement), `skill_md_size` (≈ coût en tokens), `has_scripts`, `has_references`, `file_count`, `install_type` (plugin marketplace / standalone / collection), `compat` (JSON : claude-code, claude.ai, api, codex…), `language` (FR/EN…), `enriched_by` (claude/codex/heuristic), `enriched_at`, `first_seen_at`, `updated_at`.
- **tags** + **skill_tags** : tags d'avantages normalisés (ecom, seo, token-saving, dev, testing, devops, data, design, docs, marketing, finance, sécurité, productivité, writing, research, automation…).
- **star_snapshots** : `repo_id`, `date`, `stars` — un point par crawl, pour le tri « tendance » (étoiles gagnées sur 7/30 jours).
- **crawl_runs** : début, fin, nb repos vus / modifiés / ajoutés, erreurs, quota GitHub restant.
- **skills_fts** (FTS5) sur name, description, summary, advantages, tags.

Pas de stockage du README ni du corps complet de SKILL.md : uniquement les SHA, les métadonnées et le résumé.

## Découverte des skills (discover.py)
Un `GITHUB_TOKEN` (fine-grained, lecture publique) est obligatoire : la recherche de code exige une authentification, et la limite passe à 5000 requêtes/h.
1. **Recherche de code** `filename:SKILL.md` : la source principale. L'API plafonne à 1000 résultats par requête, donc on découpe par tranches de taille `size:0..500`, `size:501..1000`… jusqu'à repasser sous le plafond.
2. **Recherche de repos** par topics : `claude-skills`, `claude-code-skills`, `agent-skills`, `anthropic-skills`, `claude-code-plugin`, `skills` + mots-clés « SKILL.md ».
3. **Marketplaces de plugins** : `filename:marketplace.json path:.claude-plugin`, puis on suit les repos qu'elles référencent.
4. **Listes « awesome »** (ex. awesome-claude-skills) : parse des liens GitHub dans le README.
5. **Seeds** : `anthropics/skills` et une liste manuelle dans `data/seeds.txt`.
Dédoublonnage par `full_name`. Les forks sans commit propre sont ignorés.

## Crawl incrémental (crawl.py)
Pour chaque repo connu ou découvert :
1. `GET /repos/{full_name}` avec `If-None-Match: etag`. Une réponse **304** ne consomme pas de quota : on met seulement à jour `last_checked_at` et on passe au suivant. Sinon on met à jour stars, forks, etc. et on ajoute un `star_snapshot`.
2. Si `pushed_at` n'a pas changé depuis le dernier passage : on s'arrête là, rien à retraiter.
3. Sinon : `GET /git/trees/{branch}?recursive=1`. Si `tree_sha` est identique, on s'arrête. Sinon on liste tous les `SKILL.md` et on compare le **blob SHA** de chacun à `skill_md_sha` :
   - nouveau skill : on télécharge le SKILL.md, on le parse, on l'enrichit et on l'insère ;
   - SHA modifié : on le re-parse, on le ré-enrichit et on le met à jour ;
   - SHA identique : on passe ;
   - skill disparu : on le marque `removed` (pas de suppression dure).
4. Gestion du quota : lecture des en-têtes `X-RateLimit-Remaining`/`Reset`, pause automatique, retries exponentiels, reprise possible (repos traités par ordre de `last_checked_at` le plus ancien).
Options : `--full` (ignore les ETag et SHA), `--limit N`, `--discover-only`, `--enrich-only` (ré-enrichit les skills `heuristic` avec un CLI), `--backend claude|codex|heuristic`.
Un journal est écrit dans `crawl_runs` et affiché en console.

## Enrichissement (enrich.py)
On envoie au modèle le frontmatter, les 4000 premiers caractères de SKILL.md et la description du repo. On attend un **JSON strict** :
`{summary_fr, advantages[3-5], category, tags[], compat[], language}`.
- **Backend claude** : `subprocess.run(["claude", "-p", prompt, "--output-format", "json", "--model", "haiku"])`, puis lecture du champ `result` et validation du JSON.
- **Backend codex** : `codex exec --json "<prompt>"` (ou `-o` vers un fichier), même validation.
- **Backend heuristic** : `description` du frontmatter comme résumé, mots-clés du texte mappés vers des tags (dictionnaire dans `enrich.py`), catégorie déduite des tags.
Le prompt impose une liste fermée de catégories et de tags pour garder des filtres propres. En cas de JSON invalide : un retry, puis repli heuristique. Une concurrence limitée (2-4 sous-processus) ménage les crédits. Le backend se choisit dans `.env` ou par option ; `--backend auto` essaie claude, puis codex, puis heuristic.

## Application web
Routes :
- `/` : recherche plein texte, filtres et tri, liste paginée. HTMX met à jour `_results` sans recharger la page, et l'URL garde l'état (`hx-push-url`) pour pouvoir la partager.
- `/skill/{id}` : nom, lien du repo et du dossier, résumé FR, **avantages en badges** (ecom, SEO, économie de tokens…), catégorie, compatibilité, étoiles, dates, taille du SKILL.md (≈ tokens), présence de scripts, licence, commande d'installation suggérée (`/plugin marketplace add …` ou copie dans `~/.claude/skills/`), autres skills du même repo.
- `/repo/{owner}/{name}` : tous les skills d'un repo.
- `/stats` : nombre de skills par catégorie et par tag, top auteurs, date du dernier crawl.
- `/api/skills` : la même recherche en JSON.

**Filtres** : catégorie, tags d'avantages (multi-sélection), étoiles minimum, mis à jour depuis (7/30/90 j/1 an), officiel Anthropic, compatibilité (Claude Code, claude.ai, API, Codex), type d'installation (plugin/standalone), avec scripts, licence, langue, taille (léger < 2k tokens), exclure les repos archivés, auteur.
**Tris** : étoiles, plus récents (première détection / création), dernière mise à jour, tendance (étoiles gagnées sur 30 j), forks, nom, pertinence (score FTS5 bm25 quand une recherche texte est active).

UI : un seul CSS sobre (light/dark), cartes de résultats avec badges, sans dépendance JS hormis `htmx.min.js` en local.

## Étapes d'implémentation (ordre conseillé pour Fable)
1. Squelette du projet, `config.py`, `db.py` (schéma + FTS5 + triggers de sync), `.env.example`.
2. `github.py` (client, ETag, rate limit), puis `parse.py` avec ses tests.
3. `discover.py` + `crawl.py` incrémental, testé avec `--limit 20 --backend heuristic`.
4. `enrich.py` : les trois backends, puis le test `--enrich-only --limit 5 --backend claude`.
5. Application FastAPI : index, filtres HTMX, page skill, page repo, stats, API.
6. Tests pytest + README (installation, `.env`, commandes, planification facultative via le Planificateur de tâches Windows).

## Vérification
- `pytest` : parse du frontmatter, détection de changement (fixtures avec SHA identique ou modifié, 304), requêtes de filtres et de tri.
- `python scripts/crawl.py --limit 20 --backend heuristic` : la base se remplit et `crawl_runs` est cohérent.
- Relancer immédiatement la même commande : la plupart des repos sortent en 304 ou « inchangés », et aucun ré-enrichissement n'a lieu.
- `python scripts/crawl.py --enrich-only --limit 5 --backend claude` : résumés FR et tags valides.
- `python scripts/serve.py`, puis http://localhost:8000 : tester la recherche, chaque filtre, chaque tri, une page skill, la page stats et `/api/skills`.
- Après le crawl complet : vérifier le volume (on s'attend à plusieurs milliers de SKILL.md) et le quota GitHub restant.
