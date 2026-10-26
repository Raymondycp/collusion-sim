from simcore.tools import SELLER, TOOLS, NoParams, NoteInput, Tool

TOOLS.register(
    Tool(
        id="write_note",
        version="1",
        kind="write",
        roles=SELLER,
        params_model=NoParams,
        input_model=NoteInput,
        description="Save a private note. Only you can read it, with read_note, in a later call.",
        handler=lambda view, p, a: view.write_note(a.text),
        query=False,
        private=True,
    )
)
