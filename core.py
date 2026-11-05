#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心地基:配置/OCR/语音/识别引擎调度。

题库的读写/更新/匹配在 bank.py,科举的匹配/收益统计在 tools/keju.py,
命令行(cmd_selftest / --stats 等)在 cli.py,这里只留共用的地基。
截图(只读屏幕) -> OCR 识别题目 -> 本地题库模糊匹配 -> TTS 播报答案。
不读写游戏进程/内存,答案由玩家自己选择。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata

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
    "exam_mode": True,   # True=御前科举(按锚点); False=三大学士(不用锚点,全题干匹配)
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
# 只留当天:启动时把非当天的行清掉(prune_events),统计口径与收益一致。
EVENTS_PATH = os.path.join(BASE_DIR, "data", "events.jsonl")


ZH_VOICE_KEYS = ("zh-cn", "zh_cn", "zh-hans", "huihui", "kangkang",
                 "yaoyao", "tracy", "minxi", "yunxi", "yunyang")

CLOUD_VOICES = {
    "云端·云希(男声)": "zh-CN-YunxiNeural",
    "云端·晓晓(女声)": "zh-CN-XiaoxiaoNeural",
}

# 固定文案只留这一份:gui 的"播报试听"与语音缓存清理的合法清单
# (prune_voice_cache)都按它算,改文案一处生效。
SAY_TTS_TEST = "语音测试,C铁观音,唐朝"


def _play_mp3_blocking(path: str):
    """用 Windows MCI(winmm)同步播放 mp3,无需额外依赖。"""
    from ctypes import windll, create_unicode_buffer

    winmm = windll.winmm

    def send(cmd):
        buf = create_unicode_buffer(256)
        if winmm.mciSendStringW(cmd, buf, 256, 0) != 0:
            raise RuntimeError(f"MCI failed: {cmd}")

    try:
        send(f'open "{path}" alias ttsclip')
        send("play ttsclip wait")
    finally:
        try:
            send("close ttsclip")
        except Exception:
            pass


VOICE_CACHE_DIR = os.path.join(BASE_DIR, "voice_cache")


RATE_PCT = 0  # 固定语速(不对外提供配置,避免缓存反复失效)


def edge_params(cfg: dict):
    """当前云端合成参数(语速固定)。音量/音调在 app 分区。"""
    app = app_cfg(cfg)
    volume = max(-100, min(0, round((float(app.get("volume", 1.0)) - 1) * 100)))
    pitch = max(-50, min(50, int(app.get("pitch", 15))))
    return RATE_PCT, volume, pitch


def _voice_cache_path(text: str, voice: str, rate: int, volume: int,
                      pitch: int = 0) -> str:
    import hashlib
    key = f"{voice}|{rate:+d}%|{volume:+d}%|{pitch:+d}Hz|{text}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    os.makedirs(VOICE_CACHE_DIR, exist_ok=True)
    return os.path.join(VOICE_CACHE_DIR, f"{h}.mp3")


def _synth_edge_file(text: str, voice: str, rate: int, volume: int,
                     out_path: str, timeout: int = 15, retries: int = 2,
                     pitch: int = 0) -> bool:
    """合成一条答案音频到 out_path。

    源码运行:起子进程跑 edge-tts(隔离崩溃,可强杀超时)。
    打包版(sys.frozen):sys.executable 是应用自己,不是 python 解释器,
    "-c" 只会被 argparse 当未知参数拒掉(实测 exit=2)——edge_tts 本来
    就随包分发,改为进程内合成;套临时线程 + 超时,卡住就放弃,别拖住
    播报线程(同 Ocr.warmup 的模式;代价是"放弃"杀不死卡住的线程,
    而子进程方案能强杀)。
    """
    import sys

    def synth_inline() -> bool:
        import asyncio

        async def _save():
            import edge_tts
            c = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%",
                                     volume=f"{volume:+d}%",
                                     pitch=f"{pitch:+d}Hz")
            await c.save(out_path)

        box: dict = {}
        done = threading.Event()

        def work():
            try:
                asyncio.run(asyncio.wait_for(_save(),
                                             timeout=max(1, timeout - 1)))
                box["ok"] = True
            except BaseException:
                box["ok"] = False
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        if not done.wait(timeout):
            return False                  # 超时:这次算失败,别无限等
        return (bool(box.get("ok")) and os.path.exists(out_path)
                and os.path.getsize(out_path) > 0)

    for attempt in range(retries + 1):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            if getattr(sys, "frozen", False):
                ok = synth_inline()
            else:
                import subprocess
                script = (
                    "import sys, asyncio, edge_tts\n"
                    "async def main():\n"
                    "    c = edge_tts.Communicate(sys.argv[1], sys.argv[2],"
                    " rate=sys.argv[3], volume=sys.argv[4], pitch=sys.argv[6])\n"
                    "    await c.save(sys.argv[5])\n"
                    "asyncio.run(asyncio.wait_for(main(), timeout=14))\n"
                )
                subprocess.run(
                    [sys.executable, "-c", script, text, voice,
                     f"{rate:+d}%", f"{volume:+d}%", out_path,
                     f"{pitch:+d}Hz"],
                    timeout=timeout, check=True, creationflags=0x08000000)
                ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            if ok:
                return True
        except Exception:
            pass
        if attempt < retries:
            time.sleep(0.8 + attempt)  # 服务抖动,稍作退避再试
    try:
        os.remove(out_path)
    except Exception:
        pass
    return False

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
    for name in ("keju_questions.json", "keju_metadata.json"):
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
            "last_bank_check", "skin"),
    "keju": ("regions", "interval", "exam_mode", "bank_activities"),
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
                saved = split_config(flatten_config(json.load(f)))
            for name, part in saved.items():     # 逐分区合并,默认值不丢
                cfg[name].update(part)
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

    统计只看当天,旧事件留着没有意义(和收益统计一个口径);坏行同样视为
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


# 梦幻西游电脑版主窗口的窗口类名(实测)。标题比类名不可靠:
# "梦幻西游"这四个字在浏览器标签、本应用自己的窗口标题里都有。
from tools.keju import KejuTool        # noqa: E402 科举(独立功能)
# 命令行那层(cmd_selftest / cmd_stats / ...)已搬到 cli.py,要用 core/bank/tools
# 的名字时它自己调用时现取 —— 这里不再代为转手。
# 题库子系统(数据/IO)单独一层,见 bank.py:语音缓存的维护(prune/count/build)
# 要读题库,这是 core 里唯一从它取的名字。
from bank import QuestionBank  # noqa: E402

CLIENT_WINDOW_CLASS = "MHXYMainFrame"
CLIENT_WINDOW_TITLE = "梦幻西游 ONLINE"
CLIENT_MIN_W, CLIENT_MIN_H = 400, 300     # 比这还小就不可能是客户端画面

# 游戏分辨率档(用户口径):640×480 / 800×600 受支持;1024×768/HD 不再支持。
# 档位与文案按 96-DPI 逻辑像素算(见 GAME_DPI/_logical);860×600 的窗口
# 按面积自然归到 800×600 档,同样放行
GAME_RES_STEPS = ((640, 480), (800, 600), (1024, 768))
MIN_GAME_RES = GAME_RES_STEPS[0]          # 最小档:整窗口截图最快,实测最划算

GAME_DPI = 96    # 游戏窗口所在显示器的有效 DPI(125% 缩放=120);枚举时刷新


def _logical(box):
    """物理像素矩形 -> 游戏设置口径(96-DPI 逻辑)的同形矩形。

    游戏是 DPI-unaware 的:它眼里 800×600 的窗口,在 125% 缩放的屏幕上
    物理是 1015×828(感知进程量到的就是这个)。档位判定必须折算回逻辑值,
    截图与区域坐标仍是物理值(mss 要物理像素),别混。
    """
    if GAME_DPI == 96:
        return box
    k = 96.0 / GAME_DPI
    return [round(v * k) for v in box]


def _logical_size(box):
    """折算到逻辑口径后的 (宽, 高) 与按面积就近归到的分辨率档。

    游戏是 DPI-unaware 的:它眼里 800×600 的窗口,在 125% 缩放的屏幕上
    物理是 1015×828(感知进程量到的就是这个)。档位判定必须折算回逻辑值,
    截图与区域坐标仍是物理值(mss 要物理像素),别混。
    窗口可以被拉伸,所以按面积取最接近的一档来猜。
    """
    b = _logical(box)
    w = max(1, int(b[2]) - int(b[0]))
    h = max(1, int(b[3]) - int(b[1]))
    step = min(GAME_RES_STEPS, key=lambda r: abs(r[0] * r[1] - w * h))
    return w, h, step


def client_size_info(box):
    """客户端像素尺寸 -> (尺寸说明文字, 是否已经是最小分辨率)。

    猜不到最小档就提醒用户调小;数字与游戏设置界面对得上(见 _logical)。
    """
    w, h, step = _logical_size(box)
    return f"{w}×{h} ≈ {step[0]}×{step[1]} 档", step == MIN_GAME_RES


# 只支持两档(2026-11-05):1024×768/HD 与其它尺寸不再定位 —— 识别慢、字也小,
# 定位时直接跳过并弹框说明。归到这两档的都算支持(窗口被拉伸过也算)。
SUPPORTED_RES = set(GAME_RES_STEPS[:2])    # (640,480) / (800,600)


def supported_res_text():
    """「640×480 / 800×600」这样的支持档文案,提示日志与弹框共用一份。"""
    return " / ".join(f"{w}×{h}" for w, h in sorted(SUPPORTED_RES))


def client_supported(box):
    """客户端尺寸是否属于支持的两档(640×480 / 800×600)。

    与 client_size_info 同一套"按面积就近归档":归到哪档就按哪档算。
    """
    return _logical_size(box)[2] in SUPPORTED_RES


def client_name(title):
    """从客户端窗口标题抠角色名:「… - (区服[服] - 名字[id])」取最后一段。

    「梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])」->「祭花′」:
    取最后一个 - 之后,截到第一个 [,再去掉两端的括号和空格。
    标题里没有 -(还没进角色的登录窗口就只有「梦幻西游 ONLINE」)或抠出来
    是空的,返回空串 —— 界面那格退回显示「客户端N」。
    """
    t = str(title or "")
    if "-" not in t:
        return ""
    seg = t.rsplit("-", 1)[1].split("[", 1)[0].strip()
    while seg and seg[0] in "()()「」":
        seg = seg[1:]
    while seg and seg[-1] in "()()「」":
        seg = seg[:-1]
    return seg


def can_follow_clients(boxes, auto_boxes):
    """区域能不能跟着客户端走:两次看到的客户端数量一致才跟。

    客户端最小化/关掉会让数量变化,那时宁可不跟,也别把别的槽位弄丢。
    """
    return bool(boxes) and bool(auto_boxes) and len(boxes) == len(auto_boxes)


def find_client_windows():
    """枚举梦幻西游客户端窗口。

    返回 ([(标题, [x1,y1,x2,y2]), ...], [已最小化的窗口标题, ...])。
    量的是**客户区**(GetClientRect + ClientToScreen):游戏选 800×600,
    量出来就是 800×600,与设置界面里的档位完全对齐;用 GetWindowRect 会
    把系统标题栏/边框也算进去(新版客户端带系统框,量出 812×663 这种数),
    档位就全歪了。结果按屏幕位置(先上后左)排序,多开时区域编号稳定。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found, minimized = [], []

    def text_of(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value

    def cls_of(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = text_of(hwnd)
        if cls_of(hwnd) != CLIENT_WINDOW_CLASS and title != CLIENT_WINDOW_TITLE:
            return True
        if user32.IsIconic(hwnd):
            minimized.append(title or "梦幻西游")      # 最小化:截不到画面
            return True
        global GAME_DPI
        # 游戏是 DPI-unaware 的:GetDpiForWindow 对它恒报 96,不能用作换算比。
        # 缩放比取"窗口所在显示器的有效 DPI"(GetDpiForMonitor 的 MDT_EFFECTIVE
        # _DPI;感知进程里拿真实值,无感知进程会被虚拟化成 96 —— 探针验证时
        # 必须先 dpi_aware)。四参数原型:dpiX/dpiY 两个出参,少一个就 E_INVALIDARG。
        try:
            mon = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
            shcore = getattr(ctypes.windll, "shcore", None)
            dx, dy = wintypes.UINT(), wintypes.UINT()
            if mon and shcore is not None:
                shcore.GetDpiForMonitor.argtypes = [
                    ctypes.c_void_p, ctypes.c_int,
                    ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
                shcore.GetDpiForMonitor.restype = ctypes.c_long
                if shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx),
                                           ctypes.byref(dy)) == 0:
                    GAME_DPI = dx.value or 96
        except Exception:
            pass
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return True
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return True
        box = [pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom]
        if (box[2] - box[0] < CLIENT_MIN_W or box[3] - box[1] < CLIENT_MIN_H
                or box[0] < -10000):                  # 最小化窗口的坐标是 -32000
            return True
        found.append((title or "梦幻西游", box))
        return True

    try:
        user32.EnumWindows(proto(visit), 0)
    except Exception:
        return [], []
    found.sort(key=lambda t: (t[1][1], t[1][0]))       # 先上后左
    return found, minimized


WARMUP_TIMEOUT = 30.0      # OCR 预热超时(本机首次 GPU 预热约 1 秒,给足余量)


class Ocr:
    """RapidOCR 封装(线程安全,懒加载)。"""

    def __init__(self, log=None):
        self._engine = None
        self._lock = threading.Lock()
        self._log = log or (lambda m: None)
        self.device = "CPU"
        self.ok = True                 # warmup 那次真实推理成功过吗(False=假就绪)
        self.err = ""                  # 不健康的原因(引擎/看门狗据此重建与报错)

    def warmup(self, timeout: float = WARMUP_TIMEOUT):
        """预热:建好引擎 + 真跑一次推理,结果记在 ok 上。

        GPU(DirectML) 首次推理要初始化显存/算子(约 1 秒),提前吃掉,
        否则第一道题会明显变慢。这次推理失败**不再静默**:GPU 上先按既有
        机制回退 CPU 再试,仍然不行就把 ok 标成 False(附 err)——引擎据此
        报"识别不可用"而不是"已就绪",看门狗也能发现假就绪。

        预热整段放进临时线程跑,主线程只等 timeout:万一卡在驱动/引擎里不
        返回,这里照样标 ok=False。否则引擎线程恒活 + ok 默认 True,看门狗
        永远不动作,日志还会说"引擎已就绪"。
        """
        box: dict = {}
        done = threading.Event()

        def _work():
            try:
                box["ok"], box["err"] = self._warmup_once()
            except BaseException as e:      # 临时线程里出事也得落到 ok 上
                box["ok"], box["err"] = False, f"{type(e).__name__}: {e}"
            finally:
                done.set()

        th = threading.Thread(target=_work, name="OCR预热", daemon=True)
        th.start()
        if not done.wait(timeout):
            # 超时:那次预热的结论不再采信(卡住的线程还在就让它去),按不可用报
            self.ok = False
            self.err = f"预热超时({timeout:.0f} 秒没返回,可能卡在显卡驱动)"
            self._log(f"[OCR]{self.err},识别不可用,看门狗将重建引擎")
            return
        self.ok = bool(box.get("ok", False))
        self.err = str(box.get("err") or "")

    def _warmup_once(self):
        """真跑一次预热推理,返回 (ok, err);不直接改 self.ok(超时那次不采信)。"""
        with self._lock:
            self._get()
        dummy = np.full((32, 160, 3), 255, dtype=np.uint8)
        try:
            self._infer(dummy)
        except Exception as err:
            self._log(f"[OCR]推理初始化失败({err}),设备可能不可用")
            if str(self.device).startswith("GPU"):
                self._fallback_to_cpu(err)     # 既有机制:GPU 出问题就重建为 CPU
            try:
                self._infer(dummy)             # 回退后再试一次
            except Exception as err2:
                self._log(f"[OCR]推理仍不可用({err2}):"
                          "识别会一直报错,看门狗将重建引擎")
                return False, str(err2)
        return True, ""

    def _get(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self.device = "CPU"
            try:
                cfg = load_config()
                if app_cfg(cfg).get("use_gpu", True):
                    import onnxruntime as ort
                    if "DmlExecutionProvider" in ort.get_available_providers():
                        self._engine = RapidOCR(det_use_dml=True,
                                                rec_use_dml=True,
                                                cls_use_dml=True)
                        self.device = "GPU(DirectML)"
            except Exception:
                self._engine = None
            if self._engine is None:
                self._engine = RapidOCR()
        return self._engine

    def recognize(self, img_bgr) -> str:
        return "".join(b["text"] for b in self.recognize_blocks(img_bgr))

    def _infer(self, img_bgr, limit_side=None):
        """跑一次推理。limit_side 临时改 det 的输入下限(改完还原)。

        det 的下限是引擎实例上的一个属性,整段在锁里做 —— 两个线程同时识别也
        不会互相看到对方的临时值。取不到那个属性(库换实现)就按默认跑。
        """
        with self._lock:
            eng = self._get()
            det = getattr(eng, "text_det", None)
            old = getattr(det, "limit_side_len", None)
            if limit_side and old is not None:
                try:
                    det.limit_side_len = int(limit_side)
                except Exception:
                    old = None
            try:
                result, _ = eng(img_bgr, use_cls=False)
            finally:
                if old is not None:
                    det.limit_side_len = old
        return result or []

    def _fallback_to_cpu(self, err):
        """GPU 运行中出错时,重建为 CPU 引擎,保证识别不中断。

        重建也进锁:引擎的创建一律发生在 self._lock 里(_get 同理),两个
        线程同时走到这里也只会排队建一份,不会各建一份再互相覆盖(白占
        一份模型内存)。重建要一两秒,锁外等着的那条本来就是想用引擎的。
        """
        self._log(f"[OCR]GPU 识别异常({err}),自动回退 CPU")
        try:
            from rapidocr_onnxruntime import RapidOCR
            with self._lock:
                self._engine = RapidOCR()
                self.device = "CPU(已回退)"
        except Exception as e:
            self._log(f"[OCR]CPU 引擎重建失败: {e}")

    def recognize_blocks(self, img_bgr, limit_side=None):
        """返回带坐标的文本块列表,按 (y, x) 排序。

        limit_side: 这一次识别的 det 输入下限(默认跟着引擎配置 736)。
        小图会被 det 放大到这个下限再检测 —— 识别准一点,但**很贵**(一块
        860x240 的小区域放大到 2840x736 再跑 det)。只扫游戏界面里的一小块时
        调小它,又快又不掉准头(实测调小后连"朱紫国"都读得更对)。
        """
        try:
            result = self._infer(img_bgr, limit_side)
        except Exception as e:
            if str(self.device).startswith("GPU"):
                self._fallback_to_cpu(e)
                result = self._infer(img_bgr, limit_side)   # CPU 重试一次
            else:
                raise
        if not result:
            return []

        def sort_key(item):
            box = item[0]
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            return (sum(ys) / len(ys), sum(xs) / len(xs))

        blocks = []
        for box, text, conf in sorted(result, key=sort_key):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            blocks.append({
                "text": str(text),
                "x1": int(min(xs)), "y1": int(min(ys)),
                "x2": int(max(xs)), "y2": int(max(ys)),
                "conf": float(conf),
            })
        return blocks


class Screen:
    """mss 截图封装。region 为主显示器内的相对物理像素。

    mss 实例按线程各建一个:GDI DC 是线程亲和资源(实例创建时缓存),
    在 Engine 线程建、拿到工具线程里用会失效,所以谁第一次 grab 谁
    自己惰性建(threading.local 每线程各存一份)。
    """

    def __init__(self):
        self._local = threading.local()

    @property
    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss
            sct = mss.mss()
            self._local.sct = sct
        return sct

    def grab(self, region=None):
        sct = self._sct
        mon = sct.monitors[1]
        if region:
            x1, y1, x2, y2 = region
            box = {"left": mon["left"] + int(x1), "top": mon["top"] + int(y1),
                   "width": max(1, int(x2 - x1)), "height": max(1, int(y2 - y1))}
        else:
            box = dict(mon)
        shot = sct.grab(box)
        arr = np.asarray(shot)          # BGRA
        return np.ascontiguousarray(arr[:, :, :3])  # BGR


class Speaker(threading.Thread):
    """独立线程跑 Windows SAPI(SpVoice),避免阻塞界面。"""

    def __init__(self, cfg: dict, log):
        super().__init__(daemon=True)
        self.cfg = cfg                 # 完整分区配置:语音只读 app 分区
        self._log = log
        self.q: queue.Queue = queue.Queue()
        self._tts = None
        self._ready = threading.Event()
        self.dead = False                # 语音引擎起不来:say() 直接丢弃
        self.voice_name = ""

    def _setting(self, key, default=None):
        """语音相关的设置全在 app 分区:音量/音色/静音/合成后端。"""
        return app_cfg(self.cfg).get(key, default)

    def run(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            import win32com.client
            self._tts = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception as e:
            self._log(f"[语音]引擎初始化失败: {e}")
            # 置位并标记:否则每次播报都会让识别线程在这里等满 10 秒
            self.dead = True
            self._ready.set()
            return
        self._apply()
        self._ready.set()
        while True:
            item = self.q.get()
            if item[0] == "quit":
                break
            if item[0] == "say":
                text = item[1]
                if str(self._setting("tts_backend")) == "edge":
                    t0 = time.time()
                    if self._speak_edge(text):
                        tag = "本地缓存" if getattr(self, "_cache_hit", False) else "云端合成"
                        self._log(f"[语音]播报完成({tag} {time.time() - t0:.1f}s)")
                        continue
                try:
                    t0 = time.time()
                    self._tts.Speak(text)  # 同步播报,结束才返回
                    self._log(f"[语音]播报完成({time.time() - t0:.1f}s)")
                except Exception as e:
                    self._log(f"[语音]出错: {e}")
        # 正确释放 COM:先丢弃 SpVoice 引用,再反初始化公寓,避免退出时崩溃
        try:
            self._tts = None
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass

    def _speak_edge(self, text: str) -> bool:
        try:
            voice = self._setting("voice_hint") or "zh-CN-YunxiNeural"
            rate, volume, pitch = edge_params(self.cfg)
            cpath = _voice_cache_path(text, voice, rate, volume, pitch)
            if os.path.exists(cpath) and os.path.getsize(cpath) > 0:
                self._cache_hit = True
                _play_mp3_blocking(cpath)  # 缓存命中:零网络,直接播
                return True
            self._cache_hit = False
            last_err = "unknown"
            for _ in range(2):  # 服务偶发抖动,重试一次
                if _synth_edge_file(text, voice, rate, volume, cpath,
                                    timeout=7, retries=1, pitch=pitch):
                    _play_mp3_blocking(cpath)
                    return True
                last_err = "合成失败"
            self._log(f"[语音]云端失败({last_err}),回退系统语音")
            return False
        except Exception as e:
            self._log(f"[语音]云端出错({e}),回退系统语音")
            return False

    def _apply(self):
        tts = self._tts
        if tts is None:
            return
        try:
            # SAPI Rate 取值 -10..10;语速固定(RATE_PCT=0,界面不提供语速控件,
            # 固定才能让音频缓存不因改速反复失效)
            tts.Rate = max(-10, min(10, round(RATE_PCT / 10)))
            tts.Volume = max(0, min(100,
                                    int(float(self._setting("volume", 1.0)) * 100)))
        except Exception:
            pass
        hint = (self._setting("voice_hint") or "").strip()
        try:
            voices = tts.GetVoices()
            chosen_idx = None
            for i in range(voices.Count):
                desc = voices.Item(i).GetDescription()
                low = desc.lower()
                if hint and hint.lower() in low:
                    chosen_idx = i
                    break
                if chosen_idx is None and any(k in low for k in ZH_VOICE_KEYS):
                    chosen_idx = i
            if chosen_idx is None and voices.Count:
                chosen_idx = 0
            if chosen_idx is not None:
                tts.Voice = voices.Item(chosen_idx)  # 始终选好中文音,作为断网回退
                self.voice_name = voices.Item(chosen_idx).GetDescription()
            else:
                self.voice_name = "默认"
        except Exception:
            self.voice_name = "默认"
        if str(self._setting("tts_backend")) == "edge":
            self.voice_name = next((n for n, v in CLOUD_VOICES.items()
                                    if v == hint), "云端语音")
            return
        if self.voice_name != "默认" and not hint and not any(
                k in self.voice_name.lower() for k in ZH_VOICE_KEYS):
            self._log(f"[语音]未找到中文语音(当前 {self.voice_name}),"
                      "可在 设置→时间和语言→语音 中添加中文语音包")

    def say(self, text: str):
        if self.dead or self._setting("mute"):  # 起不来/静音都不拖住识别线程
            return
        if not self._ready.wait(timeout=10):
            self._log("[语音]引擎未能启动,无法播报")
            return
        self.q.put(("say", text))

    def quit(self):
        self.q.put(("quit", None))


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

def prune_voice_cache(cfg, bank) -> int:
    """删除与当前合成参数不匹配的缓存音频(改语速/音色后留下的失效文件)。"""
    voice = app_cfg(cfg).get("voice_hint") or "zh-CN-YunxiNeural"
    rate, volume, pitch = edge_params(cfg)
    texts = {e["a"] for e in bank.entries}
    texts.add(SAY_TTS_TEST)
    valid = {os.path.basename(_voice_cache_path(t, voice, rate, volume, pitch))
             for t in texts}
    removed = 0
    try:
        for f in os.listdir(VOICE_CACHE_DIR):
            if f.endswith(".mp3") and f not in valid:
                try:
                    os.remove(os.path.join(VOICE_CACHE_DIR, f))
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


def count_missing_voice(cfg, bank) -> int:
    """统计题库答案里还没有本地音频缓存的数量。"""
    voice = app_cfg(cfg).get("voice_hint") or "zh-CN-YunxiNeural"
    rate, volume, pitch = edge_params(cfg)
    n = 0
    for e in bank.entries:
        a = e["a"].strip()
        pth = _voice_cache_path(a, voice, rate, volume, pitch)
        if not (os.path.exists(pth) and os.path.getsize(pth) > 0):
            n += 1
    return n


def build_voice_cache(limit: int = 0, workers: int = 4, log=print):
    """并发合成答案音频。log 为进度回调(默认 print)。

    返回 (新合成, 失败, 已有)。可中断,重跑续传。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    bank = QuestionBank()
    cfg = load_config()
    voice = app_cfg(cfg).get("voice_hint") or "zh-CN-YunxiNeural"
    rate, volume, pitch = edge_params(cfg)
    seen, items = set(), []
    for e in bank.entries:
        a = e["a"].strip()
        if a and a not in seen:
            seen.add(a)
            items.append(a)
    if limit > 0:
        items = items[:limit]
    todo = []
    for a in items:
        pth = _voice_cache_path(a, voice, rate, volume, pitch)
        if not (os.path.exists(pth) and os.path.getsize(pth) > 0):
            todo.append(a)
    skipped = len(items) - len(todo)
    log(f"待合成 {len(todo)} 条(共 {len(items)} 条,已有 {skipped} 条,"
        f"{workers} 路并行)")
    done = failed = 0
    lock = threading.Lock()
    t0 = time.time()

    def work(a):
        nonlocal done, failed
        cpath = _voice_cache_path(a, voice, rate, volume, pitch)
        ok = _synth_edge_file(a, voice, rate, volume, cpath,
                              timeout=10, retries=1, pitch=pitch)
        with lock:
            if ok:
                done += 1
            else:
                failed += 1
            n = done + failed
            if n % 25 == 0 or n == len(todo):
                el = time.time() - t0
                eta = el / max(1, n) * (len(todo) - n) / 60
                log(f"音频合成进度 {n}/{len(todo)} | 成功 {done} 失败 {failed} | "
                    f"已用 {el/60:.1f} 分钟,预计还需 {eta:.0f} 分钟")

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))
    except KeyboardInterrupt:
        log("已中断,下次重跑会继续。")
    return done, failed, skipped


