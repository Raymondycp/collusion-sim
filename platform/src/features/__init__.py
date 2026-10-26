"""Episode feature registry (DECISIONS-10 §5). Depends only on simschema and indicators.

To add a feature: a class in a module of features/builtin/ that registers itself, a row in features/README.md, and a test.
"""

from features.base import (
    REGISTRY,
    BranchView,
    EpisodeView,
    Feature,
    FeatureRegistry,
    LayerError,
    Missing,
    ScenarioFacts,
)
from simschema.plugins import load_all

load_all("features.builtin")

LOADED_FEATURES_HASH = REGISTRY.code_hash()
"""The feature code this process runs (CR-26 m1): compared with the files on disk before a freeze or an export."""

__all__ = [
    "LOADED_FEATURES_HASH",
    "REGISTRY",
    "BranchView",
    "EpisodeView",
    "Feature",
    "FeatureRegistry",
    "LayerError",
    "Missing",
    "ScenarioFacts",
]
