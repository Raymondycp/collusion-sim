from simcore.tools import SELLER, TOOLS, PostGrant, PostInput, Tool

TOOLS.register(
    Tool(
        id="post",
        version="1",
        kind="write",
        roles=SELLER,
        params_model=PostGrant,
        input_model=PostInput,
        description="Post one message to a chat channel. Other agents see it next round.",
    )
)
