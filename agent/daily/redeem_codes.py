"""Automatically sync and redeem Infinity Nikki gift codes in 设置 -> 其他 -> 兑换码."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
import re
from task_control import TaskStopped, task_time as time
import urllib.error
import urllib.request

import cv2
import numpy as np

from account_state import AccountState, GAME_TIMEZONE
from adventure_report import try_record_adventure_entry
from daily.settings import ROOT
from data_paths import data_path


BILIWIKI_REDEEM_URL = (
    "https://wiki.biligame.com/wxnn/api.php"
    "?action=parse&page=%E5%85%91%E6%8D%A2%E7%A0%81%E5%90%88%E9%9B%86&prop=wikitext&format=json"
)
QQBOT_REDEEM_URL = "https://qqbot.maanikki.cn/v1/redeem-codes"

PASTE_BUTTON_TARGET = [831, 340]
INPUT_BOX_TARGET = [614, 342]
MODAL_USE_TARGET = [639, 469]
MODAL_CLOSE_TARGET = [854, 244]

ERROR_BAND_SLICE = (364, 442, 420, 860)  # y0, y1, x0, x1
ERROR_OCR_ROI = [405, 356, 470, 92]
TOAST_OCR_ROI = [340, 55, 600, 175]
USE_COOLDOWN_SECONDS = 3.2

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_CODE_TOKEN_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff]{3,32}$")
_SUBOBJECT_RE = re.compile(r"\{\{#subobject:[^|}]*\|([^}]+)\}\}")

_COOLDOWN_RE = re.compile(r"频繁|稍后|冷却|太快|间隔|请勿重复")
_USED_RE = re.compile(r"已使用|已被使用|已兑换|已领取|使用过|领取过|兑换过|上限|同批次|同类型|重复领取")
_EXPIRED_RE = re.compile(r"过期|失效|不在有效期|已结束|不存在|无效|错误|未开启|无法使用|不正确|未激活")


def _parse_expiry(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(GAME_TIMEZONE)
    text = str(value).strip().replace("/", "-").replace(" ", "T")
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T23:59:59+08:00"
    elif not re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", text, re.IGNORECASE):
        text += "+08:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone(GAME_TIMEZONE) if dt.tzinfo else dt.replace(tzinfo=GAME_TIMEZONE)


def _fetch_json(url: str, timeout: float = 4.5) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "MaaNikki/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_biliwiki_codes(now: datetime) -> tuple[list[str], set[str]]:
    data = _fetch_json(BILIWIKI_REDEEM_URL, timeout=4.5)
    wikitext = str(data.get("parse", {}).get("wikitext", {}).get("*", "") or "")
    active: list[str] = []
    expired: set[str] = set()
    for match in _SUBOBJECT_RE.finditer(wikitext):
        fields: dict[str, str] = {}
        for part in match.group(1).split("|"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
        if fields.get("平台") != "国服":
            continue
        code = fields.get("兑换码", "").strip()
        if not code or any(ch.isspace() for ch in code):
            continue
        exp = _parse_expiry(fields.get("期限"))
        if exp is not None and exp <= now:
            expired.add(code.lower())
        else:
            active.append(code)
    return active, expired


def _fetch_qqbot_codes(now: datetime, known_expired: set[str]) -> list[str]:
    data = _fetch_json(QQBOT_REDEEM_URL, timeout=4.5)
    now_ts = now.timestamp()
    found: list[str] = []
    for item in data.get("codes") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or any(ch.isspace() for ch in code):
            continue
        if code.lower() in known_expired:
            continue
        exp = _parse_expiry(item.get("expires_at"))
        if exp is not None:
            if exp <= now:
                known_expired.add(code.lower())
                continue
        else:
            pub = item.get("published_at")
            if isinstance(pub, (int, float)) and pub > 0 and (now_ts - float(pub)) > 30 * 86400:
                continue
        found.append(code)
    return found


def _load_local_catalog_codes(now: datetime, known_expired: set[str]) -> list[str]:
    path = data_path("resource/data/redeem_codes.json", ROOT)
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    found: list[str] = []
    for item in doc.get("codes") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or any(ch.isspace() for ch in code):
            continue
        if code.lower() in known_expired:
            continue
        exp = _parse_expiry(item.get("expires_at"))
        if exp is not None and exp <= now:
            known_expired.add(code.lower())
            continue
        found.append(code)
    return found


def parse_extra_codes(raw: str) -> list[str]:
    tokens = re.split(r"[,，;；\s]+", str(raw or "").strip())
    result: list[str] = []
    for tok in tokens:
        cleaned = tok.strip().strip("「」『』【】\"'`<>()")
        if cleaned and _CODE_TOKEN_RE.fullmatch(cleaned) and not cleaned.isdigit():
            result.append(cleaned)
    return result


def collect_candidate_codes(
    *,
    online: bool = True,
    extra_raw: str = "",
    now: datetime | None = None,
    log=None,
) -> list[tuple[str, bool]]:
    """Return deduplicated ``(code, is_manual)`` tuples ordered by freshness."""
    current = (now or datetime.now(timezone.utc)).astimezone(GAME_TIMEZONE)
    known_expired: set[str] = set()
    auto_codes: list[str] = []

    if online:
        try:
            wiki_active, wiki_expired = _fetch_biliwiki_codes(current)
            known_expired.update(wiki_expired)
            auto_codes.extend(wiki_active)
        except (OSError, urllib.error.URLError, ValueError) as error:
            if log:
                log(f"兑换码同步：百科列表暂时不可用（{error}），继续尝试其他来源。", detail=True)

        try:
            auto_codes.extend(_fetch_qqbot_codes(current, known_expired))
        except (OSError, urllib.error.URLError, ValueError) as error:
            if log:
                log(f"兑换码同步：实时公告源暂时不可用（{error}），继续读取本地清单。", detail=True)

    auto_codes.extend(_load_local_catalog_codes(current, known_expired))

    ordered: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for code in auto_codes:
        key = code.lower()
        if key in seen or key in known_expired:
            continue
        seen.add(key)
        ordered.append((code, False))
    for code in parse_extra_codes(extra_raw):
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append((code, True))
    return ordered


def _setup_win32_clipboard():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    return user32, kernel32


def _open_clipboard(user32) -> bool:
    for _ in range(5):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text() -> tuple[bool, str]:
    user32, kernel32 = _setup_win32_clipboard()
    if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return False, ""
    if not _open_clipboard(user32):
        return False, ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return False, ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False, ""
        try:
            return True, ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    user32, kernel32 = _setup_win32_clipboard()
    encoded = (str(text) + "\0").encode("utf-16-le")
    hmem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(encoded))
    if not hmem:
        return False
    ptr = kernel32.GlobalLock(hmem)
    if not ptr:
        kernel32.GlobalFree(hmem)
        return False
    try:
        ctypes.memmove(ptr, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(hmem)
    if not _open_clipboard(user32):
        kernel32.GlobalFree(hmem)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(_CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _error_band_stats(frame) -> tuple[int, int]:
    y0, y1, x0, x1 = ERROR_BAND_SLICE
    band = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    red_mask = (
        cv2.inRange(hsv, np.array([0, 70, 80], dtype=np.uint8), np.array([14, 255, 255], dtype=np.uint8))
        | cv2.inRange(hsv, np.array([166, 70, 80], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
    )
    red_px = int(cv2.countNonZero(red_mask))
    dark_px = int((gray < 175).sum())
    return red_px, dark_px


def _classify_hint(text: str) -> str | None:
    cleaned = "".join(str(text or "").split())
    if not cleaned:
        return None
    if _COOLDOWN_RE.search(cleaned):
        return "cooldown"
    if _USED_RE.search(cleaned):
        return "already_used"
    if _EXPIRED_RE.search(cleaned):
        return "expired"
    return None


class RedeemCodes:
    ENTRY = "MaaNikki_RedeemCodes"

    def __init__(self, runtime):
        self.rt, self.ui = runtime, runtime.ui
        self.account = AccountState.for_runtime(ROOT, runtime)
        self.last_use_at = 0.0

    def fail(self, reason: str) -> bool:
        if not self.rt.stopped:
            self.rt.diagnostic("redeem_codes", reason)
            self.rt.log(f"兑换码领取：{reason}，本项未完成。")
        return False

    def _other_ready(self) -> bool:
        with self.rt.observe():
            return bool(
                self.rt.hit("MaaNikki_Redeem_OtherReady")
                and not self.rt.hit("MaaNikki_Redeem_ModalTitle")
            )

    def _modal_ready(self) -> bool:
        with self.rt.observe():
            return bool(
                self.rt.hit("MaaNikki_Redeem_ModalReady")
                and not self.rt.hit("MaaNikki_RewardLayer")
                and not self.rt.hit("MaaNikki_Monthly_ParadiseReward")
            )

    def _open_other_tab(self) -> bool:
        if not self.ui.find_menu_entry(node="MaaNikki_GameKeys_SettingsEntrance"):
            return self.fail("未能进入设置页面")
        if self._other_ready():
            return True
        if self.rt.hit("MaaNikki_Redeem_OtherTab"):
            if self.rt.click_template("MaaNikki_Redeem_OtherTab", attempts=1):
                return True
        elif self.rt.hit("MaaNikki_Redeem_FunctionTab"):
            # Clicking 功能 scrolls the top tab strip right so 其他 becomes visible.
            self.rt.click_template("MaaNikki_Redeem_FunctionTab", attempts=1, wait_seconds=0.4, verify=False)
            if self.rt.hit("MaaNikki_Redeem_OtherTab") and self.rt.click_template("MaaNikki_Redeem_OtherTab", attempts=1):
                return True
        for _ in range(6):
            if self.rt.stopped:
                return False
            if self._other_ready():
                return True
            if not self.rt.key("E", 0.35):
                return False
        return self._other_ready() or self.fail("未能切换到设置的「其他」页")

    def _open_modal(self) -> bool:
        if self._modal_ready():
            return True
        if not self._open_other_tab():
            return False
        if not self.rt.click_template("MaaNikki_Redeem_OpenModal"):
            return self.fail("未能打开兑换奖励弹窗")
        return True

    def _reset_modal_if_dirty(self) -> bool:
        if not self._modal_ready():
            return self._open_modal()
        if self.rt.hit("MaaNikki_Redeem_ModalPlaceholder"):
            return True
        # Reopening the modal guarantees an empty input box without relying on Ctrl+A/Backspace.
        if not (self.rt.action("Click", target=MODAL_CLOSE_TARGET)
                and self.ui.wait_page(self._other_ready, seconds=4)):
            return self.fail("未能重置兑换码输入框")
        if not self.rt.click_template("MaaNikki_Redeem_OpenModal"):
            return self.fail("未能重新打开兑换奖励弹窗")
        return True

    def _fill_code(self, code: str) -> bool:
        pasted = False
        if set_clipboard_text(code):
            if not self.rt.action("Click", target=PASTE_BUTTON_TARGET) or not self.rt.pause(0.3):
                return False
            pasted = not self.rt.hit("MaaNikki_Redeem_ModalPlaceholder")
        if not pasted:
            if not (self.rt.action("Click", target=INPUT_BOX_TARGET)
                    and self.rt.pause(0.25)
                    and self.rt.input_text(code)
                    and self.rt.pause(0.3)):
                return False
        return not self.rt.hit("MaaNikki_Redeem_ModalPlaceholder")

    def _dismiss_reward_to_modal(self) -> bool:
        if not self.rt.pause(0.5):
            return False
        for _ in range(10):
            if self.rt.stopped:
                return False
            if self._modal_ready():
                return True
            if not self.rt.key("skip", 0.4):
                return False
            if self.ui.wait_page(self._modal_ready, seconds=1.2, allow_reward=True):
                return True
        return self._modal_ready()

    def _poll_submit_outcome(self) -> tuple[str, str]:
        deadline = time.monotonic() + 4.5
        saw_red_band = False
        last_hint = ""
        poll_idx = 0
        while not self.rt.stopped and time.monotonic() < deadline:
            frame = self.rt.capture()
            if frame is None:
                return "error", "无法获取画面"
            with self.rt.observe():
                reward_hit = bool(
                    self.rt.hit("MaaNikki_RewardLayer", image=frame)
                    or self.rt.hit("MaaNikki_Monthly_ParadiseReward", image=frame)
                )
            if reward_hit:
                if not self._dismiss_reward_to_modal():
                    return "error", "领取奖励后未能返回兑换弹窗"
                return "redeemed", ""

            red_px, dark_px = _error_band_stats(frame)
            if red_px >= 18 or dark_px >= 25:
                saw_red_band = True
                band_text = self.rt.text(ERROR_OCR_ROI, ".+", image=frame)
                if band_text:
                    last_hint = band_text
                    kind = _classify_hint(band_text)
                    if kind is not None:
                        return kind, band_text

            if poll_idx % 2 == 1:
                toast_text = self.rt.text(
                    TOAST_OCR_ROI,
                    "频繁|稍后|冷却|使用|兑换|领取|上限|过期|失效|有效|不存在|无效|错误|失败",
                    image=frame,
                )
                if toast_text:
                    last_hint = toast_text
                    kind = _classify_hint(toast_text)
                    if kind is not None:
                        return kind, toast_text

            if saw_red_band and time.monotonic() - self.last_use_at >= 1.6:
                return "expired", last_hint or "检测到红色失效提示"

            poll_idx += 1
            if not self.rt.pause(0.12):
                return "error", "任务已停止"
        return "unknown", last_hint

    def _redeem_one(self, code: str) -> tuple[str, str]:
        for attempt in range(2):
            if self.rt.stopped:
                return "error", "任务已停止"
            if not self._reset_modal_if_dirty():
                return "error", "兑换弹窗未就绪"
            if not self._fill_code(code):
                return "error", "未能填入兑换码"
            if self.last_use_at > 0.0:
                remaining = USE_COOLDOWN_SECONDS - (time.monotonic() - self.last_use_at)
                if remaining > 0.0 and not self.rt.pause(remaining):
                    return "error", "任务已停止"
            use_btn = self.rt.recognize("MaaNikki_Redeem_ModalUse")
            target = list(use_btn.box) if use_btn and use_btn.hit and use_btn.box else MODAL_USE_TARGET
            if not self.rt.action("Click", target=target):
                return "error", "未能点击使用按钮"
            self.last_use_at = time.monotonic()
            outcome, detail = self._poll_submit_outcome()
            if outcome == "cooldown" and attempt == 0:
                self.rt.log(f"兑换码「{code}」触发使用冷却提示，等待后重试。", detail=True)
                if not self.rt.pause(USE_COOLDOWN_SECONDS):
                    return "error", "任务已停止"
                continue
            return outcome, detail
        return "error", "超过重试次数"

    def run(self) -> bool:
        from daily.runtime import parameters

        online = parameters(self.rt.context, "MaaNikki_RedeemCodes_Online").get("value", True) is not False
        extra_raw = str(parameters(self.rt.context, "MaaNikki_RedeemCodes_Extra").get("value", "") or "")
        candidates = collect_candidate_codes(online=online, extra_raw=extra_raw, log=self.rt.log)

        done = self.account.redeemed_codes()
        pending: list[str] = []
        for code, is_manual in candidates:
            prev = done.get(code)
            if prev in ("redeemed", "already_used"):
                continue
            if not is_manual and prev == "expired":
                continue
            pending.append(code)

        if not pending:
            self.rt.log("兑换码领取：暂无待兑换的新兑换码，跳过。")
            if not self.rt.stopped:
                _, ok = self.rt.finish(self.ENTRY)
                return ok
            return False

        self.rt.log(f"兑换码领取：待尝试 {len(pending)} 个兑换码。")
        had_text, saved_clip = get_clipboard_text()
        completed = False
        redeemed_count = 0
        opened_ui = False
        try:
            if not self._open_modal():
                return False
            opened_ui = True
            for code in pending:
                if self.rt.stopped:
                    return False
                outcome, detail = self._redeem_one(code)
                if outcome == "redeemed":
                    self.account.record_redeem_code(code, "redeemed")
                    redeemed_count += 1
                    self.rt.log(f"兑换码「{code}」兑换成功。")
                elif outcome == "already_used":
                    self.account.record_redeem_code(code, "already_used", detail=detail)
                    self.rt.log(f"兑换码「{code}」已使用过，已记录跳过。")
                elif outcome == "expired":
                    self.account.record_redeem_code(code, "expired", detail=detail)
                    self.rt.log(f"兑换码「{code}」已过期或无效，已记录跳过。")
                elif outcome == "unknown":
                    self.rt.log(f"兑换码「{code}」未检测到成功或错误提示，暂不记录，跳过继续。", detail=True)
                else:
                    return self.fail(detail or f"处理兑换码「{code}」失败")

            if redeemed_count > 0:
                try_record_adventure_entry(
                    ROOT,
                    self.rt,
                    category="daily",
                    task="redeem_codes",
                    title="兑换码领取",
                    detail=f"成功兑换 {redeemed_count} 个兑换码",
                    runs=redeemed_count,
                )
            elif not self.rt.stopped:
                self.rt.log("兑换码领取：本批兑换码均已使用或失效。")
            completed = True
        except TaskStopped:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as error:
            self.fail(str(error))
        finally:
            if had_text:
                set_clipboard_text(saved_clip)
            if not self.rt.stopped:
                if opened_ui and self.rt.hit("MaaNikki_Redeem_ModalTitle"):
                    self.rt.action("Click", target=MODAL_CLOSE_TARGET)
                    self.rt.pause(0.25)
                returned = (
                    self.ui.back_to("MaaNikki_MainDetected") or self.rt.main()
                    if opened_ui
                    else True
                )
                if returned:
                    _, returned = self.rt.finish(self.ENTRY)
                if not returned:
                    completed = self.fail("未能返回后续任务需要的页面")
        return completed and not self.rt.stopped
