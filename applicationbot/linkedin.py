"""Import a LinkedIn data export into the résumé catalogue.

LinkedIn cannot be live-linked to pull a full profile (their API restricts it to approved
partners, and scraping violates their ToS + Agent Guideline #4). The compliant path is
LinkedIn's own "Get a copy of your data" export — a ZIP of CSVs. This module parses the
relevant CSVs (Positions, Education, Skills) and MERGES new entries into the catalogue,
deduping against what's already there (never overwrites existing entries or contact info).
"""

from __future__ import annotations

import csv
import io
import zipfile

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
    """Parse a LinkedIn export and merge new experience/education/skills into `path`."""
    csvs = _csvs_from_upload(filename, data)
    resume = load_resume(path)
    added = {"experience": 0, "education": 0, "skills": 0}

    # Positions -> experience
    have_roles = {
        (e.organization.strip().lower(), e.role.strip().lower()) for e in resume.experience
    }
    for row in csvs.get("positions.csv", []):
        org = _get(row, "Company Name", "Company")
        role = _get(row, "Title", "Position Title")
        if not org or not role or (org.lower(), role.lower()) in have_roles:
            continue
        have_roles.add((org.lower(), role.lower()))
        resume.experience.append(
            Experience(
                organization=org,
                role=role,
                location=_get(row, "Location"),
                start=_get(row, "Started On", "Start Date") or "",
                end=_get(row, "Finished On", "End Date") or "Present",
                bullets=_bullets(_get(row, "Description")),
            )
        )
        added["experience"] += 1

    # Education
    have_edu = {
        (e.school.strip().lower(), (e.degree or "").strip().lower()) for e in resume.education
    }
    for row in csvs.get("education.csv", []):
        school = _get(row, "School Name", "School")
        if not school:
            continue
        degree = _get(row, "Degree Name", "Degree") or ""
        if (school.lower(), degree.lower()) in have_edu:
            continue
        have_edu.add((school.lower(), degree.lower()))
        details = [d for d in (_get(row, "Notes"), _get(row, "Activities")) if d]
        resume.education.append(
            Education(
                school=school,
                degree=degree,
                graduation=_get(row, "End Date", "Finished On"),
                details=details,
            )
        )
        added["education"] += 1

    # Skills -> a "LinkedIn Skills" category (deduped against all existing skills)
    existing = {i.strip().lower() for c in resume.skills for i in c.items}
    new_skills: list[str] = []
    for row in csvs.get("skills.csv", []):
        name = _get(row, "Name", "Skill")
        if name and name.lower() not in existing:
            existing.add(name.lower())
            new_skills.append(name)
    if new_skills:
        cat = next((c for c in resume.skills if c.category.lower() == "linkedin skills"), None)
        if cat:
            cat.items.extend(new_skills)
        else:
            resume.skills.append(SkillCategory(category="LinkedIn Skills", items=new_skills))
        added["skills"] = len(new_skills)

    save_resume(path, resume)
    return {"added": added, "found_files": sorted(csvs.keys())}
