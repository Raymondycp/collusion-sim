from simcore.tools import SELLER, TOOLS, Tool
from simschema.base import NoParams

TOOLS.register(
    Tool(
        id="get_news",
        version="1",
        kind="read",
        roles=SELLER,
        params_model=NoParams,
        input_model=NoParams,
        description="News that you received in the last rounds.",
        handler=lambda view, p, a: view.news(),
    )
)
