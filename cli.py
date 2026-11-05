#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行层:--selftest / --stats / --uncollected / --rewards / --tts-test 等 cmd_*。

从 core.py 搬出来的(2026-10-22):core 只留地基(配置/OCR/截图/语音/引擎调度),
自检的合成图夹具与统计解析都是"命令行才用"的东西,归这一层。
与 gui.py 同一条规矩:core / bank / tools 里的名字一律调用时现取(core.xxx),
不拷贝到本模块 —— patch() 换 core/bank 上的那份,这里立刻跟着变。
"""
import json
import os
import random
import re
import time

import bank
import core
from tools import keju as tools_keju

# 自检夹具用的中文字体(只有合成图会用,按找到的第一个为准)
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


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
    读都读不出来记为 WARN(不计入失败)—— 严格的回归门禁在 tests.py。
    """
    import numpy as np   # 只在自检里用:入口不能因为没有 numpy 就 import 失败

    if seed:
        random.seed(seed)
    qbank = bank.QuestionBank()
    ocr = core.Ocr()

    groups: dict[str, list] = {}
    for e in qbank.entries:
        groups.setdefault(e["q"], []).append(e)
    ambiguous = [v for v in groups.values() if len({x["a"] for x in v}) > 1]

    cases = random.sample(qbank.entries, 4)
    if ambiguous:
        cases += random.choices(random.choice(ambiguous), k=2)

    passed = 0
    for e in cases:
        noisy = _add_ocr_noise(e["q"])
        q_img = _render_question(noisy)
        q_text = ocr.recognize(np.asarray(q_img)[:, :, ::-1].copy())

        # 构造选项:正确答案 + 两个随机干扰答案
        others = [x["a"] for x in random.sample(qbank.entries, 12) if x["a"] != e["a"]][:2]
        opts_line = f"A.{e['a']}  B.{others[0]}  C.{others[1]}"
        o_img = _render_question(opts_line, size=26)
        o_text = ocr.recognize(np.asarray(o_img)[:, :, ::-1].copy())

        candidates = qbank.match_all(q_text, 62)
        entry, score, note = qbank.pick_with_options(candidates, o_text)
        ok = entry is not None and entry["a"] == e["a"]
        passed += ok
        got = f"{entry['a']}{note}" if entry else "无匹配"
        print(f"[{'PASS' if ok else 'FAIL'}] 相似度{score:3.0f}% "
              f"“{e['q'][:20]}…” -> “{got}” 期望“{e['a']}”")
    print(f"\n自检结果: {passed}/{len(cases)} 通过(含 OCR 噪声与选项消歧模拟)")

    # ---- 整屏找题场景(锚点/滑窗/聚类,三大学士模式同一套匹配核心) ----
    print("\n-- 整屏找题场景 --")
    e1 = random.choice(qbank.entries)
    fs_pass = True

    img = _render_fullscreen([_add_ocr_noise(e1["q"], truncate=False)])
    blocks = ocr.recognize_blocks(np.asarray(img)[:, :, ::-1].copy())
    n_q, entry, score, note, _ = tools_keju.resolve_questions(qbank, blocks, 70)
    ok = n_q == 1 and entry is not None and entry["a"] == e1["a"]
    detail = f"相似度{score:.0f}% -> “{entry['a']}”{note}" if entry else "无结果"
    fs_pass &= _selftest_report(
        ok, (not blocks) or (n_q == 0 and score < 65), "单题+干扰UI",
        f"判定 {n_q} 道题, {detail} 期望“{e1['a']}”")

    e2 = random.choice(qbank.entries)
    while e2["n"] == e1["n"]:
        e2 = random.choice(qbank.entries)
    img = _render_fullscreen([e1["q"], e2["q"]])
    blocks = ocr.recognize_blocks(np.asarray(img)[:, :, ::-1].copy())
    n_q, _, _, _, _ = tools_keju.resolve_questions(qbank, blocks, 70)
    fs_pass &= _selftest_report(
        n_q >= 2, n_q < 2, "双题检测",
        f"判定 {n_q} 道题(期望≥2,应播报提醒)")

    # 回声抑制:屏幕上同时有"刚答过的旧题残留"和"新题",应忽略旧题答新题
    e0 = random.choice(qbank.entries)
    while e0["n"] in (e1["n"], e2["n"]):
        e0 = random.choice(qbank.entries)
    img = _render_fullscreen([e0["q"], e2["q"]])
    blocks = ocr.recognize_blocks(np.asarray(img)[:, :, ::-1].copy())
    n_all, _, _, _, _ = tools_keju.resolve_questions(qbank, blocks, 70)
    n_fix, entry_fix, _, note_fix, _ = tools_keju.resolve_questions(
        qbank, blocks, 70, recent=[e0["n"]])
    suppressed_ok = (n_fix == 1 and entry_fix is not None
                     and entry_fix["a"] == e2["a"])
    got_fix = f"“{entry_fix['a']}”{note_fix}" if entry_fix else "无结果"
    fs_pass &= _selftest_report(
        suppressed_ok and n_all >= 2, suppressed_ok and n_all < 2,
        "回声抑制", f"无记忆判定 {n_all} 题 / 有记忆判定 {n_fix} 题 -> "
                    f"{got_fix} 期望“{e2['a']}”")

    # ---- 短文本防误报 ----
    lt0 = qbank.match("是", 62)[0]
    ok = lt0 is None
    print(f"[{'PASS' if ok else 'FAIL'}] 短文本防误报: 单字“是”匹配={'有' if lt0 else '无'}(期望无)")
    fs_pass &= ok

    # 窄栏布局(带图):题干拆成多行短句,验证大窗口能拼出完整题干
    narrow_target = next(e for e in qbank.entries if len(e["q"]) >= 18)
    nimg = _render_narrow(narrow_target["q"])
    nblocks = ocr.recognize_blocks(np.asarray(nimg)[:, :, ::-1].copy())
    n_all, n_entry, n_score, _, _ = tools_keju.resolve_questions(qbank, nblocks, 65)
    ok = (n_all == 1 and n_entry is not None
          and n_entry["a"] == narrow_target["a"])
    got_n = f"{n_score:.0f}% -> {n_entry['a']}" if n_entry else "无"
    fs_pass &= _selftest_report(
        ok, (not nblocks) or (n_all == 0 and n_score < 65), "窄栏(带图)布局",
        f"判定 {n_all} 题, {got_n} 期望“{narrow_target['a']}”")

    # ---- 锚点剥头 ----
    print("\n-- 锚点剥头 --")
    s1 = tools_keju.strip_exam_header("御前科举大赛第9关：这一关考的是综合。题目：下列哪种茶叶不属于绿茶？")
    s2 = tools_keju.strip_exam_header("没有任何头的普通题目？")
    ok = ("下列哪种茶叶" in s1 and "御前科举" not in s1
          and s2 == "没有任何头的普通题目？")
    print(f"[{'PASS' if ok else 'FAIL'}] 剥头: 有头->{s1[:14]}… 无头不变={s2 == '没有任何头的普通题目？'}")
    fs_pass &= ok

    # 带“御前科举大赛…题目:”完整头的对话框,端到端应命中裸题库
    hx = random.choice(qbank.entries)
    hdr_q = "御前科举大赛第9关：这一关考的是综合。题目：" + hx["q"]
    himg = _render_fullscreen([hdr_q])
    hblocks = ocr.recognize_blocks(np.asarray(himg)[:, :, ::-1].copy())
    n_h, entry_h, score_h, _, _ = tools_keju.resolve_questions(qbank, hblocks, 70)
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
    cfg = core.split_config(core.DEFAULT_CONFIG)
    sp = core.Speaker(cfg, lambda m: print("[语音]", m))
    sp.start()
    if not sp._ready.wait(10) or sp.dead:
        print("语音引擎启动失败")
        return 1
    print(f"语音: {sp.voice_name}(音调 {core.edge_params(cfg)[2]:+d}Hz)")
    sp.say("梦幻西游科举答题器,语音测试成功。")
    time.sleep(4)
    return 0


def cmd_rewards() -> int:
    """打印今天的答题收益(银两/经验/物品明细)。"""
    rt = tools_keju.RewardTally()
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
    if not os.path.exists(core.UNCOLLECTED_PATH):
        print(f"还没有记录(文件: {core.UNCOLLECTED_PATH})")
        print("遇到“读到像题目、但匹配分不到 %d%%”的读法才会写进去。"
              % tools_keju.MIN_MATCH_PCT)
        return 0
    rows = []
    try:
        with open(core.UNCOLLECTED_PATH, encoding="utf-8") as f:
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
        uniq[core.normalize(r.get("text", ""))] = r      # 同一段画面文字只留最后一次
    qbank = None
    try:
        # 显式传路径:BANK_PATH 在测试里会被指到临时目录
        qbank = bank.QuestionBank(bank.BANK_PATH)
    except Exception:
        pass                  # 题库读不出来也不该挡住"看清单"这件事
    items, done = [], 0
    for r in sorted(uniq.values(), key=lambda r: -int(r.get("score", 0))):
        q = r.get("q") or r.get("text", "")
        if qbank is not None and qbank.match(q, 95)[0] is not None:
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
        if tools_keju.add_manual_question(q, ans, act):
            added += 1
            print(f"已补进题库[{act}]: {q} → {ans}")
        else:
            print(f"题库里已有: {q} → {ans}")
    for b in bad:
        print(f"跳过: {b}")
    if added:
        print(f"补了 {added} 条,题库共 {len(bank.QuestionBank(bank.BANK_PATH))} 题"
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


def _num(value, default: float = 0.0) -> float:
    """事件字段转数字:手改/写坏的值(含 null)按默认值算,别让 --stats 崩。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _events_from_log(path: str, since_norm: str) -> list[dict]:
    """从 keju.log 的文本行解析出识别事件(旧数据回退;新数据在 events.jsonl)。"""
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if since_norm and len(line) >= 16 and line[11:16] < since_norm:
                continue
            for kind, pat in _STATS_PATTERNS:
                m = pat.match(line)
                if not m:
                    continue
                g = m.groups()
                ev = {"ts": g[0], "kind": kind}
                if kind == "match":
                    ev.update(score=float(g[1]), note=g[2].strip(),
                              answer=g[3].strip(), response=float(g[4]),
                              ocr=float(g[5]), match=float(g[6]))
                elif kind == "match_old":
                    ev.update(score=float(g[1]), note=g[2].strip(),
                              answer=g[3].strip(), response=float(g[6]),
                              ocr=float(g[4]), match=float(g[5]))
                    ev["kind"] = "match"
                elif kind == "nomatch":
                    ev.update(score=int(g[1]), refresh=bool(g[2]))
                elif kind == "blind":
                    ev.update(refresh=bool(g[1]))
                elif kind == "mode":
                    ev["mode"] = g[1].strip()
                elif kind == "seen":
                    ev["text"] = g[1]
                events.append(ev)
                break
    return events


def _events_from_jsonl(path: str, since_norm: str) -> list[dict]:
    """从 data/events.jsonl 读结构化识别事件(识别管线直写,不怕日志改措辞)。"""
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except Exception:
                continue                  # 坏行跳过,别让一条脏数据废掉统计
            if not isinstance(ev, dict):  # 合法 JSON 但不是事件(手改/写坏)
                continue
            ts = str(ev.get("ts", ""))
            if since_norm and len(ts) >= 16 and ts[11:16] < since_norm:
                continue
            if ev.get("kind") in ("match", "nomatch", "blind", "dup", "low",
                                  "mode", "boot"):
                ev["ts"] = ts             # 缺 ts 的行也归一,后面按 ts 分段
                events.append(ev)
    return events


def cmd_stats(since: str = "") -> int:
    """命中率/失败构成(分模式段) + '题目出现 → 出答案'响应性能。

    数据源优先 data/events.jsonl(识别管线直写的结构化事件);没有这个文件
    (老版本跑出来的数据)才回退解析 keju.log 文本。since 如 '19:30'。
    """
    source = "events"
    if os.path.exists(core.EVENTS_PATH):
        events = _events_from_jsonl(core.EVENTS_PATH, since.strip())
    elif os.path.exists(core.LOG_PATH):
        source = "log"
        events = _events_from_log(core.LOG_PATH, since.strip())
    else:
        print("还没有日志文件")
        return 1
    # 只统计最新一天(正常就是当天):应用跨午夜长跑时,事件文件里会留着
    # 前一天的行;启动时的 prune_events 只在启动那一刻清理,这里再兜一道。
    day = max((str(ev.get("ts", ""))[:10] for ev in events), default="")
    if day:
        events = [ev for ev in events if str(ev.get("ts", ""))[:10] == day]
    print(f"数据源: {'data/events.jsonl(结构化事件)' if source == 'events' else 'keju.log(文本解析,旧数据)'}")

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
    for ev in events:
        kind, ts = ev["kind"], ev["ts"]
        if kind == "mode":
            # 普通/三大学士是同一个模式的两种叫法(改了界面文案)
            name = ev.get("mode", "")
            mode = ("三大学士" if ("三大" in name or "普通" in name)
                    else "御前科举")
            cur = new_seg(ts)
            continue
        if kind == "boot":
            cur = new_seg(ts)
            continue
        if kind == "seen":
            last_seen = ev.get("text", "")
            continue
        cur["ts1"] = ts
        if kind == "match":
            cur["match"] += 1
            cur["resp"].append(_num(ev.get("response")))
            if "按选项消歧" in (ev.get("note") or ""):
                cur["disamb"] += 1
        elif kind == "nomatch":
            if ev.get("refresh"):
                continue          # "(连续N次)"= 同一次漏答在刷新,不重复计数
            text = ev.get("text", "")
            if tools_keju.is_exam_prompt(text) or tools_keju.is_exam_prompt(last_seen):
                # 考官开场白(不是题目):新版本已在校验前过滤掉,老数据里还在
                cur["prompt"] += 1
                continue
            cur["nomatch"] += 1
            near = int(_num(ev.get("score")))
            if near > 0:          # 有候选 = 疑似真题没答出,值得逐条看
                cur["near"].append((ts, near, (text or last_seen)[:24]))
        elif kind == "blind":
            if not ev.get("refresh"):
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

    rows = [(ev["ts"], _num(ev.get("score")), ev.get("answer") or "",
             _num(ev.get("response")), _num(ev.get("ocr")),
             _num(ev.get("match")))
            for ev in events if ev.get("kind") == "match"]

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
        print(f"{ts:<19}{score:>4.0f}%{resp:>7.2f}s{ocr:>7.2f}s{match:>6.2f}s  {ans[:26]}")
    print("-" * 88)
    bad = [r for r in rows if r[1] < 80]
    if bad:
        print(f"低置信匹配(得分<80%,共{len(bad)}题,建议抽查):")
        for ts, score, ans, *_ in bad:
            print(f"  {ts}  {score:.0f}%  {ans[:30]}")
    return 0


def cmd_build_voice_cache(limit: int, workers: int = 4) -> int:
    """命令行入口:全量预合成答案音频(合成实现在 core.build_voice_cache)。"""
    done, failed, skipped = core.build_voice_cache(limit, workers, log=print)
    print(f"完成:新合成 {done},失败 {failed},原有 {skipped}")
    return 0 if failed == 0 else 1
