from __future__ import annotations

from datetime import datetime
import json

from maa.custom_action import CustomAction
from registration import agent_action
from task_control import task_time as time
from daily.runtime import Executors, ROOT


@agent_action("nikki.start_game", stop_on_failure=True)
class StartGameAction(CustomAction):
    def run(self, context, argv):
        runtime = Executors(context)
        options = argv.custom_action_param
        options = json.loads(options or "{}") if isinstance(options, str) else options or {}
        timeout = int(options.get("timeout", 900))
        bootstrap = options.get("bootstrap") is True
        self.restart_requested = False
        self.elapsed_seconds = 0.0
        if not 30 <= timeout <= 900:
            runtime.log("启动等待上限应在30至900秒之间。")
            context.tasker.post_stop()
            return False
        started_at = time.monotonic()
        deadline = started_at+timeout
        entered, logged_in, update_attempts = 0, 0, 0
        update_waiting = False
        waiting_logged = False
        intro_logged = False
        unknown_since = None

        def incomplete(reason="尚未完成进入游戏"):
            if not runtime.stopped:
                import cv2
                folder = ROOT / "logs/startup"
                folder.mkdir(parents=True, exist_ok=True)
                name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-enter"
                record = {
                    "stage": "enter_game", "success": False, "reason": reason,
                    "enter_attempts": entered, "login_attempts": logged_in,
                    "update_attempts": update_attempts,
                }
                image = runtime.capture()
                if image is not None and cv2.imwrite(str(folder / (name+".png")), image):
                    record["screenshot"] = "logs/startup/"+name+".png"
                (folder / (name+".json")).write_text(
                    json.dumps(record, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                runtime.log(f"{reason}，本轮停止；记录：logs/startup/{name}.json。")
                context.tasker.post_stop()
            return False

        def complete():
            if bootstrap:
                self.elapsed_seconds = time.monotonic()-started_at
                return True
            _, finished = runtime.finish("MaaNikki_StartGame")
            return finished

        def transition_ready():
            with runtime.observe():
                if runtime.hit("MaaNikki_StartGame_RestartConfirm"):
                    return True
                if runtime.hit("MaaNikki_StartGame_UpdateTitle"):
                    return runtime.hit("MaaNikki_StartGame_UpdateConfirm")
                return (runtime.hit("MaaNikki_MainDetected")
                        or runtime.hit("MaaNikki_RewardLayer")
                        or runtime.ui.asset("IconUILoading", threshold=.99)
                        or runtime.hit("MaaNikki_StartGame_Enter"))

        def advance(node, destination):
            return runtime.ui.enter_page(
                lambda: runtime.click_template(node, attempts=1, wait_seconds=0, verify=False),
                destination, source=node, attempts=1,
                seconds=max(.1, deadline-time.monotonic()))

        def entered_game():
            with runtime.observe():
                if runtime.hit("MaaNikki_StartGame_RestartConfirm"):
                    return True
                if runtime.hit("MaaNikki_StartGame_UpdateTitle"):
                    return runtime.hit("MaaNikki_StartGame_UpdateConfirm")
                return not runtime.hit("MaaNikki_StartGame_Enter") and transition_ready()

        def confirm_update():
            nonlocal update_attempts
            if update_attempts >= 3:
                return False
            update_attempts += 1
            return runtime.click_template(
                "MaaNikki_StartGame_UpdateConfirm", attempts=1, wait_seconds=0, verify=False)

        while not runtime.stopped and time.monotonic() < deadline:
            with runtime.observe(fresh=True):
                restart = runtime.hit("MaaNikki_StartGame_RestartConfirm")
                update = not restart and runtime.hit("MaaNikki_StartGame_UpdateTitle")
                reward = not restart and not update and runtime.hit("MaaNikki_RewardLayer")
                main = not restart and not update and runtime.hit("MaaNikki_MainDetected")
                enter = not update and not main and runtime.hit("MaaNikki_StartGame_Enter")
                login = not update and not main and not enter and runtime.hit("MaaNikki_StartGame_Login")
                intro = (not restart and not update and not reward and not main
                         and not enter and not login and runtime.hit("MaaNikki_StartGame_Intro"))
            if restart or update or reward or main or enter or login or intro:
                unknown_since = None
            if restart:
                if not bootstrap or options.get("allow_restart", True) is not True:
                    # The regular batch must not continue on a window that is
                    # about to disappear. Startup normally handles this earlier.
                    return incomplete("更新需要重启，请重新运行启动游戏任务")
                runtime.log("资源更新完成，正在确认重启并等待新的游戏窗口。")
                result = runtime.recognize("MaaNikki_StartGame_RestartConfirm")
                if not result or not result.hit:
                    continue
                box = list(result.box)
                point = [box[0]+box[2]//2, box[1]+box[3]//2]
                if not runtime.action("TouchMove", target=point) or not runtime.pause(.2):
                    return incomplete("未能点击更新重启确认")
                result = runtime.recognize("MaaNikki_StartGame_RestartConfirm")
                if not result or not result.hit:
                    continue
                box = list(result.box)
                self.elapsed_seconds = time.monotonic()-started_at
                # Record intent before submitting: the game can close before
                # the click response. Do not capture, unhover or wait on it again.
                self.restart_requested = True
                runtime.action("Click", ui_click=False, target=[
                    box[0]+box[2]//2, box[1]+box[3]//2])
                return True
            elif update:
                if not update_waiting:
                    runtime.log("检测到客户端资源更新，正在确认并等待下载完成。")
                    update_waiting = True
                if runtime.hit("MaaNikki_StartGame_UpdateConfirm"):
                    if update_attempts >= 3:
                        return incomplete("客户端资源更新确认未生效")
                    # Wait for the known prompt to leave; download duration is
                    # bounded by the startup deadline, with no Esc recovery.
                    if not runtime.ui.enter_page(
                            confirm_update,
                            lambda: not runtime.hit("MaaNikki_StartGame_UpdateConfirm"),
                            source="MaaNikki_StartGame_UpdateConfirm", attempts=3,
                            seconds=min(5, max(.1, deadline-time.monotonic()))):
                        return incomplete("客户端资源更新确认未生效")
            elif reward:
                if not runtime.key("skip", .5):
                    return incomplete()
            elif main:
                if runtime.ui.wait_page("MaaNikki_MainDetected"):
                    runtime.log("已进入游戏主页。" if waiting_logged
                                else "游戏已在主页，跳过启动流程，继续后续任务。")
                    return complete()
            elif enter and entered < 3:
                entered += 1
                if not advance("MaaNikki_StartGame_Enter", entered_game):
                    return incomplete()
            elif login and logged_in < 3:
                logged_in += 1
                if not advance("MaaNikki_StartGame_Login", transition_ready):
                    return incomplete()
            elif intro:
                if not intro_logged:
                    runtime.log("正在播放健康提示或光敏警告，等待启动画面结束。")
                    intro_logged = True
            elif any(runtime.hit(node) for node in (
                    "MaaNikki_MenuReady", "MaaNikki_CalendarReady", "MaaNikki_Monthly_PageReady")):
                if runtime.main():
                    return complete()
                return incomplete()
            elif not enter and not login:
                if runtime.ui.asset("IconUILoading", threshold=.99):
                    unknown_since = None
                else:
                    # Allow a short unrecognized transition before invoking
                    # the ordinary, bounded recovery used by other tasks.
                    now = time.monotonic()
                    unknown_since = now if unknown_since is None else unknown_since
                    if now-unknown_since >= 3:
                        runtime.log("当前为未知页面，正在尝试正常恢复到游戏主页。")
                        if runtime.main():
                            runtime.log("已恢复到游戏主页，继续后续任务。")
                            return complete()
                        # Recovery can overlap a cold-start transition. The
                        # login/enter screen needs the startup handler, not Esc.
                        with runtime.observe(fresh=True):
                            startup_ready = (transition_ready()
                                             or runtime.hit("MaaNikki_StartGame_Login")
                                             or runtime.hit("MaaNikki_StartGame_Intro"))
                        if startup_ready:
                            unknown_since = None
                            runtime.log("已识别启动页面，继续等待登录或进入游戏。")
                            continue
                        return incomplete("无法从未知页面恢复到游戏主页，请手动返回主页后重试")
            if not waiting_logged:
                runtime.log("正在等待进入游戏；切出窗口会暂停，切回后继续。")
                waiting_logged = True
            if not runtime.pause(.5):
                return False
        return incomplete("进入游戏主页超时，请检查登录或更新提示")
