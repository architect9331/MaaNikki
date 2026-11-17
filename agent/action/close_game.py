"""「关闭游戏」任务：只关闭当前控制器连接的游戏实例。"""
from __future__ import annotations

import os

from client_config import parse_custom_param
from daily.runtime import Runtime
from game_close import (Win32Api, bound_game_hwnd, bring_maannikki_forward,
                        close_connected_game, close_launcher, launcher_eligible,
                        resolve_official_launcher)
from registration import agent_action

from maa.custom_action import CustomAction


def _restore_window_size(api):
    """Undo this session's 16:9 auto-adjustment before the game can persist it.

    A hung window is skipped because SetWindowPos would block on it.
    """
    def restore(hwnd):
        if api.is_hung(hwnd):
            return
        from tall_window import restore_session_16_9_windows
        restore_session_16_9_windows()
    return restore


@agent_action("nikki.close_game")
class CloseGameAction(CustomAction):
    """Runs only when the queue reaches it; it is not a finally/posttask hook."""

    def run(self, context, argv: CustomAction.RunArg) -> bool:
        runtime = Runtime(context)
        try:
            options = parse_custom_param(argv.custom_action_param)
        except ValueError as error:
            runtime.log(f"关闭游戏参数无效：{error}")
            return False
        if os.name != "nt":
            runtime.log("关闭游戏任务仅支持 Windows。")
            return False
        force = options.get("force_on_timeout") is True
        close_launcher_too = options.get("close_launcher") is True
        api = Win32Api()
        result = close_connected_game(
            api, bound_game_hwnd(runtime.controller),
            force_on_timeout=force,
            stopped=lambda: runtime.stopped,
            before_close=_restore_window_size(api),
            on_countdown=lambda remaining: runtime.log(f"关闭游戏倒计时：{remaining} 秒"),
            bring_forward=lambda: self._bring_forward(api, runtime),
            on_window_gone=lambda: runtime.log("游戏窗口已关闭，等待其进程退出…"),
        )
        self._maybe_close_launcher(runtime, api, result, close_launcher_too)
        if not result.ok and result.status != "cancelled":
            # FailureEvidence never captures; this action keeps no frame either.
            runtime.diagnostic("close_game", result.message, details={
                "status": result.status, "force_on_timeout": force, **result.details})
        runtime.log(result.message)
        return result.ok

    @staticmethod
    def _bring_forward(api, runtime):
        outcome = bring_maannikki_forward(api)
        if outcome == "activated":
            runtime.log("MaaNikki 窗口已置前。")
        elif outcome == "activate_failed":
            runtime.log("找到 MaaNikki 窗口但置前失败，继续执行。")
        else:
            runtime.log("未找到 MaaNikki 窗口，跳过置前。")

    @staticmethod
    def _maybe_close_launcher(runtime, api, result, close_launcher_too):
        if not close_launcher_too:
            return
        if launcher_eligible(result):
            launcher_path = resolve_official_launcher(result.details.get("image"))
            if launcher_path is None:
                runtime.log("未能确认与本次游戏实例关联的官方启动器，跳过关闭。")
                return
            runtime.log(close_launcher(api, launcher_path=launcher_path,
                                       stopped=lambda: runtime.stopped).message)
            return
        if result.details.get("close_requested"):
            runtime.log("已发送游戏关闭请求但未成功退出，跳过关闭启动器。")
        else:
            runtime.log("本次未实际发送关闭请求，跳过关闭启动器。")
