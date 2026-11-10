"""Mode d'emploi et exemple d'un skill, extraits de son SKILL.md (sans modèle)."""

from __future__ import annotations

import re
from typing import Any

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^#{1,5}\s+(.*?)\s*#*\s*$")
_USAGE_TITLE = re.compile(
    r"\b(usage|how to use|quick ?start|getting started|basic use|invocation|workflow|"
    r"utilisation|comment utiliser|démarrage)\b", re.IGNORECASE)
_EXAMPLE_TITLE = re.compile(r"\b(examples?|exemples?|sample|demo)\b", re.IGNORECASE)
_WHEN_TITLE = re.compile(r"\b(when to use|when to activate|quand utiliser|triggers?)\b",
                         re.IGNORECASE)
_WHEN_SENTENCE = re.compile(
    r"\b(use (this |it |the )?(skill )?(when|whenever|for|to|if)\b|use when\b|"
    r"triggers? (on|with|when)\b|invoke (when|with)\b|à utiliser|"
    r"utiliser (quand|lorsque|pour)\b)", re.IGNORECASE)
_QUOTED = re.compile(r'["“«]\s*([^"“”«»\n]{6,100}?)\s*["”»]')
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}(:[a-z0-9][a-z0-9_-]{0,63})?")

EXAMPLE_MAX_LINES = 25
EXAMPLE_MAX_CHARS = 900


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return (cut[:cut.rfind(" ")] if " " in cut[limit // 2:] else cut).rstrip(" ,;:") + "…"


def clip_example(text: str) -> str:
    lines = (text or "").strip("\n").splitlines()
    clipped = len(lines) > EXAMPLE_MAX_LINES
    text = "\n".join(lines[:EXAMPLE_MAX_LINES])
    if len(text) > EXAMPLE_MAX_CHARS:
        head = text[:EXAMPLE_MAX_CHARS]
        text, clipped = (head.rsplit("\n", 1)[0] if "\n" in head else head), True
    return text + ("\n…" if clipped and text else "")


def split_sections(body: str) -> list[tuple[str, list[str]]]:
    """Découpe le corps en sections (titre, lignes). Les « # » dans les blocs de code sont ignorés."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_code = False
    for line in (body or "").splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            sections[-1][1].append(line)
            continue
        m = None if in_code else _HEADING.match(line)
        if m:
            sections.append((m.group(1), []))
        else:
            sections[-1][1].append(line)
    return sections


def code_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if _FENCE.match(line):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current).strip("\n"))
                current = None
        elif current is not None:
            current.append(line)
    return [b for b in blocks if b.strip()]


def prose(lines: list[str], max_lines: int = 4) -> list[str]:
    """Premières lignes de texte d'une section (hors code, tableaux, images), puces retirées."""
    out: list[str] = []
    in_code = False
    for line in lines:
        if _FENCE.match(line):
            in_code = not in_code
            continue
        t = line.strip()
        if in_code or not t or t.startswith(("|", "<", "![", "---", ">")):
            continue
        t = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", t)
        t = re.sub(r"\*\*|__", "", t)
        out.append(clip(t, 220))
        if len(out) >= max_lines:
            break
    return out


def extract_usage(skill: dict[str, Any]) -> tuple[str, str | None]:
    """Retourne (mode d'emploi, exemple). Le mode d'emploi contient une ligne par étape.

    L'exemple vient, par ordre de préférence : d'un bloc de code d'une section « Exemples »,
    d'une section « Usage », des phrases de déclenchement citées dans la description,
    du texte d'une section « Exemples », puis du premier bloc de code du fichier.
    Il vaut None si rien n'est exploitable.
    """
    description = skill.get("description") or ""
    sections = split_sections(skill.get("body") or "")
    name = (skill.get("name") or "").strip()
    folder = (skill.get("path") or "").rstrip("/").split("/")[-1]
    slug = name if _SLUG.fullmatch(name) else folder if _SLUG.fullmatch(folder) else None

    if slug:
        steps = [f"Dans Claude Code, tapez /{slug} ou décrivez simplement votre besoin : "
                 "Claude charge le skill quand la demande correspond à sa description."]
    else:
        steps = ["Décrivez simplement votre besoin : Claude charge le skill quand la "
                 "demande correspond à sa description."]

    when = next((t for t in re.split(r"(?<=[.!?。])\s+", description) if _WHEN_SENTENCE.search(t)),
                None)
    if not when:
        for title, lines in sections:
            if _WHEN_TITLE.search(title):
                when = " ; ".join(prose(lines, 3)) or None
                break
    if when:
        steps.append("Quand l'utiliser : " + clip(when, 320))

    usage_sections = [lines for title, lines in sections if _USAGE_TITLE.search(title)]
    example_sections = [lines for title, lines in sections if _EXAMPLE_TITLE.search(title)]
    if usage_sections:
        steps += prose(usage_sections[0], 4)

    example: str | None = None
    for lines in example_sections + usage_sections:
        blocks = code_blocks(lines)
        if blocks:
            example = blocks[0]
            break
    if not example:
        quoted = list(dict.fromkeys(_QUOTED.findall(description)))[:3]
        if quoted:
            example = "\n".join(quoted)
    if not example and example_sections:
        example = "\n".join(prose(example_sections[0], 3)) or None
    if not example:
        blocks = code_blocks([line for _title, lines in sections for line in lines])
        example = blocks[0] if blocks else None
    return "\n".join(steps), (clip_example(example) or None if example else None)
