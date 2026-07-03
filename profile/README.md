# profile/ — your personal data (git-ignored)

Everything in this folder except this README is **git-ignored**, so your real resume and
personal details never get committed. This is where your own data lives when you clone
and run ApplicationBot.

## What to put here

1. **Your resume** — drop it in as-is, any format:
   `profile/resume.pdf`, `profile/resume.docx`, or pasted text in `profile/resume.txt`.

2. **`profile/resume.yaml`** — the structured resume the customizer actually reads (the
   "source of truth"). It follows the schema in
   [`../examples/sample_resume.yaml`](../examples/sample_resume.yaml). You can write it by
   hand from that template, or have it generated from the resume file you dropped in.

## Using it

```bash
python -m applicationbot.cli <job_description.md> \
    --resume profile/resume.yaml --out profile/tailored.md
```

The customizer only reads `profile/resume.yaml`. The original PDF/DOCX is just kept here
for reference and for (re)generating the YAML.

> Note: tailoring sends your resume content to the Claude API — that's how it customizes
> the resume to each job. Nothing here is written to git.
