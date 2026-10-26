"""Run-quality columns (DECISIONS-22 entry 6 §3; CR-88 addendum 3 gap 5). Not evidence: the decision rule's manipulation
check reads timeout_fallbacks, and a candidate rule never uses either column."""

from features.base import REGISTRY, Feature


@REGISTRY.register
class TimeoutFallbacks(Feature):
    id, version, name = "timeout_fallbacks", "1", "Timeout fallbacks"
    description = "Seller turns that the system ended after two slow attempts: holds (acted) plus PASS replies (talk_replied)."
    evidence_type, data_layer, access = "quality", "A", "log"
    inputs = ("acted", "talk_replied")

    def compute(self, parent, branches, evaluations):
        holds = sum(e.fallback_reason == "timeout" for e in parent.of("acted"))
        return holds + sum(e.reason == "timeout" for e in parent.of("talk_replied"))


@REGISTRY.register
class DegradedCalls(Feature):
    id, version, name = "degraded_calls", "1", "Degraded calls"
    description = "LLM calls sent with the model's thinking_on_timeout after a slow attempt. A count for the report only."
    evidence_type, data_layer, access = "quality", "B", "model"
    inputs = ("llm_call",)

    def compute(self, parent, branches, evaluations):
        return sum(bool(e.degraded) for e in parent.of("llm_call"))
