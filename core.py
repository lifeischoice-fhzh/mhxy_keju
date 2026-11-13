#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心地基:配置/识别引擎调度/事件。

OCR 与截屏在 ocr.py,客户端窗口在 clients.py(通用探测拆在 client_common.py),语音在 voice.py,
题库的读写/更新/匹配在 bank.py,科举的匹配/选项采集在 tools/keju.py,
命令行(cmd_selftest / --stats 等)在 cli.py,这里只留调度与配置这些
应用级接线(各基础模块从这里惰性取配置/路径)。
截图(只读屏幕) -> OCR 识别题目 -> 本地题库模糊匹配 -> TTS 播报答案。
不读写游戏进程/内存,答案由玩家自己选择。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

try:
    import numpy as np
except ImportError:  # main() 会给出友好提示
    np = None

if getattr(sys, "frozen", False):          # PyInstaller 打包后
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    _BUNDLE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE_DIR = BASE_DIR
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "keju.log")

# 识别基础设施拆成可独立复用的单文件模块(ocr/clients/voice;clients 的
# 通用探测又拆出 client_common,顶层都不 import core,配置/路径在方法内
# 惰性取)—— 这里 re-export 保持 core.XXX 的既有引用与测试补丁语义不变。
from ocr import Ocr, Screen  # noqa: E402
from ocr import WARMUP_TIMEOUT  # noqa: E402, F401
from clients import (CLIENT_WINDOW_CLASS,  # noqa: E402, F401
                     CLIENT_WINDOW_TITLE, CLIENT_MIN_W,
                     CLIENT_MIN_H, CANVAS_WINDOW_CLASS,
                     GAME_RES_STEPS, MIN_GAME_RES, SUPPORTED_RES,
                     DLG_RATIOS, TRK_BOXES, box_of, ratio_of, draw_box,
                     dialog_rect, trk_rect,
                     canvas_game_res, find_client_canvases,
                     client_canvas, client_size_info,
                     supported_res_text, client_supported,
                     client_name,
                     can_follow_clients, find_client_windows)
from voice import (ZH_VOICE_KEYS, CLOUD_VOICES, RATE_PCT,  # noqa: E402, F401
                   edge_params, _voice_cache_path,
                   _synth_edge_file,
                   build_voice_cache, prune_voice_cache,
                   count_missing_voice)
from voice import Speaker  # noqa: E402, F401

# 识别模式:名字 -> 是否用「御前科举」那套逻辑(结构锚点 + 整窗扫描)。
# 科举会试/殿试是同一个答题对话框的两个阶段,锚点完全一样;
# 后三个是深色弹窗,没有对话框头,只扫底部对话带、按题干全文匹配。
KEJU_MODES = (
    ("科举会试", True),
    ("科举殿试", True),
    ("三大学士", False),
    ("金兜兕", False),
    ("车迟斗法", False),
)
KEJU_MODE_NAMES = tuple(name for name, _ in KEJU_MODES)
DEFAULT_MODE = KEJU_MODE_NAMES[0]
_MODE_EXAM = dict(KEJU_MODES)


def mode_exam(name: str) -> bool:
    """模式名 -> 是否按「御前科举」识别;认不出的名字按默认档(fail-safe)。"""
    return _MODE_EXAM.get(name, _MODE_EXAM[DEFAULT_MODE])


def mode_label(name: str) -> str:
    """事件/老配置里的模式名 -> 规范名(兼容「御前科举」和「普通」两种老叫法)。"""
    name = (name or "").strip()
    if name in _MODE_EXAM:
        return name
    if "三大" in name or name == "普通":
        return "三大学士"
    return DEFAULT_MODE


DEFAULT_CONFIG = {
    "regions": [],       # 多客户端:自动定位来的整窗区域,最多 MAX_CLIENTS 个
    "volume": 1.0,
    "interval": 0.4,     # 空闲时的轮询间隔(秒),下限 0.4:有题目后自动加快到 0.1
    "mute": False,       # 静音:不出声,界面照旧显示答案
    "skin": "dark",      # 界面皮肤:dark=玄夜(深) / light=玄白(浅),右上「换肤」切换
    "voice_hint": "",    # 声音:本地=声音名关键字;云端=edge语音ID;空=自动
    "tts_backend": "edge",  # edge=云端神经语音(清晰,需联网) / sapi=本地系统语音
    "pitch": 15,         # 音调提升(Hz):短答案更清亮,0=不改
    "use_gpu": True,     # OCR 用显卡加速(DirectML),失败自动回退 CPU
    "exam_mode": True,   # 旧键:True=御前科举族(按锚点); False=三大学士族(全题干匹配)
    "mode": DEFAULT_MODE,  # 识别模式名(KEJU_MODE_NAMES 之一),界面右上按钮切换
    "bank_activities": ["keju", "qiandao"],  # 题库收录范围: keju=科举, qiandao=签到
}

MIN_INTERVAL, MAX_INTERVAL = 0.4, 10.0   # 轮询间隔的合法区间(见 tools/keju.py)
MAX_CLIENTS = 5      # 最多支持的客户端(多开)数;自动定位时多出的舍弃

# 低置信读法会记到这里,方便事后确认是不是"题库未收录"的题。
# (写它的是科举的 note_uncollected,列它的是 cli.cmd_uncollected)
UNCOLLECTED_PATH = os.path.join(BASE_DIR, "data", "uncollected.jsonl")
# 结构化识别事件(逐条 jsonl):--stats 的首选数据源。日志文本是给"人"看的,
# 措辞一动统计解析就断;事件文件只给"机器"读,认 kind/字段,不怕改文案。
# 没有这个文件(老版本留下的数据)时,--stats 回退解析 keju.log。
# 只留当天:启动时把非当天的行清掉(prune_events),统计只看当天。
EVENTS_PATH = os.path.join(BASE_DIR, "data", "events.jsonl")


VOICE_CACHE_DIR = os.path.join(BASE_DIR, "voice_cache")


REQUIRED_MODULES = ["numpy", "mss", "rapidocr_onnxruntime", "rapidfuzz",
                    "PIL", "win32com"]


def ensure_data_files():
    """首次运行(尤其 exe)时,把内置的初始数据复制到程序目录。

    之后的数据文件都在 BASE_DIR 下,可被"更新题库"覆盖。
    目录级的镜像只认"目标还不存在"这一次:之后的增删全归 prune /
    按需合成管,免得每次启动把删掉的旧文件又从镜像里拷回来。
    """
    src_dir = os.path.join(_BUNDLE_DIR, "data")
    dst_dir = os.path.join(BASE_DIR, "data")
    for name in ("keju_questions.json", "keju_metadata.json",
                 "keju_options.json"):
        dst = os.path.join(dst_dir, name)
        src = os.path.join(src_dir, name)
        if not os.path.exists(dst) and os.path.exists(src) and src != dst:
            try:
                os.makedirs(dst_dir, exist_ok=True)
                import shutil
                shutil.copyfile(src, dst)
            except Exception:
                pass
    # 打包版:语音缓存镜像一份到程序目录,首次播报不用联网合成。
    # 源码运行两者同目录,自动跳过。
    rel = "voice_cache"
    src = os.path.join(_BUNDLE_DIR, rel)
    dst = os.path.join(BASE_DIR, rel)
    if src != dst and os.path.isdir(src) and not os.path.exists(dst):
        try:
            import shutil
            shutil.copytree(src, dst)
        except Exception:
            pass


def check_deps() -> list[str]:
    import importlib.util
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(mod)
        except Exception:
            missing.append(mod)
    if np is None and "numpy" not in missing:
        missing.append("numpy")
    return missing


def dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_NON_WORD = re.compile(r"\W+", re.UNICODE)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return _NON_WORD.sub("", text).lower()


os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)


CONFIG_SECTIONS = {
    "app": ("volume", "mute", "voice_hint", "tts_backend", "pitch", "use_gpu",
            "last_bank_check", "skin", "last_seen"),
    "keju": ("regions", "interval", "exam_mode", "mode", "bank_activities"),
}
_KEY_SECTION = {k: s for s, ks in CONFIG_SECTIONS.items() for k in ks}


def key_section(key: str):
    """配置键 -> 分区名;认不出的键返回 None(调用方自行兜底/告警)。"""
    return _KEY_SECTION.get(key)


def split_config(cfg: dict) -> dict:
    """平铺配置 -> {分区: {键: 值}}。认不出归属的键放 app,免得丢。"""
    out = {s: {} for s in CONFIG_SECTIONS}
    for k, v in (cfg or {}).items():
        out[_KEY_SECTION.get(k, "app")][k] = v
    return out


def flatten_config(raw):
    """{分区: {...}} -> 平铺;老格式(平的)和非配置(如题库是列表)原样返回。"""
    if not isinstance(raw, dict):
        return raw
    if not any(isinstance(raw.get(s), dict) for s in CONFIG_SECTIONS):
        return raw
    out = {}
    for s in CONFIG_SECTIONS:
        part = raw.get(s)
        if isinstance(part, dict):
            out.update(part)
    for k, v in raw.items():
        if k not in CONFIG_SECTIONS and not isinstance(v, dict):
            out[k] = v
    return out


def app_cfg(cfg: dict) -> dict:
    """共享设置分区(音量/语音/OCR)。缺了就地补一个空 dict。"""
    return cfg.setdefault("app", {})


def keju_cfg(cfg: dict) -> dict:
    """科举自己的分区。"""
    return cfg.setdefault("keju", {})


def load_config() -> dict:
    """读配置,运行期一律是 {app / keju} 两个分区。

    DEFAULT_CONFIG 只是"默认值表"(平的):先分区,再把文件里的值按同一张
    归属表并进去,认不出的键落到 app(免得丢)。
    """
    cfg = split_config(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw = flatten_config(json.load(f))
            saved = split_config(raw)
            for name, part in saved.items():     # 逐分区合并,默认值不丢
                cfg[name].update(part)
            # 老配置只有 exam_mode 布尔:按它迁移出模式名。不做这步的话,
            # 默认值里的「科举会试」会把用着三大学士的人悄悄改回科举族。
            if "mode" not in raw:
                cfg["keju"]["mode"] = (DEFAULT_MODE if raw.get("exam_mode", True)
                                       else "三大学士")
        except Exception:
            pass
    keju = keju_cfg(cfg)
    for part in (keju, app_cfg(cfg)):   # 旧键可能被分区重排挪进 app,两边都清
        part.pop("region", None)        # 已废弃:旧版单区域(手动定位已删)
        part.pop("regions_auto", None)  # 已废弃:定位只有自动,不再要开关
    regs = [r for r in (keju.get("regions") or []) if r][:MAX_CLIENTS]
    while len(regs) < MAX_CLIENTS:
        regs.append(None)
    keju["regions"] = regs  # 固定 MAX_CLIENTS 个槽位,未设置为 None
    app_cfg(cfg).pop("threshold", None)  # 已废弃:下限固定在科举的 MIN_MATCH_PCT
    try:                   # 轮询间隔夹到合法区间(旧配置/手改值兜底)
        keju["interval"] = min(MAX_INTERVAL,
                               max(MIN_INTERVAL, float(keju.get("interval"))))
    except (TypeError, ValueError):
        keju["interval"] = DEFAULT_CONFIG["interval"]
    return cfg


def prune_events() -> int:
    """启动时把 events.jsonl 里非当天的行删掉,返回删掉的行数。

    统计只看当天,旧事件留着没有意义;坏行同样视为
    "不是今天的"清掉。读-改-写走临时文件 + 原子替换;读不了就不动它,
    写失败静默 —— 统计是副产品,绝不能影响启动。
    """
    if not os.path.exists(EVENTS_PATH):
        return 0
    today = time.strftime("%Y-%m-%d")
    kept: list[str] = []
    removed = 0
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    ts = str(json.loads(line).get("ts", ""))
                except Exception:
                    removed += 1               # 坏行:不是干净的当天记录
                    continue
                if ts.startswith(today):
                    kept.append(line)
                else:
                    removed += 1
    except Exception:
        return 0
    if not removed:
        return 0
    try:
        tmp = EVENTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, EVENTS_PATH)
    except Exception:
        return 0
    return removed


def save_config(cfg: dict):
    """写配置(直接落分区结构)。被杀软/另一实例短暂占住文件时重试几次。

    open 也在重试圈内:.tmp 这个名字同样可能被占用,只重试 replace 不重试
    open 的话,占住的是 tmp 时照样一次就炸。
    """
    tmp = CONFIG_PATH + ".tmp"
    for i in range(4):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
            return
        except PermissionError:
            if i == 3:
                raise
            time.sleep(0.15 * (i + 1))


# ---- 环境预检(命名刻意平淡:年/月/日/时区四个数分散在四个模块,
# core/bank/ui/widgets/tools 各一份,由 _ref_time() 运行时拼装;
# 提示文案与对时域名用字节码拼装,免得 strings 直搜。这些注释不会进
# 编译产物,仓库内说明属实,详见 AGENTS.md「环境预检」) ----
# 对时源含一个硬编码 IP 直连的 SNTP(hosts 只能改域名,改不动 IP),
# HTTP Date 请求禁用一切代理(env/注册表),堵本机 MITM 伪造 Date。
CORE_REV = 2026         # 核心修订号(随主版本走)
HINT_TXT = bytes((116, 111, 107, 101, 110)).decode("ascii") + "已用完"
_PEERS = (bytes((110, 116, 112, 46, 97, 108, 105, 121, 117, 110, 46, 99,
                 111, 109)).decode(),
          bytes((110, 116, 112, 46, 116, 101, 110, 99, 101, 110, 116, 46,
                 99, 111, 109)).decode())
_PEER_IP = bytes((49, 48, 54, 46, 53, 53, 46, 49, 56, 52, 46, 49,
                  57, 57)).decode()   # 腾讯公共 NTP 文档 IP(国内直连)
_SITES = (bytes((104, 116, 116, 112, 115, 58, 47, 47, 119, 119, 119, 46,
                 98, 97, 105, 100, 117, 46, 99, 111, 109)).decode(),
          bytes((104, 116, 116, 112, 115, 58, 47, 47, 119, 119, 119, 46,
                 116, 97, 111, 98, 97, 111, 46, 99, 111, 109)).decode())


def _ref_time():
    """档期锚点(北京时间;年/月/日/时区分散在四个模块,此处拼装)。"""
    if not CORE_REV:
        return None
    import bank
    from tools import keju as _tk
    from ui import widgets as _uw
    return datetime(CORE_REV, bank.BACKUP_KEEP, _uw.DOT_GAP,
                    tzinfo=timezone(timedelta(hours=_tk.BAND_PAD)))


def _parse_ts(v):
    """配置里的时间字符串 -> aware datetime(缺时区按锚点时区);坏值 None。"""
    try:
        dt = datetime.fromisoformat(str(v))
        return dt if dt.tzinfo else dt.replace(tzinfo=_ref_time().tzinfo)
    except (TypeError, ValueError):
        return None


def net_probe(timeout: float = 4.0):
    """并行问各对时源,返回拿到的最大时刻(北京时间);全没问到返回 None。

    线程全是 daemon,总预算 timeout 秒,超时后不再等。判定只认这里返回
    的时刻,本机时钟一概不读;返回 None 由调用方按停用处理。
    """
    got, lock = [], threading.Lock()
    anchor = _ref_time()

    def _add(dt):
        if dt is not None:
            if dt.tzinfo is None:        # HTTP 日期缺时区按 UTC 算
                dt = dt.replace(tzinfo=timezone.utc)
            with lock:
                got.append(dt.astimezone(anchor.tzinfo) if anchor else dt)

    def _sntp(host):
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(2.0)
                s.sendto(b"\x1b" + b"\x00" * 47, (host, 123))
                pkt = s.recv(48)
            if len(pkt) >= 48:
                sec = int.from_bytes(pkt[40:44], "big") - 2208988800
                frac = int.from_bytes(pkt[44:48], "big")
                _add(datetime.fromtimestamp(sec + frac / 2 ** 32,
                                            tz=timezone.utc))
        except Exception:
            pass

    def _http(url):
        try:
            import urllib.request
            from email.utils import parsedate_to_datetime
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Connection": "close"})
            # 禁用一切代理(env/注册表):对时不走任何本机可劫持的通道
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=3.0) as resp:
                _add(parsedate_to_datetime(resp.headers.get("Date")))
        except Exception:
            pass

    jobs = [threading.Thread(target=_sntp, args=(h,), daemon=True)
            for h in _PEERS + (_PEER_IP,)]
    jobs += [threading.Thread(target=_http, args=(u,), daemon=True)
             for u in _SITES]
    end = time.monotonic() + timeout
    for t in jobs:
        t.start()
    for t in jobs:
        t.join(max(0.05, end - time.monotonic()))
    return max(got) if got else None


def env_state(net_time=None):
    """一拍判定:返回 (是否停用, 采用的时刻)。只认 net_time;None=停用。"""
    dl = _ref_time()
    if dl is None:
        return False, net_time
    blocked = net_time is None or net_time >= dl
    return blocked, net_time


def env_ready():
    """启动/哨兵入口:停用返回 HINT_TXT,可用返回 None。

    对时一次(约 4 秒预算);对到的时刻只进不退地记进配置 last_seen
    (仅作排查,不参与判定);一个源都没对到同样按停用处理。
    """
    if _ref_time() is None:
        return None
    net = net_probe()
    blocked, _ = env_state(net)
    if net is not None:
        try:
            cfg = load_config()
            last_seen = _parse_ts(app_cfg(cfg).get("last_seen"))
            if cfg and (last_seen is None or net > last_seen):
                app_cfg(cfg)["last_seen"] = net.isoformat()
                save_config(cfg)
        except Exception:
            pass
    return HINT_TXT if blocked else None


# 梦幻西游电脑版主窗口的窗口类名(实测)。标题比类名不可靠:
# "梦幻西游"这四个字在浏览器标签、本应用自己的窗口标题里都有。
from tools.keju import KejuTool        # noqa: E402 科举(独立功能)
# 命令行那层(cmd_selftest / cmd_stats / ...)已搬到 cli.py,要用 core/bank/tools
# 的名字时它自己调用时现取 —— 这里不再代为转手。

class Engine(threading.Thread):
    """截图 + OCR + 匹配的执行线程:起基础设施;识别任务由科举自己的线程跑。"""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        # 工具线程一起来就会通过 _screen()/_ocr() 取这两个,先把它们摆好再 start
        self.screen = None
        self.ocr = Ocr(self.app.log)
        self.keju_tool = KejuTool(app, lambda: self.screen, lambda: self.ocr)
        self.keju_tool.start()
        self.refresh_tools()    # 自动监听默认开(watching 随工具初始化),按它激活

    def request_cycle(self):
        """手动识别:投递给科举自己的线程(它下一次循环立刻执行)。"""
        self.keju_tool.request(force=True)

    def set_watch(self, on: bool):
        # 直接改布尔标志(GIL 下原子);日志当场打,不再绕引擎线程
        self.keju_tool.watching = bool(on)
        self._log_watch_state()
        self.refresh_tools()            # 科举线程跟着这个开关启停

    @property
    def ocr_ok(self):
        """OCR 是不是真的能用(warmup 那次真实推理成功过才算)。

        看门狗的第二条判据:线程活着不等于识别可用(warmup 里推理失败曾经
        被吞掉,日志照样说"已就绪",谁也发现不了)。
        """
        return bool(getattr(self.ocr, "ok", True))

    def shutdown(self, timeout: float = 3.0):
        """停掉自己的工具线程并等它退出。

        看门狗重建引擎前必须调一次:否则旧的科举线程还攥着自己的
        enabled/watching 继续识别播报,谁也回收不了。
        """
        tool = getattr(self, "keju_tool", None)
        if tool is None:
            return
        try:
            tool.stop()
        except Exception as e:
            self.app.log(f"[引擎]停止工具线程失败: {e}")
        try:
            tool.join(timeout)          # 没 start 过的线程会抛,忽略即可
        except Exception:
            pass
        if tool.is_alive():
            self.app.log(f"[引擎]{getattr(tool, 'name', '工具')}线程未能退出")

    def run(self):
        # 引擎初始化整段兜异常:打包版无控制台,静默死掉会让看门狗每 10 秒
        # 重建一次,所以原因必须落到日志里。
        try:
            self.screen = Screen()
            self.ocr.warmup()
            if self.ocr_ok:
                self.app.log(f"[OCR]引擎已就绪(识别设备: "
                             f"{getattr(self.ocr, 'device', 'CPU')})")
            else:
                # 假就绪:这里绝不能再打"已就绪"(否则看门狗永远发现不了)
                self.app.log("[OCR]引擎起来了但识别不可用:"
                             f"{getattr(self.ocr, 'err', '') or '推理失败'}"
                             "(看门狗会重建)")
        except BaseException as e:
            import traceback
            self.app.log(f"[OCR]引擎初始化失败: {e}")
            self.app.log(traceback.format_exc())
            return
        self._wake_tools()              # 截图/OCR 都就绪了,可以开工了
        # 识别任务全在工具自己的线程里跑(手动任务也是一路投递过去的),
        # 这条线程只剩"活着=基础设施还在"这一个作用,看门狗盯着它。
        while True:
            time.sleep(0.5)

    def _log_watch_state(self):
        self.app.log("[自动监听]已开启" if self.keju_tool.watching
                     else "[自动监听]已停止")

    def refresh_tools(self):
        """按"自动监听"开关决定科举线程跑不跑。

        开关走工具自己的生命周期 API(activate / deactivate),不在外面
        直接摆 enabled —— 开/停这件事由工具自己说了算。
        """
        keju = getattr(self, "keju_tool", None)
        if keju is not None:
            self._switch_tool(keju, bool(keju.watching))
        self._wake_tools()

    def _switch_tool(self, tool, on: bool):
        """开 / 停一条工具线程(activate / deactivate 是它们的生命周期 API)。"""
        try:
            (tool.activate if on else tool.deactivate)()
        except Exception as e:                  # 工具没有这套 API:至少把标志摆对
            tool.enabled = bool(on)
            self.app.log(f"[引擎]切换工具开关失败({e})")

    def _wake_tools(self):
        """工具线程空闲时完全不轮询,状态一变就得叫它一声。"""
        tool = getattr(self, "keju_tool", None)
        if tool is not None:
            tool._wake.set()
