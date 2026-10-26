from simcore.roles import ROLES, RoleSpec

ROLES.register(
    RoleSpec(
        name="seller",
        stage="run",
        phases=["talk", "act", "settle"],
        tools=[
            "get_rival_prices",
            "get_news",
            "read_channel",
            "get_own_sales",
            "set_price",
            "hold",
            "post",
            "calc_profit",
            "get_price_history",
            "write_note",
            "read_note",
        ],
        output_events=[
            "observed",
            "tool_called",
            "llm_call",
            "posted",
            "talk_replied",
            "talk_text",
            "acted",
            "planned",
            "budget_warning",
            "memory_summarised",
        ],
        research_allowed=True,
        built=True,
        notes="A seller observes and reads (push and pull) inside its talk and act calls. In settle it updates its memory.",
    )
)
