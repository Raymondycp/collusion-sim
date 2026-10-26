from features.base import REGISTRY, Feature, Missing


@REGISTRY.register
class MsgVolume(Feature):
    id, version, name = "msg_volume", "1", "Message volume"
    description = "Messages posted per settled round, all sellers together."
    evidence_type, data_layer, access = "conversation", "A", "log"
    inputs = ("settled", "posted")

    def compute(self, parent, branches, evaluations):
        rounds = len(parent.of("settled"))
        if not rounds:
            raise Missing("no_rounds")
        return len(parent.of("posted")) / rounds
