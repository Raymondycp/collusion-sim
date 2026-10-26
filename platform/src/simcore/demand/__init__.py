"""Demand models as plug-ins. To add one: a module in demand/builtin/ that calls DEMAND.register(...), and its known-answer tests."""

from simcore.demand.base import DEMAND, REFERENCES, DemandModel, DemandModelSpec, DemandRegistry, Solver
from simschema.plugins import load_all

load_all("simcore.demand.builtin")

__all__ = ["DEMAND", "REFERENCES", "DemandModel", "DemandModelSpec", "DemandRegistry", "Solver"]
