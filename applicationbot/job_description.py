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


# Trailing legal/EEO boilerplate markers — these sections close out a posting and carry no
# tailoring signal. Searched only in the LAST 40% of the text, so a requirements section that
# happens to mention accommodation or benefits early is never cut.
_BOILERPLATE_MARKERS = (
    "equal opportunity employer", "equal employment opportunity", "eeo is the law",
    "we are an equal opportunity", "equal opportunity workplace", "affirmative action",
    "e-verify", "reasonable accommodation", "privacy policy", "privacy notice",
    "applicant privacy", "fair chance", "criminal histories", "pay transparency nondiscrimination",
)


def trim_for_prompt(body: str, cap: int = 8000) -> str:
    """The JD as sent to an LLM prompt: trailing boilerplate stripped and hard-capped at `cap`
    characters on a paragraph boundary. The stored JD is untouched (pay-band parsing and the
    fit judge read the full body)."""
    text = body or ""
    low = text.lower()
    tail = int(len(text) * 0.6)
    cuts = [p for m in _BOILERPLATE_MARKERS if (p := low.find(m, tail)) != -1]
    if cuts:
        text = text[: min(cuts)].rstrip()
    if len(text) > cap:
        nl = text.rfind("\n\n", 0, cap)
        text = text[: nl if nl > cap // 2 else cap].rstrip()
    return text
