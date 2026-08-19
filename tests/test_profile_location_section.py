"""The Profile screen's Location section (decision 181).

Every location field lives in one card now — home address plus the work-location preferences that
used to sit five fields apart in Applicant details. The failure mode worth pinning is silent: a
field rendered in the new card but not read back by `collectProfile()` would look editable and
then be dropped on save, wiping a saved answer (the shape of the data loss in decision 159).
"""
import re

from applicationbot import web

HTML = web.INDEX_HTML

# Every field the Location card renders, in render order.
LOCATION_KEYS = [
    "country", "state", "city", "street_address", "postal_code",
    "willing_to_relocate", "open_to_remote", "work_arrangement",
    "max_commute_miles", "preferred_locations",
]


def _block(start: str, end: str) -> str:
    i = HTML.index(start)
    return HTML[i:HTML.index(end, i)]


def _keys(block: str) -> list[str]:
    """The data-k keys of the fields built in a block of the profile-form JS."""
    return re.findall(r'(?:fld|area|selField|boolSel)\("[^"]*","([a-z_]+)"', block)


def test_location_card_holds_exactly_the_location_fields():
    got = _keys(_block('const location = el("div", {id:"location-card"', 'put("s-location"'))
    assert got == LOCATION_KEYS


def test_applicant_card_no_longer_holds_them():
    applicant = _keys(_block('const applicant = el("div", {id:"profile-card"', 'put("s-applicant"'))
    assert not set(applicant) & set(LOCATION_KEYS)
    assert "first_name" in applicant and "gender" in applicant   # the rest stayed put


def test_every_location_field_is_saved():
    body = _block("function collectProfile()", "async function loadProfile()")
    assert 'cardData($("location-card"))' in body       # the card is read at all
    for k in LOCATION_KEYS:
        assert f'"{k}"' in body, f"{k} is editable but never collected — it would be lost on save"


def test_location_has_its_own_section_and_nav_pill():
    assert 'put("s-location"' in HTML
    assert '["s-location","Location"]' in HTML
