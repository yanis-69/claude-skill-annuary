import json

from skillshub.parse import (extract_github_repos, find_skills_in_tree, install_type,
                             is_plugin_repo, parse_marketplace, parse_skill_md)


def test_frontmatter_standard():
    text = "---\nname: pdf-tools\ndescription: Lit et remplit des PDF.\nlicense: MIT\n---\n\n# PDF\n\nCorps."
    p = parse_skill_md(text)
    assert p["name"] == "pdf-tools"
    assert p["description"] == "Lit et remplit des PDF."
    assert p["frontmatter"]["license"] == "MIT"
    assert p["body"].startswith("# PDF")


def test_frontmatter_multiligne_et_crlf():
    text = "---\r\nname: x\r\ndescription: >\r\n  Première ligne\r\n  seconde ligne\r\n---\r\nCorps"
    p = parse_skill_md(text)
    assert p["description"] == "Première ligne seconde ligne"
    assert p["body"] == "Corps"


def test_yaml_invalide_repli_ligne_a_ligne():
    # Un « : » non échappé dans la description rend le YAML invalide.
    text = "---\nname: seo-audit\ndescription: Use when: the user asks for SEO: audits\n---\nCorps"
    p = parse_skill_md(text)
    assert p["name"] == "seo-audit"
    assert p["description"].startswith("Use when: the user asks")


def test_sans_frontmatter():
    p = parse_skill_md("# Mon Skill\n\nFait quelque chose d'utile.\n\n## Usage\n", "dossier")
    assert p["name"] == "Mon Skill"
    assert p["description"] == "Fait quelque chose d'utile."
    assert parse_skill_md("", "dossier")["name"] == "dossier"


def test_frontmatter_non_dict_et_cles_majuscules():
    assert parse_skill_md("---\n- a\n- b\n---\ntexte", "fb")["name"] == "fb"
    assert parse_skill_md("---\nName: Abc\n---\n")["name"] == "Abc"


def _blob(path, sha="s", size=10):
    return {"path": path, "type": "blob", "sha": sha, "size": size}


def test_find_skills_in_tree():
    tree = [
        _blob("README.md"),
        _blob("skills/pdf/SKILL.md", "sha-pdf", 1200),
        _blob("skills/pdf/scripts/fill.py"),
        _blob("skills/pdf/references/forms.md"),
        _blob("skills/notes/SKILL.md", "sha-notes", 300),
        _blob("skills/notes/nested/SKILL.md", "sha-nested", 50),
        _blob("skills/notes/nested/extra.md"),
        {"path": "skills/pdf", "type": "tree", "sha": "t"},
        _blob("docs/skill.md"),
    ]
    skills = {s["path"]: s for s in find_skills_in_tree(tree)}
    assert set(skills) == {"skills/pdf", "skills/notes", "skills/notes/nested"}
    pdf = skills["skills/pdf"]
    assert (pdf["sha"], pdf["size"], pdf["file_count"]) == ("sha-pdf", 1200, 3)
    assert pdf["has_scripts"] and pdf["has_references"]
    notes = skills["skills/notes"]
    assert notes["file_count"] == 1 and not notes["has_scripts"] and not notes["has_references"]
    assert skills["skills/notes/nested"]["has_references"]


def test_skill_a_la_racine():
    tree = [_blob("SKILL.md", "root"), _blob("src/main.py"), _blob("scripts/run.sh")]
    (skill,) = find_skills_in_tree(tree)
    assert skill["path"] == "" and skill["file_count"] == 3
    assert skill["has_scripts"]  # grâce à scripts/, pas à src/main.py
    assert not find_skills_in_tree([_blob("src/main.py")])


def test_install_type():
    assert is_plugin_repo([_blob(".claude-plugin/marketplace.json")])
    assert is_plugin_repo([_blob("plugins/a/.claude-plugin/plugin.json")])
    assert not is_plugin_repo([_blob("marketplace.json")])
    assert install_type(True, 5) == "plugin"
    assert install_type(False, 5) == "collection"
    assert install_type(False, 1) == "standalone"


def test_parse_marketplace():
    data = {"name": "m", "plugins": [
        {"name": "local", "source": "./plugins/local"},
        {"name": "gh", "source": {"source": "github", "repo": "acme/plugin-a"}},
        {"name": "url", "source": {"source": "url", "url": "https://github.com/acme/plugin-b.git"}},
        {"name": "short", "source": "other/plugin-c"},
        {"name": "dup", "source": {"source": "github", "repo": "ACME/plugin-a"}},
        "invalide",
    ]}
    assert parse_marketplace(json.dumps(data)) == ["acme/plugin-a", "acme/plugin-b", "other/plugin-c"]
    assert parse_marketplace("pas du json") == []
    assert parse_marketplace("[1, 2]") == []


def test_extract_github_repos():
    md = """
    - [A](https://github.com/acme/skill-one) et https://github.com/acme/skill-one/tree/main/x
    - [B](https://github.com/Other/repo.name.git)
    - https://github.com/topics/claude-skills (pas un dépôt)
    - git@github.com:team/ssh-repo.git
    """
    assert extract_github_repos(md) == ["acme/skill-one", "Other/repo.name", "team/ssh-repo"]
