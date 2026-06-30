"""Templated message rendering with {first_name} style placeholders.

No AI. Placeholders are substituted from task/profile fields. Missing or
empty fields fall back to sensible defaults (e.g. {first_name} -> "there").
"""

from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# Fields that should fall back to a friendly default when missing.
_FRIENDLY_DEFAULTS = {
    "first_name": "there",
    "name": "there",
    "full_name": "there",
}


def build_context(task) -> dict:
    """Build the substitution context from a UoTask (+ cached profile fields)."""
    first = (task.first_name or "").strip()
    last = (task.last_name or "").strip()
    full = (task.full_name or (first + " " + last)).strip()
    return {
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "name": first or full,
        "headline": (task.headline or "").strip(),
        "company": (task.company or "").strip(),
        "location": (task.location or "").strip(),
    }


def render(template: str, context: dict, max_length: int | None = None) -> str:
    """Render a template string, substituting {placeholders} from context."""
    if not template:
        return ""

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key)
        if value:
            return str(value)
        return _FRIENDLY_DEFAULTS.get(key, "")

    rendered = _PLACEHOLDER_RE.sub(_sub, template)
    # Collapse whitespace introduced by empty substitutions.
    rendered = re.sub(r"\s{2,}", " ", rendered).strip()
    # Tidy stray spaces before punctuation, e.g. "Hi ," -> "Hi,"
    rendered = re.sub(r"\s+([,.!?])", r"\1", rendered)

    if max_length and len(rendered) > max_length:
        rendered = rendered[: max_length - 3].rstrip() + "..."
    return rendered


def has_required_name(task) -> bool:
    """Whether the task already has a usable first name for personalisation."""
    return bool((task.first_name or "").strip())
