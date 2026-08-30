"""Compact, editable retrieval for the personal trading knowledge Markdown file."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CARD_HEADING = re.compile(r"^###\s+((?:RULE|CASE)-[A-Z]+-\d+)\s+(.+?)\s*$")
_META = re.compile(r"`([^`]+)`")
_ALWAYS_IDS = {
    "RULE-RISK-101",
    "RULE-RISK-102",
    "RULE-ENV-101",
    "RULE-SIGNAL-101",
    "RULE-POSITION-101",
}


@dataclass(frozen=True)
class KnowledgeCard:
    card_id: str
    title: str
    body: str
    tags: tuple[str, ...]
    priority: int
    always_apply: bool
    status: str

    def compact(self, max_chars: int = 520) -> dict[str, Any]:
        text = " ".join(
            line.lstrip("- ").strip() for line in self.body.splitlines() if line.strip()
        )
        return {
            "id": self.card_id,
            "title": self.title,
            "rule": text[:max_chars],
        }


class KnowledgeBase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        self.sha256 = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        version = re.search(r"版本：`([^`]+)`", self.text)
        self.version = version.group(1) if version else self.sha256[:12]
        self.cards = self._parse(self.text)

    @staticmethod
    def _parse(text: str) -> list[KnowledgeCard]:
        lines = text.splitlines()
        cards: list[KnowledgeCard] = []
        index = 0
        while index < len(lines):
            match = _CARD_HEADING.match(lines[index])
            if not match:
                index += 1
                continue
            card_id, title = match.groups()
            end = index + 1
            while end < len(lines) and not _CARD_HEADING.match(lines[end]):
                if lines[end].startswith("## "):
                    break
                end += 1
            body_lines = lines[index + 1 : end]
            meta_line = next((line for line in body_lines if "status:" in line), "")
            metadata = " · ".join(_META.findall(meta_line))
            tags_match = re.search(r"tags:\s*([^·`]+)", metadata)
            priority_match = re.search(r"priority:\s*(\d+)", metadata)
            status_match = re.search(r"status:\s*([a-z_]+)", metadata)
            always_match = re.search(r"always_apply:\s*(true|false)", metadata)
            cards.append(
                KnowledgeCard(
                    card_id=card_id,
                    title=title,
                    body="\n".join(
                        line for line in body_lines if line != meta_line
                    ).strip(),
                    tags=tuple(
                        tag.strip()
                        for tag in (
                            tags_match.group(1).split(",") if tags_match else []
                        )
                        if tag.strip()
                    ),
                    priority=int(priority_match.group(1)) if priority_match else 50,
                    always_apply=(
                        always_match.group(1) == "true"
                        if always_match
                        else card_id in _ALWAYS_IDS
                    ),
                    status=status_match.group(1) if status_match else "draft",
                )
            )
            index = end
        return cards

    def retrieve(self, context: dict[str, Any], limit: int = 8) -> dict[str, Any]:
        searchable = " ".join(_flatten_strings(context)).lower()
        active = [card for card in self.cards if card.status == "active"]
        fixed = [
            card for card in active if card.always_apply or card.card_id in _ALWAYS_IDS
        ]
        candidates = [card for card in active if card not in fixed]

        def score(card: KnowledgeCard) -> tuple[int, int, int, str]:
            matches = sum(1 for tag in card.tags if tag.lower() in searchable)
            title_matches = sum(
                1
                for token in re.split(r"[、，,\s]+", card.title)
                if token and token.lower() in searchable
            )
            relevance = matches + title_matches
            return (
                1 if relevance > 0 else 0,
                matches * 20 + title_matches * 5 + card.priority,
                card.priority,
                card.card_id,
            )

        selected = sorted(candidates, key=score, reverse=True)[: max(0, limit)]
        return {
            "version": self.version,
            "sha256": self.sha256,
            "fixed_rules": [card.compact(360) for card in fixed],
            "retrieved_rules": [card.compact() for card in selected],
        }


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            result.append(str(key))
            result.extend(_flatten_strings(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    return [str(value)] if value is not None else []
