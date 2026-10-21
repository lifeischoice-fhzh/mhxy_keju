#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试:python tests.py(自 keju-answerer 移植;降妖伏魔两组已随功能裁掉)

覆盖 9 组:
  1 配置与工具函数            2 题库与匹配
  3 题库更新(离线桩)         4 语音与音频缓存
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
import threading
import time
import types

import numpy as np

import bank as bank_mod        # 题库子系统(从 core.py 搬出去的,见 bank.py)
import cli                     # 命令行层(cmd_*:自检/统计/补题)
import core
import gui
import keju
from bank import QuestionBank  # 类名直接用(本文件里的同名局部变量叫 bank,不冲突)
from core import (Ocr, edge_params, load_config, normalize,
                  _voice_cache_path)
# 科举的匹配/收益统计在 tools/keju.py,这里按它的位置取
from tools import keju as tools_keju
from tools.keju import (RewardTally, resolve_fullscan, scan_rewards,
                        strip_exam_header)
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
#   core —— 地基本体(配置/截图/OCR/语音/引擎/CLI)
#   bank —— 题库子系统本体(BANK_PATH / update_bank / fetch_* / QuestionBank / ...)
# gui / keju 不在表里:它们调用时现取(core.xxx / bank.xxx),换 core / bank
# 上的那份就够了。例外(不在这两个模块里的,用例单独指名):
#   tools/ 里自己的常量(REWARD_PATH)—— mods=(tools_keju,)
#   cli —— 命令行层的 cmd_*(keju.py 入口按 cli.xxx 现取,换这份才生效)
PATCH_MODULES = (core, bank_mod, cli)


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
        self.reward = os.path.join(self.dir, "rewards.json")
        self.events = os.path.join(self.dir, "data", "events.jsonl")
        os.makedirs(self.voice, exist_ok=True)
        self._p = patch(BANK_PATH=self.bank, BANK_META_PATH=self.meta,
                        BANK_BACKUP_PATH=self.backup, CONFIG_PATH=self.config,
                        LOG_PATH=self.log, VOICE_CACHE_DIR=self.voice,
                        EVENTS_PATH=self.events)
        self._p2 = patch(mods=(tools_keju,), REWARD_PATH=self.reward)
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

    with patch(_http_get=fake_official()):
        check("official_chunk_hash:轻量检查读出版本号",
              bank_mod.official_chunk_hash() == "deadbeef")
    with patch(_http_get=raiser(OSError("断网"))):
        check("official_chunk_hash:断网返回空串(静默跳过)",
              bank_mod.official_chunk_hash() == "")

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

        valid = [cpath(a) for a in ("甲答", "乙答", "请保证屏幕里只有一个题目",
                                    "语音测试,C铁观音,唐朝")]
        for p in valid:
            with open(p, "wb") as f:
                f.write(b"x")
        junk = os.path.join(sb.voice, "junk.mp3")
        with open(junk, "wb") as f:
            f.write(b"x")
        with open(os.path.join(sb.voice, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("不是音频")
        removed = core.prune_voice_cache(cfg, bank)
        check("prune_voice_cache:只删无关 mp3,保留答案与提示语",
              removed == 1 and not os.path.exists(junk)
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

        real_cls = core.QuestionBank
        core.QuestionBank = lambda path=None: bank
        try:
            with patch(_synth_edge_file=fake_synth, load_config=lambda: cfg):
                d1, f1, s1 = core.build_voice_cache(log=lambda m: None)
                check("build_voice_cache:合成失败会计数(不抛异常)",
                      d1 == 0 and f1 == 1 and s1 == 1, f"{d1}/{f1}/{s1}")
                d2, f2, s2 = core.build_voice_cache(log=lambda m: None)
                check("build_voice_cache:重跑续传(在的跳过、缺的补上)",
                      d2 == 1 and f2 == 0 and s2 == 1, f"{d2}/{f2}/{s2}")
                d3, f3, s3 = core.build_voice_cache(log=lambda m: None)
                check("build_voice_cache:全齐时不再请求合成",
                      d3 == 0 and f3 == 0 and s3 == 2, f"{d3}/{f3}/{s3}")
        finally:
            core.QuestionBank = real_cls

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
    app.logs = []            # 界面滚动框会显示的(ui=True)
    app.logs_all = []        # 全部日志(含只写文件的),带 ui 标记

    def _log(msg, ui=True, detail="", ui_text=None):
        app.logs_all.append((msg, ui))
        if ui:
            app.logs.append(msg)

    app.speaker = types.SimpleNamespace(say=app.said.append)
    app.log = _log
    eng.screen = FakeScreen(crops)
    eng.ocr = types.SimpleNamespace(recognize_blocks=lambda img, **kw: blocks)
    eng.keju_tool = tools_keju.KejuTool(app, lambda: eng.screen,
                                        lambda: eng.ocr)
    # 不带"自动定位"的记忆:_follow_clients 就不会去枚举真机窗口
    # (真开着游戏时会把测试的假区域"跟随"到真客户端上,用例全歪)
    eng.keju_tool._auto_boxes = None
    eng.keju_tool.rewards = types.SimpleNamespace(  # 收益统计(本桩给空实现)
        feed=lambda *a, **k: [])
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

    n, entry, score, note, win = resolve_fullscan(bank, [blk(QA, 100, 200)], 62)
    check("滑窗:单题命中并给出窗口下标",
          n == 1 and entry["a"] == "答案甲" and win == [0], f"{score:.0f}% win={win}")

    n, entry, _, _, _ = resolve_fullscan(
        bank, [blk(QA, 100, 200), blk(QB, 100, 600)], 62)
    check("滑窗:两处不同题 -> 判为两道题(要求屏幕只留一道)", n == 2)

    n, entry, _, note, _ = resolve_fullscan(
        bank, [blk(QA, 100, 200), blk(QB, 100, 240)], 62)
    check("滑窗:纵向位置相近的命中聚成一道题(匹配到邻居题)",
          n == 1 and entry is not None and "近重复题干" in note, note.strip())

    n, entry, _, note, _ = resolve_fullscan(
        bank, [blk(QC, 100, 200), blk(QD, 100, 700)], 62)
    check("滑窗:近重复题干算一道题(不误报多题)",
          n == 1 and entry["a"] == "答案丙" and "近重复题干" in note,
          f"{entry['a']} {note.strip()}")

    # 窄栏排版:最后一行"类"被单独切出去,于是两个窗口分别命中 C / D 两条近重复题干
    n, entry, _, note, _ = resolve_fullscan(
        bank, [blk(QD, 100, 200), blk("类", 100, 232),
               blk("A 答案丁 B 无关文字", 100, 600)], 62)
    check("滑窗:近重复题干 + 屏幕上有选项 -> 按选项消歧",
          n == 1 and entry["a"] == "答案丁" and "按选项消歧" in note,
          f"{entry['a']} {note.strip()}")

    n, entry, _, note, _ = resolve_fullscan(
        bank, [blk(QA, 100, 200, h=60), blk(QB, 100, 700, h=28)], 62)
    check("滑窗:多题时取字号最大的一道(小字多为聊天残留)",
          n == 1 and entry["a"] == "答案甲" and "字号最大" in note, note.strip())

    n, entry, _, note, _ = resolve_fullscan(
        bank, [blk(QA, 100, 200), blk(QB, 100, 600)], 62, recent=[AN])
    check("滑窗:回声抑制(刚答过的题不再当作新题)",
          n == 1 and entry["a"] == "答案乙" and "回声" in note, note.strip())

    n, _, _, _, _ = resolve_fullscan(
        bank, [blk(QA, 100, 200), blk(QB, 100, 600)], 62, recent=[AN, normalize(QB)])
    check("滑窗:两道题都是回声时保持多题(交给字号/手动判断)", n == 2)

    n, entry, score, note, _ = resolve_fullscan(
        bank, [blk("第一道题的题干内容是啥", 100, 200)], 100)
    check("滑窗:没到阈值也记录最高候选(便于日志定位)",
          n == 0 and entry is None and score > 0 and "最高候选" in note,
          f"{score:.0f}% {note}")

    n, _, score, note, _ = resolve_fullscan(
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

    # 不该触发:只有“科举”两字的地方(任务追踪栏/找考官提示)不能当锚点
    for plain in ("任务追踪 科举 你完成了会试", "科举找下一关", "科举当前答题进度",
                  "你完成了会试"):
        check(f"锚点:不误触发({plain[:8]}…)",
              not tools_keju.is_exam_anchor(plain)
              and tools_keju.extract_anchored_question([blk(plain, 100, 200)]) == ("", []))

    # 活动公告含“御前科举大赛”但不是题目:可以拿来试匹配,但绝不能到阈值
    chat = blk("每到御前科举大赛，各族才子齐聚长安城一试身手", 100, 200)
    hits_chat, best_chat = tools_keju.fullscan_find(bank, [chat], 80, use_anchor=True)
    check("锚点:公告/聊天不能配出答案(低于阈值不播报)",
          not hits_chat and (best_chat is None or best_chat[0] < 80),
          f"{best_chat[0]:.0f}%" if best_chat else "无候选")

    hits, best_any = tools_keju.fullscan_find(bank, [blk(hdr + QA, 100, 200)], 62,
                                        use_anchor=True)
    check("fullscan_find:锚点路径命中(win 就是锚点窗口)",
          len(hits) == 1 and next(iter(hits.values()))[3] == [0])
    hits2, _ = tools_keju.fullscan_find(bank, [blk(QA, 100, 200)], 62, use_anchor=True)
    check("fullscan_find:没有锚点时滑窗仍能命中", len(hits2) == 1)
    hits3, best3 = tools_keju.fullscan_find(bank, [blk("完全无关的一段文字内容", 100, 200)],
                                      62, use_anchor=False)
    check("fullscan_find:毫无关系 -> 没有命中也没有最高候选",
          not hits3 and best3 is None, f"best={best3}")
    hits4, best4 = tools_keju.fullscan_find(bank, [blk("第一道题的题干内容是啥", 100, 200)],
                                      100, use_anchor=False)
    check("fullscan_find:相似但没到阈值 -> 命中为空但留下最高候选",
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
            hits, _ = tools_keju.fullscan_find(real_bank, decoys(k), 65, use_anchor=False)
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
    QA, QB = "第一道题的题干内容是什么", "今天天气晴朗适合出门散步吗"
    AN = normalize(QA)
    HDR = "御前科举大赛第1关：这一关考的是综合。题目："
    KEY = (0, 0, 60, 30)
    NO_REGIONS = {"keju": {"regions": [None] * core.MAX_CLIENTS}}

    # 日志标签:自动轮询的日志要按"当时到底在不在监听"贴标签
    eng, app = mk_engine(bank, [blk(QA, 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng.keju_tool.watching = False
    eng._safe("cycle", False)
    check("日志标签:监听关着时的自动轮询标 [检测](不冒充自动监听)",
          app.logs and all(m.startswith("[检测]") for m in app.logs),
          app.logs[:2])
    eng2, app2 = mk_engine(bank, [blk(QA, 100, 200)],
                           cfg={"keju": {"exam_mode": False}})
    eng2.keju_tool.watching = True
    eng2._safe("cycle", False)
    check("日志标签:只有监听中才标 [自动监听]",
          app2.logs and all(m.startswith("[自动监听]") for m in app2.logs),
          app2.logs[:2])
    eng3, app3t = mk_engine(bank, [blk(QA, 100, 200)])
    eng3.keju_tool.watching = False
    eng3._safe("cycle", True)
    check("日志标签:手动识别标 [手动识别](即使没在监听)",
          app3t.logs and all(m.startswith("[手动识别]") for m in app3t.logs),
          app3t.logs[:2])

    eng, app = mk_engine(bank, [blk(QA, 100, 200)])          # 没有锚点
    eng._safe("cycle", True)
    check("cycle:御前科举模式下没有锚点 -> 不播报(当前不是该界面)",
          not app.said and not any(m.startswith("识别") for m in app.logs))

    eng, app = mk_engine(bank, [blk(HDR + "短", 100, 200)])
    eng._safe("cycle", True)
    check("cycle:碎片文本(题干<4 字)-> 不播报", not app.said)

    # 短题干:题库里最短的真题只有 4 个字(实测漏答过的 "“佛家三宝”是指"),
    # 不能因为"太短"就当碎片丢掉
    short_bank = make_bank(
        os.path.join(tempfile.mkdtemp(prefix="keju_short_"), "s.json"),
        [{"q": "“佛家三宝”是指", "a": "佛、法、僧"},
         {"q": "“藕”是莲的", "a": "茎"}])
    eng, app = mk_engine(short_bank, [blk(HDR + "“佛家三宝”是指", 100, 200)])
    eng._safe("cycle", True)
    check("短题干:锚点模式下 6 字的真题照样播报(曾被 <8 字门槛挡掉)",
          app.said == ["佛、法、僧"], str(app.said))
    eng, app = mk_engine(short_bank, [blk("“藕”是莲的", 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng._safe("cycle", True)
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
    eng._safe("cycle", True)
    check("短题干+折行:滑窗靠那 5 字的半行也能命中(窗口下限 6->4)",
          app.said == ["佛、法、僧"], str(app.said))

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng._safe("cycle", True)
    check("cycle:锚点命中 -> 播报 + 记忆 + 统计",
          app.said == ["答案甲"] and app.last_answer == "答案甲"
          and AN in eng.keju_tool.recent_answered and eng.keju_tool._region_last[KEY] == AN
          and eng.keju_tool.round_stats["n"] == 1,
          f"{app.said} 统计 {eng.keju_tool.round_stats['n']} 题")

    calls = [0]

    def counting_ocr(img):
        calls[0] += 1
        return [blk(HDR + QA, 100, 200)]

    changed = np.zeros((30, 60, 3), np.uint8)
    changed[10:20, :] = 255                       # 画面确实变了
    eng.screen = FakeScreen([changed])
    eng.ocr = types.SimpleNamespace(recognize_blocks=counting_ocr)
    eng._safe("cycle", False)
    check("cycle:画面变了但题干相同(相似度≥88)-> 识别了却不重复播报",
          calls[0] == 1 and app.said == ["答案甲"], f"OCR {calls[0]} 次 {app.said}")

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng.keju_tool.recent_answered[AN] = time.time()
    eng._safe("cycle", False)
    check(f"cycle:自动轮询遇到 {tools_keju.REPEAT_GUARD_S} 秒内答过的题 -> 跳过不重播",
          not app.said and any("已播过" in m for m in app.logs),
          [m for m in app.logs if "已播过" in m][:1])
    eng._safe("cycle", True)
    check("cycle:手动识别同一题 -> 允许重听", app.said == ["答案甲"], f"{app.said}")

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng.keju_tool.recent_answered[AN] = time.time() - tools_keju.REPEAT_GUARD_S - 1
    eng._safe("cycle", False)
    check(f"cycle:超过 {tools_keju.REPEAT_GUARD_S} 秒后同一道题可以重播",
          app.said == ["答案甲"], f"{app.said}")

    eng, app = mk_engine(bank, [blk(QA, 100, 200)], cfg={"keju": {"exam_mode": False}})
    eng._safe("cycle", True)
    check("cycle:三大学士(无锚点)也能作答并标注模式",
          app.said == ["答案甲"] and any("三大学士" in m for m in app.logs),
          f"{app.said}")

    # 没定位到客户端:不再自动全屏扫描(全屏扫描只能手动触发),提示 + 暂停监听
    eng, app = mk_engine(bank, [], cfg=NO_REGIONS)
    called = []
    eng.keju_tool._fullscan_once = lambda: (called.append(1), 1)[1]
    eng.keju_tool.watching = True
    eng._safe("cycle", False)
    check("cycle:没定位到客户端 -> 不再自动全屏扫描,给出提示并暂停监听",
          called == [] and eng.keju_tool.watching is False
          and any("还没有定位到客户端" in m for m in app.logs)
          and any("已暂停" in m for m in app.logs),
          f"全屏扫描 {len(called)} 次, watching={eng.keju_tool.watching}")

    eng, app = mk_engine(bank, [], cfg=NO_REGIONS)
    eng.keju_tool._fullscan_once = lambda: 1
    eng._safe("cycle", True)
    check("cycle:手动识别时没区域 -> 也要在界面给出结果",
          any("还没有定位到客户端" in m for m in app.logs))

    # 考官开场白:压根不是题目 -> 直接跳过(界面/文件都不刷"识别:"),不进匹配
    prompt = "这里是御前科举大赛第4关，你作好答题的准备了吗？开始答题我还没准备"
    eng, app = mk_engine(bank, [blk(prompt, 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng._safe("cycle", False)
    check("cycle:考官开场白自动轮询时完全不刷日志(界面和文件都没有)",
          not any(m.startswith("识别") for m in app.logs)
          and not any(m.startswith("识别") for m, _ in app.logs_all),
          f"{app.logs_all[:1]}")
    eng, app = mk_engine(bank, [blk(prompt, 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng._safe("cycle", True)
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
        eng._safe("cycle", True)          # 连手动识别也不能报
        check(f"回归:考官开场白不会被误配成“乡试”({'锚点' if mode else '普通'}模式)",
              not app.said
              and any("考官开场对话" in m for m in app.logs), f"{app.said}")

    # 手动识别:读到文字但凑不出题干 / 没匹配上,都必须有可见结果
    eng, app = mk_engine(bank, [blk("短", 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng._safe("cycle", True)
    check("cycle:手动识别读到碎片文字 -> 界面给出原因",
          not app.said and any("凑不出题干" in m for m in app.logs),
          [m for m in app.logs if "题干" in m][:1])
    eng, app = mk_engine(bank, [blk("完全无关的一段文字内容", 100, 200)],
                         cfg={"keju": {"exam_mode": False}})
    eng._safe("cycle", True)
    check("cycle:手动识别没匹配上 -> 界面给出结果",
          not app.said and any("未找到匹配" in m for m in app.logs),
          [m for m in app.logs if "未找到匹配" in m][:1])
    eng, app = mk_engine(bank, [blk(QA, 100, 200)])       # 御前科举模式 + 无锚点
    eng._safe("cycle", True)
    check("cycle:手动识别没认出科举题目 -> 提示切三大学士模式(不提锚点)",
          any("没认出科举题目" in m and "三大学士" in m for m in app.logs)
          and not any("锚点" in m for m in app.logs),
          [m for m in app.logs if "没认出" in m][:1])

    # ---- 今日收益:银两/经验/物品 ----
    S1 = "你得到了66580点经验，6831两银子"
    S2 = "连续答对3题，鬼王令+1你得到了61358点经验，7320两银子"
    S3 = "你获得了2点成就点。你获得了定魂珠"
    got = scan_rewards([blk(S1, 100, 200), blk(S2, 100, 240), blk(S3, 100, 280)])
    check("奖励:认出银两/经验/物品(鬼王令是活动道具,不算物品)",
          list(got["exp"]) == [66580, 61358] and list(got["silver"]) == [6831, 7320]
          and "鬼王令" not in got["items"]
          and list(got["items"]) == ["定魂珠"], f"{got}")
    # 截图里的真实四条:物品没有 +N,鬼王令是活动道具不算,过场提示不算
    real = scan_rewards([blk("你得到了38340点经验，7631两银子。", 100, 200),
                              blk("你获得了超级金柳露", 100, 240),
                              blk("您已经通过会试，还可进行一轮。", 100, 280),
                              blk("恭喜你完成会试，共用时9分1秒。", 100, 320),
                              blk("连续答对3题，鬼王令+1", 100, 360)])
    check("奖励:真实四条(经验/银两/无+N物品/过场提示/鬼王令)",
          real["exp"] == {38340: 1} and real["silver"] == {7631: 1}
          and list(real["items"]) == ["超级金柳露"], f"{real}")
    chat = scan_rewards([blk("（帮派）张三 获得了金刚石", 100, 200),
                             blk("我获得了高兽决", 100, 240)])
    check("奖励:别人聊天里的\"获得了XX\"不算自己收益",
          chat["items"] == {} and chat["exp"] == {}, f"{chat}")
    check("奖励:成就点不算物品,无关文本不计",
          "成就点" not in got["items"]
          and scan_rewards([blk("速度+1 你使用了金柳露", 100, 200),
                                blk("御前科举大赛第13关：这一关考的是佛道礼仪",
                                    100, 240)])
          == {"exp": {}, "silver": {}, "items": {}, "round_sec": None})

    rt_path = os.path.join(tempfile.mkdtemp(prefix="keju_rw_"), "r.json")
    rt = RewardTally(rt_path)
    line = "你得到了58168点经验，6523两银京任妖廣道帮众"
    check("奖励:首次计入一份", rt.feed([blk(line, 100, 200)]) and rt.silver == 6523)
    check("奖励:同一屏反复 OCR 不重复计", rt.feed([blk(line, 100, 200)]) == []
          and rt.silver == 6523, f"{rt.silver}")
    check("奖励:多开同屏 5 份就计 5 份", bool(rt.feed([blk(line, 100, 200)] * 5))
          and rt.silver == 6523 * 5, f"{rt.silver}")
    news6 = rt.feed([blk(line, 100, 200)] * 6)
    check("奖励:第 6 个客户端加入只补 1 份",
          "银两 +6523" in news6 and "×" not in " ".join(news6)
          and rt.silver == 6523 * 6, f"{rt.silver} {news6}")
    rt._peak["1|silver:6523"] = (1, time.time() - 999)    # 假装过了 TTL
    rt.feed([blk(line, 100, 200)])
    check("奖励:超过 TTL 后同样数值算新的一次", rt.silver == 6523 * 7, f"{rt.silver}")
    rt.save()
    rt2 = RewardTally(rt_path)
    check("奖励:当天重开应用还记得", rt2.silver == rt.silver and rt2.exp == rt.exp)
    rt2.day = "2000-01-01"
    rt2.save()
    check("奖励:换天就从头算", RewardTally(rt_path).silver == 0)
    check("奖励:界面那行有银两/经验/物品种数",
          all(k in rt.summary() for k in ("银两", "经验", "物品")),
          rt.summary())

    # ---- 收益区改版:总收益行 / 明细行 ----
    check("收益:总收益那行只写银两",
          tools_keju.reward_earn_text(118649) == "收益：118649两"
          and tools_keju.reward_earn_text(0) == "收益：0两"
          and tools_keju.reward_earn_text(None) == "收益：0两")
    rline = tools_keju.reward_round_lines([
        {"slot": "1", "ts": "19:46:32", "sec": 481, "pass": True,
         "silver": 6829, "exp": 34308, "items": {"藏宝图": 1}}])
    check("收益明细:每轮一行,含用时/达标/银两/物品",
          len(rline) == 1 and rline[0].startswith("1号")
          and "用时 8分1秒(达标)" in rline[0]
          and "银两 6,829" in rline[0] and "藏宝图×1" in rline[0], rline[0])
    check("收益明细:没物品写“无”,没达标标“超时”",
          "物品 无" in tools_keju.reward_round_lines(
              [{"slot": 2, "sec": 700, "pass": False, "silver": 0}])[0])

    # ---- 单轮收益:按客户端分开归轮 ----
    def _mkt(s, y=10):
        return blk(s, 100, y)

    rt3 = RewardTally(
        os.path.join(tempfile.mkdtemp(prefix="keju_rd_"), "r.json"))
    check("单轮:落轮提示能读出用时",
          scan_rewards([_mkt("恭喜你完成会试，共用时9分1秒。")])["round_sec"]
          == 541)
    rt3.feed([_mkt("你得到了38340点经验，7631两银子。"),
              _mkt("你获得了超级金柳露", 40)], slot=1)
    rt3.feed([_mkt("恭喜你完成会试，共用时9分1秒。", 70)], slot=1)
    rt3.feed([_mkt("你得到了40000点经验，8000两银子。")], slot=2)
    rt3.feed([_mkt("你获得了彩果", 40)], slot=2)
    rt3.feed([_mkt("恭喜你完成会试，共用时11分20秒。", 70)], slot=2)
    rt3.feed([_mkt("你得到了12345点经验，999两银子。")], slot=1)
    r1 = [r for r in rt3.rounds if r["slot"] == "1"][-1]
    r2 = [r for r in rt3.rounds if r["slot"] == "2"][-1]
    check("单轮:每个客户端各结各的轮(银两/经验/物品/用时/达标)",
          (r1["silver"], r1["exp"], list(r1["items"]), r1["sec"], r1["pass"])
          == (7631, 38340, ["超级金柳露"], 541, True)
          and (r2["silver"], r2["exp"], list(r2["items"]), r2["sec"],
               r2["pass"]) == (8000, 40000, ["彩果"], 680, False),
          f"1号 {r1['sec']}s/{r1['silver']} 2号 {r2['sec']}s/{r2['silver']}")
    check("单轮:多开的收益不会串到对方轮里(当天累计仍合在一起)",
          rt3.silver == 7631 + 8000 + 999 and rt3.exp == 38340 + 40000 + 12345)
    lines3 = rt3.round_lines()
    check("单轮:界面按客户端分行,含用时/达标与进行中",
          any(l.startswith("1号 最近一轮") and "9分1秒(达标)" in l
              and "7,631" in l for l in lines3)
          and any(l.startswith("2号 最近一轮") and "11分20秒(超时)" in l
                  for l in lines3)
          and any(l.startswith("1号 本轮进行中") and "999" in l for l in lines3),
          lines3)
    rt3.save()
    rt4 = RewardTally(rt3.path)
    check("单轮:轮次留档跨启动保留",
          len(rt4.rounds) == 2 and rt4.round_lines() == lines3)

    # 引擎每轮顺手统计(写在文件里,不刷界面)
    eng_r, app_r = mk_engine(bank, [blk(S1, 100, 200)])
    eng_r.keju_tool.rewards = RewardTally(
        os.path.join(tempfile.mkdtemp(prefix="keju_rw2_"), "r.json"))
    eng_r._safe("cycle", True)
    check("奖励:识别循环每轮都会统计收益",
          eng_r.keju_tool.rewards.silver == 6831 and eng_r.keju_tool.rewards.exp == 66580,
          f"{eng_r.keju_tool.rewards.silver}/{eng_r.keju_tool.rewards.exp}")
    check("奖励:每条收益写进文件(界面不刷屏)",
          any("[奖励]银两 +6831" in m and not ui for m, ui in app_r.logs_all),
          [m for m, _ in app_r.logs_all][-2:])

    st_r = eng_r.keju_tool.ui_state()
    check("ui_state:界面只认这个口,给银两/物品/逐轮明细",
          set(st_r) == {"stats", "silver", "items", "rounds"}
          and st_r["silver"] == 6831 and st_r["items"] == []
          and st_r["rounds"] == [], str(st_r))
    eng_r.keju_tool.rewards.feed([blk("你获得了定魂珠", 100, 240)], slot=1)
    eng_r.keju_tool.rewards.feed(
        [blk("恭喜你完成会试，共用时9分1秒。", 100, 300)], slot=1)
    st_r2 = eng_r.keju_tool.ui_state()
    check("ui_state:物品种类/件数(降序)与逐轮明细跟着走",
          st_r2["items"] == [("定魂珠", 1)] and len(st_r2["rounds"]) == 1
          and st_r2["rounds"][0]["slot"] == "1",
          f"{st_r2['items']} {st_r2['rounds'][:1]}")

    eng, app = mk_engine(bank, [], cfg=NO_REGIONS)
    seq = []
    eng.keju_tool._fullscan_once = lambda: (seq.append(1), 0 if len(seq) == 1 else 1)[-1]
    eng.keju_tool._manual_fs = True                     # 手动扫描
    eng.keju_tool.fullscan()
    check("fullscan:第一次没找到会自动重试一次",
          len(seq) == 2 and any("自动重试" in m for m in app.logs), f"调用 {len(seq)} 次")

    # 重试前那 1 秒别浪费:先扫区域,手动扫描期间新弹的题不会漏
    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])   # 有区域,区域里是真题
    seq = []
    eng.keju_tool._fullscan_once = lambda: (seq.append(1), 0)[1]
    eng.keju_tool._manual_fs = True
    t0 = time.time()
    eng.keju_tool.fullscan()
    el = time.time() - t0
    check("fullscan:重试等待期间会先扫一遍区域(等待时长不变)",
          len(seq) == 2 and app.said == ["答案甲"] and el >= 0.9,
          f"全屏 {len(seq)} 次 / 播报 {app.said} / 耗时 {el:.1f}s")

    fs_cfg = {"keju": {"regions": [None] * core.MAX_CLIENTS}}
    eng, app = mk_engine(bank, [], cfg=fs_cfg)
    eng.ocr = types.SimpleNamespace(recognize_blocks=lambda img, **kw: [])
    eng.keju_tool._manual_fs = True                     # 手动扫描:结果必须进界面日志
    check("_fullscan_once:一个字都没识别到 -> 返回 0 且界面可见",
          eng.keju_tool._fullscan_once() == 0
          and any("未识别到任何文字" in m for m in app.logs))

    eng, app = mk_engine(bank, [blk("完全无关的一段文字内容", 100, 200)], cfg=fs_cfg)
    eng.keju_tool._manual_fs = True
    check("_fullscan_once:屏幕上没有题目 -> 返回 0 且界面可见",
          eng.keju_tool._fullscan_once() == 0
          and any("屏幕未认出科举题目" in m for m in app.logs))

    eng, app = mk_engine(bank, [blk(QA, 100, 200), blk(QB, 100, 600)], cfg=fs_cfg)
    rc = eng.keju_tool._fullscan_once()
    check("_fullscan_once:屏幕上有两道题 -> 播报提醒并要求只留一道",
          rc == 2 and app.said == ["请保证屏幕里只有一个题目"], f"rc={rc}")

    eng, app = mk_engine(bank, [blk(QA, 100, 200)], cfg=fs_cfg)
    rc = eng.keju_tool._fullscan_once()
    check("_fullscan_once:单题 -> 播报 + 计入统计",
          rc == 1 and app.said == ["答案甲"] and eng.keju_tool.round_stats["n"] == 1)
    eng.keju_tool.recent_answered[AN] = time.time()
    rc = eng.keju_tool._fullscan_once()
    check("_fullscan_once:自动轮询遇到刚答过的题 -> 跳过",
          rc == 1 and app.said == ["答案甲"], f"{app.said}")
    eng.keju_tool._manual_fs = True
    rc = eng.keju_tool._fullscan_once()
    check("_fullscan_once:手动扫描同一题 -> 允许重听",
          app.said == ["答案甲", "答案甲"], f"{app.said}")

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

    eng.keju_tool._record_stat(0.5)
    eng.keju_tool._record_stat(1.5)
    check("_record_stat:累加题数与总响应时间",
          eng.keju_tool.round_stats["n"] == 2 and abs(eng.keju_tool.round_stats["resp_sum"] - 2.0) < 1e-9,
          f"{eng.keju_tool.round_stats}")

    eng, app = mk_engine(bank, [])
    eng._safe("这个方法不存在")
    check("_safe:任务异常被吞掉并写日志(引擎线程不死)",
          any("[引擎]任务异常" in m for m, _ in app.logs_all),)

    eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
    eng.request_cycle()
    eng.request_fullscan()
    eng.set_watch(True)
    pending = (eng.keju_tool._req_cycle, eng.keju_tool._req_fullscan)
    ran = [eng.keju_tool._run_pending() for _ in range(3)]
    check("手动任务:cycle / fullscan 投递给科举自己的线程(引擎不再有任务队列)",
          pending == (True, True) and ran == [True, False, False]
          and eng.keju_tool._manual_fs is False and eng.keju_tool.watching is True
          and not hasattr(eng, "jobs") and eng.keju_tool._wake.is_set()
          and "[自动监听]已开启" in app.logs, f"{pending} {ran}")
    eng.toggle_watch()
    check("监听开关:toggle_watch 反转", eng.keju_tool.watching is False)

    # 看门狗恢复开关:真实引擎走 set_watch(→ refresh_tools → activate);
    # 原来就关着的不能空打一条"[自动监听]已停止"(状态根本没变)
    logs_r = []

    def _fake_self(engine):
        return types.SimpleNamespace(
            engine=engine,
            log=lambda m, ui=True, detail="", **kw: logs_r.append(m))

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
        app.log = lambda msg, ui=True, detail="": app.logs.append(msg)
        app.speaker = types.SimpleNamespace(say=app.said.append)
        run_eng = core.Engine(app)
        run_eng.ocr = RunOcr([blk(HDR + QA, 100, 200)])
        with patch(Screen=lambda: FakeScreen()), \
                patch(find_client_windows=lambda *a, **k: ([], [])):
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
    with Sandbox() as sb, patch(official_chunk_hash=lambda timeout=15: ""):
        # App() 建题库时读的是被 patch 的 BANK_PATH,先往沙箱里放一份
        make_bank(sb.bank, [{"q": "第一道题的题干内容是什么", "a": "答案甲"}])
        old = time.time() - 86400 * 2
        with open(sb.log, "w", encoding="utf-8") as f:
            f.write("2020-01-01 00:00:00 很久以前的一条日志\n")
        os.utime(sb.log, (old, old))

        # 本应用启动即自动定位:这里固定成"找到 1 个客户端"
        with patch(find_client_windows=lambda: (
                [("梦幻西游 ONLINE", [100, 150, 800, 650])], [])):
            app = gui.App()
        app.root.withdraw()               # 测试时不弹窗
        real_engine = app.engine          # 真引擎先留着,收尾时关掉
        check("界面:启动即自动定位,整窗口填进槽位(其余槽位留空)",
              app.cfg["keju"]["regions"][0] == [100, 150, 800, 650]
              and len(app.cfg["keju"]["regions"]) == core.MAX_CLIENTS
              and app.region_vars[0].get() == "(100,150)-(800,650)",
              str(app.cfg["keju"]["regions"]))
        log_rec = []                      # 自己留一份日志,避免和滚动框抢队列
        real_log = app.log
        app.log = lambda msg, ui=True, detail="", ui_text=None: (
            log_rec.append(msg), real_log(msg, ui, detail, ui_text))

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
        dead = tk.Toplevel(app.root)
        dead.destroy()
        hit = []
        ui_widgets.fade_out(dead, then=lambda: hit.append(1))
        ui_widgets.stop_anim(dead)          # 已经没了的窗口也得安全
        check("界面:窗口已经没了,淡出直接走回调、停表也不抛", hit == [1])
        try:
            raise RuntimeError("故意出错")
        except RuntimeError as e:
            app._on_tk_error(type(e), e, e.__traceback__)
        check("界面:回调里没接住的异常写进日志(默认只打到 stderr,用户看不到)",
              any("[界面]回调出错" in m for m in log_rec),
              log_rec[-1] if log_rec else "")

        # ---- 日志归属:界面滚动框只显示科举 + 公共的行 ----
        check("日志:按 [模块] 认归属(科举那套=科举,其余=公共)",
              core.log_scope("[科举]已进入") == "keju"
              and core.log_scope("[题目对话框2]设置") == "keju"
              and core.log_scope("[自动监听]匹配 92%") == "keju"
              and core.log_scope("[客户端]发现 2 个客户端") == "keju"
              and core.log_scope("[全屏扫描]匹配 88%") == "keju"
              and core.log_scope("[应用]启动") == ""
              and core.log_scope("没标签的一行") == "")
        app.log_box.configure(state="normal")
        app.log_box.delete("1.0", "end")
        app.log_box.configure(state="disabled")
        app.log("[自动监听]这条该出现")
        app.log("[应用]公共的行也该出现")
        app._drain_logs()
        shown = app.log_box.get("1.0", "end")
        check("日志:科举与公共的行都进滚动框",
              "该出现" in shown and "公共的行" in shown,
              shown.replace("\n", " | "))

        check("界面:默认配置反映到控件(间隔 0.4 / 5 个区域槽 / 监听关)",
              app.interval_var.get() == "0.4"
              and len(app.region_vars) == core.MAX_CLIENTS
              and "关" in str(app.watch_btn["text"])
              and str(app.mode_keju_btn["style"]) == "Accent.TButton")

        app.cfg["keju"]["regions"][0] = [10, 20, 30, 40]
        app._refresh_regions()
        check("界面:区域坐标显示成 (x,y)-(x,y),没定位的写“未定位”",
              app.region_vars[0].get() == "(10,20)-(30,40)"
              and app.region_vars[1].get() == "未定位",
              app.region_vars[0].get())

        app._set_exam_mode(False)
        with open(sb.config, encoding="utf-8") as f:
            saved2 = json.load(f)["keju"]
        check("界面:切换识别模式 -> 落盘 + 高亮跟着换",
              app.cfg["keju"]["exam_mode"] is False and saved2["exam_mode"] is False
              and str(app.mode_plain_btn["style"]) == "Accent.TButton"
              and str(app.mode_keju_btn["style"]) == "TButton"
              and str(app.mode_keju_btn["text"]) == "御前科举"
              and str(app.mode_plain_btn["text"]) == "三大学士",
              f'{app.mode_keju_btn["text"]}/{app.mode_plain_btn["text"]}')
        mode_line = [m for m in ui_logs() if m.startswith("[识别模式]")]
        check("界面:切模式日志只有 [识别模式]+模式名,不带括号解释",
              mode_line == ["[识别模式]三大学士"], mode_line[:1])
        app._set_exam_mode(True)
        check("界面:再切回御前科举只多一条日志(重复点不重复写)",
              [m for m in ui_logs() if m.startswith("[识别模式]")]
              == ["[识别模式]三大学士", "[识别模式]御前科举"],
              [m for m in ui_logs() if m.startswith("[识别模式]")])

        def line_tags(needle):
            """按内容找那一行,再取它的标签;找不到返回 None(免得"没找到"也算通过)。"""
            last = int(app.log_box.index("end-1c").split(".")[0])
            for ln in range(1, last + 1):
                if needle in app.log_box.get(f"{ln}.0", f"{ln}.end"):
                    return app.log_box.tag_names(f"{ln}.0")
            return None

        app.log("[识别模式]御前科举")
        app._drain_logs()
        mode_tags = line_tags("[识别模式]御前科举")
        check("界面:切模式这行不高亮(只有答案行高亮)",
              mode_tags is not None and "match" not in mode_tags,
              str(mode_tags))
        app.log("[自动监听]匹配 100% -> 第一道题… 【答案甲】| 响应 0.30s",
                detail=" (OCR 0.20s 匹配 0.01s)")
        time.sleep(0.1)
        file_lines = [l for l in open(sb.log, encoding="utf-8").read()
                      .strip().splitlines() if "匹配 100%" in l]
        check("日志:界面只显示响应时间,文件里仍保留 OCR/匹配耗时",
              file_lines and "响应 0.30s" in file_lines[-1]
              and "(OCR 0.20s 匹配 0.01s)" in file_lines[-1]
              and not any("OCR" in m for m in log_rec if "匹配 100%" in m),
              file_lines[-1][-56:] if file_lines else "没写进文件")
        app.log("[手动识别]匹配 100% -> 第一道题… 【答案甲】")
        app._drain_logs()
        ans_tags = line_tags("[手动识别]匹配 100%")
        check("界面:认出题目给答案的那行绿字加粗(match 标签)",
              ans_tags is not None and "match" in ans_tags
              and app.log_box.tag_cget("match", "foreground") == "#0a7d3f"
              and "bold" in app.log_box.tag_cget("match", "font"),
              str(ans_tags))
        for plain in ("[手动识别]读到的文字太碎,凑不出题干(仅 1 字):短…",
                      "[全屏扫描]屏幕未认出科举题目(0.8s,最高 32%)",
                      "[自动监听]题库中未找到匹配(最高 32%)",
                      "[客户端1]整个客户端 (1,2)-(3,4) 640×480 ≈ 640×480 档"):
            app.log(plain)
            app._drain_logs()
            tags = line_tags(plain[:14])
            check(f"界面:非答案行不高亮({plain[6:16]}…)",
                  tags is not None and "match" not in tags, str(tags))

        # ---- 收益区:总收益一行 + 物品标签 + 逐轮明细窗 ----
        app.engine.keju_tool.rewards.feed([{"text": "你得到了66580点经验，6831两银子",
                                "x1": 1, "y1": 1, "x2": 2, "y2": 2, "conf": .9}])
        app.engine.keju_tool.rewards.feed([{"text": "你获得了定魂珠",
                                "x1": 1, "y1": 1, "x2": 2, "y2": 2, "conf": .9}])
        app._drain_logs()
        check("界面:收益区只留总收益那一行(格式=收益：N两)",
              app.reward_var.get() == "收益：6831两", app.reward_var.get())
        check("界面:物品行上画了东西(名称标签 + ×件数)",
              bool(app.reward_items_canvas.find_all()),
              str(app.reward_items_canvas.find_all()[:3]))
        app.engine.keju_tool.rewards.feed(  # 1 号客户端:一轮收益 + 落轮
            [{"text": "恭喜你完成会试，共用时9分1秒。", "x1": 1,
              "y1": 1, "x2": 2, "y2": 2, "conf": .9}], slot=1)
        app._drain_logs()
        app.earn_detail_btn.invoke()          # 走按钮本身,别只调方法
        detail = app.earn_detail_text.get("1.0", "end")
        check("界面:「收益明细」按钮打开逐轮明细(用时/银两/物品)",
              "1号" in detail and "9分1秒(达标)" in detail
              and "银两" in detail and "定魂珠" in detail, detail.strip()[:70])
        app.earn_detail_win.destroy()
        app.earn_detail_btn.invoke()          # 旧窗口已销毁:再点不能崩
        check("界面:明细窗被销毁后再点按钮能重新开",
              bool(app.earn_detail_win.winfo_exists()))
        app.earn_detail_win.destroy()

        keys = ("_region_last", "_region_ref", "_region_frame", "_region_ocr_ts",
                "_region_t_detect", "_region_dirty")
        app.cfg["keju"]["regions"] = [None] * core.MAX_CLIENTS
        for attr in keys:
            getattr(app.engine.keju_tool, attr)[(1, 2, 3, 4)] = "y"
        app.on_clear_regions()
        check("界面:清除全部 -> 槽位清空 + 记忆清空",
              app.cfg["keju"]["regions"] == [None] * core.MAX_CLIENTS
              and all(not getattr(app.engine.keju_tool, a) for a in keys))

        # ---- 自动定位客户端:整窗口一条区域 ----
        check("界面:提示文案含“最多 5 个”与“最小分辨率”",
              "最小分辨率效率最高" in app.hint_label["text"],
              app.hint_label["text"])
        with patch(find_client_windows=lambda: ([], [])):
            app.autolocate_btn.invoke()          # 走按钮本身,别只调方法
        check("自动定位:界面上有按钮且点了会跑到定位逻辑",
              str(app.autolocate_btn["text"]) == "自动定位客户端"
              and any("没找到梦幻西游客户端窗口" in m for m in ui_logs()),
              str(app.autolocate_btn["text"]))

        two = [("梦幻西游 ONLINE", [100, 150, 800, 650]),
               ("梦幻西游 ONLINE", [850, 150, 1550, 650])]
        with patch(find_client_windows=lambda: (list(two), [])):
            app.on_autolocate()
        with open(sb.config, encoding="utf-8") as f:
            saved3 = json.load(f)["keju"]
        check("自动定位:按整个客户端填区域 + 落盘 + 刷新显示",
              app.cfg["keju"]["regions"][0] == [100, 150, 800, 650]
              and app.cfg["keju"]["regions"][1] == [850, 150, 1550, 650]
              and app.cfg["keju"]["regions"][2:] == [None] * (core.MAX_CLIENTS - 2)
              and saved3["regions"][0] == [100, 150, 800, 650]
              and app.region_vars[0].get() == "(100,150)-(800,650)"
              and any("[客户端]发现 2 个客户端" in m for m in ui_logs()),
              str(app.cfg["keju"]["regions"])[:40])

        for attr in keys:
            getattr(app.engine.keju_tool, attr)[(100, 150, 800, 650)] = "z"
        six = [("c", [0, 0, 500, 400]) for _ in range(core.MAX_CLIENTS + 1)]
        with patch(find_client_windows=lambda: (list(six), [])):
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

        with patch(find_client_windows=lambda: (list(two), [])):
            app._set_exam_mode(False)
            app.on_autolocate()
            app._set_exam_mode(True)
        check("自动定位:三大学士模式下提示改切御前科举",
              any("建议切「御前科举」模式" in m for m in ui_logs()),
              [m for m in ui_logs() if "建议切" in m][:1])

        # 分辨率:报出来 + 不是最小档就提醒;并提醒移动/改分辨率后会自动跟随
        big = [("梦幻西游 ONLINE", [0, 0, 1015, 829])]
        with patch(find_client_windows=lambda: (list(big), [])):
            app.on_autolocate()
        check("自动定位:打出客户端分辨率档,不是最小档就提醒调小",
              any("1015×829 ≈ 1024×768 档" in m for m in ui_logs())
              and any("不是最小分辨率" in m and "640×480" in m
                      for m in ui_logs()),
              [m for m in ui_logs() if "档" in m][:1])
        check("自动定位:提示客户端挪了会自动跟随",
              sum(1 for m in ui_logs() if "会自动跟随" in m) >= 1,
              [m for m in ui_logs() if "跟随" in m][:1])

        small = [("梦幻西游 ONLINE", [0, 0, 686, 478])]
        n_before = len(ui_logs())
        with patch(find_client_windows=lambda: (list(small), [])):
            app.on_autolocate()
        new_lines = ui_logs()[n_before:]
        check("自动定位:已经是最小分辨率就不刷那条提醒",
              any("686×478 ≈ 640×480 档" in m for m in new_lines)
              and not any("不是最小分辨率" in m for m in new_lines),
              new_lines[:2])

        check("分辨率:3 档尺寸都认得出来(最小档才不提醒)",
              core.client_size_info([0, 0, 640, 480])[1] is True
              and core.client_size_info([0, 0, 860, 600]) == (
                  "860×600 ≈ 860×600 档", False)
              and core.client_size_info([0, 0, 1024, 768])[1] is False
              and core.client_size_info([0, 0, 2048, 1536])[1] is False,
              str(core.client_size_info([0, 0, 640, 480])))

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

        app.engine.keju_tool.round_stats = {"n": 3, "resp_sum": 1.2, "unmatched": 1}
        app.on_reset_stats()
        check("界面:重置本局统计",
              app.engine.keju_tool.round_stats == {"n": 0, "resp_sum": 0.0, "unmatched": 0})

        app.log("这条只写日志文件", ui=False)
        app.log("这条进界面滚动框")
        app._drain_logs()
        box = app.log_box.get("1.0", "end")
        filetxt = open(sb.log, encoding="utf-8").read()
        check("界面:ui=False 的消息只进日志文件,不进滚动框",
              "这条进界面滚动框" in box and "这条只写日志文件" not in box
              and "这条只写日志文件" in filetxt)

        app.log("这条会被清空按钮清掉")
        app._drain_logs()
        had = "这条会被清空按钮清掉" in app.log_box.get("1.0", "end")
        app.on_clear_log()
        check("界面:清空日志只清滚动框,不动 keju.log",
              had and app.log_box.get("1.0", "end").strip() == ""
              and "这条会被清空按钮清掉" in open(sb.log, encoding="utf-8").read())

        app.on_toggle_mute()
        muted = (app.cfg["app"]["mute"] is True and "开" in str(app.mute_btn["text"])
                 and json.load(open(sb.config, encoding="utf-8"))["app"]["mute"] is True)
        app.on_toggle_mute()
        check("界面:静音按钮切换状态并落盘",
              muted and app.cfg["app"]["mute"] is False
              and "关" in str(app.mute_btn["text"]))

        class RecSp:
            def __init__(self):
                self.said = []

            def say(self, text):
                self.said.append(text)

            def quit(self):
                pass

        real_sp = app.speaker
        app.speaker = RecSp()
        app.on_test_tts()
        check("界面:播报试听按钮把测试语句交给语音线程",
              app.speaker.said == ["语音测试,C铁观音,唐朝"])
        fake_sp = app.speaker
        app.speaker = real_sp

        app.last_answer = "一阵飓风卷起了龙宫的一些物品散落建邺城"
        app.on_mini()
        app._drain_logs()
        app.mini.update()
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
        app.on_restore()
        check("界面:还原 -> 迷你窗销毁、主窗恢复",
              app.mini is None and str(app.root.state()) == "normal")

        class FakeEngine:
            def __init__(self, a):
                self.keju_tool = types.SimpleNamespace(   # 科举那套状态在工具自己身上
                    round_stats={"n": 0, "resp_sum": 0.0, "unmatched": 0},
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

        app.speaker = fake_sp

        app.on_mini()
        app.on_mini()                     # 第二次:只把已有小窗提到前面
        check("界面:重复点迷你置顶只提升已有窗口", app.mini is not None)

        app.interval_var.set("0.05")      # 低于下限
        app._save_interval()
        low = app.cfg["keju"]["interval"]
        app.interval_var.set("abc")       # 非法输入
        app._save_interval()
        check("界面:间隔输入被夹到下限、非法输入不写进配置",
              low == 0.4 and app.cfg["keju"]["interval"] == 0.4
              and app.interval_var.get() == "abc", f"{low}")

        app._set_exam_mode(True)          # 与当前一致:直接返回(不重复写盘)
        app.mode_keju_btn.destroy()       # 按钮没了 -> 刷新要兜住
        app._refresh_mode_btn()
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

        class HalfBtn:
            def configure(self, **kw):
                if "style" in kw:            # 文本能设,样式设不了
                    raise RuntimeError("主题不支持该样式")

        real_btn = app.watch_btn
        app.watch_btn = HalfBtn()
        app._refresh_watch_btn()
        app.watch_btn = real_btn
        check("界面:监听按钮样式设置失败也不影响状态刷新", True)

        app._auto_bank_check()            # 当天已检查过:直接返回(不联网)
        app.cfg["app"]["last_bank_check"] = ""
        with patch(save_config=raiser(OSError("配置写不进去"))):
            app._auto_bank_check()
            time.sleep(0.2)
        check("界面:记录'今天已检查'失败也不影响启动",
              app.cfg["app"].get("last_bank_check") != "")

        class FakeEngineOK(FakeEngine):
            def is_alive(self):
                return True

        class DeadEngine(FakeEngineOK):
            def is_alive(self):
                return False

        class BoomEngine:
            def __init__(self, a):
                raise RuntimeError("引擎起不来")

        import datetime as _dt
        today = _dt.date.today().isoformat()
        if not FAST:
            # 更新题库 / 自动检查这两组要反复加载真题库(约 4 秒),快速模式跳过
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

            with patch(update_bank=lambda: (False, "题库已是最新(5 题)")):
                app.on_update_bank()
                for _ in range(60):
                    if not getattr(app, "_bank_updating", False):
                        break
                    time.sleep(0.05)
                n_first = sum(1 for m in ui_logs() if "[题库]正在检查更新" in m)
                app._bank_updating = True           # 模拟正在更新:按钮应防重入
                app.on_update_bank()
                app._bank_updating = False
                check("界面:更新题库按钮后台执行且防重入",
                      n_first == 1 and not getattr(app, "_bank_updating", False))

            app.cfg["app"]["last_bank_check"] = ""
            with patch(official_chunk_hash=lambda timeout=15: ""):
                app._auto_bank_check()
                time.sleep(0.3)
            check("界面:题库检查断网时静默跳过并记下当天已检查",
                  app.cfg["app"]["last_bank_check"] == today)

            app.cfg["app"]["last_bank_check"] = ""
            with patch(official_chunk_hash=lambda timeout=15: "same",
                       _local_meta=lambda: {"chunk_hash": "same"}):
                app._auto_bank_check()
                time.sleep(0.3)
            check("界面:哈希一致时不再下载题库(只记已经检查过)",
                  app.cfg["app"]["last_bank_check"] == today)

            app.cfg["app"]["last_bank_check"] = ""
            with patch(official_chunk_hash=lambda timeout=15: "changed",
                       _local_meta=lambda: {"chunk_hash": "same"},
                       update_bank=lambda: (False, "题库已是最新(5 题)")):
                app._auto_bank_check()
                time.sleep(0.4)
            check("界面:发现新版本会自动走更新流程",
                  any("[题库]检测到新版本" in m for m in ui_logs())
                  and not getattr(app, "_bank_updating", False))

            app.cfg["app"]["last_bank_check"] = ""
            with patch(official_chunk_hash=raiser(OSError("断网"))):
                app._auto_bank_check()
                time.sleep(0.3)
            check("界面:题库检查异常被忽略(不影响使用)",
                  app.cfg["app"]["last_bank_check"] == today)

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
        app.engine.keju_tool.round_stats = None        # 统计坏掉:刷新统计该兜住
        app._drain_logs()
        app.engine.keju_tool.round_stats = {"n": 1, "resp_sum": 0.5, "unmatched": 0}
        app.watch_btn.destroy()              # 按钮没了:直接刷新也不该抛
        try:
            app._refresh_watch_btn()
            btn_ok = True
        except Exception:
            btn_ok = False
        app._drain_logs()
        check("界面:监听按钮被销毁后直接刷新不抛异常(回归)", btn_ok,
              f"btn_ok={btn_ok}")

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
              filetxt.count("刷新项「本局统计」") == 1
              and filetxt.count("刷新项「监听按钮」") == 1,
              [l for l in filetxt.splitlines() if "刷新项" in l][:3])
        check("界面:引擎状态读取异常时刷新链不断", True)
        app.engine = FakeEngineOK(app)

        app.log("日志文件写不进去时的消息")
        with patch(LOG_PATH=sb.dir):         # 把日志路径指成目录 -> 写入失败
            app.log("这条写不进文件")
        check("界面:迷你窗/统计/按钮/日志写入异常都不会影响主循环", True)
        app.on_restore()

        # ---- 收尾:覆盖 App.run() 与 on_close ----
        app.speaker = real_sp
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
        eng._safe("cycle", True)
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
                   cmd_tts_test=lambda: 0, cmd_stats=lambda since="": 0,
                   cmd_rewards=lambda: 0, cmd_uncollected=lambda add=(): 0,
                   update_bank=lambda: (True, "题库已更新: 1 → 2 题"),
                   build_voice_cache=lambda *a, **k: (1, 0, 0),
                   check_deps=lambda: [], ensure_data_files=lambda: None):
            for flag in ("--selftest", "--tts-test", "--stats", "--update-bank",
                         "--rewards", "--uncollected"):
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

        with patch(check_deps=lambda: ["numpy"], ensure_data_files=lambda: None):
            sys.argv = ["keju.py"]
            rc, out = capture(keju.main)
            check("命令行:缺依赖时给安装提示并返回 1",
                  rc == 1 and "缺少依赖" in out, out.splitlines()[0] if out else "")

        with patch(check_deps=lambda: [], ensure_data_files=lambda: None,
                   BANK_PATH=os.path.join(tempfile.gettempdir(),
                                          "keju_没有这个题库.json")):
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
        eng._safe("cycle", True)
        check("cycle:区域截图失败 -> 记日志并安全退出(不崩)",
              any("截图失败" in m for m in app.logs))

        eng, app = mk_engine(bank, [], cfg={"keju": {"exam_mode": False}})
        eng.ocr = types.SimpleNamespace(recognize_blocks=lambda img, **kw: [])
        eng._safe("cycle", True)
        check("cycle:区域里一个字都没识别到 -> 本轮跳过", not app.said)

        eng, app = mk_engine(bank, [blk("短", 100, 200)],
                             cfg={"keju": {"exam_mode": False}})
        eng._safe("cycle", True)
        check("cycle:三大学士模式下的碎片文本(<4 字)-> 跳过", not app.said)

        eng, app = mk_engine(bank, [blk("完全无关的一段文字内容", 100, 200)],
                             cfg={"keju": {"exam_mode": False}})
        eng._safe("cycle", True)
        check("cycle:区域文字与题库对不上 -> 记未匹配(不播报)",
              not app.said and eng.keju_tool.round_stats["unmatched"] == 1
              and any("未找到匹配" in m for m in app.logs),
              str(eng.keju_tool.round_stats))

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
        eng._safe("cycle", False)
        check("多开:一个区域没匹配不挡同轮其他客户端(一轮看完全部区域)",
              app.said == ["答案甲"] and seen["n"] == 2,
              f"{app.said} OCR {seen['n']} 次")

        eng, app = mk_engine(bank, [blk(HDR + QA, 100, 200)])
        eng._safe("cycle", False)               # 首帧识别
        eng._safe("cycle", False)               # 同一画面:被帧门控挡住
        check("cycle:画面没变时第二次直接跳过", app.said == ["答案甲"])

        eng, app = mk_engine(bank, [], cfg={"keju": {"regions": [None] * core.MAX_CLIENTS}})
        eng.screen = BoomScreen()
        eng.keju_tool._manual_fs = True                       # 手动扫描:要看得见结果
        check("_fullscan_once:全屏截图失败 -> 返回 0 并记日志",
              eng.keju_tool._fullscan_once() == 0
              and any("[全屏扫描]截图失败" in m for m in app.logs))

        class BadWarmOcr:
            def warmup(self):
                raise RuntimeError("模型文件损坏")

        app2 = types.SimpleNamespace()
        app2.cfg = core.split_config({"regions": [None] * core.MAX_CLIENTS,
                                      "interval": 0.4, "exam_mode": True})
        app2.logs = []
        app2.log = lambda msg, ui=True: app2.logs.append(msg)
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
        app3.log = lambda msg, ui=True, detail="": app3.logs.append(msg)
        app3.speaker = types.SimpleNamespace(say=app3.said.append)
        e3 = core.Engine(app3)
        e3.ocr = RunOcr([blk(HDR + QA, 100, 200)])
        with patch(Screen=lambda: FakeScreen()), \
                patch(find_client_windows=lambda *a, **k: ([], [])):
            e3.start()
            time.sleep(0.4)
            e3.set_watch(True)                  # 监听中的 watch 任务
            time.sleep(0.3)
            e3.request_cycle()                  # 监听中手动识别
            time.sleep(0.3)
            e3.request_fullscan()               # 监听中全屏扫描
            time.sleep(0.4)
            e3.set_watch(False)
            time.sleep(0.4)
            e3.request_cycle()                  # 空闲时手动识别
            time.sleep(0.3)
            e3.request_fullscan()               # 空闲时全屏扫描
            time.sleep(0.5)
        check("任务循环:监听中/空闲时的手动任务都被执行",
              set(app3.said) == {"答案甲"} and len(app3.said) >= 2
              and any("[全屏扫描]" in m for m in app3.logs)
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
            core._play_mp3_blocking("x.mp3")
        finally:
            _ct.windll = real_windll
        check("_play_mp3_blocking:正常走 open→play wait→close",
              len(mci_calls) == 3 and "open" in mci_calls[0]
              and "play" in mci_calls[1] and "close" in mci_calls[2],
              str(mci_calls[:1]))

        fail_calls = []

        class FailWinmm:
            def mciSendStringW(self, cmd, buf, n, h):
                fail_calls.append(cmd)
                return 1                        # MCI 非 0 = 失败

        raised = False
        try:
            _ct.windll = types.SimpleNamespace(winmm=FailWinmm())
            try:
                core._play_mp3_blocking("x.mp3")
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

            def Speak(self, text):
                self.spoke = text

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

        logs_edge = []
        sp_edge = core.Speaker({"app": {"tts_backend": "edge"}},
                               logs_edge.append)
        sp_edge.start()
        sp_edge._ready.wait(10)
        sp_edge._speak_edge = lambda text: True      # 不真的合成/播放
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

        def no_hash(url, timeout=30):
            if "app." in url:
                return b"var x=1;"                  # 没有 306:"xxxx"
            return b'<script src="static/js/app.abc12345.js"></script>'

        with patch(_http_get=no_hash):
            check("official_chunk_hash:找不到哈希时返回空串",
                  bank_mod.official_chunk_hash() == "")

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
            e_._safe("cycle", True)
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

            with patch(Speaker=_FakeSpeakerForTts), fast_sleep():
                rc2, out2 = capture(cli.cmd_tts_test)
            check("cmd_tts_test:语音测试命令能正常走完", rc2 == 0 and "语音" in out2,
                  out2.strip()[:24])
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
            eng._safe("cycle", True)
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
            eng_o._safe("cycle", True)
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
            def Speak(self, text):
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
            check("official_chunk_hash:页面里没有 app.js 时返回空串",
                  bank_mod.official_chunk_hash() == "")

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

        class DeadSp:
            def __init__(self, cfg, log):
                self.voice_name = ""
                self.dead = True
                self._ready = threading.Event()
                self._ready.set()          # 置位,靠 dead 判定失败

            def start(self):
                pass

            def say(self, text):
                pass

        with patch(Speaker=DeadSp):
            rc, out = capture(cli.cmd_tts_test)
        check("cmd_tts_test:语音引擎起不来时返回 1 并提示",
              rc == 1 and "启动失败" in out, out.strip())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeSpeakerForTts:
    """给 cmd_tts_test 用的假语音线程(不发声)。"""

    def __init__(self, cfg, log):
        self.voice_name = "测试音色"
        self.dead = False
        self._ready = threading.Event()
        self._ready.set()
        self.said = []

    def start(self):
        pass

    def say(self, text):
        self.said.append(text)


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
