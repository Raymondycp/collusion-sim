from simcore.tools import SELLER, TOOLS, NoParams, Tool

TOOLS.register(
    Tool(
        id="read_note",
        version="1",
        kind="read",
        roles=SELLER,
        params_model=NoParams,
        input_model=NoParams,
        description="Your private notes from earlier calls.",
        handler=lambda view, p, a: view.notes(),
        query=False,
        private=True,
    )
)
