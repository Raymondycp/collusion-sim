from simcore.roles import ROLES, RoleSpec

ROLES.register(
    RoleSpec(
        name="host",
        stage="run",
        phases=["news", "settle"],
        tools=[],
        output_events=["round_started", "news_published", "intervened", "settled", "reference"],
        research_allowed=True,
        research_scripted_only=True,
        built=True,
        notes="Deterministic by default. An LLM host may only rephrase frozen news, in exploratory runs only.",
    )
)
