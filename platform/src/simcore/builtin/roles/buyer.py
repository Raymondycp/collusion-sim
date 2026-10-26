from simcore.roles import ROLES, RoleSpec

ROLES.register(
    RoleSpec(
        name="buyer",
        stage="run",
        phases=["push", "pull", "act"],
        tools=["get_posted_prices", "buy"],
        output_events=["observed", "tool_called", "llm_call", "acted", "outcome"],
        research_allowed=False,
        built=False,
        notes="Extension (S2/S3). With buyer agents the market uses agent demand, so layer-B references do not exist.",
    )
)
