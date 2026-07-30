"""Import a LinkedIn data export into the résumé catalogue.

LinkedIn cannot be live-linked to pull a full profile (their API restricts it to approved
partners, and scraping violates their ToS + Agent Guideline #4). The compliant path is
LinkedIn's own "Get a copy of your data" export — a ZIP of CSVs. This module parses the
relevant CSVs (Positions, Education, Skills) and MERGES new entries into the catalogue, deduping
against what's already there with the shared entry matcher in `resume_import` (never overwrites
existing entries or contact info, and never re-adds a role under LinkedIn's wording of it).
"""

from __future__ import annotations

import csv
import io
import zipfile

from . import resume_import
from .catalogue import save_resume
from .models import Education, Experience, SkillCategory
from .resume import load_resume


def _csvs_from_upload(filename: str, data: bytes) -> dict[str, list[dict]]:
    """Return {lowercased basename: parsed rows} for CSVs in a ZIP or a single CSV."""
    raw_csvs: dict[str, bytes] = {}
    is_zip = filename.lower().endswith(".zip") or data[:2] == b"PK"
    if is_zip:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1].lower()
                if base.endswith(".csv"):
                    raw_csvs[base] = z.read(name)
    elif filename.lower().endswith(".csv"):
        raw_csvs[filename.rsplit("/", 1)[-1].lower()] = data
    else:
        raise ValueError("Upload your LinkedIn export .zip (or a Positions/Education/Skills .csv).")

    parsed: dict[str, list[dict]] = {}
    for base, blob in raw_csvs.items():
        text = blob.decode("utf-8-sig", errors="replace")
        parsed[base] = list(csv.DictReader(io.StringIO(text)))
    return parsed


def _get(row: dict, *keys: str) -> str | None:
    """Case-insensitive column lookup, tolerant of header variations."""
    low = {(k or "").strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = low.get(k.lower())
        if v and v.strip():
            return v.strip()
    return None


def _bullets(desc: str | None) -> list[str]:
    if not desc:
        return []
    out = []
    for line in desc.replace("\r", "").split("\n"):
        line = line.strip().lstrip("•-*·").strip()
        if line:
            out.append(line)
    return out


def import_into(path, filename: str, data: bytes) -> dict:
    """Parse a LinkedIn export and merge new experience/education/skills into `path`.

    Dedup uses the same entry matcher as the résumé-document import, so the two paths agree on what
    counts as the same entry: LinkedIn writes the legal company name a résumé shortens ("Acme Corp.
    Inc." vs "Acme") and its own job titles, which exact string matching re-added as duplicates."""
    csvs = _csvs_from_upload(filename, data)
    resume = load_resume(path)
    added = {"experience": 0, "education": 0, "skills": 0}
    skipped: list[str] = []   # entries the résumé already had, named so the user can see what we dropped
    enriched: list[str] = []  # existing entries whose blank fields this import filled in

    # Positions -> experience
    for row in csvs.get("positions.csv", []):
        org = _get(row, "Company Name", "Company")
        role = _get(row, "Title", "Position Title")
        if not org or not role:
            continue
        entry = Experience(
            organization=org,
            role=role,
            location=_get(row, "Location"),
            start=_get(row, "Started On", "Start Date") or "",
            end=_get(row, "Finished On", "End Date") or "Present",
            bullets=_bullets(_get(row, "Description")),
        )
        match = resume_import.find_role(resume, entry)
        if match is not None:
            label = f"{match.organization} — {match.role}"
            filled = resume_import.fill_blanks(match, entry, ("location", "start", "end"))
            (enriched if filled else skipped).append(label)
            continue
        resume.experience.append(entry)
        added["experience"] += 1

    # Education
    for row in csvs.get("education.csv", []):
        school = _get(row, "School Name", "School")
        if not school:
            continue
        entry = Education(
            school=school,
            degree=_get(row, "Degree Name", "Degree") or "",
            graduation=_get(row, "End Date", "Finished On"),
            details=[d for d in (_get(row, "Notes"), _get(row, "Activities")) if d],
        )
        match = resume_import.find_education(resume, entry)
        if match is not None:
            label = f"{match.school} — {match.degree}" if match.degree.strip() else match.school
            filled = resume_import.fill_blanks(match, entry, ("degree", "graduation"))
            (enriched if filled else skipped).append(label)
            continue
        resume.education.append(entry)
        added["education"] += 1

    # Skills -> a "LinkedIn Skills" category (deduped against all existing skills)
    existing = {resume_import.skill_key(i) for c in resume.skills for i in c.items}
    new_skills: list[str] = []
    for row in csvs.get("skills.csv", []):
        name = _get(row, "Name", "Skill")
        if name and resume_import.skill_key(name) not in existing:
            existing.add(resume_import.skill_key(name))
            new_skills.append(name)
    if new_skills:
        cat = next((c for c in resume.skills if c.category.lower() == "linkedin skills"), None)
        if cat:
            cat.items.extend(new_skills)
        else:
            resume.skills.append(SkillCategory(category="LinkedIn Skills", items=new_skills))
        added["skills"] = len(new_skills)

    save_resume(path, resume)
    return {"added": added, "found_files": sorted(csvs.keys()),
            "skipped": skipped, "enriched": enriched}
