from simcore.tools import SELLER, TOOLS, SetPriceInput, Tool
from simschema.base import NoParams

TOOLS.register(
    Tool(
        id="set_price",
        version="1",
        kind="write",
        roles=SELLER,
        params_model=NoParams,
        input_model=SetPriceInput,
        description="Set your price for this round.",
    )
)
