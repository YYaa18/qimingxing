"""Compatibility facade.

Implementations moved to :mod:`brain.rich.sources`, :mod:`brain.rich.tables`
and :mod:`brain.rich.media`. Import paths stay unchanged for callers.
"""

from __future__ import annotations

from brain.rich.media import collect_media
from brain.rich.sources import (
    clean_answer_and_sources,
    disambiguate_duplicate_source_titles,
    enrich_sources_deterministically,
    extract_source_snippets,
    is_generic_source_title,
    label_sources_with_llm,
    needs_source_labeling,
)
from brain.rich.tables import extract_table_media

__all__ = [
    "clean_answer_and_sources",
    "collect_media",
    "disambiguate_duplicate_source_titles",
    "enrich_sources_deterministically",
    "extract_source_snippets",
    "extract_table_media",
    "is_generic_source_title",
    "label_sources_with_llm",
    "needs_source_labeling",
]
