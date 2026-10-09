"""Shared foreground startup for ordinary routes and developer tests."""
import time

from navigation.models import NavigationError


def focus_game(runtime, inputs):
    if runtime.stopped:
        raise NavigationError("任务已停止。")
    runtime.log("正在自动聚焦已连接的游戏窗口。")
    if not inputs.foreground():
        # Seize activates the connected window on key-up too. Releasing Alt
        # avoids the old cursor warp to the top-left during scene startup.
        runtime.invalidate_frame()
        if not runtime.controller.post_key_up(18).wait().succeeded:
            raise NavigationError("未能自动聚焦游戏，请检查窗口连接和前台输入设置。")
    deadline, stable_since = time.monotonic()+3, None
    while time.monotonic() < deadline and not runtime.stopped:
        now = time.monotonic()
        if inputs.foreground():
            stable_since = now if stable_since is None else stable_since
            if now-stable_since >= .3:
                break
        else:
            stable_since = None
        time.sleep(.05)
    inputs.check()
    if stable_since is None or time.monotonic()-stable_since < .3:
        raise NavigationError("未能确认游戏已自动聚焦，请检查窗口连接和权限。")
