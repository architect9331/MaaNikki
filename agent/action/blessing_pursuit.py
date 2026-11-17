"""Guarded realm collection step: organize when needed, then farm and upgrade."""
from datetime import datetime
import json

import cv2
from maa.custom_action import CustomAction

from registration import agent_action
from task_control import TaskStopped
from daily.runtime import Executors, parameters
from daily.settings import ROOT
from collection.pursuit import FiveStarPursuit
from collection.decompose import DuplicateDecomposer, LowRarityDecomposer
from collection.inventory_workflow import FiveStarInventory
from collection.organize import organize_inventory
from collection.scanner import RescanSlot
from collection.statistics import is_enabled as stats_enabled, upload
from collection.storage import InventoryStore
from collection.blessing import SLOTS
from account_state import AccountState, game_day

ORGANIZE_OPTIONS = {"auto": None, "always": False, "always_unlock": True}


def blessing_mode(role: dict) -> str:
    """Return active 祝福闪光 plan mode: ``all``, ``power`` or ``selected``."""
    plan = (role.get("materials") or {}).get("blessing_plan")
    if isinstance(plan, dict) and plan.get("mode") in ("power", "selected"):
        return plan["mode"]
    return "all"


def blessing_focus(role: dict) -> list[str] | None:
    """Set names chosen in the 祝福闪光 自选 plan; None means 全收集 or 战力."""
    if blessing_mode(role) != "selected":
        return None
    plan = (role.get("materials") or {}).get("blessing_plan") or {}
    return [str(name) for name in plan.get("sets") or [] if isinstance(name, str)]


def glow_shortfall(rt, role: dict, organize_mode: str = "auto") -> bool:
    """Whether the blessing goal still has colours/kinds to chase (or must organize first)."""
    if organize_mode != "auto":
        return True
    from data_paths import data_path
    catalog = json.loads(data_path("resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
    targets = [item for item in catalog["sets"] if item["rarity"] == 5 and item.get("collection_target", True)]
    mode = blessing_mode(role)
    focus = blessing_focus(role)
    if focus is not None:
        targets = [item for item in targets if item["name"] in set(focus)]
        if not targets:
            return False
    try:
        order, protected, _ = InventoryStore(ROOT, catalog, rt).load_organization()
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if order.get("pending_operation") is not None or (order.get("recovery") or {}).get("slots"):
        return True
    if mode == "power":
        return any(
            not any((item["name"], slot, color) in protected for color in item["colors"])
            for item in targets
            for slot in SLOTS
        )
    return any((item["name"], slot, color) not in protected
               for item in targets for slot in SLOTS for color in item["colors"])


def fallback_realm(context, rt, node):
    """Spend the energy on the configured realm; settlement still reports the failure."""
    return context.override_pipeline({
        node: {"next": ["MaaNikki_Bless"]},
        "MaaNikki_Bless": {"next": ["MaaNikki_RealmReviewFailed"],
                           "on_error": ["MaaNikki_RealmReviewFailed"],
                           "custom_action_param": {"kind": "bless", "return_page": "calendar"}}})


def organization_issue(rt, catalog):
    try:
        InventoryStore(ROOT, catalog, rt).load_organization()
    except (OSError, ValueError, KeyError, TypeError) as error:
        return str(error)
    return None


@agent_action("nikki.blessing_pursuit")
class BlessingPursuitAction(CustomAction):
    def run(self, context, argv):
        rt = Executors(context)
        folder = ROOT / "logs/blessing-pursuit" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        pursuit = None
        try:
            from data_paths import data_path
            options = parameters(context, argv.node_name)
            catalog = json.loads(data_path("resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            workflow = parameters(context, "MaaNikki_Pursuit_WorkflowOption").get("mode", "managed")
            farm_only = workflow == "farm_only"
            raw_reserve = parameters(context, "MaaNikki_Pursuit_ReserveBlingOption").get("amount", 0)
            try:
                reserve_bling = max(0, int(str(raw_reserve or 0).strip().replace(",", "")))
            except (ValueError, TypeError):
                reserve_bling = 0
            organize = parameters(context, "MaaNikki_Pursuit_OrganizeOption").get("mode", "auto")
            if organize not in ORGANIZE_OPTIONS:
                raise ValueError("未知祝福闪光整理选项")
            issue = None if organize != "auto" else organization_issue(rt, catalog)
            if organize != "auto" or issue is not None:
                if farm_only:
                    rt.log("正在读取全部祝福闪光：扫描头部、手部、脚部建立仓库记录（只刷胚子模式不修改锁定）。",
                           ui_text="正在读取全部祝福闪光。")
                else:
                    rt.log("正在整理全部祝福闪光：读取头部、手部、脚部并锁定每款每色的满级收藏。",
                           ui_text="正在整理全部祝福闪光。")
                if issue is not None:
                    rt.log(f"需要整理的原因：{issue}。", detail=True)
                result = organize_inventory(
                    rt,
                    catalog,
                    allow_unlock=False if farm_only else bool(ORGANIZE_OPTIONS[organize]),
                    read_only=farm_only,
                )
                if not result["published"] and organization_issue(rt, catalog) is not None:
                    reason = result.get("reason")
                    suffix = f"（{reason}）" if reason else ""
                    if result.get("warehouse_full"):
                        rt.log(f"整理未完成且祝福闪光仓库已满{suffix}，本轮无法刷取祝福闪光副本，下次运行重新整理。",
                               ui_text="整理未完成且仓库已满，本轮不刷祝福闪光副本。")
                        if options.get("return_page") == "calendar":
                            rt.ui.calendar()
                        else:
                            rt.finish(argv.node_name)
                        return False
                    rt.log(f"整理未完成且没有可用记录{suffix}，本轮改用「默认副本」刷取，下次运行重新整理。",
                           ui_text="整理未完成，本轮改刷默认副本。")
                    return fallback_realm(context, rt, argv.node_name)
                if not result["published"]:
                    reason = result.get("reason")
                    suffix = f"（{reason}）" if reason else ""
                    rt.log(f"本次整理未完成{suffix}，继续使用上一份有效记录。")
            pursuit = FiveStarInventory(rt, catalog, folder)
            decompose = parameters(context, "MaaNikki_Pursuit_DecomposeOption").get("enabled") is True
            auto_bling = parameters(context, "MaaNikki_Pursuit_BlingOption").get("enabled") is not False
            share = stats_enabled(
                ROOT,
                parameters(context, "MaaNikki_Pursuit_StatsOption").get("enabled") is not False,
            )
            role_state = AccountState.for_runtime(ROOT, rt).load()
            flow = FiveStarPursuit(pursuit, decompose_duplicates=decompose, auto_bling=auto_bling,
                                  chance_priority=True, share_statistics=share,
                                  farm_only=farm_only, reserve_bling=reserve_bling,
                                  focus_sets=blessing_focus(role_state),
                                  power_mode=(blessing_mode(role_state) == "power"))
            ok = flow.run()
            if not ok and not rt.stopped:
                rt.diagnostic("blessing_pursuit", "祝福闪光处理未完成")
            progress_path = pursuit.report.get("inventory", {}).get("path") or pursuit.report.get("startup", {}).get("path")
            rt.log(f"五星处理记录：{folder / 'pursuit.json'}。"
                   f"收集进度：{progress_path or pursuit.store.reference(pursuit.store.last_scan_path)}。", detail=True)
            if options.get("return_page") == "calendar":
                if not rt.ui.calendar() or not ok:
                    return False
                if options.get("resume_queue") and flow.target_complete():
                    # Completion is explicit: a normal pause (energy/materials)
                    # must not start a second pursuit or skip unfinished colours.
                    return context.override_pipeline({
                        argv.node_name: {"next": ["MaaNikki_RealmQueue"]},
                        "MaaNikki_RealmQueue": {"custom_action_param": {
                            **parameters(context, "MaaNikki_RealmQueue"), "after_goal": "glow"}}})
                return True
            _, finished = rt.finish(argv.node_name)
            return finished and ok
        except TaskStopped as error:
            if pursuit is not None:
                pursuit.record_interruption(error, reason="用户停止，进度已保存")
            raise
        except (RescanSlot, OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            reason = "焦点变化使当前升级或列表状态失效，请重新运行核对" if isinstance(error, RescanSlot) else str(error)
            if not rt.stopped:
                rt.diagnostic("blessing_pursuit", reason)
            if pursuit is not None:
                pursuit.record_interruption(error, reason=reason)
                try:
                    pursuit.image("interrupted.jpg", rt.capture())
                except (OSError, RuntimeError, cv2.error):
                    pass
            rt.log(f"五星处理暂停：{reason}。已保存操作记录：{folder}。",
                   ui_text=f"五星处理暂停：{reason}。")
            # A failure before any realm submission leaves the energy unspent.
            farmed = pursuit is not None and any(
                op.get("kind") == "realm" and op.get("input_attempted") is not False
                for op in pursuit.report["operations"])
            capacity = (pursuit.organization or {}).get("capacity") if pursuit is not None else None
            warehouse_full = bool(
                (pursuit is not None and pursuit.report.get("warehouse_full"))
                or (isinstance(capacity, list) and len(capacity) == 2 and capacity[0] >= capacity[1]))
            if not rt.stopped and not farmed and not warehouse_full:
                rt.log("本轮尚未刷取，改用「默认副本」刷取，已保存的进度下次继续。",
                       ui_text="全收集流程未完成，本轮改刷默认副本。")
                return fallback_realm(context, rt, argv.node_name)
            if not rt.stopped:
                if options.get("return_page") == "calendar":
                    rt.ui.calendar()
                else:
                    rt.finish(argv.node_name)
            return False
        finally:
            if pursuit is not None:
                try:
                    pursuit.log_progress("本轮结束" if pursuit.report.get("complete") else "本轮中断")
                    pursuit.record_history()
                    if pursuit.share_statistics:
                        upload(pursuit.store.account, ROOT, catalog_version=pursuit.catalog["version"])
                    # Save confirmed keepers and scoped recovery independently
                    # of stale, untracked material quantities.
                    if pursuit.prepared and "inventory" not in pursuit.report:
                        pursuit.checkpoint()
                    else:
                        pursuit.save()
                except (OSError, ValueError, KeyError, TypeError) as error:
                    rt.log(f"五星追全进度保存失败：{error}。")


@agent_action("nikki.blessing_maintain")
class BlessingMaintainAction(CustomAction):
    """Zero-stamina warehouse organize / upgrade embryos / decompose surplus triggered from UI."""

    def run(self, context, argv):
        rt = Executors(context)
        folder = ROOT / "logs/blessing-maintain" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        folder.mkdir(parents=True, exist_ok=True)
        pursuit = None
        try:
            options = parameters(context, argv.node_name)
            mode = str(options.get("mode", "organize"))
            if mode not in ("organize", "upgrade", "decompose"):
                raise ValueError(f"未知的祝福闪光整理动作：{mode}")
            from data_paths import data_path
            catalog = json.loads(data_path("resource/data/blessing_catalog.json").read_text(encoding="utf-8"))
            raw_reserve = parameters(context, "MaaNikki_Pursuit_ReserveBlingOption").get("amount", 0)
            try:
                reserve_bling = max(0, int(str(raw_reserve or 0).strip().replace(",", "")))
            except (ValueError, TypeError):
                reserve_bling = 0

            if mode == "organize":
                rt.log("祝福闪光整理：正在检查头部、手部、脚部并锁定新增的五星满级颜色。")
                result = organize_inventory(rt, catalog, allow_unlock=False, read_only=False)
                if result["published"]:
                    try:
                        _, protected, _ = InventoryStore(ROOT, catalog, rt).load_organization()
                        AccountState.for_runtime(ROOT, rt).update(
                            lambda data: data["observations"]
                            .setdefault("collection_history", {})
                            .__setitem__(str(game_day()), {"owned": len(protected)})
                        )
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                if not rt.stopped:
                    rt.main()
                if not result["published"]:
                    reason = result.get("reason")
                    suffix = f"（{reason}）" if reason else ""
                    if organization_issue(rt, catalog) is not None:
                        rt.log(
                            f"祝福闪光整理未完成且仓库已满{suffix}，缺少可用仓库记录。"
                            if result.get("warehouse_full")
                            else f"祝福闪光整理未完成{suffix}，缺少可用仓库记录。"
                        )
                    else:
                        rt.log(f"祝福闪光整理未完成{suffix}，已保留上一份有效记录。")
                    return False
                _, finished = rt.finish(argv.node_name)
                return bool(result["published"]) and finished

            issue = organization_issue(rt, catalog)
            if issue is not None:
                rt.log("祝福闪光仓库缺少完整排序记录，先执行一次整理与锁定。")
                result = organize_inventory(rt, catalog, allow_unlock=False, read_only=False)
                if not result["published"] and organization_issue(rt, catalog) is not None:
                    rt.log("仓库整理未完成且没有可用记录，暂不能执行后续操作。")
                    if not rt.stopped:
                        rt.main()
                    return False

            pursuit = FiveStarInventory(rt, catalog, folder)
            pursuit.reserve_bling = reserve_bling
            pursuit.share_statistics = stats_enabled(
                ROOT,
                parameters(context, "MaaNikki_Pursuit_StatsOption").get("enabled") is not False,
            )
            pursuit.prepare(f"manual_{mode}")

            if pursuit.organization.get("decomposition_pending") or any(
                row["rarity"] <= 4 and row["level"] == 0 and not row["locked"] and row["mutable"]
                for rows in pursuit.organization.get("slots", {}).values()
                for row in rows
            ):
                LowRarityDecomposer(pursuit).run()

            if mode == "upgrade":
                if pursuit.queued_rolls():
                    outcome = pursuit.process_inventory()
                    reasons = {
                        "collection_complete": "图鉴内五星颜色已收齐",
                        "bling_shortage": (
                            f"当前噗灵扣除保留数（{reserve_bling}）后不足继续升级，已保存进度"
                            if reserve_bling > 0
                            else "升级噗灵不足，已保存进度"
                        ),
                        "materials_shortage": "升级材料（四星及以下素材）不足，已保存进度",
                        "no_embryos": "当前可升级的缺色五星胚子已全部处理完成",
                    }
                    msg = reasons.get(outcome["status"], "仓库胚子升级阶段已完成")
                else:
                    rt.log("当前仓库暂无需要升级的缺色五星胚子。")
                    msg = "仓库中暂无待升级的缺色五星胚子，已保存状态"
                ok = pursuit.finish(msg)
            else:
                # DuplicateDecomposer handles 5-star unlocked surplus only when no pending 5-star embryos await upgrade.
                if (
                    pursuit.pending is None
                    and not pursuit.organization.get("recovery", {}).get("slots")
                    and not pursuit.organization.get("unfinished")
                    and not pursuit.queued_rolls()
                ):
                    for _ in range(10):
                        if rt.stopped or not DuplicateDecomposer(pursuit).run():
                            break
                        copies = (pursuit.report.get("duplicate_decomposition") or {}).get("copies", 0)
                        if copies < 100:
                            break
                    msg = "已完成低星素材与多余五星闪光清理"
                else:
                    rt.log(
                        f"已完成四星及以下清理；尚有 {pursuit.queued_rolls()} 个未升级五星胚子等待升级，已保留未分解。"
                    )
                    msg = "已完成四星及以下清理，待升级五星胚子已保留"
                ok = pursuit.finish(msg)

            if not rt.stopped:
                rt.main()
            _, finished = rt.finish(argv.node_name)
            return bool(ok) and finished
        except TaskStopped as error:
            if pursuit is not None:
                pursuit.record_interruption(error, reason="用户停止，进度已保存")
            raise
        except (RescanSlot, OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            reason = "前台变化使当前升级或列表状态失效，请重试" if isinstance(error, RescanSlot) else str(error)
            if not rt.stopped:
                rt.diagnostic("blessing_maintain", reason)
            if pursuit is not None:
                pursuit.record_interruption(error, reason=reason)
            rt.log(f"祝福闪光仓库整理暂停：{reason}。")
            if not rt.stopped:
                rt.main()
            return False
        finally:
            if pursuit is not None:
                try:
                    pursuit.log_progress("本轮结束" if pursuit.report.get("complete") else "本轮中断")
                    pursuit.record_history()
                    if pursuit.share_statistics:
                        upload(pursuit.store.account, ROOT, catalog_version=pursuit.catalog["version"])
                    if pursuit.prepared and "inventory" not in pursuit.report:
                        pursuit.checkpoint()
                    else:
                        pursuit.save()
                except (OSError, ValueError, KeyError, TypeError) as error:
                    rt.log(f"祝福闪光维护进度保存失败：{error}。")
