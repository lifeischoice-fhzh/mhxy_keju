#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试:python tests.py(自 keju-answerer 移植;降妖伏魔两组已随功能裁掉)

覆盖 10 组:
  1 配置与工具函数            2 题库与匹配
  3 题库更新(离线桩)         3.5 选项采集(答完回写仓库)
  4 语音与音频缓存
  5 识别管线(锚点/滑窗/多题/回声/歧义)
  6 识别引擎(帧门控/区域/全屏/任务循环/统计/看门狗)
  7 OCR 与截图                8 界面(Tk)
  9 日志与统计 CLI            + 10/11 分支补漏(覆盖率驱动/错误兜底)

原则:不联网、不发出声音、不碰真实题库/配置/缓存(全部指向临时目录),
图片用合成图;需要 GPU 时会自动回退 CPU,不影响结果。
`python keju.py --selftest` 是另一套带随机抽样的"体检",不在本文件内。
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types

import numpy as np

import bank as bank_mod        # 题库子系统(从 core.py 搬出去的,见 bank.py)
import cli                     # 命令行层(cmd_*:自检/统计/补题)
import client_common           # clients 拆出的通用探测(枚举/画布/档位表)
import clients                 # 本应用窗口口径:支持档判定/文案/跟随闸
import core
import gui
import keju
import ocr                     # 截屏 + OCR(core 拆出的单文件模块)
import voice                   # 语音播报(core 拆出的单文件模块)
from bank import QuestionBank  # 类名直接用(本文件里的同名局部变量叫 bank,不冲突)
from core import (Ocr, edge_params, load_config, normalize,
                  _voice_cache_path)
# 科举的匹配/选项采集在 tools/keju.py,这里按它的位置取
from tools import keju as tools_keju
from tools.keju import (OptionsStore, parse_options, resolve_questions,
                        split_options_markers, strip_exam_header)
from ui import keju as ui_keju          # 主窗布局(确认已删的函数不在了)
from ui import widgets as ui_widgets   # 窗口摆位/动效

# 全程把事件文件指到临时目录:下面成片的识别用例会真写 match/nomatch 事件,
# 不能落进仓库的 data/events.jsonl(要专门断言事件的用例再自行 patch)。
core.EVENTS_PATH = os.path.join(tempfile.mkdtemp(prefix="keju_events_"),
                                "events.jsonl")

results = []
sections = []
QUIET = False          # --quiet:只打 FAIL 和结果行(省上下文,给 agent 用)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    if QUIET and ok:
        return
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def section(fn):
    """把函数登记成一个测试组(按登记顺序执行)。"""
    sections.append(fn)
    return fn


# patch() 要换哪几个模块里的同名值。列在这里的就是"真身所在的那份名字":
#   core —— 地基本体(配置/引擎调度/事件;ocr/clients/voice 经它 re-export)
#   bank —— 题库子系统本体(BANK_PATH / update_bank / fetch_* / QuestionBank / ...)
#   ocr / clients / voice —— 从 core.py 拆出的单文件模块:模块内互调走自己
#   那份名字(如 Speaker 播 _synth_edge_file、画布探测内调 client_canvas),
#   只换 core 上的 re-export 拦不到,真身这份必须一起换
#   client_common —— clients 再拆出的通用探测(窗口/画布枚举、档位表、
#   角色名,可整文件拷给别的应用):client_canvas 内调的 find_client_canvases
#   真身在这份,桩画布必须连这份一起换
# gui / keju 不在表里:它们调用时现取(core.xxx / bank.xxx),换 core / bank
# 上的那份就够了。例外(不在上面这些模块里的,用例单独指名):
#   tools/ 里自己的常量(OPTIONS_PATH)—— mods=(tools_keju,)
#   cli —— 命令行层的 cmd_*(keju.py 入口按 cli.xxx 现取,换这份才生效)
PATCH_MODULES = (core, bank_mod, cli, ocr, clients, voice, client_common)


class patch:
    """临时替换模块级名字:core/bank 里同名的值一起换,退出还原。

    mods 指定时只换这些模块 —— 真身不在 core/bank 里的符号(在 tools/ 里的)
    得这样单独指一下。
    """

    def __init__(self, mods=None, **kw):
        self.mods = tuple(mods) if mods else PATCH_MODULES
        self.kw = kw
        self.saved = []

    def __enter__(self):
        for name, val in self.kw.items():
            for mod in self.mods:
                if hasattr(mod, name):
                    self.saved.append((mod, name, getattr(mod, name)))
                    setattr(mod, name, val)
        return self

    def __exit__(self, *exc):
        for mod, name, old in reversed(self.saved):
            setattr(mod, name, old)
        self.saved = []
        return False


class Sandbox:
    """把数据文件/配置/日志/音频缓存全部指到临时目录。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="keju_test_")

    def __enter__(self):
        self.data = os.path.join(self.dir, "data")
        os.makedirs(self.data, exist_ok=True)
        self.bank = os.path.join(self.data, "keju_questions.json")
        self.meta = os.path.join(self.data, "keju_metadata.json")
        self.backup = os.path.join(self.data, "keju_questions.backup.json")
        self.config = os.path.join(self.dir, "config.json")
        self.log = os.path.join(self.dir, "keju.log")
        self.voice = os.path.join(self.dir, "voice_cache")
        self.events = os.path.join(self.dir, "data", "events.jsonl")
        self.options = os.path.join(self.dir, "data", "keju_options.json")
        os.makedirs(self.voice, exist_ok=True)
        self._p = patch(BANK_PATH=self.bank, BANK_META_PATH=self.meta,
                        BANK_BACKUP_PATH=self.backup, CONFIG_PATH=self.config,
                        LOG_PATH=self.log, VOICE_CACHE_DIR=self.voice,
                        EVENTS_PATH=self.events)
        self._p2 = patch(mods=(tools_keju,), OPTIONS_PATH=self.options)
        self._p.__enter__()
        self._p2.__enter__()
        return self

    def __exit__(self, *exc):
        self._p2.__exit__(*exc)
        self._p.__exit__(*exc)
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def make_bank(path, entries):
    """把若干 {'q','a'} 写成临时题库并加载(用于精确控制候选集)。"""
    raw = [{"question": e["q"], "answer": e["a"],
            "normalized_question": e.get("n") or normalize(e["q"])}
           for e in entries]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    return QuestionBank(path)


def blk(text, x1, y1, h=30):
    """构造一个 OCR 文本块(手写场景用)。"""
    return {"text": text, "x1": x1, "y1": y1, "x2": x1 + 30 * len(text),
            "y2": y1 + h, "conf": 0.95}


def capture(fn, *a, **kw):
    """跑一个会 print 的函数,返回 (返回码, 输出文本)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


# ---------------------------------------------------------------- 1 配置与工具

@section
def sec_config():
    check("normalize:NFKC+去标点+小写",
          normalize("Ａb１ 中，文。！") == "ab1中文")
    check("normalize:空值不炸", normalize("") == "" and normalize(None) == "")

    r, v, p = edge_params({"app": {"volume": 1.0, "pitch": 15}})
    _, v_low, p_high = edge_params({"app": {"volume": 0.5, "pitch": 999}})
    _, v_high, p_low = edge_params({"app": {"volume": 5.0, "pitch": -999}})
    check("edge_params:音量/音调夹紧",
          v == 0 and v_low == -50 and v_high == 0 and p == 15
          and p_high == 50 and p_low == -50, f"{v_low}/{p_high}/{p_low}")

    path_a = _voice_cache_path("铁观音", "zh-CN-YunxiNeural", 0, 0, 15)
    path_b = _voice_cache_path("铁观音", "zh-CN-YunxiNeural", 0, 0, 15)
    path_c = _voice_cache_path("龙井", "zh-CN-YunxiNeural", 0, 0, 15)
    path_d = _voice_cache_path("铁观音", "zh-CN-YunxiNeural", 0, 0, 30)
    check("缓存键:稳定且区分文本/音调",
          path_a == path_b and len({path_a, path_c, path_d}) == 3)
    check("缓存目录会被自动创建", os.path.isdir(os.path.dirname(path_a)))

    with Sandbox() as sb:
        core.save_config({"app": {"mute": True},
                          "keju": {"regions": [[1, 2, 3, 4]], "interval": 0.2,
                                   "threshold": 70}})
        with open(sb.config, encoding="utf-8") as f:
            raw = json.load(f)
        c1 = load_config()
        check("配置:save→load 往返(真分区落盘) + 间隔夹到下限 + threshold 丢弃",
              sorted(raw) == ["app", "keju"]
              and c1["keju"]["regions"][0] == [1, 2, 3, 4]
              and c1["keju"]["interval"] == 0.4
              and c1["app"]["mute"] is True
              and all("threshold" not in part for part in c1.values())
              and len(c1["keju"]["regions"]) == core.MAX_CLIENTS,
              f"interval={c1['keju']['interval']} 顶层={sorted(raw)}")

        with open(sb.config, "w", encoding="utf-8") as f:   # 旧版平铺格式
            json.dump({"region": [9, 8, 77, 66], "interval": 0.05}, f)
        c2 = load_config()
        check("配置:旧版平铺单区域已废弃(region 键清掉) + 槽位补齐",
              "region" not in c2["keju"] and "region" not in c2["app"]
              and c2["keju"]["regions"] == [None] * core.MAX_CLIENTS
              and c2["keju"]["interval"] == 0.4)

        with open(sb.config, "w", encoding="utf-8") as f:
            json.dump({"keju": {"interval": "abc"}}, f)
        check("配置:非法间隔回落默认",
              load_config()["keju"]["interval"] == core.DEFAULT_CONFIG["interval"])

        with open(sb.config, "w", encoding="utf-8") as f:   # 老配置:只有布尔
            json.dump({"keju": {"exam_mode": False}}, f)
        check("配置:老 exam_mode=False 迁移成「三大学士」模式",
              load_config()["keju"]["mode"] == "三大学士")
        with open(sb.config, "w", encoding="utf-8") as f:
            json.dump({"keju": {"mode": "车迟斗法", "exam_mode": True}}, f)
        check("配置:新 mode 键优先(exam_mode 只是识别管线的旧键)",
              load_config()["keju"]["mode"] == "车迟斗法")

        with open(sb.config, "w", encoding="utf-8") as f:
            f.write("{坏掉的 json")
        c3 = load_config()
        check("配置:损坏文件不影响启动",
              c3["keju"]["interval"] == 0.4
              and c3["keju"]["regions"] == [None] * core.MAX_CLIENTS
              and sorted(c3) == ["app", "keju"])

        check("MIN/MAX_INTERVAL 与 _poll_interval 语义一致",
              core.MIN_INTERVAL == 0.4 and core.MAX_INTERVAL == 10.0)

        # save_config:open 也在重试圈内(.tmp 本身被占住时不能一次就炸)
        real_open = open
        calls = {"n": 0}

        def flaky_open(path, *a, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "tmp 被占用")
            return real_open(path, *a, **kw)

        core.open = flaky_open        # 模块级影子:patch() 只换已存在的名字
        try:
            core.save_config({"app": {"mute": True}})
        finally:
            del core.open
        check("save_config:tmp 被占住时重试(重试圈含 open)",
              calls["n"] == 3 and load_config()["app"]["mute"] is True,
              f"尝试 {calls['n']} 次")

        # ---- 事件文件:启动时只留当天(非当天/坏行清掉) ----
        today = time.strftime("%Y-%m-%d")
        rows = [
            {"ts": f"{today} 08:00:00", "kind": "boot"},
            {"ts": "2020-01-01 09:00:00", "kind": "match", "answer": "旧的"},
            "坏行,不是 json\n",
            {"ts": f"{today} 09:00:01", "kind": "match", "answer": "新的"},
        ]
        with open(sb.events, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(r if isinstance(r, str)
                        else json.dumps(r, ensure_ascii=False) + "\n")
        removed = core.prune_events()
        with open(sb.events, encoding="utf-8") as f:
            kept = [json.loads(l) for l in f if l.strip()]
        check("prune_events:非当天与坏行清掉,当天的原序保留",
              removed == 2 and [k.get("answer") for k in kept] == [None, "新的"]
              and all(k["ts"].startswith(today) for k in kept), str(kept))
        check("prune_events:全是当天时不再动文件(幂等)",
              core.prune_events() == 0
              and len(kept) == sum(1 for _ in open(sb.events,
                                                   encoding="utf-8")))
        os.remove(sb.events)
        check("prune_events:没有事件文件时返回 0", core.prune_events() == 0)

    check("check_deps:本机依赖齐全", core.check_deps() == [],
          str(core.check_deps()))
    core.dpi_aware()          # 不该抛异常
    check("dpi_aware 可调用", True)


# ---------------------------------------------------------------- 2 题库与匹配

@section
def sec_bank():
    bank = QuestionBank()
    bad = [e for e in bank.entries if not e["q"] or not e["a"] or not e["n"]]
    check(f"题库完整({len(bank)} 题)", len(bank) >= 1000 and not bad,
          f"异常 {len(bad)} 条")

    q = bank.entries[3]
    hit, score = bank.match(q["q"], 62)
    check("match:精确命中", hit is not None and hit["a"] == q["a"] and score >= 95,
          f"{score:.0f}%")
    noisy = cli._add_ocr_noise(q["q"])
    hit_n, _ = bank.match(noisy, 62)
    check("match:带 OCR 噪声(形近字+截断)仍能命中", hit_n is not None,
          f"“{noisy[:16]}…” -> {hit_n['a'] if hit_n else '无'}")
    check("match:英文乱码长串不命中",
          bank.match("qwertyuiopasdfghjkl", 62)[0] is None)
    check("match:阈值高于最高分则返回 None",
          bank.match(q["q"], 101)[0] is None)
    check("match:单字不命中(短文本防误报)", bank.match("是", 62)[0] is None)

    groups = {}
    for e in bank.entries:
        groups.setdefault(e["n"], []).append(e)
    ambiguous = {k: v for k, v in groups.items() if len({x["a"] for x in v}) > 1}
    amb_first = next(iter(ambiguous.values()))[0]
    cands = bank.match_all(amb_first["q"], 62)
    scores = [s for _, s in cands]
    check("match_all:按分数降序且带回答案冲突的并列候选",
          len(cands) >= 2 and scores == sorted(scores, reverse=True)
          and len({e["a"] for e, _ in cands}) >= 2,
          f"{[(round(s), e['a']) for e, s in cands][:3]}")

    check("pick_with_options:无候选", QuestionBank.pick_with_options([], "A x")[0] is None)
    e1 = ({"q": "题", "a": "甲", "n": "题"}, 100.0)
    check("pick_with_options:空选项文本 -> 取最高分",
          QuestionBank.pick_with_options([e1], "")[0]["a"] == "甲")
    two = [({"q": "题", "a": "甲", "n": "题"}, 100.0),
           ({"q": "题", "a": "乙", "n": "题"}, 99.0)]
    picked, _, note = QuestionBank.pick_with_options(two, "A 乙 B 丙")
    check("pick_with_options:选项命中谁就选谁", picked["a"] == "乙" and note,
          note.strip())
    picked2, _, note2 = QuestionBank.pick_with_options(two, "A 完全不相干")
    check("pick_with_options:选项都不沾边则回落最高分",
          picked2["a"] == "甲" and not note2)

    # 填空题的空格线:题库存着"____",OCR 读不出这条图形线(读了也是长短
    # 随机的几截)—— 两边比对都剥掉下划线,否则 31 字存 norm vs 10 字读数
    # 只有 44%(2026-11-15 车迟斗法实测:八佾舞于庭整题静默漏答)
    tmp = tempfile.mkdtemp(prefix="keju_blank_")
    bbank = make_bank(os.path.join(tmp, "blank_bank.json"), [
        {"q": "孔子谓季氏：八佾舞于庭，_____________________",
         "a": "是可忍也，孰不可忍也。"}])
    hit_b, score_b = bbank.match("孔子谓季氏：“八偷舞于庭，”", 80)
    check("填空线:题库空格线不参与比对(读不出图形线也能命中)",
          hit_b is not None and hit_b["a"] == "是可忍也，孰不可忍也。"
          and score_b >= 80, f"{score_b:.0f}%")
    pre_b = bbank.prefilter(normalize("孔子谓季氏八偷舞于庭"), 65)
    check("填空线:滑窗预筛同样放行",
          pre_b is not None and pre_b[0] >= 65, f"{pre_b}")
    amb_pair = [({"q": "题", "a": "花妖", "n": "题"}, 100.0),
                ({"q": "题", "a": "蜃气妖", "n": "题"}, 100.0)]
    picked3, _, note3 = QuestionBank.pick_with_options(
        amb_pair, "A.屋气妖B.鹿")
    check("pick_with_options:平手组选项 OCR 读别字(蜃→屋)按近似线消歧",
          picked3["a"] == "蜃气妖" and "近似" in note3, note3.strip())
    lone_top = [({"q": "题", "a": "花妖", "n": "题"}, 100.0),
                ({"q": "题", "a": "屋气妖", "n": "题"}, 67.0)]
    picked4, _, note4 = QuestionBank.pick_with_options(
        lone_top, "A.蜃气妖B.鹿")
    check("pick_with_options:非平手不启用近似线,仍回退最高分",
          picked4["a"] == "花妖" and not note4, note4.strip())

    sha1 = bank_mod._bank_content_sha([{"question": "a", "answer": "1"},
                                   {"question": "b", "answer": "2"}])
    sha2 = bank_mod._bank_content_sha([{"question": "b", "answer": "2"},
                                   {"question": "a", "answer": "1"}])
    sha3 = bank_mod._bank_content_sha([{"question": "a", "answer": "9"},
                                   {"question": "b", "answer": "2"}])
    check("题库指纹:与顺序无关、内容变了会变",
          sha1 == sha2 and sha1 != sha3)

    with Sandbox():
        check("_local_meta:文件不存在返回空 dict", bank_mod._local_meta() == {})

    check("_js_unescape:转义还原",
          bank_mod._js_unescape('a\\"b') == 'a"b')
    check("_js_unescape:非法转义原样返回",
          bank_mod._js_unescape("坏\\q") == "坏\\q")

    sample = 'x=[{Id:"1",Q:"甲题？",A:"答案一"},{Id:"2",Q:"乙题？",A:"答案二，带逗号"}]'
    import re as _re
    pat = _re.compile(r'\{Id:"([^"]*)",Q:"((?:[^"\\]|\\.)*)",A:"((?:[^"\\]|\\.)*)"\}')
    recs = pat.findall(sample)
    check("官方源:记录正则", len(recs) == 2 and bank_mod._js_unescape(recs[1][1]) == "乙题？",
          f"{len(recs)} 条")

    long_q = "晴天白日依山尽，黄河入海流，欲穷千里目"      # 20 字
    noisy1 = cli._add_ocr_noise(long_q)
    check("_add_ocr_noise:只换第一个形近字;长题砍 3 字、短题不砍",
          noisy1[0] == "睛" and len(noisy1) == len(long_q) - 3
          and cli._add_ocr_noise("晴天白日依山尽") == "睛天白日依山尽"
          and cli._add_ocr_noise("末末", truncate=False) == "未末")

    from PIL import Image
    img = cli._render_question("测试题干内容")
    check("_render_question:返回可识别尺寸的图",
          isinstance(img, Image.Image) and img.size[0] > 100 and img.size[1] > 30,
          f"{img.size}")
    check("_find_font:能找到中文字体",
          cli._find_font(30) is not None)
    fs = cli._render_fullscreen(["第一道题？", "第二道题？"])
    check("_render_fullscreen:多题画面高度随内容增长",
          fs.size[0] == 1280 and fs.size[1] >= 720, f"{fs.size}")
    nr = cli._render_narrow("窄栏里被拆成很多短行的题干内容")
    check("_render_narrow:窄栏布局宽度固定",
          nr.size[0] == 760 and nr.size[1] > 80, f"{nr.size}")


def raiser(exc):
    """返回一个"总是抛异常"的桩函数。"""
    def f(*a, **k):
        raise exc
    return f


def _tcl_drain():
    """在主线程里回收 Tk 对象,避免解释器被后台线程的 GC 释放(Tcl_AsyncDelete)。"""
    import gc
    gc.collect()


def _pump(widget, ms=600):
    """替 Tk 事件循环转一会儿。

    窗口动效(淡入淡出/滑入)是 after() 驱动的,断言"窗口藏没藏/显没显"之前
    得让它跑完;不然测的是动画中间态。
    """
    end = time.time() + ms / 1000.0
    while time.time() < end:
        try:
            widget.update()
        except Exception:
            break
        time.sleep(0.01)


def _pump_until(widget, cond, ms=3000):
    """转事件循环直到 cond() 成立(或超时),返回最后一次判断结果。

    动效是 after 驱动的:机器忙(跑全量时那些 OCR 线程)回调会被拖慢,
    用固定时长等就容易偶发失败 —— 等**状态**而不是等时间。
    """
    end = time.time() + ms / 1000.0
    while time.time() < end:
        if cond():
            return True
        try:
            widget.update()
        except Exception:
            break
        time.sleep(0.01)
    return bool(cond())


def _settled(widget, *wins, ms=3000):
    """等这些窗口的动效都跑完 —— 位置断言前必须等,不然读到的是滑动中间态。"""
    return _pump_until(widget, lambda: all(
        getattr(w, "_anim_after", None) is None for w in wins), ms)


@contextlib.contextmanager
def fast_sleep():
    """把 time.sleep 变成空操作(仅用于被测函数里写死的等待,别处勿用)。"""
    real = time.sleep
    time.sleep = lambda s: None
    try:
        yield
    finally:
        time.sleep = real


def _oblock(text):
    """选项解析用的识别块(blk 的轻量版,位置无关)。"""
    return {"text": text, "x1": 0, "y1": 0, "x2": 30 * len(text), "y2": 30,
            "conf": 0.95}


# --------------------------------------------- 3.5 选项采集(答完回写仓库)

@section
def sec_options():
    with Sandbox() as sb:
        check("标记切分:半角 A. B. C. D.",
              split_options_markers("题干 A.针 B.望 C.闻 D.切")
              == ["针", "望", "闻", "切"])
        check("标记切分:全角 Ａ、Ｂ、 + 题干尾巴",
              split_options_markers("题干不包含…Ａ、针 Ｂ、望 Ｃ、闻 Ｄ、切")
              == ["针", "望", "闻", "切"])
        check("标记切分:①②③④",
              split_options_markers("①针 ②望 ③闻 ④切")
              == ["针", "望", "闻", "切"])
        check("标记切分:不足 3 段返回空",
              split_options_markers("A.针 B.望") == []
              and split_options_markers("没有任何标记的一段话") == [])

        # 按行聚类:答案行 + 前后凑 4 条;题干块(qwin)整段排除
        rblocks = [_oblock("下列哪个字具有八种基本笔画"),
                   _oblock("永"), _oblock("俗"), _oblock("松"), _oblock("宝")]
        opts, raw = parse_options(rblocks, {0}, "永")
        check("按行聚类:答案在首条时也凑满 4 条",
              opts == ["永", "俗", "松", "宝"], f"opts={opts}")
        check("按行聚类:raw 是题干外全文",
              "永" in raw and "八种基本笔画" not in raw)

        # 混进噪声行:答案对上才算数;噪声在题干块里就被 qwin 排除
        rblocks2 = [_oblock("宠环任务提示文字一大堆"),
                    _oblock("永"), _oblock("俗"), _oblock("松"), _oblock("宝")]
        opts2, _ = parse_options(rblocks2, {0}, "永")
        check("按行聚类:夹噪声也能切出 4 条", opts2 == ["永", "俗", "松", "宝"])

        # 答案对不上任何块:宁缺勿错
        opts3, raw3 = parse_options([_oblock("聊天内容一堆"), _oblock("没答案")],
                                    {0}, "永")
        check("答案不在场:不存列表也不存兜底", opts3 is None and raw3 == "")

        # 标记切分优先,且答案不在标记段里时不收
        opts4, _ = parse_options([_oblock("题干"),
                                  _oblock("A.甲 B.乙 C.丙 D.丁")], {0}, "甲")
        check("标记优先:答案在标记段里", opts4 == ["甲", "乙", "丙", "丁"])
        opts5, _ = parse_options([_oblock("题干"),
                                  _oblock("A.甲 B.乙 C.丙 D.丁")], {0}, "戊")
        check("标记切出但答案不在:不收", opts5 is None)

        # 仓库:新条目 -> 落盘 -> 重载一致
        p1 = os.path.join(sb.dir, "opt1.json")
        store = OptionsStore(p1)
        check("空库起步:无文件不炸", store.entries == {})
        store.learn("甲题", "甲题目?", "答案一",
                    options=["答案一", "乙", "丙", "丁"], mode="三大学士")
        store.save()
        e = OptionsStore(p1).entries.get("甲题")
        check("落盘重载一致",
              e and e["options"] == ["答案一", "乙", "丙", "丁"]
              and e["count"] == 1 and e["mode"] == "三大学士" and e["seen"],
              f"e={e}")

        # 重复见:count 累加;已有 options 不被 text 兜底覆盖;答案跟题库走
        store2 = OptionsStore(p1)
        store2.learn("甲题", "甲题目?", "答案一改", text="聊天噪声 答案一改")
        store2.save()
        e3 = OptionsStore(p1).entries["甲题"]
        check("重复回写:count+1、options 不被覆盖、答案跟题库",
              e3["count"] == 2 and e3["options"] == ["答案一", "乙", "丙", "丁"]
              and e3["answer"] == "答案一改" and "text" not in e3, f"e={e3}")

        # 纯文本兜底:没解析出列表时存 text;之后解析成功升级成 options
        store2.learn("乙题", "乙题目?", "答案二", text="选项原文一段 200 字内")
        store2.save()
        check("文本兜底:无列表时存 text",
              OptionsStore(p1).entries["乙题"].get("text")
              == "选项原文一段 200 字内")
        store2.learn("乙题", "乙题目?", "答案二",
                     options=["答案二", "己", "庚", "辛"])
        store2.save()
        check("升级:列表到手 text 撤下",
              OptionsStore(p1).entries["乙题"].get("options")
              == ["答案二", "己", "庚", "辛"]
              and "text" not in OptionsStore(p1).entries["乙题"])

        # curated(官方工具箱抓的)条目共存:没有 count 也能被 learn 升级
        json.dump({"_meta": {"source": "官方PC工具箱题库",
                             "official_bank_size": 409},
                   "entries": {"丙题": {"question": "丙题目?", "answer": "答案三",
                                        "options": ["答案三", "子", "丑", "寅"],
                                        "category": "古代常识",
                                        "official_first_is_answer": True}}},
                  open(p1, "w", encoding="utf-8"), ensure_ascii=False)
        store4 = OptionsStore(p1)
        check("curated 共存:原字段保留",
              store4.entries["丙题"]["official_first_is_answer"] is True
              and store4.meta.get("official_bank_size") == 409)
        store4.learn("丙题", "丙题目?", "答案三", mode="三大学士")
        store4.save()
        store5 = OptionsStore(p1)
        e5 = store5.entries["丙题"]
        # 上面的用例把文件覆写成只含丙题的 curated 库,learned 只数它一条
        check("curated 升级:count/seen/mode 加上,原字段不动",
              e5["count"] == 1 and e5["mode"] == "三大学士"
              and e5["category"] == "古代常识"
              and store5.meta.get("learned") == 1, f"e={e5} meta={store5.meta}")

        # 坏文件:挪成 .bad 留证,空库重开(不把攒的数据一错清零)
        with open(p1, "w", encoding="utf-8") as f:
            f.write("{坏掉的json")
        store6 = OptionsStore(p1)
        check("坏文件:.bad 留证 + 空库重开",
              store6.entries == {} and os.path.exists(p1 + ".bad"))

        # KejuTool._learn_options 接线:真实条目走一遍
        class _StubApp:
            cfg = {"keju": {"regions": []}}  # noqa: RUF012  一次性桩,没人改它

            @staticmethod
            def log(*a, **k):
                pass

        tool = tools_keju.KejuTool(_StubApp(), lambda: None, lambda: None)
        tool._learn_options(entry := {"q": "下列哪个字具有八种基本笔画",
                                      "a": "永",
                                      "n": normalize("下列哪个字具有八种基本笔画")},
                            rblocks, {0}, "三大学士")
        got = OptionsStore(tools_keju.OPTIONS_PATH).entries.get(entry["n"])
        check("KejuTool 接线:答题成功即回写", bool(got and got.get("options")),
              f"got={got}")

        # 端到端:mk_engine 的桩被换回真仓库后,完整 keju_cycle 也要攒
        ebank = make_bank(os.path.join(sb.dir, "cycle_bank.json"), [
            {"q": "下列哪个字具有八种基本笔画", "a": "永"}])
        eng, app = mk_engine(ebank, [blk("下列哪个字具有八种基本笔画", 100, 200),
                                     blk("永", 100, 240), blk("俗", 200, 240),
                                     blk("松", 300, 240), blk("宝", 400, 240)],
                             cfg={"keju": {"exam_mode": False}})
        eng.keju_tool.options = OptionsStore(
            os.path.join(sb.dir, "cycle_options.json"))
        eng.keju_tool.keju_cycle(True)
        got2 = OptionsStore(eng.keju_tool.options.path).entries
        hit2 = got2.get(normalize("下列哪个字具有八种基本笔画"))
        check("端到端:keju_cycle 答完也回写选项",
              bool(hit2 and hit2.get("options") == ["永", "俗", "松", "宝"]),
              f"got={hit2}")

        # 选项哨兵:严格命中规则
        check("严格命中:数值只认完全一致(200≠20 的前缀假命中)",
              not tools_keju._opt_hit_strict(normalize("200"), "20")
              and tools_keju._opt_hit_strict(normalize("20"), "20")
              and tools_keju._opt_hit_strict(normalize("200"), "200"))
        check("严格命中:文字允许包含关系(蒙古族 ⊂ 蒙古族,蒙古)",
              tools_keju._opt_hit_strict(normalize("蒙古族,蒙古"), "蒙古族")
              and tools_keju._opt_hit_strict(normalize("蒙古族"),
                                             "蒙古族,蒙古"))
        check("严格命中:2 字变体不互认(花妖≠蜃气妖)",
              not tools_keju._opt_hit_strict(normalize("花妖"), "蜃气妖"))
        check("严格命中:近似档接住 OCR 一字之差(窦娥冤→实娥冤)",
              tools_keju._opt_hit_strict(normalize("关汉卿的《窦娥冤》"),
                                         "关汉卿的《实娥冤》")
              and not tools_keju._opt_hit_strict(normalize("长沙马王堆"),
                                                 "海昏侯墓"))
        check("一字档:替换(秤砣→秤碗)/增删(金瓶掣签→金瓶签)都算同选项,"
              "两字差异(花妖/蜃气妖)不算",
              tools_keju._opt_hit_strict(normalize("秤砣"), "秤碗")
              and tools_keju._opt_hit_strict(normalize("金瓶掣签"), "金瓶签")
              and not tools_keju._opt_hit_strict(normalize("花妖"), "蜃气妖"))
        check("一字档:选项分隔长横线读成「一一」(巳──蛇→已一一蛇)算同选项",
              tools_keju._opt_hit_strict(normalize("巳——蛇"), "已一一蛇")
              and tools_keju._opt_hit_strict(normalize("一元复始"), "一元复始"))

        # 哨兵端到端:答案不在当屏选项 -> 压住不播报,但选项照常入库
        sbank = make_bank(os.path.join(sb.dir, "sentinel_bank.json"), [
            {"q": "仓库全部整理消耗多少体力", "a": "200"}])
        eng, app = mk_engine(sbank, [blk("御前科举大赛第1关：这一关考的是综合。"
                                         "题目：仓库全部整理消耗多少体力",
                                         100, 200),
                                     blk("20", 100, 240), blk("30", 200, 240),
                                     blk("0", 300, 240), blk("50", 400, 240)])
        eng.keju_tool.options = OptionsStore(
            os.path.join(sb.dir, "sentinel_options.json"))
        eng.keju_tool.keju_cycle(True)
        check("哨兵:答案不在当屏选项 -> 不播报",
              not app.said
              and any("未播报" in m for m in app.logs), app.logs[-1:])
        sent = OptionsStore(eng.keju_tool.options.path).entries.get(
            normalize("仓库全部整理消耗多少体力"))
        check("哨兵:压答的同时选项照常入库",
              bool(sent and (sent.get("options") or sent.get("text"))),
              f"got={sent}")

        # 哨兵放行:答案就在选项里(数值完全一致)-> 照常播报
        eng2, app2 = mk_engine(sbank, [blk("御前科举大赛第1关：这一关考的是综合。"
                                           "题目：仓库全部整理消耗多少体力",
                                           100, 200),
                                       blk("20", 100, 240), blk("200", 200, 240),
                                       blk("0", 300, 240), blk("50", 400, 240)])
        eng2.keju_tool.options = OptionsStore(
            os.path.join(sb.dir, "sentinel2_options.json"))
        eng2.keju_tool.keju_cycle(True)
        check("哨兵:答案在当屏选项里 -> 照常播报",
              app2.said == ["200"], f"said={app2.said}")

        # 哨兵的候选行按对话框几何过滤(2026-11-15 车迟斗法实测):真选项
        # 被 OCR 漏读的一拍,题干上方的 NPC 名牌与右侧任务追踪 HUD 的短行
        # 凑满 3 条,把题库里的正确答案「浙江」误压了。题干正下方同竖列
        # 才算选项,斜右/上方短行不算 —— 这屏该照常播报
        wbank = make_bank(os.path.join(sb.dir, "sentinel3_bank.json"), [
            {"q": "黄酒名品“加饭酒”的产地在哪", "a": "浙江"}])
        eng3, app3 = mk_engine(
            wbank, [blk("有个道士", 700, 120),
                    blk("1200）（通过长", 700, 180),
                    blk("黄酒名品“加饭酒”的产地在哪", 100, 200),
                    blk("和尚可前往道", 700, 210),
                    blk("余时间1小时5", 700, 230)],
            cfg={"keju": {"exam_mode": False, "mode": "车迟斗法"}})
        eng3.keju_tool.keju_cycle(True)
        check("哨兵:HUD/名牌短行不算选项(漏读真选项也不误压)",
              app3.said == ["浙江"] and not any("未播报" in m for m in app3.logs),
              f"said={app3.said} logs={app3.logs[-2:]}")

        # 压答前窄带复检(2026-11-15 实测):整带 OCR 把「长沙马王堆」那行
        # 漏了,答案不在读出的 3 条选项里 —— 复检读到就照常播报
        mbank = make_bank(os.path.join(sb.dir, "rescan_bank.json"), [
            {"q": "轻若烟雾薄如蝉翼的丝织品出土于哪座陵墓",
             "a": "长沙马王堆"}])
        mblocks = [blk("轻若烟雾薄如蝉翼的丝织品出土于哪座陵墓", 100, 200),
                   blk("洛庄汉王陵", 100, 240), blk("南越王陵", 100, 270),
                   blk("海昏侯墓", 100, 300)]
        eng4, app4 = mk_engine(mbank, [],
                               cfg={"keju": {"exam_mode": False}})
        calls = {"n": 0}

        def _ocr2(img, **kw):
            calls["n"] += 1
            return mblocks if calls["n"] == 1 else [blk("长沙马王堆", 100, 0)]

        eng4.ocr = types.SimpleNamespace(recognize_blocks=_ocr2)
        eng4.keju_tool.keju_cycle(True)
        check("哨兵:复检读到漏检的答案行 -> 照常播报",
              app4.said == ["长沙马王堆"]
              and any("选项复检" in m for m in app4.logs),
              f"said={app4.said} logs={app4.logs[-2:]}")

        # 复检也读不到(桩每拍都返回同一批块)-> 仍按原判压答
        eng5, app5 = mk_engine(
            sbank, [blk("御前科举大赛第1关：这一关考的是综合。"
                        "题目：仓库全部整理消耗多少体力", 100, 200),
                    blk("20", 100, 240), blk("30", 200, 240),
                    blk("0", 300, 240), blk("50", 400, 240)])
        eng5.keju_tool.keju_cycle(True)
        check("哨兵:复检仍读不到答案 -> 照旧压答",
              not app5.said and any("未播报" in m for m in app5.logs),
              f"said={app5.said} logs={app5.logs[-2:]}")

        # 无锚点模式的扫描区 = 画布 × DLG_RATIOS(2026-11-15 接隔壁降妖伏魔
        # 的定稿比例表):跟随每拍同步各槽位,识别按它截;没同步到的拍退回
        # 整窗比例带(PLAIN_BAND)。截屏区用记录桩验证
        scanbank = make_bank(os.path.join(sb.dir, "scan_bank.json"), [
            {"q": "轻若烟雾薄如蝉翼的丝织品出土于哪座陵墓", "a": "长沙马王堆"}])
        grabbed = []

        class _RecScreen:
            def grab(self, region=None):
                grabbed.append(tuple(region) if region else None)
                return np.zeros((30, 60, 3), np.uint8)

        eng6, app6 = mk_engine(scanbank, [blk("轻若烟雾薄如蝉翼的丝织品出土于哪座陵墓",
                                              100, 200)],
                               cfg={"keju": {"exam_mode": False}})
        eng6.screen = _RecScreen()
        eng6.keju_tool._follow_clients = lambda: None   # 不碰真机枚举
        eng6.keju_tool._auto_boxes = [[0, 0, 60, 30]]
        eng6.keju_tool._scan_rects = [[134, 294, 686, 474]]
        eng6.keju_tool.keju_cycle(True)
        check("扫描区:无锚点模式按画布×DLG_RATIOS 截",
              grabbed == [(134, 294, 686, 474)] and app6.said == ["长沙马王堆"],
              f"grabbed={grabbed[:2]} said={app6.said}")

        eng7, _app7 = mk_engine(scanbank, [blk("轻若烟雾薄如蝉翼的丝织品出土于哪座陵墓",
                                               100, 200)],
                                cfg={"keju": {"exam_mode": False}})
        eng7.screen = _RecScreen()
        eng7.keju_tool._follow_clients = lambda: None
        eng7.keju_tool._auto_boxes = [[0, 0, 60, 30]]
        eng7.keju_tool.keju_cycle(True)
        check("扫描区:画布框没同步到的拍退回整窗比例带",
              grabbed[-1] == (2, 12, 54, 21), f"got={grabbed[-1]}")

        eng8 = mk_engine(scanbank, [], cfg={"keju": {}})[0]
        with patch(client_canvas=lambda b, t=None: ((6, 6, 806, 606), (800, 600))):
            eng8.keju_tool._auto_boxes = [[0, 0, 60, 30]]
            eng8.keju_tool._sync_scan_rects(
                [("梦幻西游 ONLINE - 荆门好礼掌柜", [0, 0, 60, 30])])
        got = eng8.keju_tool._scan_rects
        check("扫描区:跟随同步 画布×DLG_RATIOS(800 档 0.16/0.48/0.85/0.78)",
              got[0] == [134, 294, 686, 474] and got[1:] == [None] * 4,
              f"rects={got}")


# ------------------------------------------------------- 3 题库更新(离线桩)

@section
def sec_update_bank():
    def fake_official(keju_n=600, qd_n=20):
        keju = ",".join('{Id:"%d",Q:"科举题%d？",A:"科举答案%d"}' % (i, i, i)
                        for i in range(keju_n))
        qd = ",".join('{Id:"%d",Q:"签到题%d？",A:"签到答案%d"}' % (i, i, i)
                      for i in range(qd_n))
        shared = '{Id:"S",Q:"科举题0？",A:"签到版答案0"}'
        exp = ('1:function(){return 1},'
               '8733:function(){var x=[%s]},'
               '6419:function(){var x=[%s,%s]},' % (keju, qd, shared))
        html = b'<script src="static/js/app.abc12345.js"></script>'
        app_js = b'306:"deadbeef"'

        def get(url, timeout=30):
            if "app." in url:
                return app_js
            if "exp." in url:
                return exp.encode("utf-8")
            return html
        return get

    with patch(_http_get=fake_official()):
        entries, meta = bank_mod.fetch_official_bank(["keju", "qiandao"])
        check("官方源:模块切分 + 跨活动去重(同名题保留科举版)",
              len(entries) == 620
              and meta["activities"] == {"科举": 600, "签到": 20}
              and all(e["normalized_question"] for e in entries),
              f"{len(entries)} 条 {meta['activities']}")
        only_keju, _ = bank_mod.fetch_official_bank(["keju"])
        check("官方源:按 bank_activities 过滤活动",
              len(only_keju) == 600, f"{len(only_keju)} 条")
        check("官方源:chunk 哈希写进 meta", meta["chunk_hash"] == "deadbeef")

    with patch(_http_get=fake_official(keju_n=10)):
        try:
            bank_mod.fetch_official_bank()
            too_few = False
        except ValueError:
            too_few = True
        check("官方源:题目过少时放弃(防污染)", too_few)

    with patch(_http_get=lambda *a, **k: "<html>没有 app.js</html>".encode("utf-8")):
        try:
            bank_mod.fetch_official_bank()
            no_app = False
        except ValueError:
            no_app = True
        check("官方源:页面结构变化抛错(交给备源)", no_app)

    def fake_gh(path, timeout=30):
        if path == "data/current.json":
            return json.dumps({"generation_id": "g1"}).encode()
        if path.endswith("metadata.json"):
            return json.dumps({"generation_id": "g1"}).encode()
        if path.endswith("keju_questions.json"):
            return json.dumps([{"question": f"镜像题{i}？", "answer": f"镜像答案{i}",
                                "normalized_question": normalize(f"镜像题{i}？")}
                               for i in range(600)]).encode()
        raise AssertionError(path)

    with patch(_gh_raw=fake_gh):
        ents, meta = bank_mod.fetch_github_bank()
        check("镜像备源:解析 + 来源标记",
              len(ents) == 600 and meta["source"] == "github", f"{len(ents)} 条")
    with patch(_gh_raw=lambda p, timeout=30: json.dumps({"generation_id": ""}).encode()):
        try:
            bank_mod.fetch_github_bank()
            bad_gen = False
        except ValueError:
            bad_gen = True
        check("镜像备源:缺版本号会抛错", bad_gen)

    real_cls = bank_mod.QuestionBank
    try:
        with Sandbox() as sb, patch(_http_get=fake_official()):
            bank_mod.QuestionBank = lambda path=None: real_cls(path or sb.bank)
            ok1, msg1 = bank_mod.update_bank()
            check("更新题库:首次写入(题库+meta 落盘)",
                  ok1 and os.path.exists(sb.bank) and os.path.exists(sb.meta)
                  and "已更新" in msg1, msg1)
            ok2, msg2 = bank_mod.update_bank()
            check("更新题库:内容没变时报'已是最新'(不重复下载写盘)",
                  (not ok2) and "最新" in msg2, msg2)
            with patch(_http_get=fake_official(keju_n=650)):
                ok3, msg3 = bank_mod.update_bank()
                check("更新题库:内容变化 -> 更新并备份旧题库",
                      ok3 and os.path.exists(sb.backup) and "670" in msg3, msg3)
            with patch(_http_get=raiser(OSError("官方源不通")), _gh_raw=fake_gh):
                ok4, msg4 = bank_mod.update_bank()
                check("更新题库:官方源失败自动回退镜像源",
                      ok4 and "镜像" in msg4, msg4)
            with patch(_http_get=raiser(OSError("官方源不通")),
                       _gh_raw=raiser(OSError("镜像也挂了"))):
                try:
                    bank_mod.update_bank()
                    both_fail = False
                except ValueError as e:
                    both_fail = "镜像源失败" in str(e)
                check("更新题库:两源都失败 -> 报错并保留原题库", both_fail)

            # 只加不减:手工补的题、官方已下架的题,更新后都不能丢
            merged, added, kept = bank_mod.merge_bank_entries(
                [{"question": "只在我本地的题", "answer": "本地答案"},
                 {"question": "科举题0？", "answer": "旧答案(官方后来改了)"},
                 {"question": "同题干", "answer": "答案甲"},
                 {"question": "同题干", "answer": "答案乙"}],
                [{"question": "科举题0？", "answer": "科举答案0"},
                 {"question": "新题", "answer": "新答案"}])
            qs = [(m.get("question"), m.get("answer")) for m in merged]
            check("只加不减:旧题一条不丢,新题追加进来",
                  ("只在我本地的题", "本地答案") in qs and ("新题", "新答案") in qs
                  and ("科举题0？", "旧答案(官方后来改了)") in qs
                  and ("科举题0？", "科举答案0") in qs
                  and qs.count(("同题干", "答案甲")) == 1
                  and qs.count(("同题干", "答案乙")) == 1
                  and added == 2 and kept == 4,
                  f"合并后 {len(merged)} 条,新增 {added},保留 {kept}")

            with patch(_http_get=fake_official()):
                with open(sb.bank, encoding="utf-8") as f:
                    cur = json.load(f)
                cur.append({"source_id": "x", "question": "我手补的一道题？",
                            "answer": "手补答案",
                            "normalized_question": normalize("我手补的一道题？")})
                with open(sb.bank, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False)
                ok5, msg5 = bank_mod.update_bank()
                with open(sb.bank, encoding="utf-8") as f:
                    after = json.load(f)
                kept_ok = any(x.get("question") == "我手补的一道题？"
                              for x in after)
                check("只加不减:更新后手工补的题还在",
                      kept_ok and "保留本地独有" in msg5, msg5)
    finally:
        bank_mod.QuestionBank = real_cls

    with tempfile.TemporaryDirectory() as td:
        bundle = os.path.join(td, "bundle")
        app_dir = os.path.join(td, "app")
        os.makedirs(os.path.join(bundle, "data"))
        os.makedirs(os.path.join(bundle, "voice_cache"))
        os.makedirs(app_dir)
        with open(os.path.join(bundle, "voice_cache", "a.mp3"), "wb") as f:
            f.write(b"x")
        for name in ("keju_questions.json", "keju_metadata.json"):
            with open(os.path.join(bundle, "data", name), "w",
                      encoding="utf-8") as f:
                f.write('{"builtin": true}')
        with patch(BASE_DIR=app_dir, _BUNDLE_DIR=bundle):
            core.ensure_data_files()
            copied = os.path.exists(os.path.join(app_dir, "data",
                                                 "keju_questions.json"))
            mirrored = os.path.exists(os.path.join(app_dir, "voice_cache",
                                                   "a.mp3"))
            with open(os.path.join(bundle, "data", "keju_questions.json"),
                      "w", encoding="utf-8") as f:
                f.write('{"builtin": "新内容"}')
            core.ensure_data_files()
            with open(os.path.join(app_dir, "data", "keju_questions.json"),
                      encoding="utf-8") as f:
                kept = json.load(f) == {"builtin": True}
            # 镜像只做一次:删掉的文件不能被"复活"(否则 prune 白干)
            os.remove(os.path.join(app_dir, "voice_cache", "a.mp3"))
            core.ensure_data_files()
            not_resurrected = not os.path.exists(
                os.path.join(app_dir, "voice_cache", "a.mp3"))
        check("ensure_data_files:首次复制 + 已存在不覆盖", copied and kept)
        check("ensure_data_files:语音缓存也会镜像(只一次,删掉的不复活)",
              mirrored and not_resurrected)


# --------------------------------------------------------- 4 语音与音频缓存

@section
def sec_voice():
    with Sandbox() as sb:
        bank = make_bank(sb.bank, [{"q": "甲题？", "a": "甲答"},
                                   {"q": "乙题？", "a": "乙答"}])
        cfg = {"app": {"voice_hint": "zh-CN-YunxiNeural", "volume": 1.0,
                       "pitch": 15}}
        rate, vol, pitch = edge_params(cfg)

        def cpath(text):
            return _voice_cache_path(text, cfg["app"]["voice_hint"], rate, vol,
                                     pitch)

        valid = [cpath(a) for a in ("甲答", "乙答")]
        stale = cpath("语音测试,C铁观音,唐朝")   # 播报试听已删:旧缓存该被清掉
        for p in valid + [stale]:
            with open(p, "wb") as f:
                f.write(b"x")
        junk = os.path.join(sb.voice, "junk.mp3")
        with open(junk, "wb") as f:
            f.write(b"x")
        with open(os.path.join(sb.voice, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("不是音频")
        removed = core.prune_voice_cache(cfg, bank)
        check("prune_voice_cache:只删无关 mp3,保留答案(旧试听缓存也会清掉)",
              removed == 2 and not os.path.exists(junk)
              and not os.path.exists(stale)
              and all(os.path.exists(p) for p in valid)
              and os.path.exists(os.path.join(sb.voice, "notes.txt")),
              f"删了 {removed} 个")

        check("count_missing_voice:全齐报 0",
              core.count_missing_voice(cfg, bank) == 0)
        os.remove(cpath("甲答"))
        check("count_missing_voice:缺 1 条报 1",
              core.count_missing_voice(cfg, bank) == 1)

        state = {"fail_once": True}

        def fake_synth(text, voice, r, v, out, timeout=15, retries=2, pitch=0):
            if text == "甲答" and state["fail_once"]:
                state["fail_once"] = False
                return False
            with open(out, "wb") as f:
                f.write(b"x")
            return True

        with patch(_synth_edge_file=fake_synth, load_config=lambda: cfg,
                   QuestionBank=lambda path=None: bank):
            d1, f1, s1 = core.build_voice_cache(log=lambda m: None)
            check("build_voice_cache:合成失败会计数(不抛异常)",
                  d1 == 0 and f1 == 1 and s1 == 1, f"{d1}/{f1}/{s1}")
            d2, f2, s2 = core.build_voice_cache(log=lambda m: None)
            check("build_voice_cache:重跑续传(在的跳过、缺的补上)",
                  d2 == 1 and f2 == 0 and s2 == 1, f"{d2}/{f2}/{s2}")
            d3, f3, s3 = core.build_voice_cache(log=lambda m: None)
            check("build_voice_cache:全齐时不再请求合成",
                  d3 == 0 and f3 == 0 and s3 == 2, f"{d3}/{f3}/{s3}")

    # ---- 打包版(frozen):进程内合成,不起子进程 ----
    class _FakeComm:
        def __init__(self, text, voice, rate=None, volume=None, pitch=None):
            pass

        async def save(self, path):
            with open(path, "wb") as f:
                f.write(b"x")

    class _HangComm(_FakeComm):
        async def save(self, path):
            import asyncio
            await asyncio.sleep(99)          # 模拟卡死:必须靠超时放弃

    _saved_edge = sys.modules.get("edge_tts")
    _saved_frozen = getattr(sys, "frozen", None)

    def _use_edge(comm_cls):
        sys.modules["edge_tts"] = types.SimpleNamespace(Communicate=comm_cls)
        sys.frozen = True

    def _restore_edge():
        if _saved_edge is not None:
            sys.modules["edge_tts"] = _saved_edge
        else:
            sys.modules.pop("edge_tts", None)
        if _saved_frozen is None:
            if hasattr(sys, "frozen"):
                del sys.frozen
        else:
            sys.frozen = _saved_frozen

    _use_edge(_FakeComm)
    try:
        with Sandbox() as sb:
            out = os.path.join(sb.dir, "inline.mp3")
            ok = core._synth_edge_file("文本", "v", 0, 0, out,
                                       timeout=5, retries=0)
            check("_synth_edge_file:打包版不起子进程,进程内合成落盘",
                  ok and os.path.getsize(out) > 0)
    finally:
        _restore_edge()

    _use_edge(_HangComm)
    try:
        out2 = os.path.join(tempfile.mkdtemp(prefix="keju_synth_"), "x.mp3")
        t0 = time.time()
        ok2 = core._synth_edge_file("文本", "v", 0, 0, out2,
                                    timeout=2, retries=0)
        el = time.time() - t0
        check("_synth_edge_file:打包版合成卡死按超时放弃(不拖住调用线程)",
              ok2 is False and el < 3, f"{el:.1f}s")
    finally:
        _restore_edge()

    sp = core.Speaker(core.split_config(core.DEFAULT_CONFIG), lambda m: None)
    sp.start()
    ready = sp._ready.wait(10)
    check("Speaker:引擎就绪并选中中文音色",
          ready and not sp.dead and bool(sp.voice_name), sp.voice_name or "未就绪")
    sp.quit()
    time.sleep(0.6)
    check("Speaker:quit 后线程退出", not sp.is_alive())

    sp2 = core.Speaker({"app": {"voice_hint": "不存在的音色xyz", "volume": 1.0,
                                "pitch": 15}}, lambda m: None)
    sp2.start()
    ok2 = sp2._ready.wait(10)
    sp2.quit()
    check("Speaker:音色关键字找不到时回退到可用语音",
          ok2 and not sp2.dead and bool(sp2.voice_name), sp2.voice_name)

    sp3 = core.Speaker(core.split_config(core.DEFAULT_CONFIG), lambda m: None)
    sp3.dead = True                     # 模拟 SAPI 初始化失败
    t0 = time.time()
    sp3.say("铁观音")
    el = time.time() - t0
    check("Speaker:引擎不可用时 say() 立即返回(不拖住识别线程)",
          el < 0.05, f"{el * 1000:.0f}ms")

    with Sandbox():
        played = []
        spc = core.Speaker({"app": {"voice_hint": "zh-CN-YunxiNeural",
                                    "volume": 1.0, "pitch": 15,
                                    "tts_backend": "edge"}}, lambda m: None)
        rate, vol, pitch = edge_params(spc.cfg)
        cp = _voice_cache_path("甲答", "zh-CN-YunxiNeural", rate, vol, pitch)
        with open(cp, "wb") as f:
            f.write(b"x")
        with patch(_play_mp3_blocking=played.append,
                   _synth_edge_file=raiser(AssertionError("命中缓存不该联网"))):
            ok = spc._speak_edge("甲答")
            check("_speak_edge:命中本地缓存 -> 零网络直接播",
                  ok and played == [cp] and spc._cache_hit is True)
            ok2 = spc._speak_edge("没有缓存的句子")
            check("_speak_edge:合成异常/失败 -> 返回 False(交给系统语音)",
                  ok2 is False and spc._cache_hit is False)


# ------------------------------------------------------------- 5 识别管线

class FakeScreen:
    """假屏幕:按顺序返回预设画面,用完后重复最后一张。"""

    def __init__(self, crops=None):
        self.crops = list(crops) if crops else [np.zeros((30, 60, 3), np.uint8)]
        self.n = 0

    def grab(self, region=None):
        i = min(self.n, len(self.crops) - 1)
        self.n += 1
        return self.crops[i]


def mk_engine(bank, blocks, cfg=None, crops=None):
    """搭一个假屏幕/假 OCR/假扬声器的 Engine,返回 (engine, app)。

    cfg 传分区结构(和 load_config() 一样):{"app": {...}, "keju": {...}},
    只填要覆盖的键,缺的走这里的默认值(已定位 1 个客户端 + 御前科举模式)。
    """
    eng = core.Engine.__new__(core.Engine)
    app = types.SimpleNamespace()
    eng.app = app
    app.cfg = core.split_config({"regions": [[0, 0, 60, 30]],
                                 "exam_mode": True})
    for name, part in (cfg or {}).items():
        app.cfg.setdefault(name, {}).update(part)
    app.bank = bank
    app.said = []
    app.last_answer = ""
    # 界面滚动框没了,日志只有落文件的一份;两个名字都留着,旧用例不折腾
    app.logs = app.logs_all = []

    app.speaker = types.SimpleNamespace(say=app.said.append)
    app.log = lambda msg, detail="": app.logs.append(msg)
    eng.screen = FakeScreen(crops)
    eng.ocr = types.SimpleNamespace(recognize_blocks=lambda img, **kw: blocks)
    eng.keju_tool = tools_keju.KejuTool(app, lambda: eng.screen,
                                        lambda: eng.ocr)
    # 不带"自动定位"的记忆:_follow_clients 就不会去枚举真机窗口
    # (真开着游戏时会把测试的假区域"跟随"到真客户端上,用例全歪);
    # 空手等待旗一并按死 —— 要测"空手自动收编"的用例自己置回 True
    eng.keju_tool._auto_boxes = None
    eng.keju_tool._readopt = False
    # 选项仓库:sec_engine 没套 Sandbox,不桩会把夹具选项写进真实
    # data/keju_options.json(2026-11-12 实测污染过);要测"答题攒选项"
    # 的用例自己换回真 OptionsStore(临时路径),sec_options 有先例
    eng.keju_tool.options = types.SimpleNamespace(
        learn=lambda *a, **k: False, save=lambda: None)
    return eng, app


@section
def sec_pipeline():
    tmp = tempfile.mkdtemp(prefix="keju_bank_")
    bank = make_bank(os.path.join(tmp, "bank.json"), [
        {"q": "第一道题的题干内容是什么", "a": "答案甲"},
        {"q": "今天天气晴朗适合出门散步吗", "a": "答案乙"},
        {"q": "下列哪种茶叶不属于绿茶类", "a": "答案丙"},
        {"q": "下列哪种茶叶不属于绿茶", "a": "答案丁"},
    ])
    QA = "第一道题的题干内容是什么"
    QB = "今天天气晴朗适合出门散步吗"
    QC = "下列哪种茶叶不属于绿茶类"
    QD = "下列哪种茶叶不属于绿茶"
    AN = normalize(QA)

    n, entry, score, note, win = resolve_questions(bank, [blk(QA, 100, 200)], 62)
    check("滑窗:单题命中并给出窗口下标",
          n == 1 and entry["a"] == "答案甲" and win == [0], f"{score:.0f}% win={win}")

    n, entry, _, _, _ = resolve_questions(
        bank, [blk(QA, 100, 200), blk(QB, 100, 600)], 62)
    check("滑窗:两处不同题 -> 判为两道题(要求屏幕只留一道)", n == 2)

    n, entry, _, note, _ = resolve_questions(
        bank, [blk(QA, 100, 200), blk(QB, 100, 240)], 62)
    check("滑窗:纵向位置相近的命中聚成一道题(匹配到邻居题)",
          n == 1 and entry is not None and "近重复题干" in note, note.strip())

    n, entry, _, note, _ = resolve_questions(
        bank, [blk(QC, 100, 200), blk(QD, 100, 700)], 62)
    check("滑窗:近重复题干算一道题(不误报多题)",
          n == 1 and entry["a"] == "答案丙" and "近重复题干" in note,
          f"{entry['a']} {note.strip()}")

    # 窄栏排版:最后一行"类"被单独切出去,于是两个窗口分别命中 C / D 两条近重复题干
    n, entry, _, note, _ = resolve_questions(
        bank, [blk(QD, 100, 200), blk("类", 100, 232),
               blk("A 答案丁 B 无关文字", 100, 600)], 62)
    check("滑窗:近重复题干 + 屏幕上有选项 -> 按选项消歧",
          n == 1 and entry["a"] == "答案丁" and "按选项消歧" in note,
          f"{entry['a']} {note.strip()}")

    n, entry, _, note, _ = resolve_questions(
        bank, [blk(QA, 100, 200, h=60), blk(QB, 100, 700, h=28)], 62)
    check("滑窗:多题时取字号最大的一道(小字多为聊天残留)",
          n == 1 and entry["a"] == "答案甲" and "字号最大" in note, note.strip())

    n, entry, _, note, _ = resolve_questions(
        bank, [blk(QA, 100, 200), blk(QB, 100, 600)], 62, recent=[AN])
    check("滑窗:回声抑制(刚答过的题不再当作新题)",
          n == 1 and entry["a"] == "答案乙" and "回声" in note, note.strip())

    n, _, _, _, _ = resolve_questions(
        bank, [blk(QA, 100, 200), blk(QB, 100, 600)], 62, recent=[AN, normalize(QB)])
    check("滑窗:两道题都是回声时保持多题(交给字号/手动判断)", n == 2)

    n, entry, score, note, _ = resolve_questions(
        bank, [blk("第一道题的题干内容是啥", 100, 200)], 100)
    check("滑窗:没到阈值也记录最高候选(便于日志定位)",
          n == 0 and entry is None and score > 0 and "最高候选" in note,
          f"{score:.0f}% {note}")

    n, _, score, note, _ = resolve_questions(
        bank, [blk("zxcvbnmlkjhgfd", 100, 200)], 100)
    check("滑窗:连候选都没有时保持干净(无最高候选)",
          n == 0 and score == 0.0 and note == "")

    hdr = "御前科举大赛第1关：这一关考的是综合。题目："
    check("剥头:带关卡的科举头被整段去掉",
          strip_exam_header(hdr + QA) == QA
          and strip_exam_header("没有头的普通题目？") == "没有头的普通题目？")
    bare, w = tools_keju.extract_anchored_question([blk(hdr + QA, 100, 200)])
    check("锚点:剥头取裸题干", bare == QA and w == [0], bare[:12])
    check("锚点:没有锚点的画面返回空",
          tools_keju.extract_anchored_question([blk(QA, 100, 200)]) == ("", []))
    bare2, w2 = tools_keju.extract_anchored_question(
        [blk(hdr + QA, 100, 200, h=30), blk("下一行接着写的内容", 100, 232, h=30)])
    check("锚点:紧贴的下一行(行距≤30)并进题干", w2 == [0, 1],
          f"win={w2}")
    bare3, w3 = tools_keju.extract_anchored_question(
        [blk(hdr + QA, 100, 200, h=30), blk("A 选项甲 B 选项乙", 100, 400, h=34)])
    check("锚点:行距大的选项行不被并进题干(留给选项消歧)", w3 == [0],
          f"win={w3}")

    # ---- 锚点容忍 OCR 错字(A 触发放宽 + B 剥离放宽)----
    # 实测:“御前科举大赛第16关”会被读成“前科举大赛第16关”“何以前科举大赛第15关”
    for garbled in ("前科举大赛第16关：这一关考的是综合。题目：",
                    "何以前科举大赛第15关：这一关考的是综合。题目：",
                    "御前科举第16关：这一关考的是综合。题目：",
                    "这里是御前科举大赛第9关：这一关考的是综合。题目："):
        check(f"锚点:读错“御前/大赛”也能剥出裸题干({garbled[:8]}…)",
              strip_exam_header(garbled + QA) == QA,
              strip_exam_header(garbled + QA)[:14])
        bare_g, w_g = tools_keju.extract_anchored_question([blk(garbled + QA, 100, 200)])
        check(f"端到端:带错字的头仍走锚点路径({garbled[:8]}…)",
              bare_g == QA and w_g == [0], bare_g[:14])

    # 数字被读丢 / 整行被聊天叠字污染,仍要认得这是准考证头
    check("锚点:关卡数字被读丢(“科举大赛第关”)仍命中",
          tools_keju.is_exam_anchor("这里是御前科举大赛第关，你作好答题的准备了吗"))
    check("锚点:被聊天叠字污染的头仍命中",
          tools_keju.is_exam_anchor("6号考官前科举大赛第16关，你作好答题的准备了吗？"))
    garbled_prompt = "6号考官前科举大赛第16关，你作好答题的准备了吗？"
    check("锚点:读丢“御前”的考官开场白照样静默跳过",
          tools_keju.is_exam_prompt(strip_exam_header(garbled_prompt)))

    # 殿试答错后换题:提示语把"科举大赛殿试部分第N关…题目:"整行换掉,
    # 没有这句头垫底,整屏题都会被当"不是科举界面"静默跳过(2026-11-07 实测)
    AGAIN = "你的答案不正确，答题时间额外增加了10秒，请答另一题："
    check("锚点:殿试答错的换题提示也认(原关卡头被整行替换)",
          tools_keju.is_exam_anchor(AGAIN + QA)
          and strip_exam_header(AGAIN + QA) == QA,
          strip_exam_header(AGAIN + QA)[:14])
    bare_a, w_a = tools_keju.extract_anchored_question(
        [blk(AGAIN, 100, 200), blk(QA, 100, 232)])
    check("锚点:换题提示单独一行时并进题干后剥掉",
          bare_a == QA and w_a == [0, 1], bare_a[:14])
    check("锚点:换题长句被 OCR 拆成两行(尾行只剩“另一题:…”)也认得",
          tools_keju.is_exam_anchor("另一题：" + QA)
          and strip_exam_header("另一题：" + QA) == QA)

    # 不该触发:只有“科举”两字的地方(任务追踪栏/找考官提示)不能当锚点
    for plain in ("任务追踪 科举 你完成了会试", "科举找下一关", "科举当前答题进度",
                  "你完成了会试"):
        check(f"锚点:不误触发({plain[:8]}…)",
              not tools_keju.is_exam_anchor(plain)
              and tools_keju.extract_anchored_question([blk(plain, 100, 200)]) == ("", []))

    # 活动公告含“御前科举大赛”但不是题目:可以拿来试匹配,但绝不能到阈值
    chat = blk("每到御前科举大赛，各族才子齐聚长安城一试身手", 100, 200)
    hits_chat, best_chat = tools_keju.find_questions(bank, [chat], 80, use_anchor=True)
    check("锚点:公告/聊天不能配出答案(低于阈值不播报)",
          not hits_chat and (best_chat is None or best_chat[0] < 80),
          f"{best_chat[0]:.0f}%" if best_chat else "无候选")

    hits, best_any = tools_keju.find_questions(bank, [blk(hdr + QA, 100, 200)], 62,
                                        use_anchor=True)
    check("find_questions:锚点路径命中(win 就是锚点窗口)",
          len(hits) == 1 and next(iter(hits.values()))[3] == [0])
    hits2, _ = tools_keju.find_questions(bank, [blk(QA, 100, 200)], 62, use_anchor=True)
    check("find_questions:没有锚点时滑窗仍能命中", len(hits2) == 1)
    hits3, best3 = tools_keju.find_questions(bank, [blk("完全无关的一段文字内容", 100, 200)],
                                      62, use_anchor=False)
    check("find_questions:毫无关系 -> 没有命中也没有最高候选",
          not hits3 and best3 is None, f"best={best3}")
    hits4, best4 = tools_keju.find_questions(bank, [blk("第一道题的题干内容是啥", 100, 200)],
                                      100, use_anchor=False)
    check("find_questions:相似但没到阈值 -> 命中为空但留下最高候选",
          not hits4 and best4 is not None and best4[0] > 0,
          f"{best4[0]:.0f}% {best4[2][:10] if best4 else ''}")

    if not FAST:
        # ---- 滑窗上限回归:块数极多时屏幕下方的大窗也不能被截断 ----
        ocr = Ocr()
        real_bank = QuestionBank()
        narrow_q = next(e for e in real_bank.entries if len(e["q"]) >= 18)
        nimg = cli._render_narrow(narrow_q["q"])
        qblocks = ocr.recognize_blocks(np.asarray(nimg)[:, :, ::-1].copy())

        def decoys(n):
            ds = [blk(f"[帮派] 玩家{i} 说:组队刷任务{i}", 40, 20 * i + 10, h=18)
                  for i in range(n)]
            tail = [dict(b, y1=b["y1"] + 20 * n + 200, y2=b["y2"] + 20 * n + 200)
                    for b in qblocks]
            return ds + tail

        found = {}
        for k in (0, 304):
            hits, _ = tools_keju.find_questions(real_bank, decoys(k), 65, use_anchor=False)
            found[k] = any(e["a"] == "玩家守则" for _, e, _, _ in hits.values())
        check("滑窗上限:304 块干扰也找得到屏幕下方的窄栏题(600→3000 回归)",
              found[0] is True and found[304] is True,
              f"0 块干扰={'找到' if found[0] else '没找到'} / "
              f"304 块={'找到' if found[304] else '没找到'}")

    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- 6 识别引擎

@section
def sec_engine():
    tmp = tempfile.mkdtemp(prefix="keju_bank_")
    bank = make_bank(os.path.join(tmp, "bank.json"), [
        {"q": "第一道题的题干内容是什么", "a": "答案甲"},
        {"q": "今天天气晴朗适合出门散步吗", "a": "答案乙"},
    ])
    QA = "第一道题的题干内容是什么"
    QB = "今天天气晴朗适合出门散步吗"
    AN = normalize(QA)
    HDR = "御前科举大赛第1关：这一关考的是综合。题目："
    KEY = (0, 0, 60, 30)
    NO_REGIONS = {"keju": {"regions": [None] * core.MAX_CLIENTS}}

    # 日志标签:自动轮询的日志要按"当时到底在不在监听"贴标签
    eng, app = mk_engine(bank, [blk(QA, 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.watching = False
    eng.keju_tool.keju_cycle(False)
    check("日志标签:监听关着时的自动轮询标 [检测](不冒充自动监听)",
          app.logs and all(m.startswith("[检测]") for m in app.logs),
          app.logs[:2])
    eng2, app2 = mk_engine(bank, [blk(QA, 100, 200)],
                           cfg={"keju": {"exam_mode": False}})
    eng2.keju_tool.watching = True
    eng2.keju_tool.keju_cycle(False)
    check("日志标签:只有监听中才标 [自动监听]",
          app2.logs and all(m.startswith("[自动监听]") for m in app2.logs),
          app2.logs[:2])
    eng3, app3t = mk_engine(bank, [blk(QA, 100, 200)])
    eng3.keju_tool.watching = False
    eng3.keju_tool.keju_cycle(True)
    check("日志标签:手动识别标 [手动识别](即使没在监听)",
          app3t.logs and all(m.startswith("[手动识别]") for m in app3t.logs),
          app3t.logs[:2])

    eng, app = mk_engine(bank, [blk(QA, 100, 200)])          # 没有锚点
    eng.keju_tool.keju_cycle(True)
    check("cycle:御前科举模式下没有锚点 -> 不播报(当前不是该界面)",
          not app.said and not any(m.startswith("识别") for m in app.logs))

    eng, app = mk_engine(bank, [blk(HDR + "短", 100, 200)])
    eng.keju_tool.keju_cycle(True)
    check("cycle:碎片文本(题干<4 字)-> 不播报", not app.said)

    # 短题干:题库里最短的真题只有 4 个字(实测漏答过的 "“佛家三宝”是指"),
    # 不能因为"太短"就当碎片丢掉
    short_bank = make_bank(
        os.path.join(tempfile.mkdtemp(prefix="keju_short_"), "s.json"),
        [{"q": "“佛家三宝”是指", "a": "佛、法、僧"},
         {"q": "“藕”是莲的", "a": "茎"}])
    eng, app = mk_engine(short_bank, [blk(HDR + "“佛家三宝”是指", 100, 200)])
    eng.keju_tool.keju_cycle(True)
    check("短题干:锚点模式下 6 字的真题照样播报(曾被 <8 字门槛挡掉)",
          app.said == ["佛、法、僧"], str(app.said))
    eng, app = mk_engine(short_bank, [blk("“藕”是莲的", 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(True)
    check("短题干:三大学士模式下 4 字的真题也播报",
          app.said == ["茎"], str(app.said))
    # 题干被折行时,单独一块可能就是整个短题干(5 字):窗口下限 6->4 才抓得到
    fold_bank = make_bank(
        os.path.join(tempfile.mkdtemp(prefix="keju_fold_"), "f.json"),
        [{"q": "“佛家三宝”是指", "a": "佛、法、僧"}])
    eng, app = mk_engine(fold_bank,
                         [blk(HDR + "“佛", 100, 200),
                          blk("家三宝”是指", 100, 224)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(True)
    check("短题干+折行:滑窗靠那 5 字的半行也能命中(窗口下限 6->4)",
          app.said == ["佛、法、僧"], str(app.said))

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng.keju_tool.keju_cycle(True)
    check("cycle:锚点命中 -> 播报 + 记忆",
          app.said == ["答案甲"] and app.last_answer == "答案甲"
          and AN in eng.keju_tool.recent_answered
          and eng.keju_tool._region_last[KEY] == AN,
          f"{app.said}")

    # 殿试答错换来的题:原关卡头被"请答另一题:"整行替换,照样要播
    again = "你的答案不正确，答题时间额外增加了10秒，请答另一题："
    eng, app = mk_engine(bank, [blk(again, 100, 200), blk(QA, 100, 232)])
    eng.keju_tool.keju_cycle(False)
    check("cycle:殿试答错后的换题照样播报(不再当非科举界面跳过)",
          app.said == ["答案甲"] and app.last_answer == "答案甲", str(app.said))

    calls = [0]

    def counting_ocr(img):
        calls[0] += 1
        return [blk(HDR + QA, 100, 200)]

    changed = np.zeros((30, 60, 3), np.uint8)
    changed[10:20, :] = 255                       # 画面确实变了
    eng.screen = FakeScreen([changed])
    eng.ocr = types.SimpleNamespace(recognize_blocks=counting_ocr)
    eng.keju_tool.keju_cycle(False)
    check("cycle:画面变了但题干相同(相似度≥88)-> 识别了却不重复播报",
          calls[0] == 1 and app.said == ["答案甲"], f"OCR {calls[0]} 次 {app.said}")

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng.keju_tool.recent_answered[AN] = time.time()
    eng.keju_tool.keju_cycle(False)
    check(f"cycle:自动轮询遇到 {tools_keju.REPEAT_GUARD_S} 秒内答过的题 -> 跳过不重播",
          not app.said and any("已播过" in m for m in app.logs),
          [m for m in app.logs if "已播过" in m][:1])
    eng.keju_tool.keju_cycle(True)
    check("cycle:手动识别同一题 -> 允许重听", app.said == ["答案甲"], f"{app.said}")

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng.keju_tool.recent_answered[AN] = time.time() - tools_keju.REPEAT_GUARD_S - 1
    eng.keju_tool.keju_cycle(False)
    check(f"cycle:超过 {tools_keju.REPEAT_GUARD_S} 秒后同一道题可以重播",
          app.said == ["答案甲"], f"{app.said}")

    eng, app = mk_engine(bank, [blk(QA, 100, 200)], cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(True)
    check("cycle:三大学士(无锚点)也能作答并标注模式",
          app.said == ["答案甲"] and any("三大学士" in m for m in app.logs),
          f"{app.said}")

    # 回归:同题干两个答案变体(官方源的花妖/蜃气妖)必须按当屏选项消歧。
    # 无锚点模式曾在 _parse_plain 里直接取最高分,把选项消歧整段跳过了。
    amb = make_bank(os.path.join(tempfile.mkdtemp(prefix="keju_amb_"), "b.json"),
                    [{"q": "下面召唤兽中参战等级与其它选项不同的是", "a": "花妖"},
                     {"q": "下面召唤兽中参战等级与其它选项不同的是", "a": "蜃气妖"}])
    blocks = [blk("下面召唤兽中参战等级与其它选项不同的是", 100, 360),
              blk("真陀护法", 100, 420), blk("蜃气妖", 100, 450),
              blk("持国巡守", 100, 480), blk("毗舍童子", 100, 510)]
    eng, app = mk_engine(amb, blocks, cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(True)
    check("cycle:无锚点模式同题干多答案变体 -> 按当屏选项消歧",
          app.said == ["蜃气妖"]
          and any("按选项消歧" in m for m in app.logs),
          f"{app.said} {[m for m in app.logs if '匹配' in m][:1]}")

    # 选项掉在对话带外(车迟斗法对话框更高):带内只有题干时再扫更宽的底部带
    calls = {"n": 0}

    def q_then_options(img, **kw):
        calls["n"] += 1
        return blocks[:1] if calls["n"] == 1 else blocks

    eng, app = mk_engine(amb, blocks, cfg={"keju": {"exam_mode": False}})
    eng.ocr = types.SimpleNamespace(recognize_blocks=q_then_options)
    eng.keju_tool.keju_cycle(True)
    check("cycle:歧义题选项不在对话带内 -> 兜底再扫宽底部带后消歧",
          app.said == ["蜃气妖"] and calls["n"] == 2,
          f"{app.said} OCR {calls['n']} 次")

    # 三大学士只扫底部对话框带:确认 cycle 截的就是按占比缩出来的带(非整窗)
    eng, app = mk_engine(bank, [blk(QA, 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    grabbed = []
    eng.screen.grab = lambda region=None: (
        grabbed.append(tuple(region) if region else region),
        np.zeros((30, 60, 3), np.uint8))[1]
    eng.keju_tool.keju_cycle(False)
    check("三大学士:cycle 只截底部对话框带(整窗按占比缩放,四周留余量)",
          grabbed and grabbed[-1] == tools_keju.plain_band(0, 0, 60, 30)
          and tools_keju.plain_band(100, 150, 800, 650) == (128, 360, 737, 515),
          f"截了 {grabbed[-1:]} / 带函数 "
          f"{tools_keju.plain_band(100, 150, 800, 650)}")
    eng2, _ = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    grabbed2 = []
    eng2.screen.grab = lambda region=None: (
        grabbed2.append(tuple(region) if region else region),
        np.zeros((30, 60, 3), np.uint8))[1]
    eng2.keju_tool.watching = True
    eng2.keju_tool.keju_cycle(False)
    check("御前科举:仍扫整个客户端窗口(不缩放)",
          grabbed2 and grabbed2[-1] == (0, 0, 60, 30), f"截了 {grabbed2[-1:]}")

    eng, app = mk_engine(bank, [], cfg=NO_REGIONS)
    eng.keju_tool.watching = True
    eng.keju_tool.keju_cycle(False)
    check("cycle:没定位到客户端 -> 给出提示并暂停监听(没有全屏兜底)",
          eng.keju_tool.watching is False
          and any("还没有定位到客户端" in m for m in app.logs)
          and any("已暂停" in m for m in app.logs),
          f"watching={eng.keju_tool.watching}")

    eng, app = mk_engine(bank, [], cfg=NO_REGIONS)
    eng.keju_tool.keju_cycle(True)
    check("cycle:手动识别没区域 -> 直接不扫(只提示)",
          any("还没有定位到客户端" in m for m in app.logs),
          f"{app.logs[:1]}")

    # 考官开场白:压根不是题目 -> 直接跳过(界面/文件都不刷"识别:"),不进匹配
    prompt = "这里是御前科举大赛第4关，你作好答题的准备了吗？开始答题我还没准备"
    eng, app = mk_engine(bank, [blk(prompt, 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(False)
    check("cycle:考官开场白自动轮询时不刷日志",
          not any(m.startswith("识别") for m in app.logs),
          f"{app.logs[:1]}")
    eng, app = mk_engine(bank, [blk(prompt, 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(True)
    check("cycle:手动识别考官开场白 -> 明说“这是开场对话,不是题目”",
          any("考官开场对话" in m for m in app.logs),
          [m for m in app.logs if "开场" in m][:1])

    check("is_exam_prompt:只认开场白,不误判真题目",
          tools_keju.is_exam_prompt(prompt) is True
          and tools_keju.is_exam_prompt(HDR + QA) is False
          and tools_keju.is_exam_prompt("曲径通幽处，禅房花木深。这句诗的作者是谁？") is False)

    # 回归:开场白里含“科举大赛”,曾被配到《科举大赛的第一阶段是》误报“乡试”
    trap = make_bank(os.path.join(tempfile.mkdtemp(prefix="keju_trap_"), "t.json"),
                     [{"q": "科举大赛的第一阶段是", "a": "乡试"}])
    for mode in (True, False):
        eng, app = mk_engine(trap, [blk(prompt, 100, 200)],
                             cfg={"keju": {"exam_mode": mode}})
        eng.keju_tool.keju_cycle(True)          # 连手动识别也不能报
        check(f"回归:考官开场白不会被误配成“乡试”({'锚点' if mode else '普通'}模式)",
              not app.said
              and any("考官开场对话" in m for m in app.logs), f"{app.said}")

    # 手动识别:读到文字但凑不出题干 / 没匹配上,都必须有可见结果
    eng, app = mk_engine(bank, [blk("短", 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(True)
    check("cycle:手动识别读到碎片文字 -> 界面给出原因",
          not app.said and any("凑不出题干" in m for m in app.logs),
          [m for m in app.logs if "题干" in m][:1])
    eng, app = mk_engine(bank, [blk("下列哪种说法完全不对？", 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.keju_cycle(True)
    check("cycle:手动识别没匹配上 -> 界面给出结果",
          not app.said and any("未找到匹配" in m for m in app.logs),
          [m for m in app.logs if "未找到匹配" in m][:1])
    eng, app = mk_engine(bank, [blk(QA, 100, 200)])       # 御前科举模式 + 无锚点
    eng.keju_tool.keju_cycle(True)
    check("cycle:手动识别没认出科举题目 -> 提示切三大学士模式(不提锚点)",
          any("没认出科举题目" in m and "三大学士" in m for m in app.logs)
          and not any("锚点" in m for m in app.logs),
          [m for m in app.logs if "没认出" in m][:1])

    # ---- 回归(2026-11-08 会试实测):关卡头整行被 OCR 读坏 ----
    # 锚点认不出、题干本体还在:本区域刚答过题(兜底窗口内)时按题干兜底播报
    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)],
                         crops=[np.zeros((30, 60, 3), np.uint8),
                                np.full((30, 60, 3), 60, np.uint8)])
    eng.keju_tool.keju_cycle(True)                       # 第一屏:正常锚点题
    check("锚点兜底:前置——第一屏正常答出", app.said == ["答案甲"], str(app.said))
    eng.ocr = types.SimpleNamespace(recognize_blocks=lambda img, **kw: [
        blk("梦梦幻西游0NLINE（湖北2区）", 100, 40), blk(QB, 100, 200)])
    eng.keju_tool.keju_cycle(True)                       # 第二屏:头读坏的新题
    check("锚点兜底:关卡头读坏 -> 按题干本体兜底播报(不再静默跳过)",
          app.said == ["答案甲", "答案乙"]
          and any("题干兜底" in m for m in app.logs),
          f"{app.said} {app.logs[-1:]}")
    with open(core.EVENTS_PATH, encoding="utf-8") as f:
        ev_all = [json.loads(l) for l in f if l.strip()]
    check("事件:兜底接住的题头异常记 parsemiss(recovered)",
          any(r.get("kind") == "parsemiss" and r.get("recovered")
              for r in ev_all), str(ev_all[-3:]))
    eng2, app2 = mk_engine(bank, [blk(QB, 100, 200)])    # 兜底窗口外(没在考试)
    eng2.keju_tool.keju_cycle(False)
    check("锚点兜底:窗口外不兜底,无锚点画面维持不播报",
          not app2.said and not any("兜底" in m for m in app2.logs),
          f"{app2.said} {app2.logs[:2]}")

    # 兜底也没接住(像题但题库里没有):记 parsemiss + 安排复检
    frag = "这道题到底在问什么呀"
    eng, app = mk_engine(bank, [blk(frag, 100, 200)])    # 画面始终没变
    eng.keju_tool._region_fallback_ts[KEY] = time.time() - 1   # 刚在考试
    eng.keju_tool.keju_cycle(False)
    check("复检:像题但锚点/题库都没接住 -> 记事件并安排复检",
          not app.said
          and eng.keju_tool._region_recheck.get(KEY) is not None
          and eng.keju_tool._region_retries.get(KEY) == 1
          and any("稍后自动重试" in m for m in app.logs),
          f"recheck={eng.keju_tool._region_recheck.get(KEY)} "
          f"{app.logs[-1:]}")
    with open(core.EVENTS_PATH, encoding="utf-8") as f:
        ev_all = [json.loads(l) for l in f if l.strip()]
    check("事件:没接住的像题读法记 parsemiss(recovered=0)",
          any(r.get("kind") == "parsemiss" and not r.get("recovered")
              and "在问什么" in (r.get("text") or "") for r in ev_all),
          str(ev_all[-3:]))
    # 复检到点:画面没变(帧门控会拦)也照样重读;这次读出真题就播
    eng.keju_tool._region_recheck[KEY] = time.time() - 0.01
    eng.ocr = types.SimpleNamespace(
        recognize_blocks=lambda img, **kw: [blk(HDR + QB, 100, 200)])
    eng.keju_tool.keju_cycle(False)
    check("复检:画面没变也照样重读,读出真题即播报并了结复检",
          app.said == ["答案乙"]
          and eng.keju_tool._region_recheck.get(KEY) is None
          and eng.keju_tool._region_retries.get(KEY) is None,
          f"{app.said}")
    # 复检次数用完就停(画面一直不变也读不出新东西,别烧 CPU);
    # 预置帧指纹让画面判"未变化",手动识别强制重读,预算才不会被清
    eng, app = mk_engine(bank, [blk(frag, 100, 200)])
    eng.keju_tool._region_fallback_ts[KEY] = time.time() - 1
    eng.keju_tool._region_retries[KEY] = tools_keju.RECHECK_MAX
    g = np.zeros((30, 60, 3), np.uint8).mean(axis=2).astype("int16")
    eng.keju_tool._region_frame[KEY] = g
    eng.keju_tool._region_ref[KEY] = g
    eng.keju_tool._region_ocr_ts[KEY] = time.time()
    eng.keju_tool.keju_cycle(True)
    check("复检:次数用完不再安排",
          eng.keju_tool._region_recheck.get(KEY) is None
          and eng.keju_tool._region_retries.get(KEY) == tools_keju.RECHECK_MAX)

    eng_u, _ = mk_engine(bank, [])
    st_u = eng_u.keju_tool.ui_state()
    check("ui_state:界面只认这个口,给各槽位角色名 + 暂停观测(红名)名单",
          set(st_u) == {"names", "unsup"} and st_u["names"] == []
          and st_u["unsup"] == [], str(st_u))


    eng, _ = mk_engine(bank, [])
    crop = np.full((40, 120, 3), 200, dtype=np.uint8)
    first = eng.keju_tool._frame_gate(KEY, crop)
    same = eng.keju_tool._frame_gate(KEY, crop)
    hard = crop.copy()
    hard[5:15, :] = 10
    chapass = eng.keju_tool._frame_gate(KEY, hard)
    check("帧门控:首帧放行 / 无变化拦截 / 实质变化立即放行",
          first and not same and chapass, f"{first}/{same}/{chapass}")
    check("帧门控:识别后清掉 dirty 标记", KEY not in eng.keju_tool._region_dirty)
    eng.keju_tool._region_ocr_ts[KEY] = time.time() - 20
    check("帧门控:画面不变但静置 15 秒以上也会复检一次",
          eng.keju_tool._frame_gate(KEY, hard) is True)
    taller = np.full((50, 120, 3), 200, dtype=np.uint8)   # 截图尺寸变了(换分辨率等)
    check("帧门控:截图尺寸变化按'内容变了'放行(回归:曾抛异常中断识别)",
          eng.keju_tool._frame_gate(KEY, taller) is True and KEY not in eng.keju_tool._region_dirty)

    eng, _ = mk_engine(bank, [])
    for i in range(205):
        eng.keju_tool._remember_answered({"n": f"题{i}", "a": "x"})
    check("_remember_answered:只记最近 200 条(防内存无限增长)",
          len(eng.keju_tool.recent_answered) == 200
          and "题0" not in eng.keju_tool.recent_answered and "题204" in eng.keju_tool.recent_answered,
          f"{len(eng.keju_tool.recent_answered)} 条")
    check("_recent_list:与记忆一致", len(eng.keju_tool._recent_list()) == 200)

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng.request_cycle()
    eng.set_watch(True)
    pending = eng.keju_tool._req_cycle
    ran = [eng.keju_tool._run_pending() for _ in range(3)]
    check("手动任务:cycle 投递给科举自己的线程(引擎不再有任务队列)",
          pending is True and ran == [True, False, False]
          and eng.keju_tool.watching is True
          and not hasattr(eng, "jobs") and eng.keju_tool._wake.is_set()
          and "[自动监听]已开启" in app.logs, f"{pending} {ran}")

    # 看门狗恢复开关:真实引擎走 set_watch(→ refresh_tools → activate);
    # 原来就关着的不能空打一条"[自动监听]已停止"(状态根本没变)
    logs_r = []

    def _fake_self(engine):
        return types.SimpleNamespace(
            engine=engine,
            log=lambda m, detail="", **kw: logs_r.append(m))

    app.logs.clear()
    eng.keju_tool.watching = False
    eng.keju_tool.enabled = False
    gui.App._restore_engine_switches(_fake_self(eng), {"watching": True})
    check("看门狗恢复:原来开着的自动监听会重新打开(走 set_watch→activate)",
          eng.keju_tool.watching is True and eng.keju_tool.enabled is True
          and "[自动监听]已开启" in app.logs, f"{app.logs[:2]}")
    logs_r.clear()
    eng2r, app2r = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng2r.keju_tool.watching = False
    gui.App._restore_engine_switches(_fake_self(eng2r), {"watching": False})
    check("看门狗恢复:原来关着的不动开关、也不空打[自动监听]已停止",
          eng2r.keju_tool.watching is False and eng2r.keju_tool.enabled is False
          and not any("已停止" in m or "已开启" in m for m in logs_r + app2r.logs),
          f"{logs_r} {app2r.logs}")

    class RunOcr:
        def __init__(self, blocks):
            self.blocks = blocks
            self.n = 0

        def warmup(self):
            pass

        def recognize_blocks(self, img):
            self.n += 1
            return self.blocks

    with Sandbox():
        app = types.SimpleNamespace()
        app.cfg = core.split_config(
            {"regions": [[0, 0, 60, 30]], "exam_mode": True,
             "interval": 0.4})
        app.bank = bank
        app.said = []
        app.last_answer = ""
        app.logs = []
        app.log = lambda msg, detail="": app.logs.append(msg)
        app.speaker = types.SimpleNamespace(say=app.said.append)
        run_eng = core.Engine(app)
        run_eng.ocr = RunOcr([blk(HDR + QA, 100, 200)])
        # 枚举桩给一个真在场的支持客户端(不然跟随会把"客户端已关闭"的
        # 观测区域移除,循环就没得扫了 —— 移除本身是新行为,见跟随组用例)
        run_win = ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色己[6])",
                   [0, 0, 640, 480])
        run_canv = [(run_win[0], (640, 480), [0, 0, 640, 480], 1.0, "win")]
        with patch(Screen=lambda: FakeScreen()), \
                patch(find_client_windows=lambda *a, **k: ([run_win], [])), \
                patch(find_client_canvases=lambda *a, **k: (list(run_canv), [])):
            run_eng.set_watch(True)
            run_eng.start()
            time.sleep(1.2)
            run_eng.set_watch(False)
            time.sleep(0.6)
        check("Engine.run:处理 watch 任务 + 自动监听循环能出答案",
              any("[自动监听]已开启" in m for m in app.logs)
              and app.said == ["答案甲"] and run_eng.ocr.n >= 1,
              f"OCR {run_eng.ocr.n} 次 播报 {app.said}")
        n_said = len(app.said)
        time.sleep(0.6)
        check("Engine.run:停止监听后不再有新的播报", len(app.said) == n_said)

    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- 7 OCR 与截图

@section
def sec_ocr_screen():
    ocr = Ocr()
    ocr.warmup()
    check("Ocr:识别设备探测到 GPU 或 CPU",
          ocr.device in ("GPU(DirectML)", "CPU", "CPU(已回退)"),
          ocr.device)

    img = np.asarray(cli._render_question("铁观音属于绿茶"))[:, :, ::-1].copy()
    blocks = ocr.recognize_blocks(img)
    shapes_ok = bool(blocks) and all(
        isinstance(b["x1"], int) and isinstance(b["y1"], int)
        and b["x1"] < b["x2"] and b["y1"] < b["y2"]
        and 0.0 <= b["conf"] <= 1.0 for b in blocks)
    check("Ocr:返回带坐标的文本块(整数坐标 + 置信度)",
          shapes_ok, f"{len(blocks)} 块")
    order = [(b["y1"] + b["y2"], b["x1"]) for b in blocks]
    check("Ocr:文本块按 (y, x) 排序", order == sorted(order))
    check("Ocr:recognize 把文本块拼成整串",
          "绿茶" in ocr.recognize(img), ocr.recognize(img)[:16])

    sc = core.Screen()
    mon = sc._sct.monitors[1]
    full = sc.grab()
    check("Screen:全屏截图 = 主显示器尺寸的 BGR 图",
          full.shape == (mon["height"], mon["width"], 3), f"{full.shape}")
    sub = sc.grab((10, 10, 110, 70))
    check("Screen:区域截图尺寸与区域一致", sub.shape == (60, 100, 3), f"{sub.shape}")

    if not FAST:      # 下面两项会真的重建一个 CPU 引擎(约 2 秒),快速模式跳过
        o2 = Ocr(lambda m: None)
        o2.device = "GPU(DirectML)"
        calls = [0]

        def fake_infer(img_bgr, limit_side=None):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("模拟 GPU 掉驱动")
            return [[[[0, 0], [50, 0], [50, 12], [0, 12]], "回退后的文本", 0.98]]

        o2._infer = fake_infer
        got = o2.recognize_blocks(np.zeros((20, 60, 3), np.uint8))
        check("Ocr:GPU 推理出错 -> 自动切 CPU 重试成功",
              calls[0] == 2 and bool(got) and got[0]["text"] == "回退后的文本"
              and o2.device.startswith("CPU"), f"device={o2.device}")

        with patch(load_config=lambda: core.split_config(
                dict(core.DEFAULT_CONFIG, use_gpu=False))):
            o3 = Ocr()
            o3.warmup()
            check("Ocr:配置里关掉 GPU 时直接走 CPU", o3.device == "CPU", o3.device)


# ------------------------------------------------------------- 8 界面(Tk)

@section
def sec_gui():
    # 真身先留住(下面的全局空枚举桩会盖掉模块属性),画布枚举回归用例用
    real_enum = client_common.find_client_canvases
    # 全局空枚举桩:界面组建真 App,引擎线程 watching 开着的用例不少,
    # 真机上游戏开着时会把真实客户端收进测试实例污染断言(2026-11-13
    # 实测:「荆门好礼掌柜」漏进胶囊断言)。具体用例自己 patch 覆盖这份
    # 空桩,退出回到空桩;真机枚举的两条用例走 tests 自己的导入,不受影响
    with Sandbox() as sb, patch(update_bank=lambda: (False, "题库已是最新")), \
            patch(find_client_windows=lambda: ([], []),
                  find_client_canvases=lambda: ([], [])):
        # App() 建题库时读的是被 patch 的 BANK_PATH,先往沙箱里放一份
        make_bank(sb.bank, [{"q": "第一道题的题干内容是什么", "a": "答案甲"}])
        old = time.time() - 86400 * 2
        with open(sb.log, "w", encoding="utf-8") as f:
            f.write("2020-01-01 00:00:00 很久以前的一条日志\n")
        os.utime(sb.log, (old, old))

        # 本应用启动即自动定位:这里固定成"找到 1 个客户端"(标题带角色名),
        # 窗口里摆一块 640×480 画布(登录角色才有画布才认,见分辨率那组用例)
        _boot_title = "梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])"
        with patch(find_client_windows=lambda: ([(_boot_title,
                                                  [100, 150, 800, 650])], [])), \
                patch(find_client_canvases=lambda: ([
                    (_boot_title, (640, 480), [110, 160, 750, 640], 1.0, "win")], [])):
            app = gui.App()
        app.root.withdraw()               # 测试时不弹窗
        real_engine = app.engine          # 真引擎先留着,收尾时关掉

        def caps_text(i, tag="name"):
            """第 i 个胶囊上某类文字(name);取不到返回空串。"""
            cv = app.client_capsules[i]
            ids = cv.find_withtag(tag)
            return cv.itemcget(ids[0], "text") if ids else ""

        check("界面:启动即自动定位,定位到几个就显示几个名字框",
              app.cfg["keju"]["regions"][0] == [100, 150, 800, 650]
              and len(app.cfg["keju"]["regions"]) == core.MAX_CLIENTS
              and caps_text(0) == "祭花′"
              and not app.client_capsules[1].grid_info()
              and app.engine.keju_tool.watching is True,   # 自动监听默认开
              f'{caps_text(0)}/可见 {len(app.client_capsules[1].grid_info())}')

        # 界面组不跑真识别:关掉监听 + 哑掉"定位后重新接管"(别碰真机屏幕)
        app.engine.keju_tool.watching = False
        app.engine.set_watch = lambda on: None

        log_rec = []                      # 自己留一份日志,避免和真实写文件互相干扰
        real_log = app.log
        app.log = lambda msg, detail="": (log_rec.append(msg),
                                          real_log(msg, detail))

        def ui_logs():                    # 已记录的界面日志文本
            return list(log_rec)

        time.sleep(0.5)                   # 等后台线程(题库检查)跑完
        check("界面:启动时把非当天日志轮转为 .old 并新建当天日志",
              os.path.exists(sb.log + ".old")
              and "[应用]启动" in open(sb.log, encoding="utf-8").read())

        # ---- 窗口摆位与动效:一份实现,所有窗口共用 ----
        check("界面:居中摆位(支持屏幕/父窗基准 + 小屏夹取)",
              ui_widgets.centered_xy((880, 680), (1920, 1080)) == (520, 186)
              and ui_widgets.centered_xy((880, 680), (800, 600)) == (0, 0)
              and ui_widgets.centered_xy((100, 100), (1920, 1080),
                                         (0, 0, 400, 300)) == (150, 100))
        import tkinter as tk
        fresh = tk.Toplevel(app.root)
        ui_widgets.pop_in(fresh, xy=(120, 120))
        check("动效:先设成全透明再显示(不会先闪一帧满不透明的窗)",
              float(fresh.attributes("-alpha")) < 0.5
              and fresh.state() == "normal",
              f"alpha={fresh.attributes('-alpha')} state={fresh.state()}")
        ui_widgets.stop_anim(fresh)
        fresh.destroy()
        try:
            raise RuntimeError("故意出错")
        except RuntimeError as e:
            app._on_tk_error(type(e), e, e.__traceback__)
        check("界面:回调里没接住的异常写进日志(默认只打到 stderr,用户看不到)",
              any("[界面]回调出错" in m for m in log_rec),
              log_rec[-1] if log_rec else "")

        app.log("[自动监听]这条该出现")
        app.log("[应用]公共的行也该出现")
        filetxt = open(sb.log, encoding="utf-8").read()
        check("日志:界面已无滚动框,两类行都只落日志文件",
              not hasattr(app, "log_box")
              and "该出现" in filetxt and "公共的行" in filetxt,
              "log_box 还在" if hasattr(app, "log_box") else "文件缺行")

        check("界面:默认控件(名字框 5 / 圆角钮齐 / 占位+状态灯 / 默认科举会试)",
              len(app.client_capsules) == core.MAX_CLIENTS
              and app.mode_btn.label() == "科举会试 ▾"
              and app.manual_btn.label() == "手动识别"
              and app.mini_btn.label() == "迷你置顶"
              and app.mute_btn.label() == "语音静音"
              and app.skin_btn.label() == "换肤"
              and app.root.title() == "mhxy_keju"
              and app.hero_canvas.find_withtag("ph")     # 没信息时显示占位
              and app.hero_canvas.find_withtag("mode_hint")  # 切换模式的引导常驻
              and not app._mode_menu_open
              and app.status_dot.itemcget("dot", "fill")
              == ui_widgets.StatusDot.RED
              and not any(hasattr(app, a) for a in
                          ("refresh_btn", "watch_btn", "interval_var",
                           "reward_var", "round_var", "earn_detail_btn",
                           "clear_log_btn", "hint_label", "scan_btn",
                           "autolocate_btn", "update_bank_btn", "tts_btn")))

        btns = (app.mode_btn, app.manual_btn, app.mini_btn, app.mute_btn)
        xs = [int(b.place_info()["x"]) for b in btns]
        gaps = [xs[i + 1] - xs[i] - ui_keju.BTN_W for i in range(len(xs) - 1)]
        check("界面:顶排 4 钮等宽等距平铺(110×36,间距一致,首尾贴齐右区)",
              all(b._bw == ui_keju.BTN_W and b._bh == ui_keju.BTN_H
                  for b in btns)
              and len(set(gaps)) == 1 and gaps[0] >= 8
              and xs[0] == ui_keju.MAIN_X
              and xs[-1] + ui_keju.BTN_W == ui_keju.WIN_W - 12,
              f"x={xs} 间距={gaps}")

        app.engine.keju_tool.watching = True
        app._drain_logs()
        check("界面:监听中 -> 状态灯变绿(闪烁)",
              app.status_dot.itemcget("dot", "fill")
              == ui_widgets.StatusDot.GREEN)
        app.engine.keju_tool.watching = False

        app.cfg["keju"]["regions"][0] = [10, 20, 30, 40]
        app.engine.keju_tool.set_auto_boxes(None)   # 引擎重启后:名字/基准都没了
        app._refresh_regions()
        check("界面:已定位但没名字的胶囊显示客户端N,没定位的不显示",
              caps_text(0) == "客户端1"
              and not app.client_capsules[1].grid_info(),
              f'{caps_text(0)}/胶囊1 {bool(app.client_capsules[1].grid_info())}')

        # 名字显示:按显示宽截到 6 个汉字(汉字算 2、半角算 1),数字尾缀不丢
        app.engine.keju_tool.set_auto_boxes([[10, 20, 30, 40]],
                                            ["杰尼龟徒弟10"])
        app._refresh_regions()
        check("界面:胶囊名字按显示宽截断(6 个汉字宽,数字尾缀不丢)",
              caps_text(0) == "杰尼龟徒弟10"
              and ui_keju._fit_name("一二三四五六七") == "一二三四五六"
              and ui_keju._fit_name("杰尼龟徒弟10号了") == "杰尼龟徒弟10"
              and ui_keju._fit_name("客户端1") == "客户端1",
              repr(caps_text(0)))
        app.engine.keju_tool.set_auto_boxes(None)   # 还原:后续用例没名字
        app._refresh_regions()

        check("模式:五个模式分两族(科举会试/殿试=锚点,后三个=无锚点)",
              [n for n, exam in core.KEJU_MODES if exam]
              == ["科举会试", "科举殿试"]
              and all(not exam for n, exam in core.KEJU_MODES
                      if n in ("三大学士", "金兜兕", "车迟斗法")))

        app._set_exam_mode(False)          # 旧接口:映射到「三大学士」
        with open(sb.config, encoding="utf-8") as f:
            saved2 = json.load(f)["keju"]
        check("界面:切到三大学士 -> 落盘 + 模式钮文案跟着换",
              app.cfg["keju"]["exam_mode"] is False
              and app.cfg["keju"]["mode"] == "三大学士"
              and saved2["exam_mode"] is False and saved2["mode"] == "三大学士"
              and app.mode_btn.label() == "三大学士 ▾",
              app.mode_btn.label())
        mode_line = [m for m in ui_logs() if m.startswith("[识别模式]")]
        check("界面:切模式日志只有 [识别模式]+模式名,不带括号解释",
              mode_line == ["[识别模式]三大学士"], mode_line[:1])

        app.mode_btn.invoke()             # 走按钮本身:放下模式下拉
        check("界面:点模式钮放下 5 个模式的下拉(当前项打勾)",
              app._mode_menu_open and bool(app.mode_menu.place_info())
              and [n for n, _b in app.mode_rows]
              == list(core.KEJU_MODE_NAMES)
              and app.mode_rows[2][1].label() == "✓ 三大学士"
              and app.mode_rows[0][1].label() == "科举会试",
              [b.label() for _n, b in app.mode_rows])
        app.on_pick_mode("科举会试")
        with open(sb.config, encoding="utf-8") as f:
            saved3 = json.load(f)["keju"]
        check("界面:下拉选「科举会试」-> 收面板 + 落盘 + 回到锚点族",
              not app._mode_menu_open
              and app.cfg["keju"]["mode"] == "科举会试"
              and app.cfg["keju"]["exam_mode"] is True
              and saved3["mode"] == "科举会试" and saved3["exam_mode"] is True
              and app.mode_btn.label() == "科举会试 ▾"
              and [m for m in ui_logs() if m.startswith("[识别模式]")]
              == ["[识别模式]三大学士", "[识别模式]科举会试"],
              app.mode_btn.label())

        app.on_toggle_mode()
        app._on_root_click(types.SimpleNamespace(widget=app.hero_canvas))
        check("界面:点主窗别处收起模式下拉", not app._mode_menu_open)
        app.on_toggle_mode()
        app.on_pick_mode("金兜兕")
        check("界面:选「金兜兕」-> 走无锚点族(底部对话带 + 全题干匹配)",
              app.cfg["keju"]["mode"] == "金兜兕"
              and app.cfg["keju"]["exam_mode"] is False
              and app.mode_btn.label() == "金兜兕 ▾",
              app.mode_btn.label())

        app.last_question = "【第一道题的题干内容是什么?】"
        app.last_answer = "答案甲"
        app._drain_logs()
        qid = app.hero_canvas.find_withtag("q")
        aid = app.hero_canvas.find_withtag("a")
        check("界面:题目识别区显示当前题目和大字答案(占位消失)",
              qid and aid
              and app.hero_canvas.itemcget(qid[0], "text")
              == "【第一道题的题干内容是什么?】"
              and app.hero_canvas.itemcget(aid[0], "text") == "答案甲"
              and not app.hero_canvas.find_withtag("ph"),
              app.hero_canvas.itemcget(qid[0], "text") if qid else "无题目")

        # ---- 收益/统计的界面显示已整个删掉(日志区只留打印框) ----
        check("界面:日志区只留打印框(统计/收益/物品/明细窗都不在界面上)",
              not any(hasattr(app, a) for a in
                      ("reward_var", "round_var", "reward_items_canvas",
                       "earn_detail_btn", "clear_log_btn"))
              and not hasattr(ui_keju, "draw_reward_items")
              and not hasattr(ui_keju, "open_earn_detail"))

        keys = ("_region_last", "_region_ref", "_region_frame", "_region_ocr_ts",
                "_region_t_detect", "_region_dirty")
        app.cfg["keju"]["regions"] = [None] * core.MAX_CLIENTS
        for attr in keys:
            getattr(app.engine.keju_tool, attr)[(1, 2, 3, 4)] = "y"
        app._clear_all_regions()
        check("界面:清空坐标(关窗时同样走这里) -> 槽位清空 + 记忆清空",
              app.cfg["keju"]["regions"] == [None] * core.MAX_CLIENTS
              and all(not getattr(app.engine.keju_tool, a) for a in keys))

        # ---- 自动定位客户端:整窗口一条区域 ----
        check("界面:定位钮旁的说明文字已删(不看说明也会用)",
              not hasattr(app, "hint_label"))
        with patch(find_client_windows=lambda: ([], [])):
            app.on_autolocate()
        check("自动定位:没找到客户端窗口 -> 落日志提示等自动接入",
              any("没找到梦幻西游客户端窗口" in m and "自动接入" in m
                  for m in ui_logs()),
              [m for m in ui_logs() if "没找到" in m][:1])

        two = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])",
                [100, 150, 800, 650]),
               ("梦幻西游 ONLINE", [850, 150, 1550, 650])]
        # 两块 640×480 画布,各躺在自己的窗口里(最小档 -> 不弹分辨率建议)
        two_canv = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])",
                     (640, 480), [110, 160, 750, 640], 1.0, "win"),
                    ("梦幻西游 ONLINE", (640, 480), [860, 160, 1500, 640], 1.0, "win")]
        with patch(find_client_windows=lambda: (list(two), [])), \
                patch(find_client_canvases=lambda: (list(two_canv), [])):
            app.on_autolocate()
        with open(sb.config, encoding="utf-8") as f:
            saved3 = json.load(f)["keju"]
        check("自动定位:按整个客户端填区域 + 落盘 + 刷新显示",
              app.cfg["keju"]["regions"][0] == [100, 150, 800, 650]
              and app.cfg["keju"]["regions"][1] == [850, 150, 1550, 650]
              and app.cfg["keju"]["regions"][2:] == [None] * (core.MAX_CLIENTS - 2)
              and saved3["regions"][0] == [100, 150, 800, 650]
              and caps_text(0) == "祭花′"
              and caps_text(1) == "客户端2"       # 标题没名字的窗口回退客户端N
              and any("[客户端]发现 2 个客户端" in m for m in ui_logs()),
              f'{caps_text(0)}/{caps_text(1)}')

        for attr in keys:
            getattr(app.engine.keju_tool, attr)[(100, 150, 800, 650)] = "z"
        # 700×520 的窗口里摆得下一块 640×480 画布(窗口没画布就不认了)
        six = [("c", [0, 0, 700, 520]) for _ in range(core.MAX_CLIENTS + 1)]
        with patch(find_client_windows=lambda: (list(six), [])), \
                patch(find_client_canvases=lambda: ([
                    ("c", (640, 480), [10, 10, 650, 490], 1.0, "win")], [])):
            app.on_autolocate()
        check("自动定位:超过上限只填前 5 个并提示(记忆一并清掉)",
              len([r for r in app.cfg["keju"]["regions"] if r]) == core.MAX_CLIENTS
              and all(not getattr(app.engine.keju_tool, a) for a in keys)
              and any("多出的 1 个舍弃" in m for m in ui_logs()),
              [m for m in ui_logs() if "多出" in m][:1])

        app.cfg["keju"]["regions"] = [[7, 8, 9, 10]] + [None] * (core.MAX_CLIENTS - 1)
        with open(sb.config, encoding="utf-8") as f:
            saved_before = json.load(f)["keju"]
        with patch(find_client_windows=lambda: ([], ["梦幻西游"])):
            app.on_autolocate()
        with open(sb.config, encoding="utf-8") as f:
            saved_after = json.load(f)["keju"]
        check("自动定位:每次先清空旧坐标(没找到客户端也不留旧的)",
              app.cfg["keju"]["regions"] == [None] * core.MAX_CLIENTS
              and saved_after["regions"] == [None] * core.MAX_CLIENTS
              and any("已最小化" in m for m in ui_logs())
              and any("客户端是最小化的,定位不到" in m for m in ui_logs()),
              f"清空前 {saved_before['regions'][:1]} -> 清空后 "
              f"{saved_after['regions'][:1]}")

        with patch(find_client_windows=lambda: (list(two), [])), \
                patch(find_client_canvases=lambda: (list(two_canv), [])):
            app._set_exam_mode(False)
            app.on_autolocate()
            app._set_exam_mode(True)
        check("自动定位:无锚点模式下提示科举三试更稳",
              any("「三大学士」没有结构锚点" in m and "更稳" in m
                  for m in ui_logs()),
              [m for m in ui_logs() if "更稳" in m][:1])

        # 分辨率:支持 640×480 / 800×600 / 1024×768 三档,按画布精确判、
        # 没画布不收 —— 其它档不定位、只落日志(红名表达,不弹框);
        # 混合时支持的照常定位。并提醒移动/改分辨率后会自动跟随
        big = [("梦幻西游 ONLINE", [0, 0, 1290, 980]),
               ("梦幻西游 ONLINE2", [0, 0, 640, 480])]
        big_canv = [("梦幻西游 ONLINE", (1280, 960), [0, 0, 1290, 980], 1.0, "win"),
                    ("梦幻西游 ONLINE2", (640, 480), [0, 0, 640, 480], 1.0, "win")]
        warned = []
        with patch(mods=(gui,), messagebox=types.SimpleNamespace(
                showwarning=lambda *a, **k: warned.append(a))), \
             patch(find_client_windows=lambda: (list(big), [])), \
             patch(find_client_canvases=lambda: (list(big_canv), [])):
            app.on_autolocate()
        check("自动定位:非支持档跳过只落日志不弹框(红名表达),支持的照常定位",
              app.cfg["keju"]["regions"][0] == [0, 0, 640, 480]
              and len([r for r in app.cfg["keju"]["regions"] if r]) == 1
              and any("游戏分辨率 1280×960 不支持,已跳过"
                      in m for m in ui_logs())
              and not warned,
              f"warned={warned} regions={app.cfg['keju']['regions'][:2]}")
        mid = [("梦幻西游 ONLINE", [0, 0, 860, 600])]
        hinted, warned_mid = [], []
        with patch(mods=(gui,), messagebox=types.SimpleNamespace(
                showwarning=lambda *a, **k: warned_mid.append(a),
                showinfo=lambda *a, **k: hinted.append(a))), \
             patch(find_client_windows=lambda: (list(mid), [])), \
             patch(find_client_canvases=lambda: ([
                 ("梦幻西游 ONLINE", (800, 600), [0, 0, 800, 600], 1.0, "win")], [])):
            app.on_autolocate()
        check("自动定位:打出画布精确分辨率,非最小档只落日志不弹框(也不弹不支持)",
              app.cfg["keju"]["regions"][0] == [0, 0, 860, 600]
              and any("[客户端1]整个客户端 (0,0)-(860,600) 800×600"
                      in m for m in ui_logs())
              and any("不是最小分辨率" in m and "640×480" in m
                      for m in ui_logs())
              and not hinted and not warned_mid,
              f"hinted={hinted} warned={warned_mid}")
        check("自动定位:提示客户端挪了会自动跟随",
              sum(1 for m in ui_logs() if "会自动跟随" in m) >= 1,
              [m for m in ui_logs() if "跟随" in m][:1])

        small = [("梦幻西游 ONLINE", [0, 0, 686, 500])]
        n_before = len(ui_logs())
        with patch(find_client_windows=lambda: (list(small), [])), \
                patch(find_client_canvases=lambda: ([
                    ("梦幻西游 ONLINE", (640, 480), [0, 0, 640, 480], 1.0, "win")], [])):
            app.on_autolocate()
        new_lines = ui_logs()[n_before:]
        check("自动定位:已经是最小分辨率就不刷那条提醒",
              any("[客户端1]整个客户端 (0,0)-(686,500) 640×480"
                  in m for m in new_lines)
              and not any("不是最小分辨率" in m for m in new_lines),
              new_lines[:2])

        # 登录窗(独立版/多标签版同判:标题抠不出角色名)定位不到;
        # 正常状态不是错 —— 只落日志提示等自动接入,不弹任何框
        login_wins = [("梦幻西游 ONLINE", [0, 0, 1000, 750])]
        login_hint, login_warn = [], []
        with patch(mods=(gui,), messagebox=types.SimpleNamespace(
                showinfo=lambda *a, **k: login_hint.append(a),
                showwarning=lambda *a, **k: login_warn.append(a))), \
             patch(find_client_windows=lambda: (list(login_wins), [])), \
             patch(find_client_canvases=lambda: ([], [])):
            app.on_autolocate()
        check("自动定位:只有登录窗 -> 只落日志提示自动接入,不弹框",
              not login_hint and not login_warn
              and any("检测到 1 个登录窗口" in m and "自动接入" in m
                      for m in ui_logs())
              and app.cfg["keju"]["regions"] == [None] * core.MAX_CLIENTS,
              f"hint={login_hint} warned={login_warn}")

        # 名字在但不支持档:记入暂停观测名单(界面红名),不定位,只落日志不弹框
        hd_named = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色己[6])",
                     [0, 0, 1380, 1060])]
        hd_named_canv = [(hd_named[0][0], (1280, 960),
                          [0, 0, 1380, 1060], 1.0, "win")]
        warned_hd = []
        with patch(mods=(gui,), messagebox=types.SimpleNamespace(
                showwarning=lambda *a, **k: warned_hd.append(a))), \
             patch(find_client_windows=lambda: (list(hd_named), [])), \
             patch(find_client_canvases=lambda: (list(hd_named_canv), [])):
            app.on_autolocate()
        check("自动定位:名字在但不支持档 -> 记入暂停观测(红名),只落日志不弹框",
              app.engine.keju_tool.ui_state()["unsup"] == ["角色己"]
              and app.engine.keju_tool._auto_boxes is None
              and not warned_hd
              and any("游戏分辨率 1280×960 不支持,已跳过" in m
                      for m in ui_logs()),
              f"unsup={app.engine.keju_tool.ui_state()['unsup']} "
              f"warned={warned_hd}")
        app.engine.keju_tool.set_unsup(None)

        # 多开窗口重叠:甲(HD,多标签版画布顶上还有标签条)的窗口框住了
        # 乙的 640×480 画布 —— 按标题配对不借邻居画布,甲照旧红名、
        # 乙照常定位(纯几何配会把甲误判成支持档收编)
        ovl_wins = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色甲[1])",
                     [100, 100, 760, 600]),
                    ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色乙[2])",
                     [100, 110, 740, 590])]
        ovl_canv = [(ovl_wins[1][0], (640, 480), [110, 120, 730, 580],
                     1.0, "win"),
                    (ovl_wins[0][0], (1280, 960), [100, 140, 760, 560],
                     1.0, "tab")]
        with patch(find_client_windows=lambda: (list(ovl_wins), [])), \
                patch(find_client_canvases=lambda: (list(ovl_canv), [])):
            app.on_autolocate()
        check("自动定位:窗口重叠不借邻居画布,HD 照旧红名、邻居照常定位",
              app.engine.keju_tool.ui_state()["unsup"] == ["角色甲"]
              and app.engine.keju_tool._auto_boxes == [[100, 110, 740, 590]]
              and app.cfg["keju"]["regions"][0] == [100, 110, 740, 590],
              f"unsup={app.engine.keju_tool.ui_state()['unsup']} "
              f"boxes={app.engine.keju_tool._auto_boxes}")
        app.engine.keju_tool.set_unsup(None)

        # 跟随:客户端改成不支持档(HD)-> 认出「谁改了档」,移除观测区域并
        # 提示;其余支持客户端照常跟随;收口后同一画面不重复提示。
        # 用 mk_engine 的独立工具实例(不起后台线程),状态不被并发污染
        feng, fapp = mk_engine(make_bank(sb.bank, [
            {"q": "第一道题的题干内容是什么", "a": "答案甲"}]), ())
        # 空手启动:一个客户端都没跟到 -> 检测到登录角色的支持客户端就
        # 自动定位,不需要手动刷新(mk_engine 默认按死等待旗,这里
        # 显式摆 True = "启动后游戏才开"的空手状态)
        feng.keju_tool._readopt = True
        feng.keju_tool._follow_ts = 0.0
        boot_wins = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色戊[5])",
                      [100, 100, 740, 580])]
        boot_canv = [(boot_wins[0][0], (640, 480), [110, 110, 730, 570], 1.0, "win")]
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(boot_wins), [])), \
                patch(find_client_canvases=lambda: (list(boot_canv), [])):
            feng.keju_tool._follow_clients()
        check("跟随:空手启动 -> 首见登录客户端自动定位,无需手动刷新",
              feng.keju_tool._auto_boxes == [[100, 100, 740, 580]]
              and feng.keju_tool.ui_state()["names"] == [
                  "角色戊", None, None, None, None]
              and feng.keju_tool._readopt is False
              and any("检测到 1 个已登录客户端,已自动定位" in m
                      for m in fapp.logs[n_before:]),
              f"boxes={feng.keju_tool._auto_boxes} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")
        feng.keju_tool._follow_ts = 0.0
        box_a = [100, 100, 740, 580]
        box_b = [900, 100, 1560, 600]
        feng.keju_tool.set_auto_boxes([box_a, box_b], ["角色甲", "角色乙"])
        hd_canv = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色甲[1])",
                    (1280, 960), [100, 100, 1380, 1060], 1.0, "win"),
                   ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色乙[2])",
                    (640, 480), [910, 110, 1550, 590], 1.0, "win")]
        raw_wins = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色甲[1])",
                     [100, 100, 1390, 1070]),
                    ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色乙[2])",
                     [900, 100, 1560, 600])]
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(raw_wins), [])), \
                patch(find_client_canvases=lambda: (list(hd_canv), [])):
            feng.keju_tool._follow_clients()
        st = feng.keju_tool.ui_state()
        check("跟随:客户端改成不支持档 -> 暂停观测不除名(红名),其余照常跟随",
              feng.keju_tool._auto_boxes == [box_b]
              and st["names"] == ["角色乙", "角色甲", None, None, None]
              and st["unsup"] == ["角色甲"]
              and fapp.cfg["keju"]["regions"] == [box_b]
              + [None] * (core.MAX_CLIENTS - 1)
              and any("角色甲(1280×960)分辨率不支持,已暂停观测" in m
                      for m in fapp.logs[n_before:])
              and not any("已移除观测区域" in m for m in fapp.logs[n_before:])
              and any("1 个客户端位置变了,已自动跟随" in m
                      for m in fapp.logs[n_before:]),
              f"boxes={feng.keju_tool._auto_boxes} "
              f"names={st['names']} unsup={st['unsup']} "
              f"regions={fapp.cfg['keju']['regions'][:2]} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(raw_wins), [])), \
                patch(find_client_canvases=lambda: (list(hd_canv), [])):
            feng.keju_tool._follow_clients()
        check("跟随:收口后同一画面不重复提示", len(fapp.logs) == n_before,
              str(fapp.logs[n_before:]))

        # 改回支持档:自动恢复观测(红名摘掉,不需要手动刷新)
        back_wins = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色甲[1])",
                      [100, 100, 740, 580]),
                     ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色乙[2])",
                      [900, 100, 1560, 600])]
        back_canv = [(back_wins[0][0], (640, 480),
                      [110, 110, 730, 570], 1.0, "win"),
                     (back_wins[1][0], (640, 480),
                      [910, 110, 1550, 590], 1.0, "win")]
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(back_wins), [])), \
                patch(find_client_canvases=lambda: (list(back_canv), [])):
            feng.keju_tool._follow_clients()
        st = feng.keju_tool.ui_state()
        check("跟随:改回支持档 -> 自动恢复观测并摘掉红名",
              feng.keju_tool._auto_boxes == [box_b, [100, 100, 740, 580]]
              and st["unsup"] == []
              and st["names"] == ["角色乙", "角色甲", None, None, None]
              and any("角色甲(640×480)分辨率已恢复,继续观测" in m
                      for m in fapp.logs[n_before:]),
              f"boxes={feng.keju_tool._auto_boxes} unsup={st['unsup']} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")

        # 跟随路径(数量没变,只刷新名字)也自检红名:滞留的误标即摘
        feng.keju_tool.set_auto_boxes([box_a, box_b], ["角色甲", "角色乙"])
        feng.keju_tool.set_unsup(["角色甲"])   # 摆一个滞留的误标
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(back_wins), [])), \
                patch(find_client_canvases=lambda: (list(back_canv), [])):
            feng.keju_tool._follow_clients()
        check("跟随:红名滞留误标 -> 跟随自检摘掉并提示恢复",
              feng.keju_tool.ui_state()["unsup"] == []
              and feng.keju_tool._auto_boxes == [box_a, box_b]
              and any("角色甲(640×480)分辨率已恢复,继续观测" in m
                      for m in fapp.logs[n_before:]),
              f"unsup={feng.keju_tool.ui_state()['unsup']} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")

        # 暂停观测的客户端直接关掉:红名静默摘掉(观测名单没变也要摘)
        feng.keju_tool.set_auto_boxes([box_b], ["角色乙"])
        feng.keju_tool.set_unsup(["角色甲"])
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: ([back_wins[1]], [])), \
                patch(find_client_canvases=lambda: ([back_canv[1]], [])):
            feng.keju_tool._follow_clients()
        check("跟随:暂停观测的客户端关闭 -> 红名静默摘掉",
              feng.keju_tool.ui_state()["unsup"] == []
              and feng.keju_tool._auto_boxes == [box_b]
              and not fapp.logs[n_before:],
              f"unsup={feng.keju_tool.ui_state()['unsup']} "
              f"logs={fapp.logs[n_before:]}")

        # 空手等待期只开着 HD 客户端:记入红名名单(不收编、不弹框),
        # 界面显示红色胶囊;等登录支持档再自动接入
        feng.keju_tool.set_auto_boxes(None)
        feng.keju_tool._readopt = True
        hd_only_wins = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色己[6])",
                         [100, 100, 1380, 1060])]
        hd_only_canv = [(hd_only_wins[0][0], (1280, 960),
                         [100, 100, 1380, 1060], 1.0, "win")]
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(hd_only_wins), [])), \
                patch(find_client_canvases=lambda: (list(hd_only_canv), [])):
            feng.keju_tool._follow_clients()
        st = feng.keju_tool.ui_state()
        check("跟随:空手期只有 HD 客户端 -> 记红名不收编,等支持档自动接入",
              st["unsup"] == ["角色己"]
              and st["names"] == ["角色己", None, None, None, None]
              and feng.keju_tool._auto_boxes is None
              and feng.keju_tool._readopt is True
              and any("角色己(1280×960)分辨率不支持,已暂停观测" in m
                      for m in fapp.logs[n_before:]),
              f"unsup={st['unsup']} boxes={feng.keju_tool._auto_boxes} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")
        feng.keju_tool.set_unsup(None)

        # 只有名字还没画布(登录/loading 中):不下结论 —— 不标红也不收编
        feng.keju_tool.set_auto_boxes(None)
        feng.keju_tool._readopt = True
        feng.keju_tool.set_unsup(None)
        name_only = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色庚[7])",
                      [100, 100, 1380, 1060])]
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(name_only), [])), \
                patch(find_client_canvases=lambda: ([], [])):
            feng.keju_tool._follow_clients()
        st = feng.keju_tool.ui_state()
        check("跟随:有名字没画布(登录/loading)不下结论 —— 不标红不收编",
              st["unsup"] == []
              and st["names"] == [None] * core.MAX_CLIENTS
              and feng.keju_tool._auto_boxes is None
              and not fapp.logs[n_before:],
              f"unsup={st['unsup']} logs={fapp.logs[n_before:]}")

        # 退出登录:标题抠不出角色名 -> 认出是谁,静默移除观测区域(不提示)
        logout_wins = [("梦幻西游 ONLINE", [100, 100, 740, 580]),
                       ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色乙[2])",
                        [900, 100, 1560, 600])]
        logout_canv = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色乙[2])",
                        (640, 480), [910, 110, 1550, 590], 1.0, "win")]
        feng.keju_tool.set_auto_boxes([box_a, box_b], ["角色甲", "角色乙"])
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(logout_wins), [])), \
                patch(find_client_canvases=lambda: (list(logout_canv), [])):
            feng.keju_tool._follow_clients()
        check("跟随:客户端退出登录 -> 静默移除观测区域,其余照常跟随",
              feng.keju_tool._auto_boxes == [box_b]
              and feng.keju_tool.ui_state()["names"] == [
                  "角色乙", None, None, None, None]
              and feng.keju_tool._readopt is False
              and not any("退出登录" in m for m in fapp.logs[n_before:])
              and any("1 个客户端位置变了,已自动跟随" in m
                      for m in fapp.logs[n_before:]),
              f"boxes={feng.keju_tool._auto_boxes} "
              f"readopt={feng.keju_tool._readopt} "
              f"names={feng.keju_tool.ui_state()['names']} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")

        # 退出登录(全部):两台都退到登录界面 -> 全部静默移除,进入等待重登
        allout_wins = [("梦幻西游 ONLINE", [100, 100, 740, 580]),
                       ("梦幻西游 ONLINE", [900, 100, 1560, 600])]
        feng.keju_tool.set_auto_boxes([box_a, box_b], ["角色甲", "角色乙"])
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(allout_wins), [])), \
                patch(find_client_canvases=lambda: ([], [])):
            feng.keju_tool._follow_clients()
        check("跟随:全部退出登录 -> 全部静默移除,进入等待重登状态",
              feng.keju_tool._auto_boxes is None
              and feng.keju_tool._readopt is True
              and fapp.cfg["keju"]["regions"] == [None] * core.MAX_CLIENTS
              and not any("退出登录" in m for m in fapp.logs[n_before:]),
              [m for m in fapp.logs[n_before:] if "客户端" in m][:2])

        # 重新登录(换了角色):等待重登状态 -> 自动重新收编,无需手动刷新
        relogin_wins = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色丙[3])",
                         [100, 100, 740, 580])]
        relogin_canv = [(relogin_wins[0][0], (640, 480),
                         [110, 110, 730, 570], 1.0, "win")]
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(relogin_wins), [])), \
                patch(find_client_canvases=lambda: (list(relogin_canv), [])):
            feng.keju_tool._follow_clients()
        check("跟随:清空后重新登录(换了角色) -> 自动重新收编,无需手动刷新",
              feng.keju_tool._auto_boxes == [[100, 100, 740, 580]]
              and feng.keju_tool.ui_state()["names"] == [
                  "角色丙", None, None, None, None]
              and feng.keju_tool._readopt is False
              and fapp.cfg["keju"]["regions"] == [[100, 100, 740, 580]]
              + [None] * (core.MAX_CLIENTS - 1)
              and any("检测到 1 个已登录客户端,已自动定位" in m
                      for m in fapp.logs[n_before:]),
              [m for m in fapp.logs[n_before:] if "客户端" in m][:2])

        # 新开客户端:在跟的同时新开的登录角色客户端自动纳入观测
        new_wins = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色丙[3])",
                     [100, 100, 740, 580]),
                    ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色丁[4])",
                     [1700, 100, 2340, 680])]
        new_canv = [("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色丙[3])",
                     (640, 480), [110, 110, 730, 570], 1.0, "win"),
                    ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色丁[4])",
                     (640, 480), [1710, 110, 2330, 670], 1.0, "win")]
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(new_wins), [])), \
                patch(find_client_canvases=lambda: (list(new_canv), [])):
            feng.keju_tool._follow_clients()
        check("跟随:新开客户端自动纳入观测",
              feng.keju_tool._auto_boxes == [[100, 100, 740, 580],
                                             [1700, 100, 2340, 680]]
              and feng.keju_tool.ui_state()["names"] == [
                  "角色丙", "角色丁", None, None, None]
              and any("检测到新客户端 角色丁(640×480),已自动纳入观测"
                      in m for m in fapp.logs[n_before:]),
              [m for m in fapp.logs[n_before:] if "客户端" in m][:2])

        # 最小化:最小化的客户端原槽位保留(枚举单独记账),等恢复自动续;
        # 不提示、不当作新客户端重新纳入
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=(
                lambda: ([new_wins[0]], [new_wins[1][0]]))), \
                patch(find_client_canvases=lambda: ([new_canv[0]], [])):
            feng.keju_tool._follow_clients()
        check("跟随:客户端最小化 -> 原槽位保留等恢复,不提示不重纳",
              feng.keju_tool._auto_boxes == [[100, 100, 740, 580],
                                             [1700, 100, 2340, 680]]
              and feng.keju_tool.ui_state()["names"] == [
                  "角色丙", "角色丁", None, None, None]
              and not fapp.logs[n_before:],
              f"boxes={feng.keju_tool._auto_boxes} "
              f"logs={fapp.logs[n_before:]}")

        # 关闭:窗口没了(最小化列表里也没有)-> 静默移除(不提示),其余跟随
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: ([new_wins[0]], [])), \
                patch(find_client_canvases=lambda: ([new_canv[0]], [])):
            feng.keju_tool._follow_clients()
        check("跟随:客户端关闭 -> 静默移除观测区域,其余照常跟随",
              feng.keju_tool._auto_boxes == [[100, 100, 740, 580]]
              and feng.keju_tool.ui_state()["names"] == [
                  "角色丙", None, None, None, None]
              and fapp.cfg["keju"]["regions"] == [[100, 100, 740, 580]]
              + [None] * (core.MAX_CLIENTS - 1)
              and not any("已关闭" in m or "角色丁" in m
                          for m in fapp.logs[n_before:]),
              f"boxes={feng.keju_tool._auto_boxes} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")

        # 已观测满 5 个:再识别的新客户端直接丢弃(记一次日志,不重复刷屏)
        five = [(f"梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色甲{i}[{i}])",
                 [i * 800, 100, i * 800 + 740, 580]) for i in range(1, 6)]
        five_canv = [(w[0], (640, 480), [w[1][0] + 10, 110, w[1][2] - 10, 570],
                      1.0, "win") for w in five]
        sixth = ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色己[6])",
                 [4800, 100, 5540, 580])
        sixth_canv = (sixth[0], (640, 480), [4810, 110, 5530, 570], 1.0, "win")
        full_names = [f"角色甲{i}" for i in range(1, 6)]
        feng.keju_tool.set_auto_boxes([w[1] for w in five], full_names)
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(five) + [sixth], [])), \
                patch(find_client_canvases=lambda:
                      (list(five_canv) + [sixth_canv], [])):
            feng.keju_tool._follow_clients()
            feng.keju_tool._follow_ts = 0.0
            feng.keju_tool._follow_clients()
        check("跟随:已观测满 5 个 -> 新客户端直接丢弃(只记一次日志)",
              feng.keju_tool._auto_boxes == [w[1] for w in five]
              and feng.keju_tool.ui_state()["names"][:5] == full_names
              and "角色己" not in feng.keju_tool.ui_state()["names"]
              and feng.keju_tool._dropped == ["角色己"]
              and sum(1 for m in fapp.logs[n_before:] if "已观测满" in m) == 1
              and not any("检测到新客户端 角色己" in m
                          for m in fapp.logs[n_before:]),
              f"dropped={feng.keju_tool._dropped} "
              f"logs={[m for m in fapp.logs[n_before:] if '客户端' in m][:2]}")

        # 槽位空出(关掉一个):丢弃的客户端自动接入,丢弃标记摘掉
        feng.keju_tool._follow_ts = 0.0
        n_before = len(fapp.logs)
        with patch(find_client_windows=lambda: (list(five[1:]) + [sixth], [])), \
                patch(find_client_canvases=lambda:
                      (list(five_canv[1:]) + [sixth_canv], [])):
            feng.keju_tool._follow_clients()
        st = feng.keju_tool.ui_state()
        check("跟随:满员丢弃后槽位空出 -> 自动接入并摘掉丢弃标记",
              feng.keju_tool._auto_boxes == [w[1] for w in five[1:]] + [sixth[1]]
              and st["names"][:5] == full_names[1:] + ["角色己"]
              and feng.keju_tool._dropped == []
              and any("检测到新客户端 角色己(640×480),已自动纳入观测" in m
                      for m in fapp.logs[n_before:]),
              f"boxes={feng.keju_tool._auto_boxes} "
              f"dropped={feng.keju_tool._dropped}")

        # 悬停角色名框:按角色名现场匹配(位置无关 —— 画布在别处、窗口拖走
        # 后老区域过期都能查到);未登录/未定位有说明,离开即收
        app._set("regions", [[100, 150, 920, 770], None, None, None, None])
        app.engine.keju_tool.set_auto_boxes(
            [[100, 150, 920, 770]], ["祭花′"])
        hover_canv = ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])",
                      (800, 600), [5555, 4444, 6355, 5244], 1.0, "win")
        with patch(find_client_canvases=lambda: ([hover_canv], [])):
            app.on_client_hover_start(0)
            tip_text = app._client_tip.tip_cv.itemcget(
                app._client_tip.tip_tid, "text")
            app.on_client_hover_start(1)
            tip_text2 = app._client_tip.tip_cv.itemcget(
                app._client_tip.tip_tid, "text")
            app.on_client_hover_end(0)
        with patch(find_client_canvases=lambda: ([], [])):
            app.on_client_hover_start(0)
            tip_text3 = app._client_tip.tip_cv.itemcget(
                app._client_tip.tip_tid, "text")
            app.on_client_hover_end(0)
        # 暂停观测(分辨率不支持)的槽位:红名 + 悬停直接说明,不查分辨率
        app.engine.keju_tool.set_unsup(["暂停甲"])
        app._refresh_regions()
        with patch(find_client_canvases=lambda: ([], [])):
            app.on_client_hover_start(1)
            tip_text4 = app._client_tip.tip_cv.itemcget(
                app._client_tip.tip_tid, "text")
            app.on_client_hover_end(0)
        name_fill = app.client_capsules[1].itemcget("name", "fill")
        app.engine.keju_tool.set_unsup(None)
        app._refresh_regions()
        check("胶囊悬停:按角色名现查分辨率(位置无关),未登录/未定位/暂停观测有说明,离开即收",
              tip_text == "800×600" and tip_text2 == "未定位"
              and tip_text3 == "还没读到游戏画面"
              and tip_text4 == "分辨率不支持"
              and name_fill == ui_keju.UNSUP_RED
              and not getattr(app, "_client_tip", None),
              f"{tip_text!r} / {tip_text2!r} / {tip_text3!r} / "
              f"{tip_text4!r} / {name_fill}")

        app.cfg["keju"]["regions"] = [None] * core.MAX_CLIENTS
        app.engine.keju_tool.set_auto_boxes(None)
        app._refresh_regions()
        check("界面:没定位到客户端 -> 左栏显示占位图,名字框全藏",
              app.side_ph.place_info()
              and app.side_ph.find_withtag("ph")
              and not any(cv.grid_info() for cv in app.client_capsules),
              f"占位 {bool(app.side_ph.place_info())}")

        check("窗口标题抠角色名:最后一个 - 后截到 [,没有 - 就是没进角色",
              core.client_name(
                  "梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])")
              == "祭花′"
              and core.client_name(
                  "梦幻西游 ONLINE - (北京1区[紫禁之巅] - 服务器大官人)")
              == "服务器大官人"
              and core.client_name("梦幻西游 ONLINE") == ""
              and core.client_name("") == ""
              and core.client_name(None) == "",
              core.client_name(
                  "梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])"))

        # 档位判定 2026-11-08 起只认**游戏画布(WSGAME)**的精确值,没有兜底
        # (用户定的:登录角色才有画布才识别;canvas_game_res 照抄降妖伏魔)。
        # 画布枚举桩成空 = 窗口里没画布(登录窗/没进角色),一律不收(不碰真机)
        with patch(find_client_canvases=lambda: ([], [])):
            check("分辨率(没画布):一律不认,没有面积归档兜底",
                  not core.client_supported([0, 0, 640, 480])
                  and not core.client_supported([0, 0, 800, 600])
                  and not core.client_supported([0, 0, 860, 600])
                  and not core.client_supported([0, 0, 1024, 768])
                  and core.client_size_info([0, 0, 812, 663]) == (
                      "未登录角色,没有游戏画面(客户区 812×663),已跳过", False)
                  and core.supported_res_text() == "640×480 / 800×600 / 1024×768",
                  core.supported_res_text())

        # 画布精确口径:canvas_game_res 纯函数(用例照抄降妖伏魔,两边同款)
        check("画布分辨率:画布尺寸+显示器DPI -> 游戏设置分辨率(两种感知模式都对)",
              core.canvas_game_res((1000, 750), 120) == (800, 600)      # 125% 感知
              and core.canvas_game_res((1200, 900), 144) == (800, 600)  # 150% 感知
              and core.canvas_game_res((960, 720), 144) == (640, 480)   # 150% 感知
              and core.canvas_game_res((800, 600), 120) == (640, 480)   # 125% 感知
              and core.canvas_game_res((800, 600), 96) == (800, 600)    # 100% 两种进程
              and core.canvas_game_res((800, 600), 96) == core.canvas_game_res(
                  (800, 600), 0)                                        # dpi 缺省兜底
              and core.canvas_game_res((812, 663), 96) == (812, 663))   # 非标准画布不猜

        # 回归(2026-11-13 真机 stderr 实锤):枚举列出窗口后、查询画布前,
        # 窗口销毁(关窗竞态)会让 canvas_of 返回 None —— 旧代码直接解包
        # TypeError,异常在 ctypes 回调里被吞掉,但 EnumWindows 收到 FALSE
        # 就提前收工,当拍还在场的窗口全部漏掉。僵尸窗故意排在真窗前面:
        # 修复前枚举在僵尸窗处中断、真窗收不到;修复后照常收齐。
        def _zomb_get_client_rect(hwnd, r):
            r = r._obj                 # 真代码传 byref(rect),拿回本体再填
            if hwnd == 11:             # 僵尸窗:查询时已销毁
                return 0
            r.right, r.bottom = 1024, 768      # 真窗:独立窗口版 1024×768
            return 1

        def _zomb_client_to_screen(_hwnd, pt):
            pt = pt._obj
            pt.x, pt.y = 100, 200
            return 1

        fake_u32 = types.SimpleNamespace(
            GetWindowTextLengthW=lambda _h: len(_boot_title),
            GetWindowTextW=lambda _h, buf, _n: setattr(buf, "value", _boot_title),
            GetClassNameW=lambda _h, buf, _n: setattr(
                buf, "value", client_common.CANVAS_WINDOW_CLASS),
            IsWindowVisible=lambda _h: 1,
            IsIconic=lambda _h: 0,
            EnumWindows=lambda fn, _lp: next(
                (0 for h in (11, 12) if not fn(h, 0)), 1),
            GetClientRect=_zomb_get_client_rect,
            ClientToScreen=_zomb_client_to_screen,
            MonitorFromPoint=lambda _pt, _fl: 0,   # 拿不到显示器 -> dpi 96
        )
        import ctypes as _ct
        real_windll, _ct.windll = _ct.windll, types.SimpleNamespace(
            user32=fake_u32, shcore=None)
        try:
            got_z = real_enum()
        finally:
            _ct.windll = real_windll
        check("画布枚举:窗口中途销毁(canvas_of None)不再打断整轮枚举",
              got_z == ([(_boot_title, (1024, 768), [100, 200, 1124, 968],
                         1.0, "win")], []), str(got_z))

        # 画布精确口径:窗口里配到画布 -> 按游戏设置分辨率判,窗口拉伸不参与
        # (1015×829 的拉伸窗配到 800×600 画布 = 支持;815×679 配到
        # 640×480 画布 = 已是最小档;画布非标准档 -> 不支持)
        def _fake_canvases():
            return ([("梦幻西游 ONLINE", (800, 600), [10, 20, 1010, 770], 1.25, "win"),
                     ("梦幻西游 ONLINE2", (640, 480), [10, 10, 650, 490], 1.0, "win"),
                     ("怪状态", (900, 680), [2000, 0, 2900, 680], 1.0, "win"),
                     ("梦幻西游 ONLINE3", (1024, 768), [3000, 0, 4024, 768],
                      1.0, "win")], [])

        with patch(find_client_canvases=_fake_canvases):
            got_a = core.client_canvas([0, 0, 1015, 829])
            exact_ok = (
                got_a == [[10, 20, 1010, 770], (800, 600), "win"]
                and core.client_canvas([2000, 2000, 3000, 3000]) is None
                and core.client_supported([0, 0, 1015, 829])
                and core.client_size_info([0, 0, 1015, 829]) == ("800×600", False)
                and core.client_supported([0, 0, 815, 679])
                and core.client_size_info([0, 0, 815, 679]) == ("640×480", True)
                and not core.client_supported([1990, 0, 3000, 700])
                and core.client_size_info([1990, 0, 3000, 700]) == (
                    "游戏分辨率 900×680 不支持,已跳过", False)
                and core.client_supported([2990, 0, 4100, 800])
                and core.client_size_info([2990, 0, 4100, 800]) == (
                    "1024×768", False))
        check("画布分辨率:配到画布就按游戏设置分辨率精确判(拉伸窗也算标准档)",
              exact_ok, str(got_a))

        bad_fmt = [m for m in ui_logs() if not m.startswith("[")]
        other_mod = [m for m in ui_logs() if m.startswith("[其他]")]
        check("界面:日志统一是 [模块]内容(没有漏标模块的)",
              not bad_fmt and not other_mod,
              f"没模块 {bad_fmt[:2]} / 兜底 {other_mod[:2]}")

        app.cfg["keju"]["regions"] = [None] * core.MAX_CLIENTS
        real_wins, real_mini = core.find_client_windows()
        check("自动定位:真机枚举窗口返回两个列表(没游戏时为空)",
              isinstance(real_wins, list) and isinstance(real_mini, list)
              and all(len(w) == 2 and len(w[1]) == 4 for w in real_wins),
              f"{len(real_wins)} 个客户端 / {len(real_mini)} 个最小化")
        real_canv, real_cmini = core.find_client_canvases()
        check("自动定位:真机枚举游戏画布返回两个列表(没游戏时为空)",
              isinstance(real_canv, list) and isinstance(real_cmini, list)
              and all(len(c) == 5 and len(c[2]) == 4 for c in real_canv)
              and all(core.client_name(c[0]) for c in real_canv),
              f"{len(real_canv)} 块画布 / {len(real_cmini)} 个最小化")

        app.log("这条手动写的日志一")
        app.log("这条手动写的日志二")
        filetxt = open(sb.log, encoding="utf-8").read()
        check("界面:日志全部落文件(界面已无滚动框)",
              not hasattr(app, "log_box")
              and "日志一" in filetxt and "日志二" in filetxt)

        app.on_toggle_mute()
        muted = (app.cfg["app"]["mute"] is True
                 and json.load(open(sb.config, encoding="utf-8"))["app"]["mute"] is True
                 and app.mute_btn.label() == "语音恢复"
                 and app.mute_btn._pal["bg"]
                 == ui_keju.SKINS[app.skin]["mute"]["bg"])
        app.on_toggle_mute()
        check("界面:静音钮切换状态并落盘(文案+配色跟着换)",
              muted and app.cfg["app"]["mute"] is False
              and app.mute_btn.label() == "语音静音"
              and app.mute_btn._pal["bg"]
              == ui_keju.SKINS[app.skin]["ghost"]["bg"])

        rc_calls = []
        real_rc = app.engine.request_cycle
        app.engine.request_cycle = lambda: rc_calls.append(1)
        app.manual_btn.invoke()           # 走按钮本身:投递一次手动识别
        app.engine.request_cycle = real_rc
        check("界面:手动识别按钮投递一次识别任务", rc_calls == [1], str(rc_calls))

        app.on_toggle_skin()
        app._drain_logs()
        check("界面:换肤 -> 落盘 + 整窗重建,按钮和题目区都在",
              app.skin == "light"
              and json.load(open(sb.config, encoding="utf-8"))["app"]["skin"]
              == "light"
              and app.mode_btn.label() == "科举会试 ▾"
              and not hasattr(app, "refresh_btn")   # 刷新钮已删,重建也不复活
              and app.hero_canvas.find_withtag("q")      # 题目还在(重画过)
              and app.status_dot.itemcget("dot", "fill")
              == ui_widgets.StatusDot.RED)
        app.on_toggle_skin()
        app._drain_logs()
        check("界面:再点换肤切回玄夜(深色)",
              app.skin == "dark"
              and json.load(open(sb.config, encoding="utf-8"))["app"]["skin"]
              == "dark")

        app.last_answer = "一阵飓风卷起了龙宫的一些物品散落建邺城"
        app.on_mini()
        app._drain_logs()
        app.mini.update()
        app._drain_logs()      # 锚定按 reqwidth 算,文字刚换时可能落后一拍;真实
        app.mini.update()      # 运行 _drain_logs 每 50ms 会重新收口,这里同样收两拍
        sw = app.mini.winfo_screenwidth()
        x, w = app.mini.winfo_x(), app.mini.winfo_width()
        check("界面:迷你窗大字显示最近答案",
              app.mini_var.get() == app.last_answer, app.mini_var.get()[:12])
        check("界面:迷你窗钉在屏幕右上角(任何分辨率都不会跑出屏幕)",
              x >= 0 and x + w <= sw, f"x={x} w={w} 屏宽={sw}")

        def mini_font():
            m = re.search(r"(\d+)\s+(bold|normal)",
                          str(app.mini_label.cget("font")))
            return int(m.group(1)) if m else -1

        app.last_answer = "很长很长的答案文本" * 6
        app._drain_logs()
        app.mini.update()
        big_font = mini_font()
        app.last_answer = "陆游"
        app._drain_logs()
        app.mini.update()
        check("界面:长答案缩过字号后,短答案会还原默认字号(回归)",
              big_font < 22 and mini_font() == 22,
              f"长答案字号 {big_font} -> 短答案字号 {mini_font()}")

        app.mini.geometry("+700+300")       # 模拟用户手动拖动
        app.mini.update()
        app.last_answer = "拖动后再来一道答案"
        app._drain_logs()
        app.mini.update()
        dx, dy = app.mini.winfo_x(), app.mini.winfo_y()
        check("界面:迷你窗被拖动后不再被拉回右上角(回归)",
              abs(dx - 700) <= 4 and abs(dy - 300) <= 4,
              f"x={dx} y={dy}")

        # 真实拖动走 gui 的两个回调(标题栏原生拖动在 DPI 感知进程里失效,
        # 见 on_mini_drag_start):抓着大字拖 60px,窗口要跟着挪
        app.on_mini_drag_start(types.SimpleNamespace(x_root=1000, y_root=500))
        app.on_mini_drag_move(types.SimpleNamespace(x_root=940, y_root=440))
        app.mini.update()
        dx, dy = app.mini.winfo_x(), app.mini.winfo_y()
        check("界面:抓着大字能拖动迷你窗(拖动回调挪窗)",
              abs(dx - 640) <= 4 and abs(dy - 240) <= 4, f"x={dx} y={dy}")
        app.on_restore()
        check("界面:还原 -> 迷你窗销毁、主窗恢复",
              app.mini is None and str(app.root.state()) == "normal")

        class FakeEngine:
            def __init__(self, a):
                self.keju_tool = types.SimpleNamespace(   # 科举那套状态在工具自己身上
                    watching=False)
                self.started = False

            def is_alive(self):
                return False

            def start(self):
                self.started = True

        with patch(Engine=FakeEngine):
            app.engine = FakeEngine(app)
            app._engine_restart_ts = 0
            app._drain_logs()
            restarted = isinstance(app.engine, FakeEngine) and app.engine.started
        check("界面:引擎线程挂掉会被看门狗自动重启", restarted)

        app.on_mini()
        app.on_mini()                     # 第二次:只把已有小窗提到前面
        check("界面:重复点迷你置顶只提升已有窗口", app.mini is not None)

        app._set_exam_mode(True)          # 与当前一致:直接返回(不重复写盘)
        app.cfg["keju"].pop("mode", None)
        app.cfg["keju"]["exam_mode"] = False
        check("界面:老配置只有 exam_mode -> 模式名按族回退",
              app._mode() == "三大学士")
        app._set("mode", "科举会试")
        app._set("exam_mode", True)
        app.mode_btn.destroy()            # 按钮没了 -> 刷新要兜住
        app._refresh_mode_btn()
        app._refresh_mode_menu()
        check("界面:模式按钮被销毁后刷新不报错", True)

        class FakeMini:
            def winfo_exists(self):
                return 1

            def winfo_reqwidth(self):
                raise RuntimeError("窗口状态异常")

            def winfo_screenwidth(self):
                return 1920

        app.mini = FakeMini()
        app._anchor_mini()
        app.mini = None
        app._anchor_mini()                # 没有小窗时直接返回
        app.on_mini()
        app.mini.destroy()                # 已被销毁:on_restore 要兜住
        app.on_restore()

        class BadMini:
            def destroy(self):
                raise RuntimeError("窗口销毁失败")

        app.mini = BadMini()
        app.on_restore()
        check("界面:小窗已销毁/销毁失败时都能正常还原主窗",
              app.mini is None and str(app.root.state()) == "normal")

        app.cfg["app"]["last_bank_check"] = ""    # 启动时已成功更新并记过账
        with patch(update_bank=raiser(OSError("断网"))):
            app._auto_bank_update()
            time.sleep(0.3)
        check("界面:启动更新题库失败也不影响使用(后台只记一声)",
              any("[题库]更新失败" in m for m in ui_logs())
              and app.cfg["app"]["last_bank_check"] == "",   # 失败不记账
              [m for m in ui_logs() if "[题库]" in m][:1])

        import datetime as _dt
        app.cfg["app"]["last_bank_check"] = _dt.date.today().isoformat()
        n_kj = len([m for m in ui_logs() if "[题库]" in m])
        app._auto_bank_update()
        time.sleep(0.2)
        check("界面:当天已经更新过,当天内再启动不再更新",
              len([m for m in ui_logs() if "[题库]" in m]) == n_kj,
              app.cfg["app"].get("last_bank_check"))

        class FakeEngineOK(FakeEngine):
            def is_alive(self):
                return True

        class DeadEngine(FakeEngineOK):
            def is_alive(self):
                return False

        class BoomEngine:
            def __init__(self, a):
                raise RuntimeError("引擎起不来")

        if not FAST:
            # 更新题库这组要反复加载真题库(约 4 秒),快速模式跳过
            with patch(update_bank=lambda: (True, "题库已更新: 1 → 2 题"),
                       count_missing_voice=lambda cfg, b: 0):
                app._bank_update_worker()
                check("界面:更新题库成功 -> 热加载 + 音频已就绪",
                      app._bank_updating is False
                      and any("题库已更新" in m for m in ui_logs()))
            with patch(update_bank=lambda: (True, "题库已更新: 1 → 2 题"),
                       count_missing_voice=lambda cfg, b: 3,
                       build_voice_cache=lambda **kw: (3, 0, 0)):
                app._bank_update_worker()
                lg = ui_logs()
                check("界面:更新后出现新答案 -> 自动补合成音频",
                      any("后台自动合成" in m for m in lg)
                      and any("合成完成" in m for m in lg), str(lg)[:150])
            with patch(update_bank=lambda: (False, "题库已是最新(5 题)")):
                app._bank_update_worker()
                check("界面:题库没变化时只提示最新",
                      any("已是最新" in m for m in ui_logs()))
            with patch(update_bank=raiser(ValueError("源不可用"))):
                app._bank_update_worker()
                check("界面:更新失败 -> 保留原题库并提示",
                      any("[题库]更新失败" in m for m in ui_logs()))

            app.cfg["app"]["last_bank_check"] = ""
            with patch(update_bank=lambda: (False, "题库已是最新(5 题)")):
                app._auto_bank_update()
                time.sleep(0.3)
            check("界面:每天首次启动更新题库,已是最新也报一声并记账",
                  any("题库已是最新" in m for m in ui_logs())
                  and app.cfg["app"]["last_bank_check"] != "")
            app.cfg["app"]["last_bank_check"] = ""
            with patch(update_bank=lambda: (True, "题库已更新: 1 → 2 题"),
                       count_missing_voice=lambda cfg, b: 0):
                n_upd = sum(1 for m in ui_logs() if "题库已更新" in m)
                app._auto_bank_update()
                time.sleep(0.4)
                check("界面:启动时发现新版本走同一套更新+热加载流程",
                      sum(1 for m in ui_logs() if "题库已更新" in m) == n_upd + 1
                      and app.cfg["app"]["last_bank_check"] != "",
                      str([m for m in ui_logs() if "[题库]" in m][-2:]))

        with patch(Engine=FakeEngine):
            app.engine = DeadEngine(app)
            app._engine_restart_ts = 0
            app._drain_logs()
            restarted = isinstance(app.engine, FakeEngine) and app.engine.started
        check("界面:引擎线程挂掉会被看门狗自动重启(第二次)", restarted)

        with patch(Engine=BoomEngine):
            app.engine = DeadEngine(app)
            app._engine_restart_ts = 0
            app._drain_logs()               # 重启失败也要兜住
        check("界面:看门狗重启失败不影响界面", True)

        # ---- 刷新链路的兜底分支 ----
        app.engine = FakeEngineOK(app)
        app.last_answer = "换了答案" + "长" * 40
        app.on_mini()
        app.mini_label.destroy()            # 标签被销毁:字号自适应该兜住
        app._drain_logs()

        class WackyEngine:
            @property
            def keju_tool(self):             # 科举那套状态读不到
                raise RuntimeError("引擎状态读不到")

            def is_alive(self):
                return True

        app.engine = WackyEngine()
        app._drain_logs()                    # 引擎对象坏掉也要把日志刷完
        app._drain_logs()                    # 第二拍:同类失败不该再刷一条
        filetxt = open(sb.log, encoding="utf-8").read()
        check("界面:刷新链兜底的失败首错记一次日志(不无声也不刷屏)",
              filetxt.count("刷新项「区域显示」") == 1
              and "刷新项「监听按钮」" not in filetxt
              and "刷新项「本局统计」" not in filetxt,
              [l for l in filetxt.splitlines() if "刷新项" in l][:3])
        check("界面:引擎状态读取异常时刷新链不断", True)
        app.engine = FakeEngineOK(app)

        app.log("日志文件写不进去时的消息")
        with patch(LOG_PATH=sb.dir):         # 把日志路径指成目录 -> 写入失败
            app.log("这条写不进文件")
        check("界面:迷你窗/统计/按钮/日志写入异常都不会影响主循环", True)
        app.on_restore()

        # ---- 预检哨:只认联网对时 —— 确认跨线一拍即停 ----
        # (探针在后台线程对时:stub net_probe,离线且确定性)
        from datetime import timedelta as _td

        def hero_texts(a):
            cv = a.hero_canvas
            return [cv.itemcget(i, "text") for i in cv.find_withtag("all")
                    if cv.type(i) == "text"]

        check("预检哨:读不到联网时间连续 3 拍也按停用处理",
              app._probe_tick(None) is False
              and app._probe_tick(None) is False
              and app._probe_tick(None) is True)

        past_net = core._ref_time() + _td(days=1)
        app._probe_ticks = gui.PROBE_TICKS - 1
        with patch(net_probe=lambda *a, **k: past_net):
            app._drain_logs()             # 起对时探针(后台线程)
            for _ in range(300):
                if not getattr(app, "_probing", False):
                    break
                time.sleep(0.01)
            app._drain_logs()             # 消费结果:确认跨线 -> 就地停用
        check("预检哨:确认跨过锚点 -> 引擎停、识别区显示停用提示",
              app.engine is None
              and any("token已用完" in t for t in hero_texts(app))
              and any("已停止识别与播报" in m for m in ui_logs()),
              hero_texts(app))

        # ---- 收尾:覆盖 App.run() 与 on_close ----
        app.engine = FakeEngineOK(app)
        app.cfg["keju"]["regions"] = [[1, 2, 3, 4]] + [None] * (core.MAX_CLIENTS - 1)
        app._engine_restart_ts = time.time()      # 收尾时别让看门狗动手
        app.root.after(200, app.on_close)
        with patch(save_config=raiser(OSError("配置写不进去"))):
            app.run()
        gone = False
        try:
            app.root.winfo_exists()
        except Exception:
            gone = True
        check("界面:关闭时即使写配置失败也能正常退出",
              gone and json.load(open(sb.config, encoding="utf-8"))["keju"]["interval"] == 0.4)
        check("界面:关闭时清空客户端坐标(下次启动重新定位)",
              app.cfg["keju"]["regions"] == [None] * core.MAX_CLIENTS,
              str(app.cfg["keju"]["regions"]))
        _tcl_drain()      # 这台 Tk 解释器也在主线程回收掉(见 _tcl_drain 的说明)
        try:
            real_engine.shutdown(timeout=3.0)   # 别把真引擎的线程留给后面的组
        except Exception:
            pass

        # ---- 预检不过启动:不开引擎,识别区常驻「token已用完」----
        # (放收尾之后:一个进程同时只活一个 Tk 根,set_icon 的 PhotoImage
        # 会绑到默认根上,前一个根还活着时第二个窗必炸)
        exp = gui.App(readonly=True)
        exp.root.withdraw()
        exp_logs = []
        exp_real_log = exp.log
        exp.log = lambda msg, detail="": (exp_logs.append(msg),
                                          exp_real_log(msg, detail))
        check("预检不过:过期启动不开引擎/语音,识别区常驻「token已用完」",
              exp.engine is None and exp.speaker is None
              and any("token已用完" in t for t in hero_texts(exp)),
              hero_texts(exp))
        exp.on_manual_scan()
        exp.on_autolocate()
        exp.on_toggle_mode()
        exp.on_toggle_mute()
        exp.on_toggle_skin()
        exp.on_mini()
        exp.on_restore()
        check("预检不过:所有按钮点击一律静默丢弃(不处理不记日志)",
              exp.engine is None and exp.speaker is None
              and exp_logs == []
              and exp._get("mode", "") == core.DEFAULT_MODE
              and bool(exp._get("exam_mode", True)) is True
              and not exp._mode_menu_open
              and bool(exp._get("mute", False)) is False
              and exp.skin == "dark"
              and getattr(exp, "mini", None) is None
              and not any(exp._get("regions") or []),
              exp_logs)
        exp._probe_ticks = gui.PROBE_TICKS - 1
        with patch(net_probe=lambda *a, **k: None):
            exp._drain_logs()             # 哨兵起探针(离线桩:读不到)
            for _ in range(300):
                if not getattr(exp, "_probing", False):
                    break
                time.sleep(0.01)
            exp._drain_logs()             # 消费结果:停用态保持,不报错
        check("预检不过:刷新链在停用态不碰引擎,状态保持",
              exp._readonly is True and exp.engine is None)
        exp.on_close()
        exp_gone = False
        try:
            exp.root.winfo_exists()
        except Exception:
            exp_gone = True
        check("预检不过:停用窗也能正常关闭(无语音线程也兜得住)", exp_gone)
        _tcl_drain()      # exp 这台 Tk 解释器同样在主线程回收掉


# --------------------------------------------------- 9 日志与统计 / 命令行

@section
def sec_cli():
    with Sandbox() as sb:
        rows = [
            "2026-01-01 10:00:00 应用启动 · 题库 4332 题",
            "2026-01-01 10:00:05 识别: 第一道题的题干内容是什么…",
            "2026-01-01 10:00:06 匹配 95% -> 第一道题… 【答案甲】| 响应 0.42s "
            "(OCR 0.21s 匹配 0.02s)",
            "2026-01-01 10:00:08 识别: 这里是御前科举大赛第1关，你作好答题的"
            "准备了吗？…",
            "2026-01-01 10:00:09 题库中未找到匹配(最高 0%)",
            "2026-01-01 10:00:10 匹配 70% -> 另一题… 【答案乙】| 响应 1.10s "
            "(OCR 0.50s 匹配 0.30s)",
            "2026-01-01 10:00:15 识别: 第一届世界杯的冠军是哪个国家？…",
            "2026-01-01 10:00:16 题库中未找到匹配(最高 72%)",
            "2026-01-01 10:00:17 题库中未找到匹配(最高 0%)(连续10次)",
            "2026-01-01 10:00:20 匹配 88% -> 老格式题… 【答案丙】| OCR 0.30s "
            "匹配 0.05s 本轮 0.90s",
            "2026-01-01 10:00:30 完全无关的一行",
            # 新格式:带 [操作] 前缀的行也要能解析
            "2026-01-01 10:00:40 [自动监听]匹配 90% -> 新格式题… 【答案丁】"
            "| 响应 0.30s (OCR 0.20s 匹配 0.01s)",
            "2026-01-01 10:00:42 [自动监听]识别: 第一届世界杯的冠军是哪个国家？…",
            "2026-01-01 10:00:43 [自动监听]题库中未找到匹配(最高 35%)",
            "2026-01-01 10:00:45 [识别模式]三大学士",
        ]
        with open(sb.log, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")

        rc, out = capture(cli.cmd_stats)
        check("cmd_stats:新/旧/带前缀三种日志格式都能解析并汇总",
              rc == 0 and "共4次" in out and "0.42" in out and "0.90" in out
              and "keju.log" in out,
              [l for l in out.splitlines() if "共4次" in l][:1])
        check("cmd_stats:命中率按题次统计(考官开场白不算题)",
              "读到题干 6 题次 → 答出 4" in out
              and "考官开场白(不算题,新版本已过滤) 1 ·" in out,
              [l for l in out.splitlines() if "读到题干" in l][:1])
        near_lines = [l for l in out.splitlines() if "疑似没答出" in l]
        check("cmd_stats:漏答里'接近的候选'单独列出(疑似真题没收录)",
              len(near_lines) == 2 and "最高 72%" in near_lines[0]
              and "最高 35%" in near_lines[1],
              near_lines[:2])
        check("cmd_stats:得分 <80 的记录被单独列出抽查",
              "低置信" in out and "答案乙" in out)
        rc2, out2 = capture(cli.cmd_stats, "23:00")
        check("cmd_stats --since:过滤掉更早的记录", rc2 == 0 and "还没有识别记录" in out2)
        rc3, out3 = capture(cli.cmd_stats, "10:00")
        check("cmd_stats --since:保留该时刻之后的记录", rc3 == 0 and "共4次" in out3)
        os.remove(sb.log)
        rc4, out4 = capture(cli.cmd_stats)
        check("cmd_stats:没有日志文件时给出提示", rc4 == 1 and "还没有日志" in out4)

    # ---- 结构化事件:识别管线直写 data/events.jsonl,--stats 首选读它 ----
    with Sandbox() as sb_ev:
        ev_bank = make_bank(sb_ev.bank, [
            {"q": "第一道题的题干内容是什么", "a": "答案甲"}])
        eng, app = mk_engine(ev_bank, [
            blk("御前科举大赛第1关：这一关考的是综合。题目：第一道题的题干内容是什么",
                100, 200)])
        eng.keju_tool.keju_cycle(True)
        with open(sb_ev.events, encoding="utf-8") as f:
            ev_rows = [json.loads(l) for l in f if l.strip()]
        check("事件:识别循环答出的题写进 events.jsonl(带得分/答案/耗时)",
              [r["kind"] for r in ev_rows] == ["match"]
              and ev_rows[0]["answer"] == "答案甲" and ev_rows[0]["score"] >= 80
              and all(k in ev_rows[0] for k in
                      ("response", "ocr", "match", "manual")), str(ev_rows))
        # 事件与旧日志并存:以事件为准,日志那份不再重复计数
        with open(sb_ev.log, "w", encoding="utf-8") as f:
            f.write("2026-01-01 09:00:00 匹配 99% -> 旧日志题… 【旧答案】"
                    "| 响应 0.10s (OCR 0.05s 匹配 0.01s)\n")
        rc, out = capture(cli.cmd_stats)
        check("cmd_stats:优先读结构化事件(并标注数据源)",
              rc == 0 and "events.jsonl" in out and "旧答案" not in out
              and "读到题干 1 题次 → 答出 1" in out,
              [l for l in out.splitlines() if "读到题干" in l][:1])

    with Sandbox() as sb2:
        rows = [
            {"ts": "2026-01-01 10:00:00", "kind": "boot"},
            {"ts": "2026-01-01 10:00:01", "kind": "mode", "mode": "三大学士"},
            {"ts": "2026-01-01 10:00:02", "kind": "nomatch", "score": 72,
             "text": "第一届世界杯的冠军是哪个国家？"},
            {"ts": "2026-01-01 10:00:03", "kind": "match", "score": 95,
             "note": " [按选项消歧]", "answer": "答案甲", "response": 0.42,
             "ocr": 0.21, "match": 0.02},
            {"ts": "2026-01-01 10:00:04", "kind": "dup"},
            {"ts": "2026-01-01 10:00:05", "kind": "blind"},
            {"ts": "2026-01-01 10:00:06", "kind": "nomatch", "score": 0,
             "text": ""},
            "坏行,不是 json",
            {"ts": "2026-01-01 10:00:07", "kind": "未知kind"},
        ]
        with open(sb2.events, "w", encoding="utf-8") as f:
            for r in rows:
                f.write((json.dumps(r, ensure_ascii=False)
                         if isinstance(r, dict) else str(r)) + "\n")
        rc, out = capture(cli.cmd_stats)
        check("cmd_stats:事件驱动分模式段(漏答/空屏/抑制/消歧都计数,坏行跳过)",
              rc == 0 and "events.jsonl" in out
              and "读到题干 3 题次 → 答出 1" in out
              and "多题消歧 1" in out and "疑似没答出" in out and "最高 72%" in out
              and "三大学士" in out, out[:200])

    # 跨午夜长跑:事件文件里可能有昨天+今天,统计只认最新一天
    with Sandbox() as sb_day:
        today = time.strftime("%Y-%m-%d")
        rows = [
            {"ts": "2020-01-01 10:00:00", "kind": "match", "score": 90,
             "note": "", "answer": "答案旧", "response": 0.40,
             "ocr": 0.20, "match": 0.01},
            {"ts": f"{today} 10:00:00", "kind": "match", "score": 90,
             "note": "", "answer": "答案新", "response": 0.30,
             "ocr": 0.20, "match": 0.01},
        ]
        with open(sb_day.events, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        rc, out = capture(cli.cmd_stats)
        check("cmd_stats:跨天的旧事件不混进当天统计(只留最新一天)",
              rc == 0 and "读到题干 1 题次" in out and "答案旧" not in out
              and "答案新" in out,
              [l for l in out.splitlines() if "读到题干" in l][:1])

    # 事件字段被手改成 null/缺字段:按 0 处理,--stats 不能崩
    with Sandbox() as sb_null:
        row = {"ts": f"{time.strftime('%Y-%m-%d')} 10:00:00", "kind": "match",
               "score": None, "answer": None, "response": None,
               "ocr": None, "match": None}
        with open(sb_null.events, "w", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        rc, out = capture(cli.cmd_stats)
        check("cmd_stats:事件字段为 null 也按 0 计,不抛异常",
              rc == 0 and "读到题干 1 题次" in out and "0.00s" in out,
              [l for l in out.splitlines() if "读到题干" in l][:1])

    check("模式:统计兼容老叫法,新名字原样保留",
          core.mode_label("普通") == "三大学士"
          and core.mode_label("御前科举") == core.DEFAULT_MODE
          and core.mode_label("车迟斗法") == "车迟斗法"
          and core.mode_label("") == core.DEFAULT_MODE)

    check("测试运行器:--only 等号/空格两种写法都能选组",
          _pick_sections(["--only=gui"]) == ("sec_gui",)
          and _pick_sections(["--only", "gui,cli"]) == ("sec_gui", "sec_cli")
          and _pick_sections([]) is None)

    keju._ensure_console()
    check("keju._ensure_console:源码运行时无副作用", sys.stdout is not None)

    argv_backup = sys.argv
    try:
        seen_seed = []
        with patch(cmd_selftest=lambda seed=None: seen_seed.append(seed) or 0,
                   cmd_stats=lambda since="": 0,
                   cmd_uncollected=lambda add=(): 0,
                   update_bank=lambda: (True, "题库已更新: 1 → 2 题"),
                   build_voice_cache=lambda *a, **k: (1, 0, 0),
                   check_deps=lambda: [], ensure_data_files=lambda: None,
                   env_ready=lambda: None):
            for flag in ("--selftest", "--stats", "--update-bank",
                         "--uncollected"):
                sys.argv = ["keju.py", flag]
                rc = keju.main()
                check(f"命令行:{flag} 正常分发", rc == 0, f"rc={rc}")
            sys.argv = ["keju.py", "--selftest", "--seed", "7"]
            keju.main()
            check("命令行:--seed 会传给自检(不传则用默认种子)",
                  seen_seed == [cli.SELFTEST_SEED, 7], str(seen_seed))
            sys.argv = ["keju.py", "--build-voice-cache", "5"]
            check("命令行:--build-voice-cache N 正常分发", keju.main() == 0)
            sys.argv = ["keju.py", "--update-bank"]
            with patch(update_bank=raiser(ValueError("源不可用"))):
                rc, out = capture(keju.main)
            check("命令行:题库更新失败返回 1 并打印原因",
                  rc == 1 and "更新失败" in out, out.strip())

        with patch(check_deps=lambda: ["numpy"], ensure_data_files=lambda: None), \
                patch(mods=(keju,), _single_instance=lambda: True):
            sys.argv = ["keju.py"]
            rc, out = capture(keju.main)
            check("命令行:缺依赖时给安装提示并返回 1",
                  rc == 1 and "缺少依赖" in out, out.splitlines()[0] if out else "")

        with patch(check_deps=lambda: [], ensure_data_files=lambda: None,
                   BANK_PATH=os.path.join(tempfile.gettempdir(),
                                          "keju_没有这个题库.json")), \
                patch(mods=(keju,), _single_instance=lambda: True):
            sys.argv = ["keju.py"]
            rc, out = capture(keju.main)
            check("命令行:题库文件缺失时返回 1",
                  rc == 1 and "题库文件不存在" in out,
                  out.splitlines()[0] if out else "")
    finally:
        sys.argv = argv_backup

    import subprocess as _sp
    here = os.path.dirname(os.path.abspath(core.__file__))
    if FAST:                 # 下面两项要起独立进程(约 1.5 秒),快速模式跳过
        return
    r = _sp.run([sys.executable, "keju.py", "--stats", "--since", "23:59"],
                capture_output=True, text=True, cwd=here, timeout=60)
    check("命令行:独立进程真跑一次 keju.py --stats(入口可用)",
          r.returncode in (0, 1)
          and ("还没有识别记录" in r.stdout or "共" in r.stdout),
          (r.stdout or r.stderr).strip()[:40])

    blocker = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n == 'numpy' or n.startswith('numpy.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n")
    blocked = blocker + (
        "import core\n"
        "assert core.np is None, core.np\n"
        "assert isinstance(core.check_deps(), list)\n"
        "print('OK')\n")
    r2 = _sp.run([sys.executable, "-c", blocked], capture_output=True, text=True,
                 cwd=here, timeout=60)
    check("核心:import 时缺 numpy 也能干净降级(np=None + 依赖检查可用)",
          r2.stdout.strip() == "OK", (r2.stdout + r2.stderr).strip()[:60])

    blocked_entry = blocker + "import keju\nprint('OK')\n"
    r3 = _sp.run([sys.executable, "-c", blocked_entry], capture_output=True,
                 text=True, cwd=here, timeout=60)
    check("入口:缺 numpy 时 import keju 不炸(能走到友好的缺依赖提示)",
          r3.stdout.strip() == "OK", (r3.stdout + r3.stderr).strip()[:80])

    # ---- 环境预检闸门:过了 core 锚点停用,只认联网对时 ----
    from datetime import timedelta as _td

    deadline = core._ref_time()
    check("预检:锚点日期钉死(年月日分散埋放,防改错分量)",
          deadline == core.datetime(2026, 12, 1, tzinfo=core.timezone(
              core.timedelta(hours=8))), str(deadline))
    check("预检:对时源清单钉死(防字节拼装笔误)",
          core._PEERS == ("ntp.aliyun.com", "ntp.tencent.com")
          and core._PEER_IP == "106.55.184.199"
          and core._SITES == ("https://www.baidu.com",
                              "https://www.taobao.com"))
    before, past = deadline - _td(days=1), deadline + _td(days=1)
    cases = [
        (past, True, "联网对到已过期停"),
        (before, False, "联网没到截止放行"),
        (None, True, "读不到联网时间按过期处理(fail-closed)"),
    ]
    for net, blocked, name in cases:
        hit, now = core.env_state(net)
        check(f"env_state:{name}", hit is blocked, f"hit={hit} now={now}")
    from datetime import timezone as _tz
    check("预检:截止固定按北京时间(UTC+8),与时区设置无关",
          core._ref_time().utcoffset() == _td(hours=8))
    shifted = past.astimezone(_tz(_td(hours=-11)))
    check("预检:对时结果带任意时区也按时刻比较(拨时区无效)",
          core.env_state(shifted)[0] is True
          and core.env_state(shifted)[1] == past)

    with Sandbox(), patch(net_probe=lambda *a, **k: None):
        check("env_ready:对不到联网时间 -> 按停用处理返回停用文案",
              core.env_ready() == "token已用完")
    fake_net = deadline - _td(hours=2)
    with Sandbox(), patch(net_probe=lambda *a, **k: fake_net):
        check("env_ready:联网没到截止 -> 放行并记住对时结果",
              core.env_ready() is None
              and core._parse_ts(core.load_config()["app"]["last_seen"])
              == fake_net)

    with Sandbox() as sb_tk, patch(net_probe=lambda *a, **k: None), \
            patch(check_deps=lambda: [], ensure_data_files=lambda: None), \
            patch(cmd_selftest=lambda seed=None: 0, cmd_stats=lambda since="": 0), \
            patch(mods=(keju,), _single_instance=lambda: True):
        make_bank(sb_tk.bank, [{"q": "一加一等于几", "a": "二"}])
        argv_backup = sys.argv
        try:
            sys.argv = ["keju.py", "--build-voice-cache"]
            rc, out = capture(keju.main)
            check("预检:预检不过用量型命令被拦(--build-voice-cache)",
                  rc == 1 and "token已用完" in out, out.strip())
            fakes = []

            class _NoApp:
                def __init__(self, readonly=False):
                    fakes.append(readonly)

                def run(self):
                    pass

            sys.argv = ["keju.py"]
            with patch(mods=(keju,), App=_NoApp):
                rc = keju.main()
            check("预检:预检不过 GUI 照常开窗,以停用态启动(不弹框不退场)",
                  rc == 0 and fakes == [True], f"rc={rc} fakes={fakes}")
            sys.argv = ["keju.py", "--stats"]
            check("预检:预检不过 --stats 照常放行(诊断不拦)",
                  keju.main() == 0)
            sys.argv = ["keju.py", "--selftest"]
            check("预检:预检不过 --selftest 照常放行(打包 QA 要用)",
                  keju.main() == 0)
        finally:
            sys.argv = argv_backup


def official_get(keju_n=600, extra="", bounds=True):
    """伪造官方源:app.js + 数据块(默认 8733=科举)。"""
    body = ",".join('{Id:"%d",Q:"科举题%d？",A:"科举答案%d"}' % (i, i, i)
                    for i in range(keju_n))
    src = ('8733:function(){var x=[%s%s]},' % (body, extra)) if bounds \
        else ("var data=[%s%s];" % (body, extra))

    def get(url, timeout=30):
        if "app." in url:
            return b'306:"deadbeef"'
        if "exp." in url:
            return src.encode("utf-8")
        return b'<script src="static/js/app.abc12345.js"></script>'
    return get


# ------------------------------------------------- 10 分支补漏(覆盖率驱动)

@section
def sec_extra():
    import ctypes as _ct
    import subprocess as _sp

    tmp = tempfile.mkdtemp(prefix="keju_bank_")
    try:
        bank = make_bank(os.path.join(tmp, "b.json"), [
            {"q": "第一道题的题干内容是什么", "a": "答案甲"},
            {"q": "今天天气晴朗适合出门散步吗", "a": "答案乙"},
        ])
        real_bank = QuestionBank()
        QA = "第一道题的题干内容是什么"
        HDR = "御前科举大赛第1关：这一关考的是综合。题目："

        # ---- 轮询间隔 ----
        eng, _ = mk_engine(bank, [])
        eng.app.cfg = {"keju": {"interval": 0.4}}
        eng.keju_tool._last_announce_ts = time.time()
        fast = eng.keju_tool._poll_interval()
        eng.keju_tool._last_announce_ts = time.time() - 30
        slow = eng.keju_tool._poll_interval()
        eng.app.cfg = {"keju": {"interval": 0.05}}    # 手改/历史配置
        eng.keju_tool._last_announce_ts = time.time()
        f2 = eng.keju_tool._poll_interval()
        eng.keju_tool._last_announce_ts = time.time() - 30
        s2 = eng.keju_tool._poll_interval()
        check("轮询:刚播报 0.1s 快扫 / 空闲按设置 / 低于下限被兜住",
              abs(fast - 0.1) < 1e-9 and abs(slow - 0.4) < 1e-9
              and abs(f2 - 0.1) < 1e-9 and abs(s2 - 0.4) < 1e-9,
              f"{fast}/{slow} 与 {f2}/{s2}")

        # ---- cycle 的边界路径 ----
        class BoomScreen:
            def grab(self, region=None):
                raise OSError("截图接口被占用")

        eng, app = mk_engine(bank, [])
        eng.screen = BoomScreen()
        eng.keju_tool.keju_cycle(True)
        check("cycle:区域截图失败 -> 记日志并安全退出(不崩)",
              any("截图失败" in m for m in app.logs))

        eng, app = mk_engine(bank, [], cfg={"keju": {"exam_mode": False}})
        eng.ocr = types.SimpleNamespace(recognize_blocks=lambda img, **kw: [])
        eng.keju_tool.keju_cycle(True)
        check("cycle:区域里一个字都没识别到 -> 本轮跳过", not app.said)

        eng, app = mk_engine(bank, [blk("短", 100, 200)],
                             cfg={"keju": {"exam_mode": False}})
        eng.keju_tool.keju_cycle(True)
        check("cycle:三大学士模式下的碎片文本(<4 字)-> 跳过", not app.said)

        eng, app = mk_engine(bank, [blk("下列哪种说法完全不对？", 100, 200)],
                             cfg={"keju": {"exam_mode": False}})
        eng.keju_tool.keju_cycle(True)
        check("cycle:区域文字与题库对不上 -> 记未匹配(不播报)",
              not app.said and any("未找到匹配" in m for m in app.logs),
              [m for m in app.logs if "未找到匹配" in m][:1])
        check("cycle:手动识别未匹配 -> 诊断摆上题目卡(界面无日志框)",
              str(getattr(app, "last_answer", "")).startswith("未匹配"),
              getattr(app, "last_answer", ""))

        # 选项兜底(2026-11-12 车迟斗法实况):题干读脏只有 56~78 分,
        # 唐/朝两变体的答案(十画/十二画)都在屏上四选项里 —— 光按分数
        # 会选错唐,靠题干引号字"朝"唯一对上十二画
        jb_bank = make_bank(os.path.join(tmp, "jb.json"), [
            {"q": "唐朝的“唐”字由多少笔画组成？", "a": "十画"},
            {"q": "唐朝的“朝”字由多少笔画组成？", "a": "十二画"},
        ])

        def jb_blocks(opts):
            texts = ["424/1200", "剩余", "有个道士", "时48分钟", "法",
                     "由多少笔画组成？", "唐朝的“朝”"] + opts
            return [blk(t, 0, 400 + i * 20) for i, t in enumerate(texts)]

        eng, app = mk_engine(jb_bank, jb_blocks(
                ["九画", "十一画", "十画", "十二画"]),
            cfg={"keju": {"exam_mode": False}})
        eng.keju_tool.keju_cycle(True)
        check("cycle:选项兜底:两变体答案都在屏 -> 引号字判别选对(十二画)",
              app.said == ["十二画"]
              and any("选项兜底" in m for m in app.logs),
              f"{app.said} {[m for m in app.logs if '匹配' in m][:1]}")

        eng, app = mk_engine(jb_bank, jb_blocks(["一画", "二画", "三画", "四画"]),
            cfg={"keju": {"exam_mode": False}})
        eng.keju_tool.keju_cycle(True)
        check("cycle:选项兜底:答案不在当屏选项 -> 不救(宁可不报)",
              not app.said, app.said)

        # 多开:一个客户端没答出,同一轮里其余客户端照常识别(不被挡一轮)
        class SeqScreen:
            def grab(self, region=None):
                return np.zeros((30, 60, 3), np.uint8)

        eng, app = mk_engine(bank, [], cfg={"keju": {"regions": [
            [0, 0, 60, 30], [100, 100, 160, 130]]}})
        seen = {"n": 0}

        def _two_ocr(img):
            seen["n"] += 1
            if seen["n"] == 1:
                return [blk("完全无关的一段文字内容", 100, 200)]   # 1 号:没匹配
            return [blk(HDR + QA, 100, 200)]                      # 2 号:真题
        eng.screen = SeqScreen()
        eng.ocr = types.SimpleNamespace(recognize_blocks=_two_ocr)
        eng.keju_tool.keju_cycle(False)
        check("多开:一个区域没匹配不挡同轮其他客户端(一轮看完全部区域)",
              app.said == ["答案甲"] and seen["n"] == 2,
              f"{app.said} OCR {seen['n']} 次")

        eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
        eng.keju_tool.keju_cycle(False)               # 首帧识别
        eng.keju_tool.keju_cycle(False)               # 同一画面:被帧门控挡住
        check("cycle:画面没变时第二次直接跳过", app.said == ["答案甲"])

        class BadWarmOcr:
            def warmup(self):
                raise RuntimeError("模型文件损坏")

        app2 = types.SimpleNamespace()
        app2.cfg = core.split_config({"regions": [None] * core.MAX_CLIENTS,
                                      "interval": 0.4, "exam_mode": True})
        app2.logs = []
        app2.log = lambda msg: app2.logs.append(msg)
        app2.bank = bank
        app2.speaker = types.SimpleNamespace(say=lambda t: None)
        e2 = core.Engine(app2)
        e2.ocr = BadWarmOcr()
        with patch(Screen=lambda: FakeScreen()):
            e2.start()
            e2.join(timeout=5)
        check("Engine.run:OCR 引擎初始化失败 -> 记日志并退出线程",
              not e2.is_alive()
              and any("[OCR]引擎初始化失败" in m for m in app2.logs))

        # ---- 任务循环:监听中 / 空闲时收到手动任务都要立刻执行 ----
        class RunOcr:
            def __init__(self, blocks):
                self.blocks = blocks
                self.n = 0

            def warmup(self):
                pass

            def recognize_blocks(self, img):
                self.n += 1
                return self.blocks

        app3 = types.SimpleNamespace()
        app3.cfg = core.split_config({"regions": [[0, 0, 60, 30]],
                                      "exam_mode": True,
                                      "interval": 0.4})
        app3.bank = bank
        app3.said = []
        app3.last_answer = ""
        app3.logs = []
        app3.log = lambda msg, detail="": app3.logs.append(msg)
        app3.speaker = types.SimpleNamespace(say=app3.said.append)
        # 枚举桩给一个真在场的支持客户端(空枚举会被跟随当"已关闭"移除
        # 观测区域,循环就没得扫了 —— 移除本身是新行为,见跟随组用例)
        e3_win = ("梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 角色庚[7])",
                  [0, 0, 640, 480])
        e3_canv = [(e3_win[0], (640, 480), [0, 0, 640, 480], 1.0, "win")]
        # 真 Engine 会造真 KejuTool(连带真选项仓库):OPTIONS_PATH 必须
        # 在 Engine 构造**之前**就指到临时文件 —— 构造后再 patch 只改了
        # 模块属性,实例已经攥着真实路径不放,夹具选项照样学进真文件
        with patch(mods=(tools_keju,), OPTIONS_PATH=os.path.join(
                tmp, "e3_options.json")), \
                patch(Screen=lambda: FakeScreen()), \
                patch(find_client_windows=lambda *a, **k: ([e3_win], [])), \
                patch(find_client_canvases=lambda *a, **k: (list(e3_canv), [])):
            e3 = core.Engine(app3)
            e3.ocr = RunOcr([blk(HDR + QA, 100, 200)])
            e3.start()
            time.sleep(0.4)
            e3.set_watch(True)                  # 监听中的 watch 任务
            time.sleep(0.3)
            e3.request_cycle()                  # 监听中手动识别
            time.sleep(0.3)
            e3.set_watch(False)
            time.sleep(0.4)
            e3.request_cycle()                  # 空闲时手动识别
            time.sleep(0.5)
        check("任务循环:监听中/空闲时的手动任务都被执行",
              set(app3.said) == {"答案甲"} and len(app3.said) >= 2
              and any("[自动监听]已开启" in m for m in app3.logs),
              f"播报 {app3.said}")

        # ---- 语音底层:MCI 命令序列 / 合成失败 ----
        mci_calls = []

        class OkWinmm:
            def mciSendStringW(self, cmd, buf, n, h):
                mci_calls.append(cmd)
                return 0

        real_windll = _ct.windll            # 函数内部是 from ctypes import ...,要改真模块
        try:
            _ct.windll = types.SimpleNamespace(winmm=OkWinmm())
            voice._play_mp3_blocking("x.mp3")
        finally:
            _ct.windll = real_windll
        check("_play_mp3_blocking:正常走 open→play wait→close",
              len(mci_calls) == 3 and "open" in mci_calls[0]
              and "play" in mci_calls[1] and "close" in mci_calls[2],
              str(mci_calls[:1]))

        # 给了打断回调:非阻塞播放 + 轮询,判定要停就发 stop 再 close
        int_calls = []

        class LongWinmm:
            def mciSendStringW(self, cmd, buf, n, h):
                int_calls.append(cmd)
                if cmd.startswith("status"):
                    buf.value = "playing"
                return 0

        polls = [0]

        def want_stop():
            polls[0] += 1
            return polls[0] > 2

        try:
            _ct.windll = types.SimpleNamespace(winmm=LongWinmm())
            voice._play_mp3_blocking("x.mp3", want_stop)
        finally:
            _ct.windll = real_windll
        check("_play_mp3_blocking:播到一半判定要打断 -> 立刻 stop 收声",
              int_calls[-1] == "close ttsclip"
              and "stop ttsclip" in int_calls
              and not any("wait" in c for c in int_calls),
              str(int_calls))

        fail_calls = []

        class FailWinmm:
            def mciSendStringW(self, cmd, buf, n, h):
                fail_calls.append(cmd)
                return 1                        # MCI 非 0 = 失败

        raised = False
        try:
            _ct.windll = types.SimpleNamespace(winmm=FailWinmm())
            try:
                voice._play_mp3_blocking("x.mp3")
            except RuntimeError:
                raised = True
        finally:
            _ct.windll = real_windll
        check("_play_mp3_blocking:MCI 报错时抛异常(由播报层兜住)",
              raised and fail_calls)

        real_run = _sp.run
        try:
            _sp.run = raiser(OSError("没有 edge-tts"))
            got = core._synth_edge_file("文本", "zh-CN-YunxiNeural", 0, 0,
                                        os.path.join(tmp, "x.mp3"),
                                        timeout=1, retries=0)
        finally:
            _sp.run = real_run
        check("_synth_edge_file:子进程失败 -> 不抛异常,返回 False", got is False)

        import importlib.util as _iu
        real_spec = _iu.find_spec
        try:
            _iu.find_spec = raiser(RuntimeError("坏掉的安装"))
            missing = core.check_deps()
        finally:
            _iu.find_spec = real_spec
        check("check_deps:探测异常时按'缺失'处理",
              len(missing) == len(core.REQUIRED_MODULES), str(missing[:2]))

        # ---- _apply 的各种坏情况 ----
        spy = core.Speaker({}, lambda m: None)
        spy._apply()
        check("_apply:语音引擎还没起来时直接返回", spy.voice_name == "")

        class BadRate:
            def __setattr__(self, k, v):
                raise RuntimeError("属性不可写")

        spy2 = core.Speaker({}, lambda m: None)
        spy2._tts = BadRate()
        spy2._apply()
        check("_apply:设不了音调/音量、取不到声音列表 -> 退到默认音色",
              spy2.voice_name == "默认", spy2.voice_name)

        class ZeroVoices:
            class _V:
                Count = 0

            def GetVoices(self):
                return self._V()

        spy3 = core.Speaker({}, lambda m: None)
        spy3._tts = ZeroVoices()
        spy3._apply()
        check("_apply:系统里一个语音都没有 -> 记为默认", spy3.voice_name == "默认")

        logs4 = []
        sp4 = core.Speaker({"app": {"voice_hint": "huihui"}}, logs4.append)
        sp4.start()
        sp4._ready.wait(10)
        sp4.quit()
        check("_apply:音色关键字命中系统语音时直接选中",
              bool(sp4.voice_name) and "huihui" in sp4.voice_name.lower(),
              sp4.voice_name)

        with patch(ZH_VOICE_KEYS=("绝不存在的音色",)):
            logs5 = []
            sp5 = core.Speaker({}, logs5.append)
            sp5.start()
            sp5._ready.wait(10)
            sp5.quit()
            check("_apply:认不出中文语音时选中第 0 个并给出提示",
                  bool(sp5.voice_name)
                  and any("未找到中文语音" in m for m in logs5),
                  f"{sp5.voice_name} {logs5[:1]}")

        sp6 = core.Speaker({}, lambda m: None)
        sp6._ready = types.SimpleNamespace(wait=lambda timeout=10: False)
        sp6.say("甲")
        check("Speaker:引擎未就绪时 say() 给出提示并返回", True)

        # ---- 语音线程里的两条播报通道 ----
        logs_sapi = []
        sp_sapi = core.Speaker({"app": {"tts_backend": "sapi"}},
                               logs_sapi.append)
        sp_sapi.start()
        sp_sapi._ready.wait(10)

        class FakeTts:
            spoke = None

            def Speak(self, text, flags=0):
                self.spoke = text

            def WaitUntilDone(self, ms):
                return True

            def __setattr__(self, k, v):
                if k == "spoke":
                    object.__setattr__(self, k, v)
                else:
                    raise RuntimeError("只读")

        sp_sapi._tts = FakeTts()
        sp_sapi.say("甲")
        time.sleep(0.4)
        check("Speaker:tts_backend=sapi 时走系统语音",
              sp_sapi._tts.spoke == "甲", str(sp_sapi._tts.spoke))
        sp_sapi.quit()

        # ---- 长答案播到一半来了下一题:旧答案立刻收声(不等念完) ----
        logs_int = []
        sp_int = core.Speaker({"app": {"tts_backend": "sapi"}},
                              logs_int.append)
        sp_int.start()
        sp_int._ready.wait(10)

        class BlockingTts:
            """模拟 SpVoice 异步播报:Speak(text,1) 开始,Speak("",2) 清掉。"""

            def __init__(self):
                self.calls = []
                self.speaking = ""

            def Speak(self, text, flags=0):
                self.calls.append((text, flags))
                self.speaking = "" if flags == 2 else text

            def WaitUntilDone(self, ms):
                return not self.speaking

        btn = BlockingTts()
        sp_int._tts = btn
        sp_int.say("很长很长的一条答案")
        time.sleep(0.15)                 # 确认它已经在念
        sp_int.say("短答案")
        time.sleep(0.3)
        check("Speaker:播报中来新答案 -> 立刻清掉旧答案(不排队等它念完)",
              ("很长很长的一条答案", 1) in btn.calls
              and ("", 2) in btn.calls
              and btn.calls[-1] == ("短答案", 1)
              and any("已打断" in m for m in logs_int),
              str(btn.calls))
        btn.speaking = ""                # 让第二条自然播完,线程好退出
        sp_int.quit()
        time.sleep(0.3)

        class LongTts:
            def __init__(self):
                self.calls = []

            def Speak(self, text, flags=0):
                self.calls.append((text, flags))

            def WaitUntilDone(self, ms):
                return False             # 一直"还在念"

        sp_purge = core.Speaker({"app": {"tts_backend": "sapi"}},
                                lambda m: None)
        sp_purge._tts = LongTts()
        sp_purge._speak_sapi("很长的答案", lambda: True)
        check("_speak_sapi:判定要打断时发 purge 并返回",
              sp_purge._tts.calls == [("很长的答案", 1), ("", 2)],
              str(sp_purge._tts.calls))

        logs_edge = []
        sp_edge = core.Speaker({"app": {"tts_backend": "edge"}},
                               logs_edge.append)
        sp_edge.start()
        sp_edge._ready.wait(10)
        sp_edge._speak_edge = lambda text, stale=None: True   # 不真的合成/播放
        sp_edge.say("乙")
        time.sleep(0.4)
        sp_edge.quit()
        check("Speaker:tts_backend=edge 命中缓存时走云端通道",
              any("播报完成" in m for m in logs_edge), str(logs_edge[:1]))

        # ---- 匹配的空输入 ----
        check("match/match_all:空输入直接返回",
              real_bank.match("。。。", 62) == (None, 0.0)
              and real_bank.match_all("", 62) == [])
        check("match_all:短文本走严格整串,单字不返回候选",
              real_bank.match_all("是", 62) == [])
        # closest:失败路径诊断用,不限阈值也要给出"最接近的分数"
        cnear = real_bank.closest("第一届世界杯的冠军是哪个国家？")
        check("closest:没收录的题也能给出最接近的分数(便于补题库)",
              0 < cnear < 80, f"{cnear:.0f}%")
        check("closest:空输入/纯符号返回 0", real_bank.closest("") == 0.0)

        # ---- 题库源的更多分支 ----
        with patch(_http_get=official_get()):
            ents, meta = bank_mod.fetch_official_bank(["keju"])
        check("官方源:正常取到题库", len(ents) == 600 and meta["chunk_hash"] == "deadbeef")

        with patch(_http_get=official_get(bounds=False)):
            ents2, _ = bank_mod.fetch_official_bank(["keju"])
            check("官方源:认不出模块边界时退化为全文扫描",
                  len(ents2) == 600 and all(e["activity"] == "keju" for e in ents2),
                  f"{len(ents2)} 条")

        bad = (',{Id:"1",Q:"",A:"空题干"},'
               ',{Id:"2",Q:"重复题？",A:"答案X"}'
               ',{Id:"3",Q:"重复题？",A:"答案X"}'
               ',{Id:"4",Q:"单字",A:""}')
        with patch(_http_get=official_get(extra=bad)):
            ents3, _ = bank_mod.fetch_official_bank(["keju"])
            check("官方源:空题干/空答案被滤掉、重复记录只留第一条",
                  len(ents3) == 601
                  and sum(1 for e in ents3 if e["question"] == "重复题？") == 1
                  and all(e["answer"] and e["question"] for e in ents3),
                  f"{len(ents3)} 条")

        def exp_fail(url, timeout=30):
            if "exp." in url:
                raise OSError("数据块下载失败")
            if "app." in url:
                return b'306:"deadbeef"'
            return b'<script src="static/js/app.abc12345.js"></script>'

        with patch(_http_get=exp_fail):
            try:
                bank_mod.fetch_official_bank()
                block_fail = False
            except ValueError as e:
                block_fail = "数据块下载失败" in str(e)
            check("官方源:数据块下载失败抛错(交给备源)", block_fail)

        def tiny_gh(path, timeout=30):
            if path.endswith("keju_questions.json"):
                return json.dumps([{"question": "少", "answer": "少"}]).encode()
            return json.dumps({"generation_id": "g1"}).encode()

        with patch(_gh_raw=tiny_gh):
            try:
                bank_mod.fetch_github_bank()
                too_small = False
            except ValueError:
                too_small = True
            check("镜像备源:条数过少时放弃", too_small)

        real_cls = bank_mod.QuestionBank
        try:
            with Sandbox() as sb, patch(_http_get=official_get()):
                os.makedirs(sb.meta)                 # meta 路径是个目录 -> 写不进去
                bank_mod.QuestionBank = lambda path=None: real_cls(path or sb.bank)
                ok, msg = bank_mod.update_bank()
                check("更新题库:meta 写失败也不影响题库本体更新",
                      ok and os.path.exists(sb.bank), msg)
        finally:
            bank_mod.QuestionBank = real_cls

        with patch(FONT_CANDIDATES=[]):
            check("_find_font:没有候选字体文件时退回默认字体",
                  cli._find_font(20) is not None)

        long_q = "这是一道非常长的题目" * 40
        img = cli._render_question(long_q)
        check("_render_question:长题目自动折行",
              img.size[0] == 760 and img.size[1] > 300, f"{img.size}")
        fs = cli._render_fullscreen([long_q])
        check("_render_fullscreen:长题目折行并撑高画布",
              fs.size[1] > 820, f"{fs.size}")

        # ---- 题库里"同题干不同答案"的组:逐个答案过一遍消歧 ----
        groups = {}
        for e in real_bank.entries:
            groups.setdefault(e["n"], []).append(e)
        amb = {k: v for k, v in groups.items() if len({x["a"] for x in v}) > 1}

        def opt_blocks(items):
            texts = (list(items) + ["甲", "乙", "丙", "丁"])[:4]
            return [blk(f"{chr(65 + i)} {t}", 100 + (i % 2) * 320,
                        340 + (i // 2) * 45) for i, t in enumerate(texts)]

        def one_cycle(blocks):
            e_, a_ = mk_engine(real_bank, blocks)
            e_.keju_tool.keju_cycle(True)
            return a_

        n_ans = sum(len({m["a"] for m in v}) for v in amb.values())
        silent, wrong, both = [], [], []
        for key, members in amb.items():
            answers = list(dict.fromkeys(m["a"] for m in members))
            qb = blk(HDR + members[0]["q"], 100, 200)
            a1 = one_cycle([qb])
            if a1.last_answer not in answers:
                silent.append(f"{key[:10]}→{a1.last_answer or '未播报'}")
            for ans in answers:
                a2 = one_cycle([qb] + opt_blocks([ans]))
                if (a2.last_answer != ans
                        or not any("按选项消歧" in m for m in a2.logs)):
                    wrong.append(f"{key[:10]} 期望{ans} 得{a2.last_answer or '未播报'}")
            a3 = one_cycle([qb] + opt_blocks(answers))
            if a3.last_answer not in answers:
                both.append(f"{key[:10]}→{a3.last_answer or '未播报'}")
        check(f"歧义题干:未框到选项时仍作答({len(amb)} 组)", not silent,
              "; ".join(silent))
        check(f"歧义题干:框到选项时按选项消歧({len(amb)} 组 / {n_ans} 个答案)",
              not wrong, "; ".join(wrong))
        check(f"歧义题干:候选答案全在选项里时仍作答({len(amb)} 组)", not both,
              "; ".join(both))

        check("cmd_selftest:默认种子固定(体检结果可复现)",
              cli.cmd_selftest.__defaults__ == (cli.SELFTEST_SEED,)
              and cli.SELFTEST_SEED > 0)

        if not FAST:      # 端到端自检约 7 秒,快速模式跳过
            rc, out = capture(cli.cmd_selftest)
            check("cmd_selftest:端到端自检(默认固定种子)全部通过", rc == 0,
                  [l for l in out.splitlines() if l.startswith("[FAIL]")] or "无失败项")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 匹配下限与低置信防护(误报:通用句式尾巴把不相干的题拉进来)----
    eng, _ = mk_engine(bank, [])
    eng.app.cfg = {"app": {"threshold": 95}}  # 旧配置项已废弃,不该再影响判定
    check(f"匹配下限:固定 {tools_keju.MIN_MATCH_PCT}%(旧 threshold 配置项已清掉)",
          eng.keju_tool._match_floor() == float(tools_keju.MIN_MATCH_PCT)
          and "threshold" not in core.DEFAULT_CONFIG
          and all("threshold" not in p for p in core.load_config().values()),
          f"{eng.keju_tool._match_floor()}")

    # “疑似未收录”清单:去重 + 按分数降序 + 给出补题格式
    with Sandbox() as sb_u:
        path_u = os.path.join(sb_u.dir, "uncollected.jsonl")
        with open(path_u, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-11 12:00:01", "score": 62,
                                "text": "科举大赛的第一阶段是",
                                "guess_q": "科举大赛的第一阶段是什么",
                                "guess_a": "乡试"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"ts": "2026-10-11 12:00:02", "score": 71,
                                "text": "曲径通幽处，禅房花木深。这句诗的作者是谁？",
                                "options": "韩愈 李顾 陈子昂 常建",
                                "guess_q": "《管子》的作者是谁？",
                                "guess_a": "管仲"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"ts": "2026-10-11 14:00:00", "score": 66,
                                "text": "科举大赛的第一阶段是",
                                "guess_q": "科举大赛的第一阶段是什么",
                                "guess_a": "乡试"}, ensure_ascii=False) + "\n")
            f.write("坏行,不是 json\n")
        with patch(UNCOLLECTED_PATH=path_u):
            rc_u, out_u = capture(cli.cmd_uncollected)
        lines = [l for l in out_u.splitlines() if "%" in l and "↳" not in l]
        check("--uncollected:去重、按分数降序、坏行跳过",
              rc_u == 0 and len(lines) == 2 and "71%" in lines[0]
              and "曲径通幽处" in lines[0] and "待确认 2 条" in out_u,
              f"{len(lines)} 条")
        rec_line = [l for l in out_u.splitlines() if "选项:" in l]
        check("--uncollected:把当屏选项一起列出来(不用回翻日志)",
              len(rec_line) == 1 and "常建" in rec_line[0],
              rec_line[:1])
    with patch(UNCOLLECTED_PATH=os.path.join("不存在的目录", "x.jsonl")):
        rc_n, out_n = capture(cli.cmd_uncollected)
        check("--uncollected:没有记录时给提示", rc_n == 0 and "还没有记录" in out_n)

    # 静音:一次不改识别,只让 Speaker 不出声
    sp_mute = core.Speaker({"app": {"mute": True}}, lambda m: None)
    sp_mute.say("铁观音")
    sp_on = core.Speaker({"app": {"mute": False}}, lambda m: None)
    sp_on._ready = types.SimpleNamespace(wait=lambda timeout=10: True)
    sp_on.say("铁观音")
    check("静音:开启时 say() 直接丢弃,关闭时照常入队",
          sp_mute.q.empty() and not sp_on.q.empty())

    read_text = "曲径通幽处，禅房花木深。这句诗的作者是谁？韩愈李顾陈子昂常建"
    with Sandbox() as sb_conf:
        conf_bank = make_bank(os.path.join(sb_conf.dir, "conf.json"),
                              [{"q": "《管子》的作者是谁？", "a": "管仲"}])
        path = os.path.join(sb_conf.dir, "uncollected.jsonl")
        with patch(UNCOLLECTED_PATH=path):
            eng, app = mk_engine(conf_bank, [blk(read_text, 100, 200)],
                                 cfg={"keju": {"exam_mode": False}})
            eng.keju_tool.keju_cycle(True)
            tips = [m for m in app.logs if "低置信" in m]
        rec = (open(path, encoding="utf-8").read().strip().splitlines()
               if os.path.exists(path) else [])
        check("低置信:像题目但分数不够 -> 不播报,只在界面提示",
              not app.said and bool(tips), str(tips[:1]))
        check(f"低置信:同时记进“疑似未收录”清单({len(rec)} 条)",
              len(rec) == 1 and "曲径通幽处" in rec[0], str(rec[:1])[:60])
        with open(sb_conf.events, encoding="utf-8") as f:
            ev_rows = [json.loads(l) for l in f if l.strip()]
        check("低置信:同时写一条 low 事件 + 一条 nomatch 事件(--stats 数据源)",
              any(r["kind"] == "low" and "曲径通幽处" in r.get("text", "")
                  for r in ev_rows)
              and any(r["kind"] == "nomatch" for r in ev_rows), str(ev_rows)[:90])

    with Sandbox() as sb_short_u:
        short_u_bank = make_bank(
            os.path.join(sb_short_u.dir, "su.json"),
            [{"q": "“归宁”指的是什么？", "a": "女子回娘家"}])
        short_u_path = os.path.join(sb_short_u.dir, "uncollected.jsonl")
        with patch(UNCOLLECTED_PATH=short_u_path):
            tip_s = tools_keju.note_uncollected(short_u_bank, "“归宁”指的是")
        rec_s = (open(short_u_path, encoding="utf-8").read()
                 if os.path.exists(short_u_path) else "")
        check("低置信:4~7 字的短题也会进待确认清单(门槛跟答题一致)",
              bool(tip_s) and "归宁" in rec_s, str(tip_s)[:40])

    check("低置信:玩家名/任务追踪这类噪声不记录",
          tools_keju.note_uncollected(conf_bank, "吻温静橙蓝色情海帮众莫亚胜20号") == "")
    check("低置信:和题库毫不沾边的句子也不记录",
          tools_keju.note_uncollected(conf_bank, "完全无关的一段文字内容") == "")

    # 锚点模式下漏答:当屏的选项也要一起记进"待确认"清单
    with Sandbox() as sb_opt:
        obank = make_bank(os.path.join(sb_opt.dir, "opt.json"),
                          [{"q": "《静夜思》的作者是谁？", "a": "李白"}])
        opt_path = os.path.join(sb_opt.dir, "uncollected.jsonl")
        hdr_o = "御前科举大赛第9关：这一关考的是综合。题目："
        opt_blocks = [
            blk(hdr_o + "曲径通幽处，禅房花木深。这句诗的作者是谁？",
                100, 200, h=30),
            blk("韩愈   李顾   陈子昂   常建", 100, 400, h=34)]
        with patch(UNCOLLECTED_PATH=opt_path):
            eng_o, app_o = mk_engine(obank, opt_blocks)
            eng_o.keju_tool.keju_cycle(True)
        opt_rec = (json.loads(open(opt_path, encoding="utf-8").read()
                             .strip().splitlines()[-1])
                   if os.path.exists(opt_path) else {})
        check("未收录:锚点模式下把题干和当屏选项都记下来",
              "曲径通幽处" in opt_rec.get("text", "")
              and "常建" in opt_rec.get("options", ""),
              str(opt_rec)[:90])

    # 事后再确认:--uncollected 1=答案 直接补进题库,且只加不减
    with Sandbox() as sb_add:
        make_bank(sb_add.bank, [{"q": "《管子》的作者是谁？", "a": "管仲"}])
        add_path = os.path.join(sb_add.dir, "uncollected.jsonl")
        with open(add_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-11 12:00:02", "score": 71,
                                "text": "曲径通幽处，禅房花木深。这句诗的作者是谁？",
                                "options": "韩愈 李顾 陈子昂 常建",
                                "guess_q": "《管子》的作者是谁？",
                                "guess_a": "管仲"}, ensure_ascii=False) + "\n")
        with patch(UNCOLLECTED_PATH=add_path):
            rc_c, out_c = capture(cli.cmd_uncollected, ["9=常建", "abc"])
        check("补题:序号超范围/格式不对只跳过,不写库",
              "序号超出" in out_c and "格式应为" in out_c
              and "补了" not in out_c, out_c.strip().splitlines()[-2:])
        with patch(UNCOLLECTED_PATH=add_path):
            rc_a, out_a = capture(cli.cmd_uncollected, ["1=常建@qiandao"])
        hit = QuestionBank(bank_mod.BANK_PATH).match(
            "曲径通幽处，禅房花木深。这句诗的作者是谁？", 95)[0]
        raw_bank = core.flatten_config(json.load(open(bank_mod.BANK_PATH, encoding="utf-8")))
        added_row = [e for e in raw_bank if e.get("answer") == "常建"]
        check("补题:--uncollected 1=答案@活动 写进题库并记对活动",
              rc_a == 0 and hit is not None and hit["a"] == "常建"
              and added_row and added_row[0]["activity"] == "qiandao",
              str(added_row[:1])[:70])
        check("补题:回显完整题干(序号看错时能核对出来)",
              "曲径通幽处，禅房花木深。这句诗的作者是谁？" in out_a
              and "已补进题库[qiandao]" in out_a,
              [l for l in out_a.splitlines() if "已补进题库" in l][:1])
        check("补题:同一题重复补只留一条",
              tools_keju.add_manual_question(
                  "曲径通幽处，禅房花木深。这句诗的作者是谁？", "常建") is False)
        with patch(UNCOLLECTED_PATH=add_path):
            rc_b, out_b = capture(cli.cmd_uncollected)
        check("补题:已入库的条目不再列进待确认",
              "没有待确认的条目" in out_b and "已入库 1 条" in out_b,
              out_b.strip().splitlines()[:1])
        merged, _added, kept = bank_mod.merge_bank_entries(
            bank_mod.load_bank_raw(), [{"question": "官方后来加的新题？",
                                     "answer": "答案新"}])
        check("补题:手工补的题不会被题库更新删掉(只加不减)",
              any(e.get("answer") == "常建" for e in merged) and kept == 2,
              f"kept={kept} 共{len(merged)} 题")


# ----------------------------------------- 11 分支补漏:OCR/语音/题库源的坏路径

@section
def sec_extra_errors():
    import onnxruntime as _ort
    import rapidocr_onnxruntime as _rr
    import shutil as _sh
    import subprocess as _sp
    import win32com.client as _wcli
    import pythoncom as _pc

    tmp = tempfile.mkdtemp(prefix="keju_err_")
    try:
        bank = make_bank(os.path.join(tmp, "b.json"), [
            {"q": "第一道题的题干内容是什么", "a": "答案甲"}])

        # ---- Ocr 的坏路径 ----
        o_warm = Ocr(lambda m: None)
        o_warm._get = lambda: None
        o_warm._infer = raiser(RuntimeError("预热推理失败"))
        o_warm.warmup()
        check("Ocr:warmup 推理失败也不影响启动", True)

        real_rapid, real_prov = _rr.RapidOCR, _ort.get_available_providers
        calls = []

        def fake_rapid(*a, **k):
            calls.append("dml" if k.get("det_use_dml") else "cpu")
            raise RuntimeError("引擎创建失败")

        try:
            _rr.RapidOCR = fake_rapid
            _ort.get_available_providers = lambda: ["CPUExecutionProvider",
                                                    "DmlExecutionProvider"]
            o_get = Ocr()
            try:
                o_get._get()
                err = ""
            except RuntimeError as e:
                err = str(e)
        finally:
            _rr.RapidOCR, _ort.get_available_providers = real_rapid, real_prov
        check("Ocr:GPU 引擎创建失败会记下来并继续尝试 CPU 引擎",
              calls == ["dml", "cpu"] and err == "引擎创建失败"
              and o_get.device == "CPU", str(calls))

        o_fb = Ocr(lambda m: None)
        o_fb.device = "GPU(DirectML)"
        logs_fb = []
        o_fb._log = logs_fb.append
        try:
            _rr.RapidOCR = fake_rapid
            o_fb._fallback_to_cpu(RuntimeError("重建也失败"))
        finally:
            _rr.RapidOCR = real_rapid
        check("Ocr:CPU 引擎也建不起来时记日志(设备状态不乱标)",
              any("CPU 引擎重建失败" in m for m in logs_fb)
              and o_fb.device.startswith("GPU"), str(logs_fb[:1]))

        o_cpu = Ocr(lambda m: None)
        o_cpu.device = "CPU"
        o_cpu._infer = raiser(RuntimeError("推理挂了"))
        try:
            o_cpu.recognize_blocks(np.zeros((10, 10, 3), np.uint8))
            re_raised = False
        except RuntimeError:
            re_raised = True
        check("Ocr:非 GPU 设备推理出错直接抛出(不吞掉)", re_raised)

        o_empty = Ocr(lambda m: None)
        o_empty._infer = lambda img, limit_side=None: []
        check("Ocr:没有识别到文本块时返回空列表",
              o_empty.recognize_blocks(np.zeros((10, 10, 3), np.uint8)) == [])

        # ---- _apply / Speaker 的坏路径 ----
        real_init, real_dispatch = _pc.CoInitialize, _wcli.Dispatch
        try:
            _pc.CoInitialize = raiser(RuntimeError("COM 不可用"))
            sp_com = core.Speaker({}, lambda m: None)
            sp_com.start()
            com_ok = sp_com._ready.wait(8)
            sp_com.quit()
        finally:
            _pc.CoInitialize = real_init
        check("Speaker:COM 初始化失败也能继续(只记不抛)", com_ok)

        logs_sapi = []
        try:
            _wcli.Dispatch = raiser(RuntimeError("SAPI 不可用"))
            sp_dead = core.Speaker({}, logs_sapi.append)
            sp_dead.start()
            dead_ok = sp_dead._ready.wait(8)
            time.sleep(0.2)
        finally:
            _wcli.Dispatch = real_dispatch
        check("Speaker:SAPI 起不来 -> 标记 dead 并唤醒等待者",
              dead_ok and sp_dead.dead and bool(logs_sapi), str(logs_sapi[:1]))
        t0 = time.time()
        sp_dead.say("甲")
        check("Speaker:dead 之后再播报都是立即返回",
              time.time() - t0 < 0.05, f"{(time.time() - t0) * 1000:.0f}ms")

        logs_speak = []
        sp_se = core.Speaker({"app": {"tts_backend": "sapi"}},
                             logs_speak.append)
        sp_se.start()
        sp_se._ready.wait(8)

        class BoomTts:
            def Speak(self, text, flags=0):
                raise RuntimeError("声卡被占用")

        sp_se._tts = BoomTts()
        sp_se.say("乙")
        time.sleep(0.4)
        sp_se.quit()
        check("Speaker:系统语音播报出错会记日志(不让线程死)",
              any("[语音]出错" in m for m in logs_speak), str(logs_speak[:1]))

        # ---- _speak_edge 的缓存未命中 ----
        played = []
        sp_me = core.Speaker({"app": {"voice_hint": "zh-CN-YunxiNeural",
                                      "volume": 1.0, "pitch": 15,
                                      "tts_backend": "edge"}}, lambda m: None)
        with patch(_play_mp3_blocking=played.append,
                   _synth_edge_file=lambda *a, **k: True):
            ok_me = sp_me._speak_edge("这次没有缓存")
        check("_speak_edge:缓存没有但云端合成成功 -> 播放并返回 True",
              ok_me is True and len(played) == 1, f"played={len(played)}")
        logs_fail = []
        sp_mf = core.Speaker({"app": {"voice_hint": "zh-CN-YunxiNeural",
                                      "volume": 1.0, "pitch": 15,
                                      "tts_backend": "edge"}}, logs_fail.append)
        with patch(_synth_edge_file=lambda *a, **k: False):
            ok_mf = sp_mf._speak_edge("还是没有缓存")
        check("_speak_edge:合成失败 -> 返回 False 交给系统语音(并记日志)",
              ok_mf is False and any("[语音]云端失败" in m for m in logs_fail),
              str(logs_fail[:1]))

        # ---- _synth_edge_file 的成功 / 重试 ----
        def run_ok(cmd, **kw):
            with open(cmd[7], "wb") as f:      # argv[7] = 输出文件
                f.write(b"x")
            return types.SimpleNamespace(returncode=0)

        real_run = _sp.run
        try:
            _sp.run = run_ok
            ok_syn = core._synth_edge_file("文本", "zh-CN-YunxiNeural", 0, 0,
                                           os.path.join(tmp, "ok.mp3"),
                                           timeout=2, retries=0)
        finally:
            _sp.run = real_run
        check("_synth_edge_file:合成成功 -> 返回 True(文件非空)", ok_syn is True)

        state = {"n": 0}

        def run_flaky(cmd, **kw):
            state["n"] += 1
            if state["n"] == 1:
                with open(cmd[7], "wb") as f:      # 留个半成品,验证下次会清掉
                    f.write("半成品".encode("utf-8"))
                raise OSError("第一次抖动")
            return run_ok(cmd, **kw)

        try:
            _sp.run = run_flaky
            t0 = time.time()
            ok_retry = core._synth_edge_file("文本", "zh-CN-YunxiNeural", 0, 0,
                                             os.path.join(tmp, "retry.mp3"),
                                             timeout=2, retries=1)
            el = time.time() - t0
        finally:
            _sp.run = real_run
        check("_synth_edge_file:失败后退避重试一次",
              ok_retry is True and state["n"] == 2 and el >= 0.8,
              f"{state['n']} 次 {el:.1f}s")

        # ---- build_voice_cache 的边界 ----
        tiny = types.SimpleNamespace(entries=[{"q": "a", "a": "答1"},
                                              {"q": "b", "a": "答2"},
                                              {"q": "c", "a": "答3"}])
        with patch(QuestionBank=lambda path=None: tiny,
                   load_config=lambda: {"app": {"voice_hint":
                                                "zh-CN-YunxiNeural"}},
                   _synth_edge_file=lambda *a, **k: True):
            d, f, s = core.build_voice_cache(limit=2, log=lambda m: None)
            check("build_voice_cache:limit 只处理前 N 条", d + f + s == 2,
                  f"{d}/{f}/{s}")
            with patch(_synth_edge_file=raiser(KeyboardInterrupt())):
                d2, f2, s2 = core.build_voice_cache(limit=1, log=lambda m: None)
            check("build_voice_cache:中途 Ctrl-C 会干净退出",
                  (d2, f2, s2) == (0, 0, 0), f"{d2}/{f2}/{s2}")

        # ---- prune_voice_cache 的坏路径 ----
        with Sandbox() as sb:
            b2 = make_bank(sb.bank, [{"q": "甲题？", "a": "甲答"}])
            cfg2 = {"app": {"voice_hint": "zh-CN-YunxiNeural", "volume": 1.0,
                            "pitch": 15}}
            stuck = os.path.join(sb.voice, "打不开.mp3")
            os.makedirs(stuck)                 # 是目录,删不掉
            n = core.prune_voice_cache(cfg2, b2)
            check("prune_voice_cache:删不掉的条目跳过,不影响其它",
                  n == 0 and os.path.isdir(stuck))
        with patch(VOICE_CACHE_DIR=os.path.join(tmp, "没有这个目录")):
            check("prune_voice_cache:缓存目录不存在时返回 0",
                  core.prune_voice_cache({"app": {"voice_hint": ""}}, bank) == 0)
        real_listdir = os.listdir
        try:
            os.listdir = raiser(PermissionError("拒绝访问"))
            check("prune_voice_cache:目录读不了时返回 0(不抛)",
                  core.prune_voice_cache({"app": {"voice_hint": ""}}, bank) == 0)
        finally:
            os.listdir = real_listdir

        sp_quit = core.Speaker({}, lambda m: None)
        sp_quit.start()
        sp_quit._ready.wait(8)
        real_uninit = _pc.CoUninitialize
        try:
            _pc.CoUninitialize = raiser(RuntimeError("COM 反初始化失败"))
            sp_quit.quit()
            time.sleep(0.5)
        finally:
            _pc.CoUninitialize = real_uninit
        check("Speaker:退出时 COM 释放失败也不影响线程结束",
              not sp_quit.is_alive())

        # ---- 题库源:结构变化的更多分支 ----
        def html_no_app(url, timeout=30):
            return "<html><body>没有脚本</body></html>".encode("utf-8")

        with patch(_http_get=html_no_app):
            try:
                bank_mod.fetch_official_bank()
                no_app = False
            except ValueError as e:
                no_app = "未找到 app.js" in str(e)
            check("官方源:页面里没有 app.js 时抛错", no_app)

        def app_no_hash(url, timeout=30):
            if "app." in url:
                return b"var x=1;"
            return b'<script src="static/js/app.abc12345.js"></script>'

        with patch(_http_get=app_no_hash):
            try:
                bank_mod.fetch_official_bank()
                no_hash = False
            except ValueError as e:
                no_hash = "未找到数据块哈希" in str(e)
            check("官方源:app.js 里没有数据块哈希时抛错", no_hash)

        # ---- 匹配:短题干严格整串路径 ----
        short_bank = make_bank(os.path.join(tmp, "s.json"),
                               [{"q": "铁观音", "a": "绿茶"}])
        got = short_bank.match_all("铁观音", 62)
        hit_s, score_s = short_bank.match("铁观音", 62)
        check("短题干(<10 字):match / match_all 都走严格整串命中",
              len(got) == 1 and got[0][0]["a"] == "绿茶"
              and hit_s is not None and hit_s["a"] == "绿茶" and score_s >= 80,
              f"{score_s:.0f}%")

        # ---- 依赖探测 / 数据文件 ----
        with patch(REQUIRED_MODULES=["绝对不存在这个模块"]):
            check("check_deps:真的缺模块时会报出来",
                  core.check_deps() == ["绝对不存在这个模块"])
        with patch(np=None):
            check("check_deps:numpy 导入失败也算缺失",
                  "numpy" in core.check_deps())

        real_copy = _sh.copyfile
        copy_ok = True
        try:
            _sh.copyfile = raiser(OSError("磁盘满了"))
            with tempfile.TemporaryDirectory() as td:
                bundle = os.path.join(td, "bundle", "data")
                appdir = os.path.join(td, "app")
                os.makedirs(bundle)
                os.makedirs(appdir)
                with open(os.path.join(bundle, "keju_questions.json"), "w") as f:
                    f.write("{}")
                with patch(BASE_DIR=appdir,
                           _BUNDLE_DIR=os.path.join(td, "bundle")):
                    core.ensure_data_files()
        except Exception:
            copy_ok = False
        finally:
            _sh.copyfile = real_copy
        check("ensure_data_files:复制失败静默跳过(不影响启动)", copy_ok)

        # ---- 单实例互斥体:同进程第二次必判"已运行" ----
        first = keju._single_instance()
        if first:
            second = keju._single_instance()
            check("单实例:同进程第二次创建互斥体必判已运行",
                  second is False, f"first={first} second={second}")
        else:
            # 应用正在跑:互斥体被外部持有,本进程测不了同进程口径
            check("单实例:互斥体被外部持有(应用在跑),本项跳过", True,
                  "关掉应用后重跑可测同进程口径")

        class BadShcore:
            def SetProcessDpiAwareness(self, v):
                raise OSError("旧系统没有 shcore")

        class BadUser32:
            def SetProcessDPIAware(self):
                raise OSError("也不支持")

        with patch(ctypes=types.SimpleNamespace(
                windll=types.SimpleNamespace(shcore=BadShcore(),
                                             user32=types.SimpleNamespace()))):
            core.dpi_aware()             # 第一层失败 -> 试 user32(也失败)
        with patch(ctypes=types.SimpleNamespace(
                windll=types.SimpleNamespace(shcore=BadShcore(),
                                             user32=BadUser32()))):
            core.dpi_aware()             # 两层都失败 -> 静默
        check("dpi_aware:老系统上的降级路径都能兜住", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


FAST = False                       # --fast:只跑代表用例(改完代码快速自检)
FAST_SKIP = ("sec_extra_errors",)  # 整组跳过:错误兜底路径,不属于日常回归
# 想自己点组:python tests.py --only gui,extra_errors(名字带不带 sec_ 都行)


def _pick_sections(argv):
    """从命令行参数决定跑哪几组:--only=... / --only ... > 全部。"""
    for i, a in enumerate(argv):
        if a.startswith("--only="):
            spec = a[len("--only="):]
        elif a == "--only" and i + 1 < len(argv):
            spec = argv[i + 1]
        else:
            continue
        have = {fn.__name__ for fn in sections}
        out = []
        for n in spec.split(","):
            n = n.strip()
            if not n:
                continue
            out.append(n if n in have else "sec_" + n)
        return tuple(out)
    return None


def main(argv=None):
    global FAST, QUIET
    argv = list(sys.argv[1:] if argv is None else argv)
    FAST = "--fast" in argv
    QUIET = "--quiet" in argv or "-q" in argv
    only = _pick_sections(argv)
    if only:
        print("【只跑选定组】" + " ".join(n[4:] for n in only) + "\n")
    elif FAST:
        print("【快速模式】只跑代表用例(改完代码自检用)。")
        print("  本模式不覆盖:界面更新题库/自动检查、OCR 引擎重建、错误兜底路径、")
        print("  极端干扰下的滑窗上限回归、--selftest 端到端。发布前请跑全量:")
        print("           python tests.py          # 全量(含界面/引擎慢用例)\n")
    t0 = time.time()
    for fn in sections:
        if only and fn.__name__ not in only:
            continue
        if FAST and fn.__name__ in FAST_SKIP:
            print(f"\n--- {fn.__name__[4:]} ---(快速模式跳过)")
            continue
        if not QUIET:
            print(f"\n--- {fn.__name__[4:]} ---")
        fn()
    failed = [r for r in results if not r[1]]
    _tcl_drain()
    print(f"\n结果: {len(results) - len(failed)}/{len(results)} 通过"
          f"(耗时 {time.time() - t0:.1f}s)"
          + ("  [选定组]" if only else ("  [快速模式]" if FAST else "")))
    for name, _, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
