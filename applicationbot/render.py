"""Render a tailored resume to Markdown.

Markdown is the first render target — readable as-is and easy to convert to PDF/DOCX
later. The renderer preserves the source resume's section order and grouping so the
output stays close to the candidate's own format (see DECISIONS.md #005). Exact visual
reproduction (right-aligned dates, single-column PDF) is a future PDF/DOCX render target.

Contact details and section order come from the base resume (they define the format);
the section *content* comes from the tailored resume.
"""

from __future__ import annotations

import html as _html

from .models import (
    SECTION_KEYS,
    Contact,
    Education,
    Experience,
    Project,
    Resume,
    SkillCategory,
    TailoredResume,
)

DEFAULT_ORDER = list(SECTION_KEYS)


def _render_contact(contact: Contact) -> list[str]:
    lines = [f"# {contact.name}", ""]
    bits = [b for b in (contact.location, contact.email, contact.phone) if b]
    bits += list(contact.links)
    if bits:
        lines += [" | ".join(bits), ""]
    return lines


def _render_experience(exp: Experience) -> list[str]:
    loc = f" — {exp.location}" if exp.location else ""
    lines = [f"### {exp.organization}{loc}", f"*{exp.role}* · {exp.start} – {exp.end}", ""]
    lines += [f"- {b}" for b in exp.bullets]
    lines.append("")
    return lines


def _render_project(proj: Project) -> list[str]:
    header = f"### {proj.name}"
    if proj.tech:
        header += f" | {proj.tech}"
    lines = [header, ""]
    lines += [f"- {b}" for b in proj.bullets]
    lines.append("")
    return lines


def _render_education(edu: Education) -> list[str]:
    loc = f" — {edu.location}" if edu.location else ""
    grad = f" · {edu.graduation}" if edu.graduation else ""
    lines = [f"### {edu.school}{loc}", f"*{edu.degree}*{grad}", ""]
    lines += [f"- {d}" for d in edu.details]
    lines.append("")
    return lines


def _render_skills(skills: list[SkillCategory]) -> list[str]:
    lines = ["## Skills", ""]
    for cat in skills:
        lines.append(f"- **{cat.category}:** {', '.join(cat.items)}")
    lines.append("")
    return lines


def render_markdown(base: Resume, tailored: TailoredResume) -> str:
    """Combine the base resume's contact + section order with tailored content."""
    order = base.section_order or DEFAULT_ORDER
    lines: list[str] = _render_contact(base.contact)

    for section in order:
        if section == "summary" and tailored.summary:
            lines += ["## Summary", "", tailored.summary, ""]
        elif section == "education" and tailored.education:
            lines += ["## Education", ""]
            for edu in tailored.education:
                lines += _render_education(edu)
        elif section == "experience" and tailored.experience:
            lines += ["## Experience", ""]
            for exp in tailored.experience:
                lines += _render_experience(exp)
        elif section == "projects" and tailored.projects:
            lines += ["## Projects", ""]
            for proj in tailored.projects:
                lines += _render_project(proj)
        elif section == "activities" and tailored.activities:
            lines += ["## Leadership and Activities", ""]
            for act in tailored.activities:
                lines += _render_experience(act)
        elif section == "skills" and tailored.skills:
            lines += _render_skills(tailored.skills)
        elif section == "certifications" and tailored.certifications:
            lines += ["## Certifications", "", ", ".join(tailored.certifications), ""]

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- HTML

# HTML render target for the web review UI. Produces a semantic single-column resume
# fragment (styled by the page's CSS) with right-aligned dates/locations so it resembles
# a real resume. All content is escaped. Exact PDF reproduction remains future work.


def _esc(s: object) -> str:
    return _html.escape(str(s))


def _row(left: str, right: str = "") -> str:
    r = f'<span class="r">{right}</span>' if right else ""
    return f'<div class="row"><span class="l">{left}</span>{r}</div>'


def _why(entry: object) -> str:
    """Attach the per-entry tailoring rationale as a data attribute (consumed by the review
    UI to show 'why this was tailored this way'). Empty for the base resume / no note."""
    note = getattr(entry, "tailor_note", None)
    return f' data-why="{_esc(note)}"' if note else ""


def _html_experience(exp: Experience) -> str:
    bullets = "".join(f"<li>{_esc(b)}</li>" for b in exp.bullets)
    return (
        f'<div class="entry"{_why(exp)}>'
        + _row(f"<b>{_esc(exp.organization)}</b>", _esc(exp.location) if exp.location else "")
        + _row(f"<em>{_esc(exp.role)}</em>", f"{_esc(exp.start)} – {_esc(exp.end)}")
        + (f"<ul>{bullets}</ul>" if bullets else "")
        + "</div>"
    )


def _html_project(proj: Project) -> str:
    bullets = "".join(f"<li>{_esc(b)}</li>" for b in proj.bullets)
    tech = f'<span class="tech">{_esc(proj.tech)}</span>' if proj.tech else ""
    return (
        f'<div class="entry"{_why(proj)}>'
        + _row(f"<b>{_esc(proj.name)}</b>", tech)
        + (f"<ul>{bullets}</ul>" if bullets else "")
        + "</div>"
    )


def _html_education(edu: Education) -> str:
    details = "".join(f"<li>{_esc(d)}</li>" for d in edu.details)
    return (
        '<div class="entry">'
        + _row(f"<b>{_esc(edu.school)}</b>", _esc(edu.location) if edu.location else "")
        + _row(f"<em>{_esc(edu.degree)}</em>", _esc(edu.graduation) if edu.graduation else "")
        + (f"<ul>{details}</ul>" if details else "")
        + "</div>"
    )


def _html_skills(skills: list[SkillCategory]) -> str:
    rows = "".join(
        f'<div class="skillrow"><b>{_esc(c.category)}:</b> {_esc(", ".join(c.items))}</div>'
        for c in skills
    )
    return rows


def _html_contact(contact: Contact) -> str:
    bits = [b for b in (contact.location, contact.email, contact.phone) if b]
    bits += list(contact.links)
    line = " | ".join(_esc(b) for b in bits)
    return f'<header><h1>{_esc(contact.name)}</h1><div class="contact">{line}</div></header>'


def render_html(base: Resume, tailored: TailoredResume) -> str:
    """Render the tailored resume as an HTML fragment (styled by the web page's CSS)."""
    order = base.section_order or DEFAULT_ORDER
    parts: list[str] = [_html_contact(base.contact)]

    def section(title: str, body: str) -> str:
        return f'<section><h2>{_esc(title)}</h2>{body}</section>'

    for name in order:
        if name == "summary" and tailored.summary:
            parts.append(section("Summary", f"<p>{_esc(tailored.summary)}</p>"))
        elif name == "education" and tailored.education:
            parts.append(section("Education", "".join(_html_education(e) for e in tailored.education)))
        elif name == "experience" and tailored.experience:
            parts.append(section("Experience", "".join(_html_experience(e) for e in tailored.experience)))
        elif name == "projects" and tailored.projects:
            parts.append(section("Projects", "".join(_html_project(p) for p in tailored.projects)))
        elif name == "activities" and tailored.activities:
            parts.append(section("Leadership and Activities", "".join(_html_experience(a) for a in tailored.activities)))
        elif name == "skills" and tailored.skills:
            parts.append(section("Skills", _html_skills(tailored.skills)))
        elif name == "certifications" and tailored.certifications:
            parts.append(section("Certifications", f"<p>{_esc(', '.join(tailored.certifications))}</p>"))

    return "\n".join(parts)
