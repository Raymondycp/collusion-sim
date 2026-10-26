from indicators.base import REGISTRY, Indicator, OutputSpec


@REGISTRY.register
class MsgVolume(Indicator):
    id, version, name = "msg_volume", "1", "Message volume"
    description = "Messages posted per round, per seller and in total. The talk-score marker comes with the judge."
    inputs = ("posts.*",)
    data_layer, pane = "A", "separate"
    outputs = (
        OutputSpec(name="msg_volume.{seller}", kind="histogram", unit="messages"),
        OutputSpec(name="msg_volume", kind="histogram", unit="messages"),
    )

    def compute(self, s, p):
        out = {f"msg_volume.{x}": s[f"posts.{x}"] for x in s.sellers}
        out["msg_volume"] = [sum(row) for row in zip(*out.values())]
        return out


@REGISTRY.register
class QueryRate(Indicator):
    id, version, name = "query_rate", "1", "Query rate"
    description = "Read-tool calls per round, per seller (refused calls count too)."
    inputs = ("queries.*",)
    data_layer, pane = "A", "separate"
    outputs = (OutputSpec(name="query_rate.{seller}", kind="histogram", unit="calls"),)

    def compute(self, s, p):
        return {f"query_rate.{x}": s[f"queries.{x}"] for x in s.sellers}
