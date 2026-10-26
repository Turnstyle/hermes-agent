"""Standalone hermes-webui (``api/routes.py``) imports ``_sort_skills`` from ``tools.skills_tool``.

The fleet's Hermex WebUI on every node still uses the pre-public name, so the alias must order
listings exactly like ``sort_skills`` (fleet unit E4-S01).
"""

from tools.skills_tool import _sort_skills, sort_skills


def test_webui_alias_orders_like_public_sort():
    skills = [
        {"name": "zeta", "category": "b"},
        {"name": "alpha", "category": None},
        {"name": "beta", "category": "a"},
        {"name": "alpha", "category": "b"},
    ]
    assert _sort_skills(list(skills)) == sort_skills(list(skills))
    assert [(s.get("category"), s["name"]) for s in _sort_skills(skills)] == [
        (None, "alpha"), ("a", "beta"), ("b", "alpha"), ("b", "zeta")]
