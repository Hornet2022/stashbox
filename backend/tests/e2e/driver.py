"""真机 E2E 驱动层 —— 用 adb 驱动真实 App，把 UI 操作和后端状态断言串起来。

CP-E2E-HARNESS。这层刻意不引入 Appium/UIAutomator2 之类的框架，理由：

1. 目标机器上只有 adb，没有 Appium server，引入等于先装一整套环境。
2. 真正的断言价值在**跨端一致性**（点了按钮 → 后端状态对不对），而不是
   「按钮在不在」这种 UI 断言。用 adb dump 拿坐标 + 直查 DB/接口，
   信息量比 UI 断言大得多。
3. agent 自己要能读懂、能改。这层就是普通的 pytest + subprocess。

Compose 的坑（实测踩过的）：
- `adb input tap` 对 Compose 按钮**偶尔不触发**。改用
  `input swipe x y x y 100`（同点带 100ms 按压）后可复现触发。
- 布局会随播放条出现/消失而变化，**不能缓存坐标**。每次都要
  `uiautomator dump` 重新取实时坐标。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SERIAL = os.getenv("STASHBOX_E2E_SERIAL", "f1a9e47d")
PKG = "com.tingxia.audio.debug"
MAIN_ACTIVITY = f"{PKG}/com.tingxia.audio.MainActivity"

# 截图与 UI dump 的落地目录。失败时保留现场，便于事后复盘。
EVIDENCE_DIR = Path(os.getenv("STASHBOX_E2E_EVIDENCE", "/tmp/stashbox-e2e"))


class E2EFailure(AssertionError):
    """E2E 断言失败。消息里带上现场文件路径。"""


@dataclass
class Node:
    """uiautomator dump 里的一个节点。只留驱动需要的字段。"""

    text: str
    desc: str
    rid: str
    clazz: str
    bounds: tuple[int, int, int, int]
    clickable: bool

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def __repr__(self) -> str:  # 失败信息里要能直接看出匹配到什么
        label = self.text or self.desc or self.rid or self.clazz
        return f"<{label!r} @{self.center} clickable={self.clickable}>"


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw)
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


class Device:
    """一台真机。所有 adb 调用都从这里走，便于集中处理失败现场。"""

    PKG = PKG
    MAIN_ACTIVITY = MAIN_ACTIVITY
    EVIDENCE_DIR = EVIDENCE_DIR

    def __init__(self, serial: str = DEFAULT_SERIAL):
        self.serial = serial

    # ---------- 底层 ----------

    def _adb(self, *args: str, timeout: int = 30) -> str:
        cmd = ["adb", "-s", self.serial, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise E2EFailure(f"adb {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def _adb_readonly(self, *args: str, timeout: int = 30) -> str:
        """只读查询：不因非零退出码抛错。

        设备端 `dumpsys ... | grep x` 在没匹配上时 grep 返回 1，adb shell 会把
        这个退出码如实带回 —— 对「查一下当前状态」这种只读操作，
        「没查到」不是错误。所以这类调用走这里，失败信息仍保留。
        """
        cmd = ["adb", "-s", self.serial, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise E2EFailure(f"adb {' '.join(args)} 无输出: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def shell(self, cmd: str, timeout: int = 30) -> str:
        return self._adb("shell", cmd, timeout=timeout)

    def shell_readonly(self, cmd: str, timeout: int = 30) -> str:
        return self._adb_readonly("shell", cmd, timeout=timeout)

    # ---------- 生命周期 ----------

    def is_online(self) -> bool:
        try:
            return self._adb("get-state", timeout=10).strip() == "device"
        except Exception:
            return False

    def app_installed(self) -> bool:
        return self.PKG in self.shell_readonly("pm list packages")

    def force_stop(self) -> None:
        self.shell(f"am force-stop {PKG}")

    def launch(self, cold: bool = True) -> None:
        """冷启动 App。cold=True 先 force-stop，确保测的是真实冷启动路径。"""
        if cold:
            self.force_stop()
            time.sleep(1.0)
        self.shell(f"am start -n {MAIN_ACTIVITY}")
        self.wait_for_app_ready(timeout=30)

    def wait_for_app_ready(self, timeout: int = 30) -> None:
        """等前台 Activity 真的是我们的 App，而不是 MIUI 桌面。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if PKG in self.current_activity():
                time.sleep(1.5)  # 给 Compose 首帧一点时间
                return
            time.sleep(0.5)
        raise E2EFailure(f"{timeout}s 内 App 未进入前台，当前: {self.current_activity()}")

    def current_activity(self) -> str:
        """当前前台 Activity。过滤在 Python 侧做，不依赖设备端 grep 的退出码。"""
        out = self.shell_readonly("dumpsys activity activities")
        for line in out.splitlines():
            if "topResumedActivity" in line or "mResumedActivity" in line:
                return line.strip()
        return "(未找到 topResumedActivity)"

    # ---------- UI 树 ----------

    def dump(self, timeout: int = 20) -> list[Node]:
        """取当前 UI 树。每次都重新 dump —— 坐标不可缓存。"""
        self._adb("shell", "uiautomator dump /sdcard/e2e_ui.xml", timeout=timeout)
        xml = self.shell("cat /sdcard/e2e_ui.xml", timeout=timeout)
        start = xml.find("<?xml")
        if start < 0:
            raise E2EFailure(f"uiautomator dump 输出无法解析: {xml[:200]}")
        try:
            root = ET.fromstring(xml[start:])
        except ET.ParseError as exc:
            raise E2EFailure(f"UI dump XML 解析失败: {exc}") from exc

        nodes: list[Node] = []
        for el in root.iter("node"):
            nodes.append(
                Node(
                    text=el.get("text", ""),
                    desc=el.get("content-desc", ""),
                    rid=el.get("resource-id", ""),
                    clazz=el.get("class", ""),
                    bounds=_parse_bounds(el.get("bounds", "")),
                    clickable=el.get("clickable") == "true",
                )
            )
        return nodes

    def find(
        self,
        *,
        text: str | None = None,
        desc: str | None = None,
        contains: str | None = None,
        exact: bool = True,
    ) -> Node | None:
        """按文本 / content-desc 找节点。

        exact=False 时用 contains 模糊匹配 —— Compose 的 text 常带动态后缀
        （"你的听感评分：★★★★☆"），精确匹配在真实数据下很容易失配。
        """
        for n in self.dump():
            for hay in (n.text, n.desc):
                if not hay:
                    continue
                if text and ((hay == text) if exact else (text in hay)):
                    return n
                if desc and ((hay == desc) if exact else (desc in hay)):
                    return n
                if contains and contains in hay:
                    return n
        return None

    def wait_for(self, *, timeout: int = 15, poll: float = 1.0, **match) -> Node:
        """轮询等待某节点出现。超时抛 E2EFailure 并附上当时的 UI 树摘要。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = self.find(**match)
            if n:
                return n
            time.sleep(poll)
        raise E2EFailure(f"{timeout}s 内未找到 {match}\n当前 UI: {self._ui_summary()}")

    def wait_gone(self, *, timeout: int = 15, poll: float = 1.0, **match) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.find(**match) is None:
                return
            time.sleep(poll)
        raise E2EFailure(f"{timeout}s 内 {match} 仍存在\n当前 UI: {self._ui_summary()}")

    def _ui_summary(self, limit: int = 25) -> str:
        try:
            nodes = [n for n in self.dump() if n.text or n.desc]
        except E2EFailure:
            return "(UI dump 失败)"
        if not nodes:
            return "(空 UI)"
        return " | ".join(repr(n) for n in nodes[:limit])

    # ---------- 交互 ----------

    def tap(self, *, timeout: int = 15, settle: float = 1.2, **match) -> None:
        """点一个节点。

        关键：用 `input swipe x y x y 100`（同点按压 100ms）而不是 `input tap`。
        实测 `input tap` 对 Compose 按钮的命中率不稳定，同点 swipe 更可靠。
        """
        node = self.wait_for(timeout=timeout, **match)
        x, y = node.center
        self.shell(f"input swipe {x} {y} {x} {y} 100")
        time.sleep(settle)

    def tap_at(self, x: int, y: int, settle: float = 1.0) -> None:
        self.shell(f"input swipe {x} {y} {x} {y} 100")
        time.sleep(settle)

    def tap_xy_fraction(self, fx: float, fy: float, settle: float = 1.0) -> None:
        """按屏幕比例点击，用于没有语义锚点的纯布局位置。"""
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        if not m:
            raise E2EFailure(f"无法解析屏幕尺寸: {out!r}")
        w, h = int(m.group(1)), int(m.group(2))
        self.tap_at(int(w * fx), int(h * fy), settle=settle)

    def back(self, settle: float = 1.0) -> None:
        self.shell("input keyevent KEYCODE_BACK")
        time.sleep(settle)

    def input_text(self, text: str) -> None:
        self.shell(f"input text {text}")

    # ---------- 证据 ----------

    def screenshot(self, name: str) -> Path:
        """截图存证。失败现场靠它。"""
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        out = EVIDENCE_DIR / f"{name}.png"
        self.shell("screencap -p /sdcard/e2e_shot.png")
        self._adb("pull", "/sdcard/e2e_shot.png", str(out), timeout=40)
        return out

    def dump_ui_file(self, name: str) -> Path:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        out = EVIDENCE_DIR / f"{name}.xml"
        self._adb("shell", "uiautomator dump /sdcard/e2e_ui.xml", timeout=20)
        self._adb("pull", "/sdcard/e2e_ui.xml", str(out), timeout=40)
        return out

    def logcat_recent(self, lines: int = 4000) -> str:
        """取最近的 logcat。

        刻意不用 `logcat -d -t N -v brief <时间>` 这种写法：`since` 参数
        （如 -2m）不是 logcat 的合法选项，会被当成缓冲区名，导致取不到东西。
        正确做法是配合 clear_logcat() 划清时间边界，然后取最近 N 行。
        """
        return self.shell_readonly(f"logcat -d -t {lines} -v brief 2>/dev/null")

    def clear_logcat(self) -> None:
        self.shell("logcat -c")


def evidence_on_failure(device: Device, name: str) -> None:
    """pytest 失败钩子：截图 + UI dump 全留档。"""
    try:
        device.screenshot(f"FAIL_{name}")
        device.dump_ui_file(f"FAIL_{name}")
    except Exception:
        pass  # 取不到现场不能反过来盖掉原始断言失败
