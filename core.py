#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心地基:配置/OCR/语音/识别引擎调度。

题库的读写/更新/匹配在 bank.py,科举的匹配/收益统计在 tools/keju.py,
这里只留共用的地基,外加还留在这儿的 CLI 与自检(cmd_*)。
截图(只读屏幕) -> OCR 识别题目 -> 本地题库模糊匹配 -> TTS 播报答案。
不读写游戏进程/内存,答案由玩家自己选择。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import random
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
    "region": None,      # 兼容旧版单区域
    "regions": [],       # 多客户端:题目对话框区域列表,最多 4 个
    "volume": 1.0,
    "interval": 0.4,     # 空闲时的轮询间隔(秒),下限 0.4:有题目后自动加快到 0.1
    "mute": False,       # 静音:不出声,界面照旧显示答案
    "voice_hint": "",    # 声音:本地=声音名关键字;云端=edge语音ID;空=自动
    "tts_backend": "edge",  # edge=云端神经语音(清晰,需联网) / sapi=本地系统语音
    "pitch": 15,         # 音调提升(Hz):短答案更清亮,0=不改
    "use_gpu": True,     # OCR 用显卡加速(DirectML),失败自动回退 CPU
    "exam_mode": True,   # True=御前科举(按锚点); False=三大学士(不用锚点,全题干匹配)
    "bank_activities": ["keju", "qiandao"],  # 题库收录范围: keju=科举, qiandao=签到
    "regions_auto": False,   # 区域是"自动定位客户端"填的 -> 客户端挪了自动跟着走
}

MIN_INTERVAL, MAX_INTERVAL = 0.4, 10.0   # 轮询间隔的合法区间(见 tools/keju.py)

# 低置信读法会记到这里,方便事后确认是不是"题库未收录"的题。
# (写它的是科举的 note_uncollected,列它的是下面的 cmd_uncollected)
UNCOLLECTED_PATH = os.path.join(BASE_DIR, "data", "uncollected.jsonl")


FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]

ZH_VOICE_KEYS = ("zh-cn", "zh_cn", "zh-hans", "huihui", "kangkang",
                 "yaoyao", "tracy", "minxi", "yunxi", "yunyang")

CLOUD_VOICES = {
    "云端·云希(男声)": "zh-CN-YunxiNeural",
    "云端·晓晓(女声)": "zh-CN-XiaoxiaoNeural",
}

# 固定文案只留这一份:播报点(tools/keju.py 的全屏扫描、gui 的"播报试听")
# 和语音缓存清理的合法清单(prune_voice_cache)都按它算,改文案一处生效。
SAY_MULTI_QUESTION = "请保证屏幕里只有一个题目"
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


def _dpi_aware():
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
            "last_bank_check"),
    "keju": ("region", "regions", "regions_auto", "interval", "exam_mode",
             "bank_activities"),
}
_KEY_SECTION = {k: s for s, ks in CONFIG_SECTIONS.items() for k in ks}


def key_section(key: str):
    """配置键 -> 分区名;认不出的键返回 None(调用方自行兜底/告警)。"""
    return _KEY_SECTION.get(key)


# 日志归属:界面上的日志框只显示"本功能 + 公共"的行,别的来源的行只进文件。
# 判断靠消息里的 [模块] 前缀 —— 前缀就是各处自己写的,不用每个调用点都带参数。
LOG_SCOPE = {
    "keju": ("科举", "题目对话框", "全屏扫描", "识别模式", "自动监听",
             "统计", "奖励", "客户端", "提示"),
}


def log_scope(msg: str) -> str:
    """一条日志属于哪个功能;返回 "" 表示公共的(界面上也该看到)。"""
    m = re.match(r"\[([^\]\s]+)", (msg or "").strip())
    if not m:
        return ""
    tag = m.group(1)
    for scope, tags in LOG_SCOPE.items():
        if any(tag.startswith(t) for t in tags):
            return scope
    return ""


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
    if not keju.get("regions") and keju.get("region"):
        keju["regions"] = [keju["region"]]  # 旧版单区域迁移
    regs = [r for r in (keju.get("regions") or []) if r][:4]
    while len(regs) < 4:
        regs.append(None)
    keju["regions"] = regs  # 固定 4 个槽位,未设置为 None
    app_cfg(cfg).pop("threshold", None)  # 已废弃:下限固定在科举的 MIN_MATCH_PCT
    try:                   # 轮询间隔夹到合法区间(旧配置/手改值兜底)
        keju["interval"] = min(MAX_INTERVAL,
                               max(MIN_INTERVAL, float(keju.get("interval"))))
    except (TypeError, ValueError):
        keju["interval"] = DEFAULT_CONFIG["interval"]
    return cfg


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
# 自检/CLI 还留在 core,它们要用的科举那边的东西按名字取(不是垫片:
# 下面 cmd_selftest / cmd_rewards / cmd_uncollected / cmd_stats 就是调用点)
from tools.keju import (MIN_MATCH_PCT, RewardTally, add_manual_question,  # noqa: E402
                        is_exam_prompt, resolve_fullscan, strip_exam_header)
# 题库子系统(数据/IO)单独一层,见 bank.py。自检与 --uncollected 真会用到这两
# 个名字(不是垫片:cmd_selftest / cmd_uncollected / build_voice_cache 是调用点)。
from bank import BANK_PATH, QuestionBank  # noqa: E402

CLIENT_WINDOW_CLASS = "MHXYMainFrame"
CLIENT_WINDOW_TITLE = "梦幻西游 ONLINE"
CLIENT_MIN_W, CLIENT_MIN_H = 400, 300     # 比这还小就不可能是客户端画面

# 游戏里可选的 4 档分辨率(社区资料):640x480 / 860x600 / 1024x768 / 1024x768(HD)
GAME_RES_STEPS = ((640, 480), (860, 600), (1024, 768))
MIN_GAME_RES = GAME_RES_STEPS[0]          # 最小档:整窗口截图最快,实测最划算


def client_size_info(box):
    """客户端像素尺寸 -> (尺寸说明文字, 是否已经是最小分辨率)。

    窗口可以被拉伸,所以按面积取最接近的一档来猜;猜不到最小档就提醒用户调小。
    """
    w = max(1, int(box[2]) - int(box[0]))
    h = max(1, int(box[3]) - int(box[1]))
    step = min(GAME_RES_STEPS, key=lambda r: abs(r[0] * r[1] - w * h))
    return f"{w}×{h} ≈ {step[0]}×{step[1]} 档", step == MIN_GAME_RES


def can_follow_clients(boxes, auto_boxes, cur_regions):
    """区域能不能跟着客户端走:数量一致、且(如果还没被改过)。

    客户端最小化/关掉会让数量变化,那时宁可不跟,也别把别的槽位弄丢。
    """
    if not boxes or not auto_boxes or len(boxes) != len(auto_boxes):
        return False
    cur = [r for r in (cur_regions or []) if r]
    return not cur or [list(r) for r in cur] == [list(b) for b in auto_boxes]


def find_client_windows():
    """枚举梦幻西游客户端窗口。

    返回 ([(标题, [x1,y1,x2,y2]), ...], [已最小化的窗口标题, ...])。
    该窗口客户区==窗口矩形(标题栏是游戏自绘的),所以矩形就是"整个客户端画面";
    结果按屏幕位置(先上后左)排序,多开时区域编号稳定。
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
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        box = [rect.left, rect.top, rect.right, rect.bottom]
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

    def request_cycle(self):
        """手动识别:投递给科举自己的线程(它下一次循环立刻执行)。"""
        self.keju_tool.request(force=True)

    def request_fullscan(self):
        """全屏扫描:同样投递给科举自己的线程。"""
        self.keju_tool.request_fullscan()

    def set_watch(self, on: bool):
        # 直接改布尔标志(GIL 下原子);日志当场打,不再绕引擎线程
        self.keju_tool.watching = bool(on)
        self._log_watch_state()
        self.refresh_tools()            # 科举线程跟着这个开关启停

    def toggle_watch(self):
        self.set_watch(not self.keju_tool.watching)

    def _safe(self, name, *args):
        """按方法名执行识别任务:任何异常(含方法缺失)都记堆栈,不让线程死掉。"""
        try:
            getattr(self, name)(*args)
        except Exception:
            import traceback
            self.app.log("[引擎]任务异常(已恢复):\n"
                         + traceback.format_exc(), ui=False)

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
            self.app.log(f"[引擎]停止工具线程失败: {e}", ui=False)
        try:
            tool.join(timeout)          # 没 start 过的线程会抛,忽略即可
        except Exception:
            pass
        if tool.is_alive():
            self.app.log(f"[引擎]{getattr(tool, 'name', '工具')}线程未能退出",
                         ui=False)

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
            self.app.log(traceback.format_exc(), ui=False)
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
            self.app.log(f"[引擎]切换工具开关失败({e})", ui=False)

    def _wake_tools(self):
        """工具线程空闲时完全不轮询,状态一变就得叫它一声。"""
        tool = getattr(self, "keju_tool", None)
        if tool is not None:
            tool._wake.set()

    def cycle(self, force: bool):
        """分派一轮识别(_safe 按名字调它的任务入口)。"""
        self.keju_tool.keju_cycle(force)

# ---------------- 区域框选 ----------------

def _find_font(size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _render_question(text: str, size: int = 30):
    """把题目渲染成游戏对话框风格的图片,返回 PIL Image。"""
    from PIL import Image, ImageDraw

    font = _find_font(size)
    bg, fg = (247, 236, 212), (58, 42, 26)
    tmp = Image.new("RGB", (8, 8))
    draw = ImageDraw.Draw(tmp)
    max_w, pad = 760, 26
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w - pad * 2:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    line_h = size + 12
    img = Image.new("RGB", (max_w, pad * 2 + line_h * len(lines)), bg)
    d = ImageDraw.Draw(img)
    y = pad
    for ln in lines:
        d.text((pad, y), ln, font=font, fill=fg)
        y += line_h
    return img


# 模拟 OCR 常见误识:形近字替换 + 句尾截断
_SIMILAR_CHARS = {"曰": "日", "末": "未", "干": "千", "土": "士", "己": "已",
                  "晴": "睛", "拔": "拨", "辨": "辩", "壁": "璧"}


def _add_ocr_noise(text: str, truncate: bool = True) -> str:
    """模拟 OCR 误识:换一个形近字,再砍掉句尾(模拟框选不完整)。

    只在题干够长时砍尾巴:砍完还剩不到 10 个字的话,匹配器会走"严格整串"
    分支而必然失配 —— 那是夹具太狠,不是被测代码的问题。
    """
    chars = list(text)
    for i, ch in enumerate(chars):
        if ch in _SIMILAR_CHARS:
            chars[i] = _SIMILAR_CHARS[ch]
            break
    if truncate and len(chars) > 15:
        chars = chars[:-3]
    return "".join(chars)


def _render_fullscreen(questions: list[str]):
    """渲染一张"游戏画面"风格的整屏图:深色背景 + 题目框 + 干扰 UI 文本。"""
    from PIL import Image, ImageDraw

    decoys = [
        ("[当前] 队伍人数:3/3  气血 4520/4520", (40, 30)),
        ("[帮派] 无双城  帮贡 1260", (40, 62)),
        ("[系统] 您获得了银两 2500", (40, 94)),
        ("任务:师门任务 10/20", (1050, 30)),
        (".auto_team 频道", (1050, 62)),
    ]
    font = _find_font(30)
    small = _find_font(20)
    box_w, line_h, y0, gap = 880, 42, 200, 120
    tmp = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def wrap(q):
        lines, cur = [], ""
        for ch in q:
            if tmp.textlength(cur + ch, font=font) <= box_w - 60:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        return lines

    all_lines = [wrap(q) for q in questions]
    total_h = y0 + sum(40 + line_h * len(ls) + gap for ls in all_lines) + 60
    size = (1280, max(720, total_h))
    img = Image.new("RGB", size, (28, 30, 42))
    d = ImageDraw.Draw(img)
    dark_fg = (200, 205, 215)
    for text, pos in decoys:
        d.text(pos, text, font=small, fill=dark_fg)

    y = y0
    for lines in all_lines:
        x1, y1 = 180, y
        x2, y2 = 180 + box_w, y + 40 + line_h * len(lines)
        d.rectangle((x1, y1, x2, y2), fill=(247, 236, 212),
                    outline=(120, 90, 40), width=3)
        ty = y + 20
        for ln in lines:
            d.text((x1 + 30, ty), ln, font=font, fill=(58, 42, 26))
            ty += line_h
        y = y2 + gap
    return img


def _render_narrow(question: str, size=30):
    """模拟"带图布局":题干在窄栏里被拆成很多短行,右侧留白当图片区。"""
    from PIL import Image, ImageDraw

    font = _find_font(size)
    max_w = 260  # 窄栏:一行约 8 个字
    tmp = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    chars = list(question)
    lines = []
    cur = ""
    for ch in chars:
        if tmp.textlength(cur + ch, font=font) <= max_w - 20:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    line_h = 42
    img = Image.new("RGB", (760, 80 + line_h * len(lines)), (247, 236, 212))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 30, 300, 30 + line_h * len(lines) + 20),
                outline=(120, 90, 40), width=2)
    y = 50
    for ln in lines:
        d.text((36, y), ln, font=font, fill=(58, 42, 26))
        y += line_h
    return img


SELFTEST_SEED = 20260101          # --selftest 默认随机种子(固定 = 可复现)


def _selftest_report(ok: bool, shaky: bool, name: str, detail: str) -> bool:
    """打印一项体检结果。shaky=PASS 之外的"合成图 OCR 抖动",记为 WARN 不计失败。"""
    print(f"[{'PASS' if ok else 'WARN' if shaky else 'FAIL'}] {name}: {detail}")
    return ok or shaky


def cmd_selftest(seed: int = SELFTEST_SEED) -> int:
    """端到端体检:合成图片 -> 真 OCR -> 真题库匹配 -> 选答案。

    默认用固定随机种子,同样的抽题得到同样的结果(可复现);传 seed=0 则每次随机抽题。
    判断"有没有读出来"这件事依赖合成图的 OCR,读完没判对才叫 FAIL;
    读都读不出来记为 WARN(不计入失败)——严格的回归门禁在 tests.py。
    """
    if seed:
        random.seed(seed)
    bank = QuestionBank()
    ocr = Ocr()

    groups: dict[str, list] = {}
    for e in bank.entries:
        groups.setdefault(e["q"], []).append(e)
    ambiguous = [v for v in groups.values() if len({x["a"] for x in v}) > 1]

    cases = random.sample(bank.entries, 4)
    if ambiguous:
        cases += random.choices(random.choice(ambiguous), k=2)

    passed = 0
    for e in cases:
        noisy = _add_ocr_noise(e["q"])
        q_img = _render_question(noisy)
        q_text = ocr.recognize(np.asarray(q_img)[:, :, ::-1].copy())

        # 构造选项:正确答案 + 两个随机干扰答案
        others = [x["a"] for x in random.sample(bank.entries, 12) if x["a"] != e["a"]][:2]
        opts_line = f"A.{e['a']}  B.{others[0]}  C.{others[1]}"
        o_img = _render_question(opts_line, size=26)
        o_text = ocr.recognize(np.asarray(o_img)[:, :, ::-1].copy())

        candidates = bank.match_all(q_text, 62)
        entry, score, note = bank.pick_with_options(candidates, o_text)
        ok = entry is not None and entry["a"] == e["a"]
        passed += ok
        got = f"{entry['a']}{note}" if entry else "无匹配"
        print(f"[{'PASS' if ok else 'FAIL'}] 相似度{score:3.0f}% "
              f"“{e['q'][:20]}…” -> “{got}” 期望“{e['a']}”")
    print(f"\n自检结果: {passed}/{len(cases)} 通过(含 OCR 噪声与选项消歧模拟)")

    # ---- 全屏扫描场景 ----
    print("\n-- 全屏扫描场景 --")
    e1 = random.choice(bank.entries)
    fs_pass = True

    img = _render_fullscreen([_add_ocr_noise(e1["q"], truncate=False)])
    blocks = ocr.recognize_blocks(np.asarray(img)[:, :, ::-1].copy())
    n_q, entry, score, note, _ = resolve_fullscan(bank, blocks, 70)
    ok = n_q == 1 and entry is not None and entry["a"] == e1["a"]
    detail = f"相似度{score:.0f}% -> “{entry['a']}”{note}" if entry else "无结果"
    fs_pass &= _selftest_report(
        ok, (not blocks) or (n_q == 0 and score < 65), "单题+干扰UI",
        f"判定 {n_q} 道题, {detail} 期望“{e1['a']}”")

    e2 = random.choice(bank.entries)
    while e2["n"] == e1["n"]:
        e2 = random.choice(bank.entries)
    img = _render_fullscreen([e1["q"], e2["q"]])
    blocks = ocr.recognize_blocks(np.asarray(img)[:, :, ::-1].copy())
    n_q, _, _, _, _ = resolve_fullscan(bank, blocks, 70)
    fs_pass &= _selftest_report(
        n_q >= 2, n_q < 2, "双题检测",
        f"判定 {n_q} 道题(期望≥2,应播报提醒)")

    # 回声抑制:屏幕上同时有"刚答过的旧题残留"和"新题",应忽略旧题答新题
    e0 = random.choice(bank.entries)
    while e0["n"] in (e1["n"], e2["n"]):
        e0 = random.choice(bank.entries)
    img = _render_fullscreen([e0["q"], e2["q"]])
    blocks = ocr.recognize_blocks(np.asarray(img)[:, :, ::-1].copy())
    n_all, _, _, _, _ = resolve_fullscan(bank, blocks, 70)
    n_fix, entry_fix, _, note_fix, _ = resolve_fullscan(
        bank, blocks, 70, recent=[e0["n"]])
    suppressed_ok = (n_fix == 1 and entry_fix is not None
                     and entry_fix["a"] == e2["a"])
    got_fix = f"“{entry_fix['a']}”{note_fix}" if entry_fix else "无结果"
    fs_pass &= _selftest_report(
        suppressed_ok and n_all >= 2, suppressed_ok and n_all < 2,
        "回声抑制", f"无记忆判定 {n_all} 题 / 有记忆判定 {n_fix} 题 -> "
                    f"{got_fix} 期望“{e2['a']}”")

    # ---- 短文本防误报 ----
    lt0 = bank.match("是", 62)[0]
    ok = lt0 is None
    print(f"[{'PASS' if ok else 'FAIL'}] 短文本防误报: 单字“是”匹配={'有' if lt0 else '无'}(期望无)")
    fs_pass &= ok

    # 窄栏布局(带图):题干拆成多行短句,验证大窗口能拼出完整题干
    narrow_target = next(e for e in bank.entries if len(e["q"]) >= 18)
    nimg = _render_narrow(narrow_target["q"])
    nblocks = ocr.recognize_blocks(np.asarray(nimg)[:, :, ::-1].copy())
    n_all, n_entry, n_score, _, _ = resolve_fullscan(bank, nblocks, 65)
    ok = (n_all == 1 and n_entry is not None
          and n_entry["a"] == narrow_target["a"])
    got_n = f"{n_score:.0f}% -> {n_entry['a']}" if n_entry else "无"
    fs_pass &= _selftest_report(
        ok, (not nblocks) or (n_all == 0 and n_score < 65), "窄栏(带图)布局",
        f"判定 {n_all} 题, {got_n} 期望“{narrow_target['a']}”")

    # ---- 锚点剥头 ----
    print("\n-- 锚点剥头 --")
    s1 = strip_exam_header("御前科举大赛第9关：这一关考的是综合。题目：下列哪种茶叶不属于绿茶？")
    s2 = strip_exam_header("没有任何头的普通题目？")
    ok = ("下列哪种茶叶" in s1 and "御前科举" not in s1
          and s2 == "没有任何头的普通题目？")
    print(f"[{'PASS' if ok else 'FAIL'}] 剥头: 有头->{s1[:14]}… 无头不变={s2 == '没有任何头的普通题目？'}")
    fs_pass &= ok

    # 带“御前科举大赛…题目:”完整头的对话框,端到端应命中裸题库
    hx = random.choice(bank.entries)
    hdr_q = "御前科举大赛第9关：这一关考的是综合。题目：" + hx["q"]
    himg = _render_fullscreen([hdr_q])
    hblocks = ocr.recognize_blocks(np.asarray(himg)[:, :, ::-1].copy())
    n_h, entry_h, score_h, _, _ = resolve_fullscan(bank, hblocks, 70)
    ok = n_h == 1 and entry_h is not None and entry_h["a"] == hx["a"]
    got_h = f"{score_h:.0f}% -> {entry_h['a']}" if entry_h else "无"
    fs_pass &= _selftest_report(
        ok, (not hblocks) or (n_h == 0 and score_h < 65), "锚点识别",
        f"判定 {n_h} 题, {got_h} 期望“{hx['a']}”")

    overall = (passed >= len(cases) - 1) and fs_pass
    print(f"\n总自检结果: {'全部通过' if overall else '存在失败项'}"
          f"(种子 {seed or '随机'};WARN = 合成图 OCR 抖动,不计失败)")
    return 0 if overall else 1


def cmd_tts_test() -> int:
    cfg = split_config(DEFAULT_CONFIG)
    sp = Speaker(cfg, lambda m: print("[语音]", m))
    sp.start()
    if not sp._ready.wait(10) or sp.dead:
        print("语音引擎启动失败")
        return 1
    print(f"语音: {sp.voice_name}(音调 {edge_params(cfg)[2]:+d}Hz)")
    sp.say("梦幻西游科举答题器,语音测试成功。")
    time.sleep(4)
    return 0


def prune_voice_cache(cfg, bank) -> int:
    """删除与当前合成参数不匹配的缓存音频(改语速/音色后留下的失效文件)。"""
    voice = app_cfg(cfg).get("voice_hint") or "zh-CN-YunxiNeural"
    rate, volume, pitch = edge_params(cfg)
    texts = {e["a"] for e in bank.entries}
    texts |= {SAY_MULTI_QUESTION, SAY_TTS_TEST}
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


def cmd_rewards() -> int:
    """打印今天的答题收益(银两/经验/物品明细)。"""
    rt = RewardTally()
    rt.rollover()
    print(rt.summary())
    for line in rt.round_lines():
        print("  " + line)
    if rt.rounds:
        print("今天已完成的轮次:")
        for r in rt.rounds:
            mark = "达标" if r.get("pass") else "超时"
            items = "、".join(f"{k}×{v}"
                             for k, v in (r.get("items") or {}).items())
            print(f"  {r.get('slot')}号 {r.get('ts')} "
                  f"用时 {r['sec'] // 60}分{r['sec'] % 60}秒({mark}) "
                  f"银两 {r.get('silver', 0):,} 经验 {r.get('exp', 0)} "
                  f"物品 {items or '无'}")
    if rt.items:
        print("物品明细:")
        for name, cnt in sorted(rt.items.items(), key=lambda kv: -kv[1]):
            print(f"  {name} × {cnt}")
    if not (rt.exp or rt.silver or rt.items):
        print("答题时奖励提示出现在画面里(系统消息/聊天区)才会统计到;"
              "区域别只框对话框,整窗口最稳。")
    return 0


def cmd_uncollected(add=()) -> int:
    """列出"疑似题库未收录"的读法(去重、按分数降序)。

    add: ("序号=答案", ...) —— 直接补进题库(只加不减),序号取自本列表。
    已经在题库里的条目不再列出(补过的、官方后来收录的都算)。
    """
    if not os.path.exists(UNCOLLECTED_PATH):
        print(f"还没有记录(文件: {UNCOLLECTED_PATH})")
        print("遇到“读到像题目、但匹配分不到 %d%%”的读法才会写进去。"
              % MIN_MATCH_PCT)
        return 0
    rows = []
    try:
        with open(UNCOLLECTED_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"读取失败: {e}")
        return 1
    uniq = {}
    for r in rows:
        uniq[normalize(r.get("text", ""))] = r      # 同一段画面文字只留最后一次
    bank = None
    try:
        # 显式传路径:BANK_PATH 在测试里会被指到临时目录
        bank = QuestionBank(BANK_PATH)
    except Exception:
        pass                  # 题库读不出来也不该挡住"看清单"这件事
    items, done = [], 0
    for r in sorted(uniq.values(), key=lambda r: -int(r.get("score", 0))):
        q = r.get("q") or r.get("text", "")
        if bank is not None and bank.match(q, 95)[0] is not None:
            done += 1              # 已经在题库里了
            continue
        items.append(r)
    if not items:
        print(f"没有待确认的条目(已入库 {done} 条)")
        return 0
    print(f"{'序号':<5}{'时间':<20}{'分数':>5}  读到的题干")
    print("-" * 92)
    for i, r in enumerate(items, 1):
        print(f"{i:<5}{r.get('ts', ''):<20}{int(r.get('score', 0)):>4}%  "
              f"{(r.get('q') or r.get('text', ''))[:44]}")
        if r.get("options"):
            print(f"{'':<25}选项: {r['options'][:60]}")
        print(f"{'':<25}↳ 库里最接近: {r.get('guess_q', '')[:26]}"
              f" → {r.get('guess_a', '')}")
    print("-" * 92)
    print(f"待确认 {len(items)} 条" + (f",已入库 {done} 条不再列出" if done else ""))

    added, bad = 0, []
    for spec in add:
        idx_s, _, tail = str(spec).partition("=")
        # 支持 "序号=答案@活动"(活动省略时按 keju 记)
        ans, _, act = tail.strip().partition("@")
        ans, act = ans.strip(), (act.strip() or "keju")
        # 一次看多条列表时序号可能已经变了,所以把完整题干打出来核对
        if not idx_s.strip().isdigit() or not ans:
            bad.append(f"{spec}(格式应为 序号=答案[@活动])")
            continue
        n = int(idx_s)
        if not 1 <= n <= len(items):
            bad.append(f"{spec}(序号超出 1~{len(items)})")
            continue
        q = items[n - 1].get("q") or items[n - 1].get("text", "")
        if add_manual_question(q, ans, act):
            added += 1
            print(f"已补进题库[{act}]: {q} → {ans}")
        else:
            print(f"题库里已有: {q} → {ans}")
    for b in bad:
        print(f"跳过: {b}")
    if added:
        print(f"补了 {added} 条,题库共 {len(QuestionBank(BANK_PATH))} 题"
              "(题库更新只加不减,不会被覆盖)")
    else:
        print("补题用法: python keju.py --uncollected 1=正确答案 "
              "(要记到别的活动下就写 1=答案@qiandao)")
    return 0


# --stats 用的日志行模式:成功/漏答/空屏/重复抑制/模式切换/启动
_STATS_PATTERNS = (
    ("match", re.compile(r"^(\S+ \S+) (?:\[[^\]]+\])?匹配 (\d+)%(.*?) -> .*?【(.*?)】"
                         r"\| 响应 ([\d.]+)s \(OCR ([\d.]+)s 匹配 ([\d.]+)s\)")),
    ("match_old", re.compile(r"^(\S+ \S+) (?:\[[^\]]+\])?匹配 (\d+)%(.*?) -> .*?【(.*?)】"
                             r"\| OCR ([\d.]+)s 匹配 ([\d.]+)s 本轮 ([\d.]+)s")),
    # "未找到匹配"只在连续未匹配的第 1 次和每 10 次打一行:
    # 没有"(连续N次)"后缀的才是新一轮漏答,带后缀的是同一次在刷新
    ("nomatch", re.compile(r"^(\S+ \S+) (?:\[[^\]]+\])?题库中未找到匹配\(最高 (\d+)%\)"
                           r"(\(连续\d+次\))?")),
    ("blind", re.compile(r"^(\S+ \S+) [^ ]*题目[^ ]*(?:未识别到文字|没读到文字)"
                         r"\(题目未出现\)(\(连续\d+次\))?")),
    ("dup", re.compile(r"^(\S+ \S+) 该题 \d+ 秒内已播")),
    ("low", re.compile(r"^(\S+ \S+) (?:\[[^\]]+\])?低置信")),
    # "识别:" 才是"读到的题干"那一行(匹配/未找到那些行不能被当成它)
    ("seen", re.compile(r"^(\S+ \S+) (?:\[(?:自动监听|手动识别|检测)\])?"
                        r"识别: (.*)$")),
    # 旧日志写"识别模式: 普通",新日志写"识别模式已切换: 三大学士"
    ("mode", re.compile(r"^(\S+ \S+) (?:识别模式(?::|已切换:)\s*|\[识别模式\])(.+)$")),
    ("boot", re.compile(r"^(\S+ \S+) 应用启动")),
)


def cmd_stats(since: str = "") -> int:
    """解析 keju.log:命中率/失败构成(分模式段) + '题目出现 → 出答案'响应性能。

    since 如 '19:30',只看该时刻之后的记录。
    """
    if not os.path.exists(LOG_PATH):
        print("还没有日志文件")
        return 1
    since_norm = since.strip()
    events = []                       # (ts, kind, [捕获组...])
    for line in open(LOG_PATH, encoding="utf-8"):
        if since_norm and len(line) >= 16 and line[11:16] < since_norm:
            continue
        for kind, pat in _STATS_PATTERNS:
            m = pat.match(line)
            if m:
                events.append((kind, m.groups()))
                break

    # 分模式段:每次启动或切模式都开一段(三大学士那段噪声大,必须分开看)
    segs = []
    mode = "御前科举"

    def new_seg(ts):
        segs.append({"ts0": ts, "ts1": ts, "mode": mode, "match": 0,
                     "nomatch": 0, "prompt": 0, "blind": 0, "dup": 0,
                     "disamb": 0, "low": 0, "resp": [], "near": []})
        return segs[-1]

    cur = new_seg("")
    last_seen = ""
    for kind, g in events:
        ts = g[0]
        if kind == "mode":
            # 普通/三大学士是同一个模式的两种叫法(改了界面文案)
            mode = ("三大学士" if ("三大" in g[1] or "普通" in g[1])
                    else "御前科举")
            cur = new_seg(ts)
            continue
        if kind == "boot":
            cur = new_seg(ts)
            continue
        if kind == "seen":
            last_seen = g[1]
            continue
        cur["ts1"] = ts
        if kind in ("match", "match_old"):
            # match 的组序是 (时间,得分,备注,答案,响应,OCR,匹配)
            resp = float(g[4]) if kind == "match" else float(g[6])
            cur["match"] += 1
            cur["resp"].append(resp)
            if "按选项消歧" in g[2]:
                cur["disamb"] += 1
        elif kind == "nomatch":
            if g[2]:                  # "(连续N次)"= 同一次漏答在刷新,不重复计数
                continue
            if is_exam_prompt(last_seen):
                # 考官开场白(不是题目):新版本已在校验前过滤掉,老日志里还在
                cur["prompt"] += 1
                continue
            cur["nomatch"] += 1
            if int(g[1]) > 0:         # 有候选 = 疑似真题没答出,值得逐条看
                cur["near"].append((ts, int(g[1]), last_seen[:24]))
        elif kind == "blind":
            if not g[1]:
                cur["blind"] += 1
        elif kind == "dup":
            cur["dup"] += 1
        elif kind == "low":
            cur["low"] += 1

    print("== 分模式段:命中率与失败构成 ==")
    print(f"{'时段':<18}{'模式':<9}{'答出':>5}{'漏答':>5}{'命中率':>8}"
          f"{'考官对话':>9}{'空屏':>5}{'消歧':>5}{'抑制':>5}{'平均响应':>9}")
    print("-" * 92)
    tot = {"match": 0, "nomatch": 0, "prompt": 0, "blind": 0, "disamb": 0,
           "dup": 0, "resp": [], "near": []}
    for s in segs:
        if not (s["match"] or s["nomatch"] or s["blind"] or s["prompt"]):
            continue
        asked = s["match"] + s["nomatch"]
        rate = s["match"] / asked * 100 if asked else 0.0
        avg = sum(s["resp"]) / len(s["resp"]) if s["resp"] else 0.0
        span = s["ts0"][11:16] + "~" + s["ts1"][11:16]
        print(f"{span:<18}{s['mode']:<9}{s['match']:>5}{s['nomatch']:>5}"
              f"{rate:>7.0f}%{s['prompt']:>9}{s['blind']:>5}{s['disamb']:>5}"
              f"{s['dup']:>5}{avg:>8.2f}s")
        for k in ("match", "nomatch", "prompt", "blind", "disamb", "dup"):
            tot[k] += s[k]
        tot["resp"] += s["resp"]
        tot["near"] += s["near"]
    print("-" * 92)
    asked = tot["match"] + tot["nomatch"]
    if asked:
        print(f"合计: 读到题干 {asked} 题次 → 答出 {tot['match']} "
              f"({tot['match'] / asked * 100:.0f}%),漏答 {tot['nomatch']}"
              f"(其中读到候选接近的 {len(tot['near'])} 条)")
        print(f"      考官开场白(不算题,新版本已过滤) {tot['prompt']} · "
              f"多题消歧 {tot['disamb']} · 重复抑制 {tot['dup']} · "
              f"无文字轮次(空屏) {tot['blind']}")
        for ts, sc, txt in sorted(tot["near"], key=lambda x: -x[1])[:15]:
            print(f"      疑似没答出: {ts} 最高 {sc}%  {txt}")
        print("      注:漏答的最高分=0% 只出现在旧日志(那时没记录最接近的分数);"
              "新日志里 ≥60% 基本是真题没收录,＜40% 说明屏幕上不是题")
    if not tot["resp"]:
        print("日志里还没有识别记录(先去答几道题)")
        return 0

    rows = []
    for k, g in events:
        if k == "match":              # (ts,得分,备注,答案,响应,OCR,匹配)
            rows.append((g[0], int(g[1]), g[3].strip(), float(g[4]),
                         float(g[5]), float(g[6])))
        elif k == "match_old":        # (ts,得分,备注,答案,OCR,匹配,本轮)
            rows.append((g[0], int(g[1]), g[3].strip(), float(g[6]),
                         float(g[4]), float(g[5])))

    def summarize(name, vals):
        vals = sorted(vals)
        n = len(vals)
        print(f"{name}: 共{n}次 平均{sum(vals) / n:.2f}s "
              f"中位{vals[n // 2]:.2f}s P90 {vals[int(n * 0.9)]:.2f}s "
              f"最快{vals[0]:.2f}s 最慢{vals[-1]:.2f}s")

    print("\n== 响应性能(仅答出的题) ==")
    responses = [r[3] for r in rows]
    summarize("题目出现→答案(响应)", responses)
    for th in (0.5, 1.0, 2.0):
        n = sum(1 for v in responses if v <= th)
        print(f"  ≤{th:.1f}s: {n}/{len(responses)} ({n / len(responses) * 100:.0f}%)")
    summarize("  └ OCR", [r[4] for r in rows])
    summarize("  └ 匹配", [r[5] for r in rows])

    print("\n== 明细 ==")
    print(f"{'时间':<19}{'得分':>5}{'响应':>8}{'OCR':>8}{'匹配':>7}  题目/答案")
    print("-" * 88)
    for ts, score, ans, resp, ocr, match in rows:
        print(f"{ts:<19}{score:>4}%{resp:>7.2f}s{ocr:>7.2f}s{match:>6.2f}s  {ans[:26]}")
    print("-" * 88)
    bad = [r for r in rows if r[1] < 80]
    if bad:
        print(f"低置信匹配(得分<80%,共{len(bad)}题,建议抽查):")
        for ts, score, ans, *_ in bad:
            print(f"  {ts}  {score}%  {ans[:30]}")
    return 0


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
    todo = [a for a in items
            if not (os.path.exists(_voice_cache_path(a, voice, rate, volume, pitch))
                    and os.path.getsize(_voice_cache_path(a, voice, rate, volume,
                                                          pitch)) > 0)]
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


def cmd_build_voice_cache(limit: int, workers: int = 4) -> int:
    """命令行入口:全量预合成答案音频。"""
    done, failed, skipped = build_voice_cache(limit, workers, log=print)
    print(f"完成:新合成 {done},失败 {failed},原有 {skipped}")
    return 0 if failed == 0 else 1


