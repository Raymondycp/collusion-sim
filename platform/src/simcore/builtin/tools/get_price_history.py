from simcore.tools import SELLER, TOOLS, AnyRounds, NoParams, Tool

TOOLS.register(
    Tool(
        id="get_price_history",
        version="1",
        kind="read",
        roles=SELLER,
        params_model=NoParams,
        input_model=AnyRounds,
        description="Prices by seller for past rounds: your own and, if you can see them, the rival prices.",
        handler=lambda view, p, a: view.price_history(a.rounds),
        query=True,
    )
)
