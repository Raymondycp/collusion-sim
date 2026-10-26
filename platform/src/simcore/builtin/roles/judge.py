from simcore.roles import ROLES, RoleSpec

ROLES.register(
    RoleSpec(
        name="judge",
        stage="evaluation",
        phases=[],
        tools=["read_episode"],
        output_events=[],
        research_allowed=True,
        built=False,
        notes="Runs after a run on the exported episode.jsonl, layer A only. Writes to the evaluation store, never to the episode.",
    )
)
