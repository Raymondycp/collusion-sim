"""Indicator registry. Depends only on simschema, so P2's features can import the same functions.

To add an indicator: one module in indicators/builtin/ that registers its class, and one test.
"""

from indicators.base import REGISTRY, Indicator, IndicatorRegistry, Instance, NoParams, OutputSpec, ParamsError, instance
from indicators.series import LAYER, LayerError, SeriesView
from simschema.plugins import load_all

load_all("indicators.builtin")

__all__ = [
    "LAYER",
    "REGISTRY",
    "Indicator",
    "IndicatorRegistry",
    "Instance",
    "LayerError",
    "NoParams",
    "OutputSpec",
    "ParamsError",
    "SeriesView",
    "instance",
]
