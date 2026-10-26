from simcore.tools import SELLER, TOOLS, HoldInput, Tool
from simschema.base import NoParams

TOOLS.register(
    Tool(
        id="hold",
        version="1",
        kind="write",
        roles=SELLER,
        params_model=NoParams,
        input_model=HoldInput,
        description="Keep your price of the last round.",
    )
)
