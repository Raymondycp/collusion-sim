from simcore.tools import SELLER, TOOLS, NoParams, PriceInput, Tool

TOOLS.register(
    Tool(
        id="calc_profit",
        version="1",
        kind="read",
        roles=SELLER,
        params_model=NoParams,
        input_model=PriceInput,
        description="Your expected units sold and profit at a price, for given rival prices (default: the rival prices you saw last"
        " round). Uses the base demand, without cost or demand changes.",
        handler=lambda view, p, a: view.profit_at(a.price, a.rival_prices),
        query=False,
    )
)
