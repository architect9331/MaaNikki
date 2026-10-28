import nonebot
from nonebot.adapters.qq import Adapter

from settings import CONFIG

nonebot.init(
    driver="~fastapi+~httpx+~websockets",
    host=CONFIG.get("host", "127.0.0.1"),
    port=CONFIG.get("port", 18960),
    log_level="WARNING",
    qq_bots=[
        {
            "id": CONFIG["app_id"],
            "secret": CONFIG["app_secret"],
            "use_websocket": True,
            "intent": {
                "guilds": False,
                "guild_members": False,
                "guild_message_reactions": False,
                "message_audit": False,
                "at_messages": False,
                "c2c_group_at_messages": True,
            },
        }
    ],
)
nonebot.get_driver().register_adapter(Adapter)
if nonebot.load_plugin("github_notifier") is None:
    raise RuntimeError("GitHub notifier plugin failed to load")

if __name__ == "__main__":
    nonebot.run()
