"""Load job-description fixtures.

Fixtures are Markdown files with a YAML front-matter header (source_url, company, title,
level, location, compensation, ...) followed by the verbatim job description text. This
mirrors what the future scraper will produce, so the customizer can stay unchanged when
real scraping lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class JobDescription:
    body: str
    meta: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None

    @property
    def title(self) -> str:
        return str(self.meta.get("title", "Unknown role"))

    @property
    def company(self) -> str:
        return str(self.meta.get("company", "Unknown company"))


def load_job_description(path: str | Path) -> JobDescription:
    """Parse a fixture Markdown file with optional YAML front matter."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = text

    if text.startswith("---"):
        # Split on the closing '---' of the front matter.
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, front, body = parts
            # Front matter from real postings can contain unquoted colons, %, etc.
            # If it doesn't parse as YAML, keep the body and just skip the metadata.
            try:
                parsed = yaml.safe_load(front)
            except yaml.YAMLError:
                parsed = None
            if isinstance(parsed, dict):
                meta = parsed

    return JobDescription(body=body.strip(), meta=meta, source_path=str(path))
