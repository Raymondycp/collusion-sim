from simcore.tools import SELLER, TOOLS, Depth, Rounds, Tool

TOOLS.register(
    Tool(
        id="get_own_sales",
        version="1",
        kind="read",
        roles=SELLER,
        params_model=Depth,
        input_model=Rounds,
        description="Your own price, sales and profit in the last rounds.",
        handler=lambda view, p, a: view.own_sales(a.rounds),
    )
)
