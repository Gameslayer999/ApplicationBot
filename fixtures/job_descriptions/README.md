# Job-description fixtures

Real, verbatim job postings used as test data for the resume customizer. Each file is
Markdown with a YAML front-matter header (source_url, company, title, level, location,
compensation, date_captured, category) followed by the posting's full text.

Captured 2026-07-03 from live applicant-tracking-system postings (Greenhouse / Lever /
Ashby / company career pages). Categories: backend and data/ML, junior → senior.

> These mirror the shape the future scraper (Discover stage) will emit, so the customizer
> needs no changes when real scraping lands. Public postings expire, so treat these as
> fixed test snapshots, not live links.

Use:

```bash
python -m applicationbot.cli fixtures/job_descriptions/backend-mid-censys.md \
    --resume examples/sample_resume.yaml
```
