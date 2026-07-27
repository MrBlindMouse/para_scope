"""Named theme WCAG contrast floors (ink / muted / link / borders)."""
from app.themes import THEMES, THEME_OPTIONS, named_theme_contrast_issues


def test_theme_options_cover_all_themes():
    assert {o["id"] for o in THEME_OPTIONS} == THEMES


def test_named_themes_meet_aa_contrast_floors():
    issues = named_theme_contrast_issues()
    assert issues == []
