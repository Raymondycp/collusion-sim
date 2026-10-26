from simcore.tools import SELLER, TOOLS, Depth, Rounds, Tool

TOOLS.register(
    Tool(
        id="get_rival_prices",
        version="1",
        kind="read",
        roles=SELLER,
        params_model=Depth,
        input_model=Rounds,
        description="Rival prices of the last rounds (never the current round).",
        handler=lambda view, p, a: view.rival_prices(a.rounds),
    )
)
