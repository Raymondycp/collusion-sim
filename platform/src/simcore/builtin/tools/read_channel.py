from simcore.tools import SELLER, TOOLS, ChannelId, Channels, Tool

TOOLS.register(
    Tool(
        id="read_channel",
        version="1",
        kind="read",
        roles=SELLER,
        params_model=Channels,
        input_model=ChannelId,
        description="Messages in one chat channel that you can read.",
        handler=lambda view, p, a: view.messages(a.channel_id) if a.channel_id in view.readable_channels() else None,
    )
)
