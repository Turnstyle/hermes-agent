"""Tests for G2: grok-4.7 in static catalog and x_search defaults."""

from hermes_cli import models_catalog_static
from hermes_cli.config_defaults import DEFAULT_CONFIG
from tools.x_search_tool import DEFAULT_X_SEARCH_MODEL


def test_xai_static_catalog_includes_grok47_at_top():
    """_XAI_TOP_MODEL and _XAI_STATIC_FALLBACK have grok-4.7 at top; 4.6 and 4.5 preserved."""
    assert models_catalog_static._XAI_TOP_MODEL == "grok-4.7"
    assert models_catalog_static._XAI_STATIC_FALLBACK[0] == "grok-4.7"
    assert "grok-4.6" in models_catalog_static._XAI_STATIC_FALLBACK
    assert "grok-4.5" in models_catalog_static._XAI_STATIC_FALLBACK


def test_xai_curated_extras_includes_grok47():
    """_XAI_CURATED_EXTRAS includes grok-4.7, grok-4.6, grok-4.5."""
    assert "grok-4.7" in models_catalog_static._XAI_CURATED_EXTRAS
    assert "grok-4.6" in models_catalog_static._XAI_CURATED_EXTRAS
    assert "grok-4.5" in models_catalog_static._XAI_CURATED_EXTRAS


def test_xai_curated_models_promotes_grok47():
    """_xai_curated_models returns grok-4.7 as the first entry."""
    models = models_catalog_static._xai_curated_models()
    assert models[0] == "grok-4.7"
    assert "grok-4.6" in models
    assert "grok-4.5" in models


def test_default_x_search_model_is_grok47():
    """DEFAULT_X_SEARCH_MODEL in tools.x_search_tool is grok-4.7."""
    assert DEFAULT_X_SEARCH_MODEL == "grok-4.7"


def test_config_defaults_x_search_model_is_grok47():
    """config_defaults DEFAULT_CONFIG["x_search"]["model"] is grok-4.7."""
    assert DEFAULT_CONFIG["x_search"]["model"] == "grok-4.7"
