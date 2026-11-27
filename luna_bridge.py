#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""luna_bridge.py — 把弹幕采集器的事件转发给 Luna AI（AI-Vtuber）的数字人。

为什么需要这一层:
    本项目的默认出口是「数字人桥」HTTP :8795 —— 那是 cyber-xiaoya-live 那一套的
    接口，Luna AI 里没有。Luna 收弹幕走的是它自己在 127.0.0.1:5001 起的
    websocket（配置里平台选「直播弹幕监听助手」的那个）。
    本脚本把两边的协议对上：左边照旧用本项目的平台客户端采集（视频号 CDP /
    抖音 / B站 / YouTube），右边按 Luna 的扁平 JSON 推过去。

数据流:
    视频号 ──CDP:9223──┐
    抖音  ──:1089──────┤   统一事件                Luna 的
    B站   ──官方WS─────┼─▶ {platform,type,data} ─▶ 「直播弹幕监听助手」
    YouTube ─InnerTube─┘                          ws://127.0.0.1:5001 ─▶ LLM/TTS/数字人

    念哪条、答什么、用什么声音、要不要念昵称，全部由 **Luna 的配置** 决定；
    本脚本不合成语音、不朗读，只搬数据。

⚠️ 跑本脚本时不要再开 reader.py：同一句弹幕会被念两遍。

用法:
    py -3 luna_bridge.py                          # 按 config.json 里已启用的平台启动
    py -3 luna_bridge.py --only wechat            # 只跑视频号（先这样验证最省事）
    py -3 luna_bridge.py --only wechat --open-browser   # 顺手拉起带调试端口的浏览器
    py -3 luna_bridge.py --print-only             # 只打印要发给 Luna 的报文，不真发
    py -3 luna_bridge.py --test                   # 发一条测试弹幕，验证 5001 通不通

配置（config.json → luna 段，全部可省略）:
    {
      "luna": {
        "ws_url": "ws://127.0.0.1:5001",     # Luna 的「直播弹幕监听助手」地址
        "forward": {                         # 哪些事件转发（礼物/进场/关注默认都转）
          "comments": true, "gifts": true, "enters": true,
          "follows": true, "subscribes": true, "superchats": true
        },
        "strip_emoji": true,                 # 去掉表情/URL，念起来更省时间
        "queue_size": 200,                   # 断线期间最多攒多少条（满了丢最旧）
        "retry_sec": 3                        # 断线后多久重连
      }
    }
"""

import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from logutil import (BOLD, CYAN, DIM, GRAY, GREEN, RED, RESET, YELLOW,
                     dim_log, event_line, plain, platform_log, set_logfile)
from wsclient import WSClient, WSClosed

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

CREATE_NO_WINDOW = 0x08000000

DEFAULT_LUNA = {
    "enabled": True,
    "ws_url": "ws://127.0.0.1:5001",
    "retry_sec": 3.0,
    "queue_size": 200,
    "strip_emoji": True,
    "forward": {
        "comments": True,
        "gifts": True,
        "enters": True,
        "follows": True,
        "subscribes": True,
        "superchats": True,
    },
}

# 视频号助手的中控台（评论面板就在这个页面上）
CONSOLE_URL = "https://channels.weixin.qq.com/platform/live/liveBuild"

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

EMOJI_RE = re.compile(
    "[\U00010000-\U0010FFFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF"
    "\uFE00-\uFE0F\u200D\u20E3\u00A9\u00AE\u2122]+"
)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# ─────────────────────────── 配置 ───────────────────────────


def load_config() -> dict:
    """读 config.json（没有就返回空），只补 luna 段的默认值。

    平台段保持原样交给各客户端：它们自己带默认值，少写字段不会崩。
    """
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                cfg = raw
        except Exception as e:
            plain(f"[warn] config.json 解析失败({e})，按「只有视频号+默认 Luna 地址」跑", YELLOW)

    luna = cfg.get("luna")
    if not isinstance(luna, dict):
        luna = {}
    merged = json.loads(json.dumps(DEFAULT_LUNA))       # 深拷贝，别改到模块级默认
    for k, v in luna.items():
        if k == "forward" and isinstance(v, dict):
            merged["forward"].update(v)
        else:
            merged[k] = v
    cfg["luna"] = merged
    return cfg


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_text(text: str, strip_emoji: bool = True) -> str:
    text = URL_RE.sub("", text or "")
    if strip_emoji:
        text = EMOJI_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# ───────────────────── 事件 → Luna 报文 ─────────────────────


def gift_total_yuan(d: dict, num: int) -> float:
    """礼物价值折算成元。

    · B站 / YouTube 的客户端已经算好 price_yuan（这条礼物的总价）；
    · 抖音给的是 diamond（单价的抖币数，1 元 ≈ 10 钻），总价乘数量。
    """
    if d.get("price_yuan") not in (None, ""):
        return round(max(0.0, _as_float(d.get("price_yuan"))), 2)
    diamond = _as_int(d.get("diamond")) or _as_int(d.get("value"))
    if diamond > 0:
        return round(diamond * max(1, num) / 10.0, 2)
    return 0.0


def to_luna(ev: dict, forward: dict, strip_emoji: bool = True) -> dict | None:
    """把采集器的统一事件翻成 Luna 的一条消息；返回 None 表示这条不发。"""
    etype = str(ev.get("type") or "").lower()
    d = ev.get("data") or {}
    if not isinstance(d, dict):
        return None

    raw_name = str(d.get("name") or "").strip()
    nick = clean_text(raw_name, strip_emoji)
    if not nick:                                  # 纯 Emoji 昵称兜底，不丢弃该观众弹幕/礼物
        if not raw_name:
            return None
        nick = "有位观众"

    if etype == "chat":
        if not forward.get("comments", True):
            return None
        content = clean_text(d.get("content") or "", strip_emoji)
        if not content:
            return None
        return {"type": "comment", "username": nick, "content": content}

    if etype == "enterroom":
        if not forward.get("enters", True):
            return None
        return {"type": "entrance", "username": nick}

    if etype == "follow":
        if not forward.get("follows", True):
            return None
        return {"type": "follow", "username": nick}

    if etype == "gift":
        if not forward.get("gifts", True):
            return None
        num = max(1, _as_int(d.get("num"), 1))
        total = gift_total_yuan(d, num)
        return {
            "type": "gift",
            "username": nick,
            "gift": clean_text(d.get("item") or "", strip_emoji) or "礼物",
            "num": num,
            "unit_price": round(total / num, 2),
            "total_price": total,
        }

    if etype == "subscribe":                      # 舰长 / 会员：Luna 只有「礼物」这条感谢线
        if not forward.get("subscribes", True):
            return None
        num = max(1, _as_int(d.get("num"), 1))
        return {
            "type": "gift",
            "username": nick,
            "gift": clean_text(d.get("item") or "", strip_emoji) or "大航海",
            "num": num,
            "unit_price": 0.0,
            "total_price": 0.0,
        }

    if etype == "superchat":                      # 醒目留言：内容别丢，按弹幕发
        if not forward.get("superchats", True):
            return None
        content = clean_text(d.get("content") or "", strip_emoji)
        if not content:
            return None
        return {"type": "comment", "username": nick, "content": content}

    # like / endlive / 其它：Luna 没有对应流水线，静默跳过
    return None


# ───────────────────── 抖音连击礼物合并 ─────────────────────


class ComboAggregator:
    """抖音的礼物是「一下一下」推的（repeatEnd=0 连击中，=1 结束）。

    一条一条转发会让 Luna 感谢十几遍，所以按 groupId 攒着，静默 wait 秒后只发总数；
    收到结束标记就立刻发。B站/YouTube 客户端自己已经聚合过，不走这里。
    """

    def __init__(self, wait_sec: float, on_flush):
        self.wait = max(0.3, float(wait_sec))
        self.on_flush = on_flush
        self._items: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._sweep, name="luna-gift-sweep", daemon=True).start()

    def feed(self, key: str, payload: tuple, finished: bool):
        now = time.time()
        with self._lock:
            ent = self._items.get(key)
            if ent is None:
                self._items[key] = {"payload": payload, "last": now}
            else:
                ent["payload"] = payload           # 后到的数量/总价是更大的那个
                ent["last"] = now
        if finished:
            self.flush(key)

    def flush(self, key: str):
        with self._lock:
            ent = self._items.pop(key, None)
        if ent:
            self.on_flush(ent["payload"])

    def _sweep(self):
        while not self._stop.wait(0.5):
            now = time.time()
            with self._lock:
                due = [k for k, e in self._items.items() if now - e["last"] >= self.wait]
            for k in due:
                self.flush(k)

    def stop(self):
        self._stop.set()


# ───────────────────── Luna 长连接 ─────────────────────


class LunaLink(threading.Thread):
    """到 Luna「直播弹幕监听助手」的长连接：断线自动重连，队列有界（弹幕洪峰丢最旧）。"""

    def __init__(self, url: str, retry_sec: float = 3.0, queue_size: int = 200,
                 print_only: bool = False):
        super().__init__(name="luna-link", daemon=True)
        self.url = url
        self.retry_sec = max(0.5, float(retry_sec))
        self.q: queue.Queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self.print_only = print_only
        self.stop = threading.Event()
        self.connected = False
        self.sent = 0
        self.dropped = 0
        self._warned = False
        self._dead = threading.Event()

    # ---- 对外 ----
    def send(self, payload: dict):
        line = json.dumps(payload, ensure_ascii=False)
        if self.print_only:
            dim_log(f"[DRY] {line}")
            return
        while True:
            try:
                self.q.put_nowait(line)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()            # 挤掉最旧的一条：直播里新弹幕比旧的有用
                    self.dropped += 1
                except queue.Empty:
                    pass

    # ---- 线程 ----
    def run(self):
        while not self.stop.is_set():
            try:
                ws = WSClient(self.url, name="luna").connect(timeout=5)
            except Exception as e:
                if not self._warned:
                    self._warned = True
                    plain(f"[Luna] 连不上 {self.url}（{e}）—— Luna 主程序起来后会自动连上", YELLOW)
                self.stop.wait(self.retry_sec)
                continue

            self.connected = True
            self._warned = False
            self._dead.clear()
            plain(f"[Luna] 已连上 {self.url}，弹幕开始转发", GREEN)
            threading.Thread(target=self._keepalive, args=(ws,),
                             name="luna-recv", daemon=True).start()

            try:
                while not self.stop.is_set() and not self._dead.is_set():
                    try:
                        line = self.q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        ws.send_text(line)
                    except Exception:
                        try:
                            self.q.put_nowait(line)  # 发送失败时放回队列，重连后补发不丢条
                        except queue.Full:
                            pass
                        raise
                    self.sent += 1
                    dim_log(f"[Luna] → {line[:90]}")
            except Exception as e:                 # noqa: BLE001 - 任何发送异常都走重连
                dim_log(f"[Luna] 发送失败: {e}")
            finally:
                self.connected = False
                ws.close()
                if not self.stop.is_set():
                    plain("[Luna] 连接断了，正在重连…", YELLOW)
                    self.stop.wait(self.retry_sec)

    def _keepalive(self, ws):
        """只负责收帧。

        Luna 的 websockets 服务端会定时 ping，不回 pong 会被判死并断开；
        WSClient 在收帧时才会自动回 pong，所以这里必须常驻一个读线程。
        """
        while not self.stop.is_set() and not self._dead.is_set():
            try:
                ws.recv_message(timeout=1.0)
            except (socket.timeout, TimeoutError):
                continue
            except WSClosed:
                self._dead.set()
                return
            except OSError:
                self._dead.set()
                return
            except Exception:                      # noqa: BLE001
                self._dead.set()
                return


# ───────────────────── 事件入口 ─────────────────────


class LunaBridge:
    """采集器事件 → Luna。自带一点点节流（连击礼物），其余全交给 Luna 的配置。"""

    def __init__(self, cfg: dict, link: LunaLink, debug: bool = False):
        luna = cfg.get("luna") or {}
        self.link = link
        self.forward = luna.get("forward") or {}
        self.strip_emoji = bool(luna.get("strip_emoji", True))
        self.debug = debug
        self.combos = ComboAggregator(1.5, self._flush_gift)

    def on_event(self, ev: dict):
        try:
            etype = str(ev.get("type") or "").lower()
            platform = ev.get("platform") or "?"

            # 连击礼物先攒着，其余事件直接翻协议发走
            if etype == "gift":
                payload = to_luna(ev, self.forward, self.strip_emoji)
                if payload is None:
                    return
                d = ev.get("data") or {}
                repeat_end = d.get("repeat_end")
                # 平台带 repeat_end 就说明这条礼物可能是连击流：**结束帧也必须走聚合器**，
                # 不能直接发——否则池里那条中途攒着的会在 1.5 秒后再补发一遍，变成感谢两次。
                if repeat_end is not None:
                    key = f"{platform}|{d.get('group_id') or payload['username'] + '|' + payload['gift']}"
                    finished = str(repeat_end) == "1"
                    self.combos.feed(key, (platform, payload), finished=finished)
                    if self.debug and not finished:
                        dim_log(f"[连击] {payload['username']} 的 {payload['gift']} 攒着中（x{payload['num']}）")
                    return
                self._emit(platform, payload)
                return

            payload = to_luna(ev, self.forward, self.strip_emoji)
            if payload is not None:
                self._emit(platform, payload)
            elif self.debug:
                dim_log(f"[跳过] {platform} {etype}（Luna 没有对应流水线或已在转发开关里关掉）")
        except Exception:                          # noqa: BLE001 - 单条事件出错不能带走整条链路
            plain(f"[warn] 事件转发异常:\n{traceback.format_exc()}", YELLOW)

    def _flush_gift(self, entry: tuple):
        platform, payload = entry
        self.link.send(payload)
        event_line(platform, "🎁", "Gift",
                   f"{payload['username']} 赠送 {payload['gift']} x{payload['num']}"
                   f"{DIM} (≈{payload['total_price']:g}元){RESET}")

    def _emit(self, platform: str, payload: dict):
        kind = payload["type"]
        if kind == "comment":
            event_line(platform, "💬", "Chat", f"{BOLD}{payload['username']}{RESET}: {payload['content']}")
        elif kind == "entrance":
            event_line(platform, "👋", "EnterRoom", payload["username"])
        elif kind == "follow":
            event_line(platform, "⭐", "Follow", payload["username"])
        elif kind == "gift":
            event_line(platform, "🎁", "Gift",
                       f"{payload['username']} 赠送 {payload['gift']} x{payload['num']}")
        self.link.send(payload)


# ───────────────────── 浏览器 / 平台启动 ─────────────────────


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def open_debug_browser(port: int) -> bool:
    """拉起带调试端口的 Edge/Chrome 并打开视频号助手（幂等：端口已在就跳过）。"""
    if port_open(port):
        plain(f"调试浏览器已在运行（127.0.0.1:{port}）", GREEN)
        return True

    exe = next((p for p in BROWSERS if os.path.isfile(p)), None)
    if not exe:
        plain("没找到 Edge/Chrome，请手动双击「启动调试浏览器.cmd」", YELLOW)
        return False

    profile = ROOT / "browser-profile"
    try:
        subprocess.Popen(
            [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", CONSOLE_URL],
            creationflags=CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                        # noqa: BLE001
        plain(f"[warn] 启动浏览器失败: {e}", YELLOW)
        return False

    plain(f"已拉起调试浏览器（端口 {port}，独立配置目录 browser-profile）", GREEN)
    for _ in range(40):
        if port_open(port):
            plain("调试端口就绪，请在弹出的窗口里登录视频号并进入直播间", GREEN)
            return True
        time.sleep(0.5)
    plain("等了 20 秒调试端口还没起来，检查是不是被安全软件拦了", YELLOW)
    return False


def start_clients(cfg: dict, bridge: LunaBridge, only: list[str] | None):
    """按 config.json 的平台段起客户端（和 reader.py 同一套开关，但不带 TTS）。"""
    started = []

    def want(name: str) -> bool:
        return not only or name in only

    def skip(name: str, why: str):
        plain(f"{name} 未启用（{why}）", GRAY)

    # 视频号（CDP）
    wx = cfg.get("wechat") or {}
    if want("wechat"):
        if wx.get("enabled"):
            from wechat_client import WechatConsoleClient
            c = WechatConsoleClient(cfg, bridge.on_event)
            c.start()
            started.append(("视频号", c))
            platform_log("视频号", f"开始监听中控台（CDP {wx.get('cdp_port', 9223)}）")
        else:
            skip("视频号", "config.json → wechat.enabled")

    # 抖音（依赖外部 douyinLive 采集服务）
    dy = cfg.get("douyin") or {}
    if want("douyin"):
        if dy.get("enabled") and str(dy.get("room_id") or "").strip():
            from douyin_client import DouyinClient
            c = DouyinClient(str(dy["room_id"]).strip(), bridge.on_event, dy)
            if dy.get("auto_start", True):
                c.start_service()
            c.start()
            started.append(("抖音", c))
        else:
            skip("抖音", "config.json → douyin.enabled / room_id")

    # B站
    bili = cfg.get("bilibili") or {}
    if want("bilibili"):
        if bili.get("enabled") and str(bili.get("room_id") or "").strip():
            from bilibili_client import BilibiliClient
            c = BilibiliClient(str(bili["room_id"]).strip(), bridge.on_event, bili)
            c.start()
            started.append(("B站", c))
        else:
            skip("B站", "config.json → bilibili.enabled / room_id")

    # YouTube
    yt = cfg.get("youtube") or {}
    if want("youtube"):
        target = str(yt.get("video_id") or "").strip() or str(yt.get("channel") or "").strip()
        if yt.get("enabled") and target:
            from youtube_client import YouTubeClient
            c = YouTubeClient(target, bridge.on_event, yt)
            c.start()
            started.append(("YouTube", c))
        else:
            skip("YouTube", "config.json → youtube.enabled / video_id / channel")

    return started


# ───────────────────── 主流程 ─────────────────────


def run_test(cfg: dict) -> int:
    """发一条测试弹幕，确认 5001 这条线通不通。"""
    luna = cfg.get("luna") or {}
    url = luna.get("ws_url") or DEFAULT_LUNA["ws_url"]
    plain(f"向 {url} 发一条测试弹幕…", CYAN)
    try:
        ws = WSClient(url, name="luna-test").connect(timeout=5)
    except Exception as e:                        # noqa: BLE001
        plain(f"❌ 连不上：{e}", RED)
        plain("   检查 Luna 是否已启动，且 通用配置 → 功能管理 → 平台相关 → 直播弹幕监听助手 "
              "的地址/端口与 luna.ws_url 一致", YELLOW)
        return 1
    try:
        ws.send_text(json.dumps({
            "type": "comment",
            "username": "桥接自检",
            "content": "这是一条桥接测试弹幕，收到就说明链路通了",
        }, ensure_ascii=False))
        time.sleep(0.3)
        plain("✅ 已发出。去 Luna 的控制台/WebUI 日志里找「[📧直播间弹幕消息] [桥接自检]」", GREEN)
        return 0
    finally:
        ws.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="把弹幕采集器接到 Luna AI（AI-Vtuber）")
    ap.add_argument("--only", action="append", choices=["wechat", "douyin", "bilibili", "youtube"],
                    help="只跑指定平台，可重复（例: --only wechat）")
    ap.add_argument("--print-only", action="store_true", help="只打印要发给 Luna 的报文，不真发")
    ap.add_argument("--test", action="store_true", help="发一条测试弹幕后退出")
    ap.add_argument("--open-browser", action="store_true",
                    help="启动带调试端口的浏览器并打开视频号助手（端口已在就跳过）")
    ap.add_argument("--debug", action="store_true", help="打印被跳过的事件")
    args = ap.parse_args()

    cfg = load_config()
    luna = cfg["luna"]

    try:
        set_logfile(ROOT / "logs" / "luna_bridge.log")
    except Exception:                             # noqa: BLE001
        pass

    print(f"{BOLD}弹幕 → Luna AI 桥接{RESET}  {DIM}(cyber-xiaoya-danmaku → AI-Vtuber){RESET}")

    if args.test:
        return run_test(cfg)

    if not luna.get("enabled", True):
        plain("config.json → luna.enabled 是 false，什么都没做", YELLOW)
        return 0

    wx = cfg.get("wechat") or {}
    if wx.get("enabled") and (not args.only or "wechat" in args.only):
        port = int(wx.get("cdp_port", 9223))
        if args.open_browser:
            open_debug_browser(port)
        elif not port_open(port):
            plain(f"⚠ 调试端口 {port} 没开：视频号要读「视频号助手」的评论面板，"
                  f"先双击「启动调试浏览器.cmd」或加 --open-browser", YELLOW)

    link = LunaLink(
        url=luna.get("ws_url") or DEFAULT_LUNA["ws_url"],
        retry_sec=luna.get("retry_sec", 3),
        queue_size=luna.get("queue_size", 200),
        print_only=args.print_only,
    )
    bridge = LunaBridge(cfg, link, debug=args.debug)

    clients = start_clients(cfg, bridge, args.only)
    if not clients:
        plain("没有任何平台在跑：检查 config.json 里对应平台的 enabled / 房间号，或用 --only 指定", RED)
        return 1

    if args.print_only:
        plain("--print-only：只打印报文，不连 Luna", YELLOW)
    else:
        link.start()

    names = " / ".join(n for n, _ in clients)
    plain(f"已启动平台：{names}。念哪条/怎么答由 Luna 的配置决定；Ctrl+C 退出。", GRAY)

    stop_evt = threading.Event()
    last_beat = time.time()
    try:
        while not stop_evt.is_set():
            time.sleep(1)
            if args.print_only:
                continue
            if time.time() - last_beat >= 30:      # 心跳：一眼看出「到底还在不在转发」
                last_beat = time.time()
                state = "已连上" if link.connected else "未连上(重试中)"
                plain(f"[Luna] {state} · 已转发 {link.sent} 条"
                      f"{f' · 弃 {link.dropped} 条' if link.dropped else ''}"
                      f" · 队列 {link.q.qsize()}", GRAY if link.connected else YELLOW)
    except KeyboardInterrupt:
        pass
    finally:
        plain("正在退出…", GRAY)
        for _name, c in clients:
            try:
                c.stop.set()
            except Exception:                     # noqa: BLE001
                pass
        bridge.combos.stop()
        link.stop.set()
        for _name, c in clients:
            proc = getattr(c, "_proc", None)       # 抖音的采集服务是子进程，别留孤儿
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                    plain("已停止 douyinLive 采集服务", GRAY)
                except Exception:                 # noqa: BLE001
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())