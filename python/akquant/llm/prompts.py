"""Load the editable Markdown prompt template and construct compact requests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PromptTemplate:
    instructions: str
    process: str
    output_requirements: str
    version: str
    sha256: str

    @classmethod
    def load(cls, path: Path) -> "PromptTemplate":
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```markdown\s*\n(.*?)```", text, flags=re.DOTALL)
        if len(blocks) < 3:
            raise ValueError(f"Prompt 模板至少需要三个 markdown 代码块: {path}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        version_match = re.search(
            r"^prompt_version:\s*([0-9A-Za-z._-]+)\s*$", text, flags=re.MULTILINE
        )
        return cls(
            instructions=blocks[0].strip(),
            process=blocks[1].strip(),
            output_requirements=blocks[2].strip(),
            version=version_match.group(1) if version_match else digest[:12],
            sha256=digest,
        )

    def dynamic_input(self, context: dict[str, Any]) -> str:
        compact = json.dumps(
            context, ensure_ascii=False, separators=(",", ":"), default=str
        )
        return f"{self.process}\n\n{self.output_requirements}\n\n<analysis_context>{compact}</analysis_context>"
