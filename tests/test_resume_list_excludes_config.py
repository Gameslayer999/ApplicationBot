"""`list_resumes()` must never offer a CONFIG file as a résumé (decision 159).

A listed path is not just selectable — it is a WRITE target for `/resume/update`, which calls
`catalogue.replace_resume` on it. So anything wrongly listed gets a résumé written over it.
That happened for real: `tests/test_web_multi_select.py` stubbed `web.load_resume` to succeed for
any path, every `profile/*.yaml` then validated as a résumé, and saving the Profile screen wrote a
"Jane Doe" résumé over the user's real `profile/application_profile.yaml`, destroying it.

The load-check alone can't hold that line, because anything that makes loading succeed re-opens it.
A name check can, and no résumé is ever named `application_profile.yaml`.

Run:  python -m tests.test_resume_list_excludes_config   (also pytest-compatible)
"""
from __future__ import annotations

from applicationbot import web
from applicationbot.models import Contact, Resume


def test_config_files_are_never_listed_as_resumes(monkeypatch, tmp_path):
    (tmp_path / "profile").mkdir()
    (tmp_path / "examples").mkdir()
    names = ["application_profile.yaml", "discovery.yaml", "mailbox.yaml", "safety.yaml",
             "notifications.yaml", "resume.yaml"]
    for n in names:
        (tmp_path / "profile" / n).write_text("contact:\n  name: X\n", encoding="utf-8")
    monkeypatch.setattr(web, "REPO_ROOT", tmp_path)
    # The exact condition that caused the data loss: load_resume succeeds for EVERY path.
    monkeypatch.setattr(web, "load_resume", lambda _p: Resume(contact=Contact(name="X", email="x@example.com")))

    listed = {r["path"] for r in web.list_resumes()}
    assert listed == {"profile/resume.yaml"}, listed
    # And the allow-list gate that guards the write must refuse the config path outright.
    for n in names:
        if n == "resume.yaml":
            continue
        try:
            web._allowlisted(f"profile/{n}", web.list_resumes())
        except ValueError:
            continue
        raise AssertionError(f"profile/{n} is writable as a résumé")


def _main() -> int:
    import tempfile, traceback
    from pathlib import Path

    class MP:
        def __init__(self): self.undo = []
        def setattr(self, obj, name, val):
            self.undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def close(self):
            for obj, name, old in reversed(self.undo):
                setattr(obj, name, old)

    mp = MP()
    try:
        test_config_files_are_never_listed_as_resumes(mp, Path(tempfile.mkdtemp()))
        print("  ok  test_config_files_are_never_listed_as_resumes")
        print("ok")
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        mp.close()


if __name__ == "__main__":
    raise SystemExit(_main())
