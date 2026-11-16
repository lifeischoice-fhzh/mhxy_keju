"""科举大赛:一个完全独立的功能。

自己一条线程、自己的区域和状态,匹配(锚点/滑窗/全屏聚类)都在这里。
跟 core / bank 只有一条单行道:调用时现取截图 / OCR / 文本归一化这些基础设施,
题库(读写/匹配)走 bank.py —— 这是唯一被允许的共享,所以 patch() 换掉
core / bank 里的名字,它立刻跟着变。
"""
import json
import os
import re
import sys
import threading
import time

try:
    import numpy as np
except ImportError:  # core 缺 numpy 时会给出友好提示,这里别让 import 就炸
    np = None

TAG = "科举"

# 数据目录的根:跟 core.BASE_DIR 同一套规则(打包后跟着可执行文件走)。
# 这里不能在模块级 import core —— core 要在模块级 import 本文件,反过来在
# 模块级取 core 的名字只会拿到一个还没加载完的 core。
if getattr(sys, "frozen", False):          # PyInstaller 打包后
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 题干最短字数:真题里最短的只有 4 个字(如""藕"是莲的"->茎),
# 门槛比它更低只是为了挡掉碎字,真正的护栏是下面的匹配下限。
MIN_QUESTION_CHARS = 4

# 同一道题在这个秒数内再次被识别到,视为"刚播过",不重复播报(防同一画面连播)
REPEAT_GUARD_S = 5

# 自动识别的匹配下限:分数不到这里,宁可不说,也不播一个可能错的答案。
# (实测:≥84% 是真题 + OCR 小噪声,≤75% 全是误报;配置阈值只能往上调,调不低)
MIN_MATCH_PCT = 80

# 选项兜底的最低分:题目读脏(OCR 丢字/混入 HUD 与聊天)时分数会掉到
# 50~79,按 MIN_MATCH_PCT 一律不播 —— 但选项是强信号:候选答案必须
# (严格)就是当屏某条选项才救;同题干多答案变体(答案都在屏上时)再用
# 题干里的引号字判别。2026-11-12 车迟斗法实测:唐朝的"唐"/"朝"字由
# 多少笔画,最脏的读法 56 分、最好的 78 分,屏上四选项里两个变体的
# 答案同时在 —— 光按分数会选错,引号字"朝"才唯一对上。
# 门槛 50 是给 find_questions 的预筛(内部再减 15)留余量,真正的闸
# 是选项命中与变体判别,分数只用来排序。
RESCUE_MIN_PCT = 50
_QUOTED = re.compile(r'[“「『"]([^”」』"]{1,4})[”」』"]')

# 无锚点模式(三大学士/副本答题)只扫底部题面对话框:整窗其余全是噪声,
# 还拖慢 OCR。对话框是固定 UI,按客户端占比框一条带,四周留足余量
# (按 860×600 档实测对话框约横 6%~88%、纵 45%~70%)。
PLAIN_BAND = (0.04, 0.42, 0.91, 0.73)   # (左, 上, 右, 下) 占整窗比例
# 歧义题的选项兜底:车迟斗法/金兜兕的对话框更高,选项可能掉在 PLAIN_BAND
# 之外;这时再扫一条更宽的底部带(只在"同题干多答案"且带内没认出选项时走)
OPTION_BAND = (0.04, 0.34, 0.91, 0.96)

# 会试锚点偶发整行读不出(OCR 误读/对话框渐入):此时题干本体通常仍读得全。
# 本区域最近认出过题目(EXAM_FALLBACK_S 内)时,锚点没认出的读法退回
# "全题干匹配"兜底,真命中(≥下限)才采用 —— 2026-11-08 会试实测:两题的
# 关卡头整屏读不出,画面静置后被帧门控拦住再不重读,答案整场没出,
# 连按手动识别读到同一批像素也一样认不出
EXAM_FALLBACK_S = 120
# 兜底也没接住时的复检:过 RECHECK_DELAY_S 无视帧门控再读一次,最多
# RECHECK_MAX 次(OCR 对同一画面不是每次读得一样,多读一两轮常就认出了;
# 次数用完就停,画面真变了会重新给预算)
RECHECK_DELAY_S = 2.0
RECHECK_MAX = 3

# 手动识别的 det 输入下限:自动轮询走引擎默认(736),手动这颗钮的价值
# 在"同批像素换一种读法" —— 自动已经失败的画面,原样重读只会复现同一
# 次失败(2026-11-16 用户反馈"识别不出时按手动一样不给答案"),抬高下限
# 才读得出不一样的结果;窄带复检靠这个杠杆救回过整行漏读(「长沙马王堆」)
MANUAL_DET_LIMIT = 1280


def _band(x1, y1, x2, y2, band):
    """整窗矩形 -> 按占比算出的子区域。"""
    w, h = x2 - x1, y2 - y1
    return (int(x1 + w * band[0]), int(y1 + h * band[1]),
            int(x1 + w * band[2]), int(y1 + h * band[3]))


def plain_band(x1, y1, x2, y2):
    """整窗矩形 -> 无锚点模式真正要 OCR 的对话框带(对应 PLAIN_BAND)。"""
    return _band(x1, y1, x2, y2, PLAIN_BAND)


# 锚点触发:块里有"科举",且出现"大赛"/"题目",或"第X关"的结构。
# 不要求"御前"在场 —— 日志实测"御前科举大赛第16关"常被 OCR 读成
# "前科举大赛第16关""何以前科举大赛第15关",死认那四个字会让整个区域静默失效。
_ANCHOR_STAGE = re.compile(r"第\s*[0-9一二三四五六七八九十百零两]*\s*关")
# 剥离头:同样容忍"御""大赛"被读丢(前面的杂字一并切掉,如被叠进来的聊天/名字)
_EXAM_HEADER = re.compile(r".*?科举(?:大赛)?.*?题目[:：]?")

# 殿试答错后换题:"你的答案不正确,答题时间额外增加了10秒,请答另一题:…"
# 这一屏会把"科举大赛殿试部分第N关…题目:"整行换掉,锚点随之消失
# (2026-11-07 实测):死认科举头的话,后面每道换来的题都会被当"不是科举界面"跳过。
# 长句可能被 OCR 拆成两行(尾行只剩"另一题:…"),所以"另一题"也算数。
_AGAIN_KEYS = ("另一题", "其他题", "下一题")
_AGAIN_HEADER = re.compile(r".*?(?:另一题|其他题|下一题)[:：]?")


def _is_again_header(text: str) -> bool:
    """这段文字是不是"答错了,请答另一题"的换题对话框头。"""
    return any(k in (text or "") for k in _AGAIN_KEYS)


def is_exam_anchor(text: str) -> bool:
    """这块文字是不是"御前科举大赛第X关:…题目:"的准考证头(容忍 OCR 错字)。"""
    t = text or ""
    if _is_again_header(t):
        return True
    if "科举" not in t:
        return False
    return ("大赛" in t or "题目" in t
            or _ANCHOR_STAGE.search(t) is not None)


def strip_exam_header(text: str) -> str:
    """剥掉准考证头/换题提示头,返回裸题干。"""
    t = text or ""
    if _is_again_header(t):
        return _AGAIN_HEADER.sub("", t, count=1)
    return _EXAM_HEADER.sub("", t, count=1)


def extract_anchored_question(blocks):
    """按结构锚点提取题干:找到准考证头那块,向下紧贴收集题目行。

    题目行间距很小(约25px一行的排版),选项区在题目下方约100px,
    用行距<=30px 自然截断。返回 (裸题干文本, 参与块下标列表)。
    """
    for i, b in enumerate(blocks):
        if not is_exam_anchor(b["text"]):
            continue
        win = [i]
        prev = b
        for j in range(i + 1, len(blocks)):
            nb = blocks[j]
            gap = nb["y1"] - prev["y2"]
            h_prev = max(1, prev["y2"] - prev["y1"])
            h_new = max(1, nb["y2"] - nb["y1"])
            if (0 <= gap <= 30 and abs(nb["x1"] - prev["x1"]) <= 120
                    and abs(h_new - h_prev) <= max(12, h_prev * 0.6)):
                win.append(j)
                prev = nb
            else:
                break
        text = "".join(blocks[k]["text"] for k in win)
        return strip_exam_header(text), win
    return "", []


def find_questions(bank, blocks: list, threshold: float,
                   use_anchor: bool = True):
    """在整屏的文字块里定位题目(锚点优先,滑窗兜底)。

    对相邻文本块做 1~6 块的滑窗拼接,与题库匹配;
    匹配到的窗口视为"一个题目"。返回 {id(entry): (score, entry, text, win)} ,
    win 为命中窗口的文本块下标列表。
    """
    import core

    hits: dict[int, tuple] = {}
    best_any = None  # (score, entry, text):即使低于阈值也记录,便于诊断

    # 首选:结构锚点。"御前科举大赛…题目:"只有真题目对话框才有,
    # 剥头后与题库(裸题干)精确比对,不受聊天/提示文字干扰
    a_text, a_win = (extract_anchored_question(blocks) if use_anchor
                     else ("", []))
    a_q = core.normalize(a_text)
    if len(a_q) >= MIN_QUESTION_CHARS:
        entry, score = bank.match_norm(a_q, threshold)
        if entry is not None:
            hits[id(entry)] = (score, entry, a_text, a_win)
        if best_any is None or score > best_any[0]:
            best_any = (score, entry, a_text)

    n = len(blocks)

    # 窗口最大 6 块:带图布局的题干在图片右侧窄栏,一行常只有几个字,
    # 需要拼更多块才能凑出完整题干。
    # 窗口最短按 MIN_QUESTION_CHARS 卡:题干被折行时,单独一块可能就是
    # 整个短题干(如"家三宝”是指"),按 6 字卡会把它直接漏掉。
    # 归一化在生成时就地算好(每个窗口只算一次,预筛/精匹配共用);
    # 上限按生成顺序数到即停,不白造后面用不上的窗口。
    def _windows():
        made = 0
        for size in range(1, 7):
            for i in range(n - size + 1):
                text = "".join(b["text"] for b in blocks[i:i + size])
                q = core.normalize(text)
                if len(q) < MIN_QUESTION_CHARS:
                    continue
                if made >= MAX_SCAN_WINDOWS:
                    return
                made += 1
                yield i, size, text, q

    for i, size, text, q in _windows():
        # 先用单指标低成本预筛,通过后再三指标精匹配
        pre = bank.prefilter(q, int(threshold) - 15)
        if pre is None:
            continue
        pre_score, pre_entry = pre
        if best_any is None or pre_score > best_any[0]:
            best_any = (pre_score, pre_entry, text)
        entry, score = bank.match_norm(q, threshold)
        if entry is None:
            continue
        prev = hits.get(id(entry))
        if prev is None or score > prev[0]:
            hits[id(entry)] = (score, entry, text, list(range(i, i + size)))
    return hits, best_any


def resolve_questions(bank, blocks: list, threshold: float,
                      recent=None, use_anchor: bool = True):
    """把找题命中结果聚类成"几道题"并选出答案(自检/三大学士共用的匹配核心)。

    recent: 最近答过的题目(归一化文本)列表;其中的题目再次出现视为回声
    (聊天/日志残留),多题时会被忽略。

    返回 (n_questions, entry, score, note, win_idx):
    - n_questions == 0: 没有题目
    - n_questions >= 2: 屏幕上有多道题(近重复题干会先聚成一簇,不算多题)
    - n_questions == 1: entry/score/note 为选定结果,win_idx 为题目文本块下标
    """
    from rapidfuzz import fuzz

    hits, best_any = find_questions(bank, blocks, threshold,
                                    use_anchor=use_anchor)
    if not hits:
        # n==0 时把最高候选塞进 score/note,便于日志诊断
        best_score = best_any[0] if best_any else 0.0
        best_note = f"最高候选“{best_any[2][:18]}…”" if best_any else ""
        return 0, None, best_score, best_note, []
    items = sorted(hits.values(), key=lambda v: v[0], reverse=True)

    def pos_overlap(w1, w2):
        """两个命中窗口是否来自同一对话框区域(纵向起点相近)。"""
        y1s = [blocks[i]["y1"] for i in w1 if i < len(blocks)]
        y2s = [blocks[i]["y1"] for i in w2 if i < len(blocks)]
        if not y1s or not y2s:
            return False
        return abs(min(y1s) - min(y2s)) <= 60

    clusters: list[list] = []
    for score, entry, text, win in items:
        for cl in clusters:
            # 同一道题的多种读法(题干相似)或同一区域的命中(匹配到"邻居题"),
            # 都算一道题;只有屏幕不同位置才算真的多题
            if (fuzz.ratio(entry["n"], cl[0][1]["n"]) >= 90
                    or pos_overlap(win, cl[0][3])):
                cl.append((score, entry, text, win))
                break
        else:
            clusters.append([(score, entry, text, win)])

    note_extra = ""
    if len(clusters) >= 2 and recent:
        def is_echo(cl):
            rep = cl[0][1]["n"]
            return any(fuzz.ratio(rep, r) >= 90 for r in recent)

        fresh = [cl for cl in clusters if not is_echo(cl)]
        suppressed = len(clusters) - len(fresh)
        if fresh and suppressed:
            clusters = fresh
            note_extra = f" [忽略{suppressed}个疑似回声(刚答过)]"
        # 全部都是回声:保持原样,按最佳重答(可能是手动重扫同一题)

    if len(clusters) >= 2:
        def avg_h(cl):
            w = cl[0][3]
            hs = [blocks[i]["y2"] - blocks[i]["y1"] for i in w if i < len(blocks)]
            return sum(hs) / len(hs) if hs else 0.0

        ordered = sorted(clusters, key=avg_h, reverse=True)
        second_h = max(1.0, avg_h(ordered[1]))
        if avg_h(ordered[0]) >= 1.25 * second_h:
            # 字号差异明显:大字是游戏题目,小字多半是聊天/日志残留
            clusters = [ordered[0]]
            note_extra += " [多题,取字号最大的一道]"
        else:
            return len(clusters), None, 0.0, "", []

    members = clusters[0]
    rep_score, rep_entry, _, rep_win = members[0]
    entry, score, note = rep_entry, rep_score, ""
    if len(members) > 1:
        # 同题近重复(如"二拍的/二拍指"):用屏幕其余文字(含选项)消歧
        rest = "".join(b["text"] for i, b in enumerate(blocks) if i not in rep_win)
        entry, score, note = bank.pick_with_options(
            [(e, s) for s, e, _, _ in members], rest)
        if not note:
            note = " [近重复题干,取最高分]"
    return 1, entry, score, note + note_extra, rep_win


# 考官开场白("这里是御前科举大赛第N关,你作好答题的准备了吗?开始答题/我还没准备")
# 只有这个对话才有这些字样,真题目不会出现 —— 用来把这类读法从界面日志里静默掉
_EXAM_PROMPT_KEYS = ("这里是御前科举大赛", "作好答题的准备", "我还没准备", "开始答题")


def is_exam_prompt(text: str) -> bool:
    """这段文字是不是考官开场对话(不是题目)。"""
    return any(k in (text or "") for k in _EXAM_PROMPT_KEYS)


_UNCOLLECTED_SEEN: set = set()    # 本次运行已记录过的读法,避免刷同一个文件


def record_event(kind: str, **fields) -> None:
    """把一条识别事件追加进 data/events.jsonl(--stats 的首选数据源)。

    事件只认 kind + 字段,不掺日志措辞 —— 日志文案怎么改都断不了统计。
    写失败(磁盘满/被占用)静默:统计是副产品,绝不能拖住识别线程。
    """
    import core
    row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
    row.update(fields)
    try:
        os.makedirs(os.path.dirname(core.EVENTS_PATH), exist_ok=True)
        with open(core.EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 像题干的文字(问句字样):"锚点没认出"的读法靠它决定要不要记事件/复检
_QUESTION_KEYS = ("作者", "出自", "是谁", "哪位", "什么", "哪一", "哪个",
                  "正确的是", "指的是")


def questionish(text: str, norm: str | None = None) -> bool:
    """这段文字像不像一道题干:长度够,且有问句字样(问号/哪/什么…)。"""
    if norm is None:
        import core
        norm = core.normalize(text)
    return len(norm) >= MIN_QUESTION_CHARS and (
        "？" in text or "?" in text or any(k in text for k in _QUESTION_KEYS))


def note_uncollected(bank, text: str, options: str = "") -> str:
    """读到像题目、但分数到不了下限时调用。

    把这条写进 data/uncollected.jsonl(人工过一遍就能补进题库),
    并返回一句给界面看的话;不像题目就返回空串。
    options: 当屏题干之外的文字(通常就是四个选项),一起记下来,
    事后 `--uncollected` 不用再回日志里翻选项。
    """
    import core
    norm = core.normalize(text)
    # 长度门槛跟答题门槛一致:题库里最短的真题只有 4 字,
    # 按 8 字卡会把这类短题漏出"疑似未收录"清单
    if not questionish(text, norm):
        return ""
    best, score = bank.match(text, 0)
    if best is None or score < 50:
        return ""
    tip = (f"低置信 {score:.0f}%(不到 {MIN_MATCH_PCT}% 不播报,疑似题库未收录): "
           f"{text[:30]}…")
    record_event("low", score=round(score), text=text[:80])
    if norm not in _UNCOLLECTED_SEEN:
        _UNCOLLECTED_SEEN.add(norm)
        if len(_UNCOLLECTED_SEEN) > 500:
            _UNCOLLECTED_SEEN.clear()
        try:
            os.makedirs(os.path.dirname(core.UNCOLLECTED_PATH), exist_ok=True)
            with open(core.UNCOLLECTED_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "score": round(score), "text": text[:120],
                     "options": options[:120],
                     "guess_q": best["q"], "guess_a": best["a"]},
                    ensure_ascii=False) + "\n")
        except Exception:
            pass
    return tip


def add_manual_question(question: str, answer: str,
                        activity: str = "keju") -> bool:
    """手工补一道题进题库(只加不减,题库更新不会覆盖)。

    同题干同答案已存在时返回 False。写库用"临时文件 + 原子替换",
    并先备份旧题库。
    """
    import bank
    import core
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        return False
    raw = bank.load_bank_raw()
    key = (core.normalize(question), answer)
    for it in raw:
        if (core.normalize(it.get("question") or ""),
                (it.get("answer") or "").strip()) == key:
            return False
    raw.append({"source_id": "manual", "question": question, "answer": answer,
                "activity": activity,
                "normalized_question": core.normalize(question)})
    try:
        if os.path.exists(bank.BANK_PATH):
            import shutil
            shutil.copyfile(bank.BANK_PATH, bank.BANK_BACKUP_PATH)
        tmp = bank.BANK_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, bank.BANK_PATH)
    except Exception:
        return False
    return True


# "题干 -> 屏上四个选项"仓库:官方工具箱抓来的初始选项 + 运行时每答一题
# 回写的当屏选项,越攒越全。文件可整删(删了只是丢已攒的选项,下次答题重新攒)
OPTIONS_PATH = os.path.join(BASE_DIR, "data", "keju_options.json")

# 滑窗匹配的窗口数上限:只防极端花屏把单次扫描拖成秒级。普通整窗 OCR 只有
# 几十个块,远到不了这里;预筛是单指标,但窗口数真到 3000 时也会到数百毫秒级。
# 600 的旧上限曾在 300+ 块干扰的屏幕上静默截断,把屏幕下方的真题漏掉。
MAX_SCAN_WINDOWS = 3000


def _boxes_overlap(a, b):
    """两个 [x1,y1,x2,y2] 矩形是否相交(重叠面积为正)。

    跟随认「谁改了档」用:改分辨率一般原位重摆,新老矩形必然交叠。
    """
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# ---- 选项采集:答完一题把当屏四个选项回写 data/keju_options.json ----

# 屏上选项的编号标记:"A. xxx B. xxx" / "Ａ、xxx" / "①xxx"。
# 有标记就切得准;游戏里多数选项是纯一行一个,没有标记,那路走按行聚类
_OPTION_MARKERS = re.compile(
    r"[Ａ-Ｄa-dA-D]\s*[.．、:：]|[(（][1-4][)）]|[①②③④]")


def split_options_markers(text: str) -> list:
    """按 A./B./① 等编号标记把选项文本切成列表;切不出 3 段返回空。

    第一个标记之前的部分是题干尾巴,丢掉(从第一个标记之后开始算)。
    """
    m = _OPTION_MARKERS.search(text or "")
    if not m:
        return []
    segs = [s.strip(" \t,，、;；。") for s in _OPTION_MARKERS.split(text[m.end():])]
    segs = [s for s in segs if s]
    return segs if len(segs) >= 3 else []


def _opt_side(s: str) -> str:
    """选项比对侧的清理:压掉"一一"连跑。

    游戏选项里的分隔长横线"──",OCR 稳定读成两个"一"(2026-11-15 金兜兕
    实测:巳──蛇 读成 已一一蛇;题库答案「巳——蛇」的长横线倒是会被
    core.normalize 当标点剥掉),两边永远差一截,整题被压答。单发"一"
    是正字(一元复始),不动。
    """
    return re.sub(r"一{2,}", "", s) if "一" in s else s


def _option_hit(answer_norm: str, option: str) -> bool:
    """答案是否(近似)就是这条选项。

    数值答案只认完全一致("200"对"20"/"0"的前缀包含是假命中,会把
    选项窗口选错);文字答案依次放三档 —— 包含关系(被包含侧 >= 2 字)、
    **一字之差**(替换或增删一格:两侧长度差 <=1 且编辑距离 <=1)、
    长串近似(>= 4 字相似度 >= 80)。一字档是 OCR 的常态误读:2026-11-15
    车迟斗法《窦娥冤》读成《实娥冤》、金兜兕「秤砣」读成「秤碗」、
    「金瓶掣签」丢成「金瓶签」,连包含都接不住,哨兵把题库正确答案
    一个个压了。
    """
    import core
    from rapidfuzz import fuzz
    from rapidfuzz.distance import Levenshtein

    onorm = _opt_side(core.normalize(option))
    anorm = _opt_side(answer_norm)
    if not onorm:
        return False
    if onorm == anorm:
        return True
    if onorm.isdigit() or anorm.isdigit():
        return False
    short, long = sorted((onorm, anorm), key=len)
    if len(short) >= 2 and short in long:
        return True
    if (len(short) >= 2 and abs(len(onorm) - len(anorm)) <= 1
            and Levenshtein.distance(onorm, anorm, score_cutoff=2) <= 1):
        return True
    return len(short) >= 4 and fuzz.ratio(onorm, anorm) >= 80


def _opt_hit_strict(ans_norm: str, option: str) -> bool:
    """答案是否(严格)就是这条选项 —— 选项哨兵专用。

    与采集定位的 _option_hit 现在同一套规则(数值完全一致 / 文字包含
    且被包含侧 >= 2 字);哨兵独立成函数是留出分化空间 —— 消歧决策
    本身仍走 bank.pick_with_options 的 partial 80 硬线,不在此处。
    """
    return _option_hit(ans_norm, option)


def option_lines(rblocks, qwin):
    """题干块之外的候选选项行(答案无关,选项哨兵用)。

    先按编号标记(A./①)切,切不出再按"题干外的短行"取,行首的
    "A "/"B."编号前缀一并剥掉 —— 哨兵不能像 parse_options 那样拿
    答案当门槛:答案不在场恰恰是要报警的时候,门槛会把警报一起吞掉。

    候选块先按对话框几何过一道:选项一定跟在题干正下方、同一竖列;
    题干上方(NPC 名牌)和斜右侧(任务追踪 HUD)的短行一概不算 ——
    2026-11-15 车迟斗法实测:OCR 漏读真选项的那两拍,追踪 HUD 的短行
    凑满 3 条,把题库里的正确答案「浙江」误压了。题干窗口没拿到
    (qwin 空,脏读整段硬配的路径)没有几何参考,宁可不压:返回空。
    """
    import core
    import re

    qwin_set = set(qwin or [])
    rest = {i: (b.get("text") or "").strip()
            for i, b in enumerate(rblocks)
            if i not in qwin_set and (b.get("text") or "").strip()}
    if not rest:
        return []
    qs = [rblocks[i] for i in qwin_set if 0 <= i < len(rblocks)]
    if not qs:
        return []
    qx1 = min(b["x1"] for b in qs)
    qy1 = min(b["y1"] for b in qs)
    qx2 = max(b["x2"] for b in qs)
    rest = {i: t for i, t in rest.items()
            if rblocks[i]["y1"] >= qy1
            and rblocks[i]["x1"] <= qx2 and rblocks[i]["x2"] >= qx1}
    if not rest:
        return []
    segs = split_options_markers(" ".join(rest.values()))
    if segs:
        return segs
    lines = []
    for t in rest.values():
        t = re.sub(r"^[Ａ-Ｄa-dA-D](?:\s*[.．、:：]|\s+)", "", t).strip()
        if 1 <= len(core.normalize(t)) <= 24:
            lines.append(t)
    return lines


def parse_options(rblocks, qwin, answer: str):
    """从识别块里把"题干之外的选项"尽量解析出来。

    返回 (选项列表或None, 原始选项文本)。解析成列表的硬门槛:答案要能
    对上其中一条(OCR 把聊天/任务文字混进块里时宁可丢弃,不存错的);
    列表解析失败但答案确实出现在剩余文字里时,把原始文本当兜底带回 ——
    它跟运行时消歧用的那坨是同一段,拿去 pick_with_options 一样能用。
    """
    import core
    from rapidfuzz import fuzz

    anorm = core.normalize(answer)
    if not anorm:
        return None, ""
    qwin_set = set(qwin or [])
    lines, rest = [], []
    for i, b in enumerate(rblocks):
        if i in qwin_set:
            continue
        t = (b.get("text") or "").strip()
        if not t:
            continue
        rest.append(t)
        n = core.normalize(t)
        if 1 <= len(n) <= 24:
            lines.append(t)
    if not rest:
        return None, ""
    raw = " ".join(rest)
    hit_raw = fuzz.partial_ratio(anorm, core.normalize(raw)) >= 80

    opts = split_options_markers(raw)
    if opts and not any(_option_hit(anorm, o) for o in opts):
        opts = []                                  # 切出来了但没答案:不收
    if not opts and lines:
        idx = next((i for i, t in enumerate(lines)
                    if _option_hit(anorm, t)), None)
        if idx is not None:
            lo = max(0, idx - 1)               # 答案行前面捎带一条
            opts = lines[lo:lo + 4]            # 连它共四条;不够就尽力
    dedup, seen = [], set()
    for o in opts:
        k = core.normalize(o)
        if k and k not in seen:
            seen.add(k)
            dedup.append(o.strip())
    if len(dedup) >= 3 and any(_option_hit(anorm, o) for o in dedup):
        return dedup[:4], raw
    return None, (raw if hit_raw else "")


class OptionsStore:
    """题干 -> 屏上选项 的本地仓库(data/keju_options.json)。

    文件里既有官方工具箱抓来的初始选项(带 category/official_first_is_answer),
    也有运行时答过的题回写的选项(带 mode/seen/count);key 一律是题库的
    归一化题干。同题重复答就 count+1;已有 options 列表的条目不会被
    纯文本兜底覆盖(列表是更好的形态,先到先得)。脏了才写盘,整文件
    原子替换;加载坏掉就把原文件挪成 .bad 留证,从空库重开 —— 采集是
    副产品,任何失败都不能惊动识别线程。
    """

    def __init__(self, path: str = OPTIONS_PATH):
        self.path = path
        self.entries: dict = {}
        self.meta: dict = {}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.entries = dict(d.get("entries") or {})
            self.meta = dict(d.get("_meta") or {})
        except FileNotFoundError:
            pass                                       # 首次运行:空库起家
        except Exception:
            try:
                os.replace(self.path, self.path + ".bad")
            except Exception:
                pass

    def learn(self, nkey: str, question: str, answer: str,
              options=None, text: str = "", mode: str = "") -> bool:
        """答完一题回写当屏选项。count/seen 每次都动,恒返回 True(要写盘)。"""
        e = self.entries.get(nkey)
        if e is None:
            self.entries[nkey] = e = {"question": question, "answer": answer}
        if options and not e.get("options"):
            e["options"] = [str(o) for o in options]
            e.pop("text", None)                        # 列表到手,text 兜底撤下
        elif text and not e.get("options") and not e.get("text"):
            e["text"] = str(text)[:200]
        if e.get("answer") != answer:
            e["answer"] = answer                       # 题库更新换答案时跟着走
        e["count"] = int(e.get("count") or 0) + 1
        e["seen"] = time.strftime("%Y-%m-%d")
        if mode:
            e["mode"] = mode
        self._dirty = True
        return True

    def save(self):
        if not self._dirty:
            return
        try:
            self.meta["learned"] = sum(1 for e in self.entries.values()
                                       if e.get("seen"))
            self.meta["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"_meta": self.meta, "entries": self.entries},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            pass


class KejuTool(threading.Thread):
    """题目对话框区域 -> 答案(语音 + 迷你窗)。"""

    name = "科举"

    def __init__(self, app, screen, ocr):
        super().__init__(daemon=True)
        self.app = app
        self._screen = screen          # 取引擎的截图封装(基础设施)
        self._ocr = ocr                # 取 OCR 封装(基础设施)
        self.enabled = False           # 由 refresh_tools() 按"进了哪个功能"开关
        self.watching = True           # 自动监听默认开(没有界面开关,定位到即接管)
        self.recent_answered: dict[str, float] = {}  # 归一化题干 -> 答题时间
        self._miss_streak = 0
        self._nomatch_streak = 0
        self._nomatch_near = 0.0       # 最近一次漏答时"最接近的候选"分数
        self._no_region_streak = 0
        self._region_last = {}         # 每个区域各自记住上一题(多客户端互不干扰)
        self._region_fallback_ts = {}  # 各区域最近认出题目结构的时间(锚点兜底窗口)
        self._region_recheck = {}      # key -> 下次复检时刻(锚点没认出时安排)
        self._region_retries = {}      # key -> 本画面已复检次数
        self._region_frame = {}        # 区域画面指纹,用于跳过未变化的OCR
        self._region_ocr_ts = {}       # 各区域上次OCR的时间(超时复检)
        self._region_ref = {}          # 各区域上次OCR时的画面(内容比对基准)
        self._region_dirty = {}        # 各区域画面开始变化的时间
        self._region_t_detect = {}     # 各区域最近一次'检测到变化'的时刻
        self._last_announce_ts = 0.0   # 最近一次播报时刻(自适应轮询用)
        # 选项仓库:测试里 OPTIONS_PATH 指到临时目录(不写启动日志,
        # 识别/界面日志有既定的行文契约,别添行)
        self.options = OptionsStore(OPTIONS_PATH)
        # "自动定位"当时记下的客户端矩形(引擎重建会造新工具:从配置把当前
        # 区域接过来,自动跟随不断线)
        boxes = [list(r) for r in (self.cfg("regions") or []) if r]
        self._auto_boxes = boxes or None
        # 各槽位的扫描区:画布 × 比例表(见 _sync_scan_rects)——
        # _scan_rects 是无锚点族的底部对话带(DLG_RATIOS),_exam_rects 是
        # 科举答题的答题面板(EXAM_RATIOS);空槽位/没同步到的拍退回整窗
        self._scan_rects: list = []
        self._exam_rects: list = []
        self._client_names: list = []   # 各槽位的角色名(抠自窗口标题,见 set_auto_boxes)
        # 分辨率不支持而暂停观测的角色(界面红名显示;改回支持档自动恢复)
        self._unsup: list = []
        # 观测满员被丢弃的客户端名(记一次日志;槽位空出自动接入并摘标)
        self._dropped: list = []
        # 空手等待旗:一个客户端都没跟到时(启动没开游戏/全退出登录/全被关闭)
        # 挂着,跟随周期检测到登录角色的支持客户端就自动定位收编
        self._readopt = True
        self._follow_ts = 0.0          # 上次检查客户端有没有挪窝
        self._req_cycle = False        # 有待办:手动识别(界面按的)
        self._req_force = True         # 手动识别一律 force=True
        self._halt = threading.Event()
        self._wake = threading.Event()

    # ---- 基础设施:调用时现取,引擎重建后自动跟上 ----
    @property
    def screen(self):
        return self._screen()

    @property
    def ocr(self):
        return self._ocr()

    # ---- 生命周期 ----
    def activate(self):
        self.enabled = True
        self._wake.set()

    def deactivate(self):
        self.enabled = False

    def stop(self):
        self._halt.set()
        self._wake.set()

    # ---- 供界面调用的几个动作(只在这里碰自己的内部状态) ----
    def request(self, force: bool = True):
        """投递一次识别任务给自己这条线程(界面上的「手动识别」走这里)。

        只置待办 + 唤醒,不碰识别用的任何状态;下一次循环立刻执行,
        而且**不受 watching 限制** —— 自动监听关着时手动识别照样能用。
        """
        self._req_force = bool(force)   # 先写内容再立标志,读到的永远是这一份
        self._req_cycle = True
        self._wake.set()

    def forget_region_memory(self, key=None):
        """忘掉各区域记住的上一题/画面指纹(区域变了,旧记忆作废)。

        传 key 只忘这一个区域(清单个槽位),不传就是全部(区域整体变了)。
        """
        for d in (self._region_last, self._region_fallback_ts,
                  self._region_recheck, self._region_retries,
                  self._region_frame, self._region_ref,
                  self._region_ocr_ts, self._region_t_detect,
                  self._region_dirty):
            if key is None:
                d.clear()
            else:
                d.pop(key, None)

    def set_auto_boxes(self, boxes, names=None):
        """记住「自动定位」当时各客户端的矩形与角色名(挪窝时按它比对/跟随)。

        names 与 boxes 一一对应,是标题里抠出来的角色名(core.client_name);
        不足 MAX_CLIENTS 的槽位补 None。只传 boxes(如清空)时名字一并清掉。
        """
        import core
        if boxes:
            self._readopt = False      # 跟上客户端了,空手等待旗收起
        self._auto_boxes = ([[int(v) for v in b] for b in boxes]
                            if boxes else None)
        names = list(names or [])
        self._client_names = (names + [None] * core.MAX_CLIENTS
                              )[:core.MAX_CLIENTS]

    def _sync_scan_rects(self, found):
        """各槽位的扫描区:画布 × 比例表(两族各一张,真机画框定稿)。

        found 是跟随周期枚举出的支持客户端 (标题, 窗口矩形) 列表,按窗口
        矩形对回槽位。无锚点族(三大学士/副本答题)只扫底部对话带
        (`dialog_rect` = DLG_RATIOS),科举答题只扫居中的答题面板
        (`exam_rect` = EXAM_RATIOS)—— 面板是**固定像素大小**的游戏 UI,
        贴在哪按游戏设置分辨率查表(640×480/800×600/1024×768 一档一套),
        比整窗紧得多,任务追踪栏/NPC 名牌/聊天从源头就进不来(2026-11-15
        车迟斗法实测:HUD 短行曾凑成"当屏选项"把题库正确答案误压,几何
        过滤只是补救,这个才是根治)。拿不到画布/该档没量到的槽位置 None,
        cycle 那拍退回整窗 —— 正常到不了:槽位本身配到了画布的支持客户端。
        """
        import core
        rects = [None] * core.MAX_CLIENTS
        exams = [None] * core.MAX_CLIENTS
        for t, b in found[:core.MAX_CLIENTS]:
            canvas = core.client_canvas(b, t)
            if canvas is None:
                continue
            try:
                i = self._auto_boxes.index([int(v) for v in b])
            except (ValueError, AttributeError):
                continue
            rects[i] = core.dialog_rect(canvas[0], canvas[1])
            exams[i] = core.exam_rect(canvas[0], canvas[1])
        self._scan_rects = rects
        self._exam_rects = exams

    # ---- 自己的配置 ----
    def cfg(self, key, default=None):
        """自己的配置只在 keju 分区取。"""
        return (self.app.cfg.get("keju") or {}).get(key, default)

    # ---- 只读状态口:gui 只认这个口,不摸下面的字段 ----
    def ui_state(self):
        """界面刷新要的只读快照:各槽位的角色名 + 暂停观测(红名)名单。

        暂停观测的客户端并进同一排槽位显示(排在已定位的后面),列表
        `unsup` 里的名字界面画红色、悬停显示「分辨率不支持」。
        """
        names = list(self._client_names)
        unsup = list(self._unsup)
        free = [k for k, n in enumerate(names) if not n]
        for k, n in enumerate(unsup):
            if k < len(free):
                names[free[k]] = n
        return {"names": names, "unsup": unsup}

    def set_unsup(self, names):
        """整体替换「分辨率不支持、暂停观测」名单(启动自动定位/清空用)。"""
        out = []
        for n in (names or []):
            n = str(n)
            if n and n not in out:
                out.append(n)
        self._unsup = out

    def _unsup_reconcile(self, raw):
        """红名/满员丢弃名单里窗口没了的(关掉/退出登录):静默摘掉。"""
        import core
        live = {core.client_name(t) for t, _b in raw}
        self._unsup = [n for n in self._unsup if n in live]
        self._dropped = [n for n in self._dropped if n in live]

    def _unsup_mark(self, raw):
        """「名字在、配到本尊画布、但档位不支持」的窗口记红名;首次记才落日志。

        不依赖槽位认领 —— 空手等待期、没排进观测槽的 HD 窗口都照记。
        **配不到画布不下结论**:登录/loading 中标题先于画布出现,那一拍
        "有名字没画布"只是还不知道档位,标了就是误报(2026-11-13 用户
        实测:独立窗口版登录瞬间被全量误标)。
        """
        import core
        for t, b in raw:
            name = core.client_name(t)
            if not name or name in self._unsup or core.client_supported(b, t):
                continue
            canvas = core.client_canvas(b, t)
            if canvas is None:
                continue
            self._unsup.append(name)
            self.app.log(f"[客户端]{name}({canvas[1][0]}×{canvas[1][1]})"
                         "分辨率不支持,已暂停观测(改回支持档自动恢复)")

    def _unsup_heal(self, raw):
        """暂停观测的窗口回到支持档:摘红名并记一声(每拍自检,防误标滞留)。"""
        import core
        for name in list(self._unsup):
            for t, b in raw:
                if (core.client_name(t) != name
                        or not core.client_supported(b, t)):
                    continue
                canvas = core.client_canvas(b, t)
                size = (f"{canvas[1][0]}×{canvas[1][1]}" if canvas else "")
                self._unsup.remove(name)
                self.app.log(f"[客户端]{name}({size})分辨率已恢复,继续观测")
                break

    def _run_pending(self) -> bool:
        """有待办就办掉:目前只有手动识别一种,办过事返回 True。

        这是"手动任务只在自己线程里跑"的落点:界面点一下 = 置待办 + 唤醒,
        绝不从别的线程直接调识别。
        """
        did = False
        if self._req_cycle:
            self._req_cycle = False
            self.keju_cycle(self._req_force)
            did = True
        return did

    def run(self):
        while not self._halt.is_set():
            try:
                # 待办优先,且不受 watching / enabled 限制(引擎还没起来时先搁着)
                if self._screen() is not None and self._run_pending():
                    continue
                if (not self.enabled or self._screen() is None
                        or not self.watching):
                    self._wake.wait()       # 空闲:完全不唤醒
                    self._wake.clear()
                    continue
                t0 = time.time()
                self.keju_cycle(False)
                # 睡到下一轮;期间来了待办/状态变化会被唤醒,立刻回到循环开头
                self._wake.wait(timeout=max(0.0, self._poll_interval()
                                            - (time.time() - t0)))
                self._wake.clear()
            except Exception as e:              # 自己扛异常,不牵连别的功能
                self.app.log(f"[{TAG}]出错: {e}")
                self._halt.wait(0.2)            # 出错这一轮别空转

    def _remember_answered(self, entry):
        # 整个会话期记住已答过的题,抑制聊天窗口/日志里的题干回声
        self.recent_answered[entry["n"]] = time.time()
        while len(self.recent_answered) > 200:
            oldest = min(self.recent_answered, key=self.recent_answered.get)
            del self.recent_answered[oldest]

    def _recent_list(self):
        return list(self.recent_answered)

    def _poll_interval(self) -> float:
        """自适应轮询。

        - 刚播报过(<12 秒):下一题随时会来,固定 0.1 秒快扫
        - 长时间没有题目:按配置里的空闲间隔(≥0.4 秒)降频省 CPU
        画面没变化时并不跑 OCR,所以空闲间隔主要省的是截图+比对的固定开销。
        """
        import core
        base = float(self.cfg("interval", core.MIN_INTERVAL))
        if time.time() - getattr(self, "_last_announce_ts", 0) < 12:
            return 0.1
        return max(base, core.MIN_INTERVAL)

    def _match_floor(self) -> float:
        """匹配下限。固定 MIN_MATCH_PCT:低于它的读法一律不播报(防误报)。"""
        return float(MIN_MATCH_PCT)

    def _show_miss(self, brief, detail=""):
        """手动识别的可见结论:界面没有日志框,失败原因摆到题目卡上。

        自动路径不走这里(每轮都闪结论会把正在看的答案冲掉);只有用户
        亲手点了「手动识别」、结果又什么都没答出来时才需要解释一句。
        """
        self.app.last_question = detail or brief
        self.app.last_answer = brief

    def _rescue_by_options(self, rblocks, qwin):
        """无锚点族的低分兜底:不到 MIN_MATCH_PCT 时,用当屏选项钉答案。

        55~79 分的读法通常是"题目读脏了"而不是"不是题"(车迟斗法把题目、
        HUD、聊天混在同一条带里)。选项是强信号,但要防两种误救:
        ①候选答案必须严格就是当屏某条选项;②同题干多答案变体里会有
        两个答案同时在场(唐朝的"唐"/"朝"字由多少笔画)—— 这时用题干
        里被引号括起来的关键字判别,唯一对上才采用。宁可不救,不救错。
        返回 (entry, score) 或 None。
        """
        import core

        def opt_exact(ans: str, option: str) -> bool:
            """答案是否(单向严格)就是这条选项。

            不能像 _opt_hit_strict 那样双向包含:选项"二画"是答案"十二画"
            的子串,反向包含会把别题答案误认成当屏选项。只认相等,或
            答案整段落在选项里(选项带编号/前后缀时)。
            """
            onorm = core.normalize(option)
            if not ans or not onorm:
                return False
            return ans == onorm or (len(ans) >= 2 and ans in onorm)

        hits, _best = find_questions(self.app.bank, rblocks, RESCUE_MIN_PCT,
                                     use_anchor=False)
        if not hits:
            return None
        top = max(hits.values(), key=lambda v: v[0])
        olines = option_lines(rblocks, top[3])
        if len(olines) < 3:
            return None
        opt_hits = [(e, s, tx) for s, e, tx, _w in hits.values()
                    if any(opt_exact(core.normalize(e["a"]), o)
                           for o in olines)]
        if not opt_hits:
            return None
        if len({e["a"].strip() for e, _s, _tx in opt_hits}) == 1:
            e, s, _tx = max(opt_hits, key=lambda t: t[1])
            return e, s
        # 多个变体的答案都在当屏选项里:题干引号字判别,唯一对上才采用
        marks = set()
        for _e, _s, tx in opt_hits:
            marks |= set(_QUOTED.findall(tx))
        if not marks:
            return None
        marked = [(e, s) for e, s, _tx in opt_hits
                  if set(_QUOTED.findall(e["q"])) & marks]
        if len({e["a"].strip() for e, _s in marked}) == 1:
            return max(marked, key=lambda t: t[1])
        return None

    @staticmethod
    def _leftover_text(rblocks, qwin):
        """题干块之外的文字 —— 通常就是四个选项;区域没框到选项时返回空串。"""
        import core
        qwin_set = set(qwin or [])
        rest = " ".join(b["text"] for i, b in enumerate(rblocks)
                        if i not in qwin_set)
        return rest if len(core.normalize(rest)) >= 6 else ""

    @staticmethod
    def _options_decide(candidates, options_text):
        """选项文字里是否已经出现某个候选答案(够 80:pick_with_options 的硬线)。"""
        import core
        from rapidfuzz import fuzz

        onorm = core.normalize(options_text)
        return bool(onorm) and any(
            fuzz.partial_ratio(core.normalize(e["a"]), onorm) >= 80
            for e, _s in candidates)

    def _scan_options(self, region):
        """歧义题兜底:按更宽的底部带再 OCR 一次,把更大范围的文字当选项。

        只在"同题干多答案变体、且本轮带内没认出任何一个答案"时走 ——
        车迟斗法/金兜兕的对话框比三大学士高,选项可能整排在 PLAIN_BAND 外。
        """
        x1, y1, x2, y2 = region
        try:
            crop = self.screen.grab(_band(x1, y1, x2, y2, OPTION_BAND))
            blocks = self.ocr.recognize_blocks(crop)
        except Exception:
            return ""
        return " ".join(b.get("text", "") for b in blocks)

    def _rescan_olines(self, base, qbox):
        """压答前的窄带复检:只截题干正下方一条,再给 OCR 一次机会。

        选项行会被整框 OCR 整行漏检,漏的常恰好是答案那一行(2026-11-15
        车迟斗法实测:素纱襌衣题「长沙马王堆」那行两拍都没读出,题库答案
        不在"当屏选项"里被压)。题干上方的东西(NPC 名牌)按构造就不在
        图里,右侧追踪栏也大半被裁掉。base 是本轮识别用的那个矩形(画布框
        或整窗比例带),块坐标都以它为基准。det 的输入下限取 max(736, 窄带
        长边):小条只会被放大,不会像整框那样缩水。读不了返回 None,
        哨兵按原判压答。
        """
        bx1, by1, bx2, by2 = base
        qx1, qy1, qx2, qy2 = qbox
        top = by1 + qy2
        strip = (bx1 + qx1, top, bx1 + qx2 + (qx2 - qx1) // 2 + 40,
                 min(by2, top + max(80, (qy2 - qy1) * 8)))
        try:
            crop = self.screen.grab(strip)
            blocks = self.ocr.recognize_blocks(
                crop, limit_side=max(736, strip[2] - strip[0],
                                     strip[3] - strip[1]))
        except Exception:
            return None
        import core
        lines = []
        for b in blocks:
            t = (b.get("text") or "").strip()
            if 1 <= len(core.normalize(t)) <= 24:
                lines.append(t)
        return lines

    def _store_second_opinion(self, entry, olines):
        """压答前的第二意见:选项仓库里这道题的历史选项 vs 当屏读数。

        只采信"历史选项里当初就有题库答案"的条目 —— 那是播报路径存的
        (答案在场才学得进);压答路径存的历史选项里没有答案,拿它印证
        等于放大过期答案(用户口径 2026-11-16:各玩法题目与答案相同,
        压答基本都是 OCR 漏检,答案必在屏上)。历史选项与当屏对上 >=2
        条才认:屏幕就是这道题,首轮只是漏检,放行播报。全程吞异常:
        印证是副产品,任何失败都按没有第二意见处理(压答照旧)。
        返回印证说明(进日志)或 None。
        """
        import core
        try:
            e = (getattr(self.options, "entries", None)
                 or {}).get(entry["n"]) or {}
            sopts = e.get("options") or []
            if not sopts or not any(
                    _opt_hit_strict(core.normalize(entry["a"]), s)
                    for s in sopts):
                return None
            hits = sum(1 for s in sopts
                       if any(_opt_hit_strict(core.normalize(s), o)
                              for o in olines))
            if hits < 2:
                return None
            return (f"选项仓库:历史选项与当屏对上({hits}/{len(sopts)}),"
                    "首轮漏检,照常播报")
        except Exception:
            return None

    def _learn_options(self, entry, rblocks, qwin, mode_name, olines=None):
        """答题成功后把当屏选项回写选项仓库(data/keju_options.json)。

        rblocks/qwin 就是刚才识别用的那批块:题干块之外的文字通常就是
        四个选项。解析+答案对不上就只落原始文本兜底;olines 传入了
        (选项哨兵压答路径)就跳过解析直接用 —— 那条路径的选项已经
        验证过是当屏真实选项。全程吞异常:采集是副产品,识别线程
        永远优先。
        """
        try:
            if olines is None:
                options, text = parse_options(rblocks, qwin, entry["a"])
            else:
                options, text = olines[:4], ""
            self.options.learn(entry["n"], entry["q"], entry["a"],
                               options=options, text=text,
                               mode=mode_name or "")
            self.options.save()
        except Exception:
            pass

    def _parse_keju(self, rblocks):
        """【科举答题】必须存在"御前科举大赛…题目:"结构锚点。

        找到 -> 剥头取裸题干,返回 (裸题干, 题干块下标集合);
        找不到 -> 返回 None(当前区域不是该界面的题,跳过)。
        """
        import core
        bare, qwin = extract_anchored_question(rblocks)
        if not qwin or len(core.normalize(bare)) < MIN_QUESTION_CHARS:
            return None
        return bare, qwin

    def _parse_plain(self, rblocks, require_hit: bool = False):
        """【无锚点模式:三大学士/副本答题】不要锚点:区域内滑窗直接全题干匹配。

        返回 (题干文本, 题干块下标集合或None)。候选由 cycle 统一走
        `match_all` 全列取回 —— 同一题干常有两个答案变体(官方源如此,
        如花妖/蜃气妖),必须交给 pick_with_options 按当屏选项消歧,
        只挑分数最高的那个等于掷硬币。
        require_hit: 只认真命中(≥下限),命中为空就返回 None ——
        会试锚点兜底走这个口,绝不能拿整屏文字去硬配(那会误报)。
        """
        thr = self._match_floor()
        hits, _ = find_questions(self.app.bank, rblocks, thr, use_anchor=False)
        if hits:
            sc, en, tx, win = max(hits.values(), key=lambda v: v[0])
            return tx, win
        if require_hit:
            return None
        return "".join(b["text"] for b in rblocks), None

    def _sticky(self, key) -> bool:
        """这个区域最近认出过题目结构(考试正在进行)→ 锚点兜底窗口内。"""
        return (time.time() - self._region_fallback_ts.get(key, 0.0)
                < EXAM_FALLBACK_S)

    def _arm_recheck(self, key):
        """给这一区域安排一次复检:过 RECHECK_DELAY_S 无视帧门控再读一次。

        OCR 对同一画面不是每次读得一样(对话框渐入/抖动),多读一两轮常就
        认出了。次数用完就停,别烧 CPU;画面真变了会重新给预算(循环里
        fresh 时清 _region_retries)。
        """
        n = self._region_retries.get(key, 0)
        if n >= RECHECK_MAX:
            return
        self._region_retries[key] = n + 1
        self._region_recheck[key] = time.time() + RECHECK_DELAY_S

    def _note_parse_miss(self, key, rblocks, force, olog):
        """锚点没认出来:考试中(兜底窗口内)像题的文字记 parsemiss + 安排复检。

        纯聊天/世界画面(不像题)不记不重试 —— 那是正常状态,不是漏答;
        没在考试(兜底窗口外)也一样,只有手动识别必须有可见结果。
        """
        text = " ".join(b.get("text", "") for b in rblocks)
        like_q = self._sticky(key) and questionish(text)
        first = False
        if like_q:
            first = (self._region_retries.get(key, 0) == 0
                     and key not in self._region_recheck)
            self._arm_recheck(key)
            if first:
                record_event("parsemiss", recovered=0, text=text[:80])
        if force:
            # 手动识别必须有可见结果,沿用既有提示(统计/习惯都认这句)
            olog("读到文字但没认出科举题目:"
                 f"{text[:60]}…(可切「三大学士/副本答题」试试)")
            self._show_miss("没认出科举题目(可切无锚点模式试试)",
                            text[:40])
        elif like_q and first:
            olog(f"读到文字但没认出题目,稍后自动重试: {text[:40]}…")

    def keju_cycle(self, force: bool):
        import core
        from rapidfuzz import fuzz

        def olog(msg, detail=""):
            """cycle 的日志带操作前缀:手动那次 [手动识别];监听中 [自动监听];
            监听关着时手动识别也照扫,标 [检测],免得让人以为自动监听还在跑。
            """
            if force:
                tag = "手动识别"
            else:
                tag = "自动监听" if self.watching else "检测"
            self.app.log(f"[{tag}]{msg}", detail=detail)

        # 客户端挪窝了就先把区域跟过去,再按区域截图
        self._follow_clients()

        regions = [r for r in (self.cfg("regions") or []) if r]
        if not regions:
            # 没有区域:直接不扫(手动也不扫),提示 + 暂停自动监听
            self._no_region_streak += 1
            await_relogin = getattr(self, "_readopt", False)
            if force or self._no_region_streak == 1:
                if await_relogin:
                    olog("等待游戏客户端登录角色,检测到后自动开始识别")
                else:
                    olog("还没有定位到客户端 —— "
                         "检测到登录角色的客户端后会自动开始识别")
            if self.watching and not await_relogin:
                self.watching = False
                olog("没有定位到客户端,已暂停(定位后自动接管)")
            return
        self._no_region_streak = 0
        found_text = False
        _t_cycle0 = time.time()
        exam_mode = bool(self.cfg("exam_mode", True))
        mode_name = self.cfg("mode", "") or ("科举答题" if exam_mode
                                             else "三大学士")
        for region in regions:
            x1, y1, x2, y2 = region
            key = tuple(region)
            # 之前安排的复检到点了:无视帧门控把这块画面再读一次
            due_at = self._region_recheck.get(key)
            recheck = due_at is not None and time.time() >= due_at
            if recheck:
                self._region_recheck.pop(key, None)
            try:
                # 只扫答题区域(画布 × 比例表,跟随每拍同步,见
                # _sync_scan_rects):科举答题 = 答题面板 EXAM_RATIOS,
                # 无锚点族(三大学士/副本答题)= 底部对话带 DLG_RATIOS。
                # 画布框没同步到的槽位退回整窗(无锚点族再缩到 PLAIN_BAND)
                scan = None
                for i, b in enumerate(self._auto_boxes or []):
                    if list(region) == list(b):
                        rects = (self._exam_rects if exam_mode
                                 else self._scan_rects) or []
                        scan = rects[i] if i < len(rects) else None
                        break
                base = scan or (region if exam_mode
                                else plain_band(x1, y1, x2, y2))
                crop = self.screen.grab(base)
            except Exception as e:
                olog(f"截图失败: {e}")
                return
            fresh = self._frame_gate(key, crop)
            if fresh:
                self._region_retries.pop(key, None)   # 新画面:复检预算重给
            if not force and not fresh and not recheck:
                # 强制识别(手动按钮)也走一遍门控:只为更新参考帧,
                # 否则下一轮会把刚识别过的同一画面再识别一次
                continue  # 画面无新内容,跳过OCR
            _t_ocr0 = time.time()
            rblocks = self.ocr.recognize_blocks(
                crop, limit_side=MANUAL_DET_LIMIT if force else None)
            _t_ocr = time.time() - _t_ocr0
            if not rblocks:
                if self._sticky(key):
                    # 对话框渐入会有一两帧空白:别让参考帧一更新就死等画面变化
                    self._arm_recheck(key)
                continue
            found_text = True
            # 两套解析逻辑,按模式选择(互不干扰)
            sticky_note = ""
            if exam_mode:
                parsed = self._parse_keju(rblocks)
                if parsed is None and self._sticky(key):
                    # 锚点偶发整行读不出(OCR 误读/对话框渐入):题干本体
                    # 通常仍读得全,退回无锚点滑窗兜底,真命中才采用
                    parsed = self._parse_plain(rblocks, require_hit=True)
                    if parsed is not None:
                        sticky_note = " [锚点缺失,题干兜底]"
                        record_event("parsemiss", recovered=1,
                                     text=parsed[0][:80])
            else:
                parsed = self._parse_plain(rblocks)
            if parsed is None:
                # 御前科举模式下未找到锚点:当前区域不是该界面
                self._note_parse_miss(key, rblocks, force, olog)
                continue
            bare, qwin = parsed
            n = core.normalize(bare)
            if len(n) < MIN_QUESTION_CHARS:
                if force:
                    olog(f"读到的文字太碎,凑不出题干(仅 {len(n)} 字):"
                         f"{bare[:20]}…")
                continue  # 碎片文字(如单个"是"),不可能是完整题干
            last = self._region_last.get(key, "")
            if not force and last and fuzz.ratio(n, last) >= 88:
                continue  # 该客户端还是同一道题,看下一个
            # 这块画面认出结构了(题目/开场白):刷新锚点兜底窗口;
            # 复检待办一并了结 —— 结构都认出来了,不用再重读
            self._region_fallback_ts[key] = time.time()
            self._region_retries.pop(key, None)
            self._region_recheck.pop(key, None)
            if is_exam_prompt(bare):
                # 考官开场对话("这里是御前科举大赛第N关…你作好答题的准备了吗")
                # 根本不是题目:直接跳过匹配,免得"科举大赛"几个字被配到
                # 《科举大赛的第一阶段是》这种题上报个假答案(曾误报"乡试")
                if force:
                    olog(f"当前是考官开场对话,不是题目: {bare[:20]}…")
                continue
            self._region_last[key] = n

            _t_m0 = time.time()
            # 候选整列取回:同一题干可能有多个答案变体(官方源常见),
            # 交给 pick_with_options 按当屏选项定,不能只取最高分
            candidates = self.app.bank.match_all(bare, self._match_floor())
            # 只有"答案不同且分数咬得紧"的候选才算真歧义,才去消歧
            need_disamb = False
            if len(candidates) > 1:
                top_e, top_s = candidates[0]
                need_disamb = any(
                    e2["a"] != top_e["a"] and s2 >= top_s - 10
                    for e2, s2 in candidates[1:])
            # 消歧只用本次OCR的结果:题干块之外的文字(通常就是选项)。
            # 区域没框到选项时先留空;无锚点模式的对话框可能更高(车迟斗法),
            # 带内没认出候选答案就再扫一条更宽的底部带兜底
            options_text = self._leftover_text(rblocks, qwin) if need_disamb else ""
            if (need_disamb and not exam_mode
                    and not self._options_decide(candidates, options_text)):
                options_text = (options_text + " "
                                + self._scan_options(region)).strip()
            entry, score, note = self.app.bank.pick_with_options(
                candidates, options_text)
            if entry is None and not exam_mode:
                # 分不够(题目读脏了):用当屏选项把答案钉死再决定
                rescued = self._rescue_by_options(rblocks, qwin)
                if rescued is not None:
                    entry, score = rescued
                    note = " [选项兜底]"
            if not exam_mode:
                note += f" [{mode_name}]"
            note += sticky_note
            # 考官开场白已在前面拦下,这里写进日志的都是真读到的题干
            olog(f"识别: {bare[:30]}…")
            if entry:
                t_ans = self.recent_answered.get(entry["n"])
                if (not force and t_ans
                        and time.time() - t_ans < REPEAT_GUARD_S):
                    olog(f"该题 {REPEAT_GUARD_S} 秒内已播过,不重复: "
                                 f"{entry['q'][:16]}…")
                    record_event("dup")
                    continue
                # 选项哨兵:当屏明明读得出选项,答案却不在其中。用户口径
                # (2026-11-16):各玩法题目与答案相同,压答基本都是 OCR
                # 漏检/误读,少数情况是题库答案过期 —— 无论哪种,报一个
                # 屏上选不出的答案都更糟,宁可不报。只管自动路径;手动
                # 识别是用户点名要答案,放行但留痕(下面 elif)
                olines = option_lines(rblocks, qwin)
                sentinel_hits = len(olines) >= 3 and not any(
                        _opt_hit_strict(core.normalize(entry["a"]), o)
                        for o in olines)
                if sentinel_hits and not force:
                    # 压答前两道第二意见,任一放行就照常播报:
                    # ①窄带复检再读一次 —— OCR 偶发整行漏检选项,漏的常是
                    #   答案那一行(2026-11-15 实测「长沙马王堆」两拍没读出
                    #   被压),重读分辨率更高;
                    # ②选项仓库第二意见 —— 这道题答过、历史选项里当初就有
                    #   题库答案(播报路径才存得进),历史与当屏对上 >=2 条
                    #   即认定屏幕就是这道题,首轮只是漏检(场景 A)
                    rlines = None
                    qs = [rblocks[i] for i in set(qwin or [])
                          if 0 <= i < len(rblocks)]
                    if qs:
                        qbox = (min(b["x1"] for b in qs),
                                min(b["y1"] for b in qs),
                                max(b["x2"] for b in qs),
                                max(b["y2"] for b in qs))
                        rlines = self._rescan_olines(base, qbox)
                    confirm = ""
                    if rlines and any(
                            _opt_hit_strict(core.normalize(entry["a"]), o)
                            for o in rlines):
                        confirm = (f"复检读到「{entry['a']}」就在选项里"
                                   "(首轮整行漏读),照常播报")
                        note += " [选项复检]"
                    if not confirm:
                        confirm = self._store_second_opinion(entry, olines)
                        if confirm:
                            note += " [仓库印证]"
                    if confirm:
                        if rlines:
                            olines += [o for o in rlines if o not in olines]
                        olog(confirm)
                    else:
                        if rlines:
                            olines += [o for o in rlines if o not in olines]
                        # 压答也要上卡(界面没有日志框):题目卡换成这道题
                        # +「选项不符」,别让玩家对着上一题的旧答案等动静
                        self.app.last_question = entry["q"]
                        self.app.last_answer = "选项不符,已压"
                        olog(f"答案「{entry['a']}」不在当屏选项"
                             f"({'/'.join(olines[:4])})里 —— 疑似 OCR 漏检"
                             "或题库答案过期,未播报")
                        record_event("suppressed", answer=entry["a"],
                                     options="/".join(olines[:4])[:60])
                        # 压答路径直接用哨兵读到的选项行入库:答案门槛在这条
                        # 路径上必然不过,但选项本身是当屏的真实选项
                        self._learn_options(entry, rblocks, qwin, mode_name,
                                            olines=olines)
                        self._nomatch_streak = 0
                        return  # 本轮此区域已处理
                elif sentinel_hits:
                    # 手动识别是用户点名要答案:哨兵不拦(它管的是自动路径
                    # "宁可不报"),但留痕 —— note 进 match 事件,日志提醒
                    # 答案可能过期,方便按新答案补题(--uncollected)
                    note += " [哨兵放行]"
                    olog(f"答案「{entry['a']}」不在当屏选项"
                         f"({'/'.join(olines[:4])})里,手动放行 —— "
                         "若屏上确实没有,这条答案可能已过期")
                self._remember_answered(entry)
                self.app.last_question = entry["q"]
                self.app.last_answer = entry["a"]
                _t_match = time.time() - _t_m0
                t_det = self._region_t_detect.get(key, _t_cycle0)
                _t_resp = time.time() - t_det
                olog(f"匹配 {score:.0f}%{note} -> "
                     f"{entry['q'][:16]}… 【{entry['a']}】"
                     f"| 响应 {_t_resp:.2f}s",
                     detail=f" (OCR {_t_ocr:.2f}s 匹配 {_t_match:.2f}s)")
                record_event("match", score=round(score), note=note.strip(),
                             answer=entry["a"], response=round(_t_resp, 2),
                             ocr=round(_t_ocr, 2), match=round(_t_match, 2),
                             manual=bool(force))
                # 顺手把当屏选项攒进选项仓库:下次没屏幕也能消歧/验证
                self._learn_options(entry, rblocks, qwin, mode_name)
                self.app.speaker.say(entry["a"])
                self._last_announce_ts = time.time()
                self._nomatch_streak = 0
                return  # 本轮已播报
            if force:
                # 手动识别的诊断上屏:界面没有日志框,只写日志等于没反馈
                self._show_miss(
                    f"未匹配(最高 {self.app.bank.closest(bare):.0f}%)",
                    bare[:40])
            if questionish(bare):
                # 漏答计数只认"像题"的读数(2026-11-15):追踪栏/聊天的 junk
                # 每拍都在推高 streak,真漏答发生时(填空线那题)连"未找到
                # 匹配"日志都被节流吞掉 —— junk 读数把漏答周期作废,别让它
                # 稀释信号
                self._nomatch_streak += 1
                if self._nomatch_streak == 1:
                    # 没答出的这一刻才算一次"最接近的候选":分数接近=真题没收录
                    # (值得补题库),分数很低=屏幕上根本不是题(聊天/任务栏)
                    self._nomatch_near = self.app.bank.closest(bare)
                    record_event("nomatch", score=round(self._nomatch_near),
                                 text=bare[:80])
                if (force or self._nomatch_streak == 1
                        or self._nomatch_streak % 10 == 0):
                    suffix = (f"(连续{self._nomatch_streak}次)"
                              if self._nomatch_streak > 1 else "")
                    # 同一次漏答只在第 1 次和每 10 次打一行,别刷屏
                    olog(f"题库中未找到匹配(最高 {self._nomatch_near:.0f}%)"
                         f"{suffix}")
                if self._nomatch_streak == 1:
                    # 像题目但分数不够:界面提示一声,并记到"疑似未收录"清单
                    # (连当屏的选项一起记,事后 --uncollected 就能直接填答案)
                    tip = note_uncollected(self.app.bank, bare,
                                           self._leftover_text(rblocks, qwin))
                    if tip:
                        olog(tip)
            else:
                self._nomatch_streak = 0   # junk/非题读数:漏答周期作废重计
            # 这个客户端的题没答出:看下一个客户端(多开时一个区域没匹配
            # 不该挡住同轮其余客户端,否则它们的题要白等一轮)
            continue
        if not found_text:
            self._miss_streak += 1
            if force or self._miss_streak == 1 or self._miss_streak % 15 == 0:
                suffix = (f"(连续{self._miss_streak}次)"
                          if self._miss_streak > 1 else "")
                # 同一轮空屏只在第 1 次和每 15 次打一行,别刷屏
                olog("题目对话框里没读到文字(题目未出现)" + suffix)
            if self._miss_streak == 1:
                record_event("blind")
        else:
            self._miss_streak = 0

    def _follow_clients(self):
        """客户端被挪动/换了分辨率/开了新的:自动定位来的区域跟着更新。

        每秒最多查一次窗口。逐老槽位认领「能认出是谁」的变化:
          - 改成不支持档(如 HD):窗口在、角色名在、画布档不支持
            -> **不移除名单**:转入暂停观测(界面红名,悬停注明
            「分辨率不支持」),改回支持档自动恢复
          - 退出登录:窗口在、标题抠不出角色名了 -> 静默移除
          - 关闭:窗口没了(最小化的不算 —— 枚举把它单独记账,原槽位
            保留等恢复,恢复后位置没变自然续上);静默移除
        剩余支持客户端照常跟随;未被认领的新登录客户端自动纳入观测;
        一个客户端都不剩时挂空手等待旗(_readopt),检测到登录角色的
        支持客户端就自动定位收编 —— 启动没开游戏/换角色/重开客户端
        都不用手动刷新。
        """
        import core
        now = time.time()
        if now - getattr(self, "_follow_ts", 0.0) < 1.0:
            return
        self._follow_ts = now
        if not getattr(self, "_auto_boxes", None):
            # 空手等待:检测到登录角色的支持客户端就自动定位收编
            if not getattr(self, "_readopt", False):
                return
            try:
                found, _minimized = core.find_client_windows()
            except Exception:
                return
            self._unsup_reconcile(found)   # 名单里窗口没了的:静默摘掉
            self._unsup_mark(found)        # 名字在但档位不支持的:记红名不收编
            self._unsup_heal(found)        # 回到支持档的:摘红名
            found = [(t, b) for t, b in found
                     if core.client_name(t) and core.client_supported(b, t)]
            if not found:
                return
            for t, b in found[core.MAX_CLIENTS:]:
                name = core.client_name(t)
                if name in self._dropped:
                    continue
                canvas = core.client_canvas(b, t)
                size = (f"{canvas[1][0]}×{canvas[1][1]}" if canvas
                        else core.client_size_info(b, t)[0])
                self._dropped.append(name)
                self.app.log(f"[客户端]已观测满 {core.MAX_CLIENTS} 个,"
                             f"新客户端 {name}({size})已丢弃")
            boxes = [[int(v) for v in b] for _t, b in found[:core.MAX_CLIENTS]]
            names = [core.client_name(t) for t, _b in found[:core.MAX_CLIENTS]]
            self.set_auto_boxes(boxes, names)
            self._sync_scan_rects(found[:core.MAX_CLIENTS])
            mine = self.app.cfg.setdefault("keju", {})
            mine["regions"] = boxes + [None] * (core.MAX_CLIENTS - len(boxes))
            self.forget_region_memory()
            self.app.log(f"[客户端]检测到 {len(boxes)} 个已登录客户端,"
                         "已自动定位")
            return
        try:
            raw, minimized = core.find_client_windows()
        except Exception:
            return
        self._unsup_reconcile(raw)         # 名单里窗口没了的:静默摘掉
        self._unsup_mark(raw)              # 名字在但配到画布档不支持的:记红名
        self._unsup_heal(raw)              # 回到支持档的:摘红名(自愈误标)
        found = [(t, b) for t, b in raw if core.client_supported(b, t)]
        self._sync_scan_rects(found)   # 每拍同步:扫描区跟着画布走(稳态也刷)
        boxes = [[int(v) for v in b] for _t, b in found[:core.MAX_CLIENTS]]
        slot_names = {n for n in self._client_names if n}
        found_names = {core.client_name(t) for t, _b in found}
        missing = slot_names - found_names          # 观测中不再支持/没了的
        extras = [(t, b) for t, b in found
                  if core.client_name(t) not in slot_names]   # 观测外新增的
        if not missing and extras:
            # 观测中的都还在,多出来的是新客户端:有空位就接入,满员直接丢弃
            slots = [list(b) for b in (self._auto_boxes or [])]
            slot_list = [n for n in self._client_names if n]
            adopted = False
            for t, b in extras:
                name = core.client_name(t)
                canvas = core.client_canvas(b, t)
                size = (f"{canvas[1][0]}×{canvas[1][1]}" if canvas else
                        core.client_size_info(b, t)[0])
                if len(slots) >= core.MAX_CLIENTS:
                    if name not in self._dropped:
                        self._dropped.append(name)
                        self.app.log(f"[客户端]已观测满 {core.MAX_CLIENTS} 个,"
                                     f"新客户端 {name}({size})已丢弃")
                    continue
                slots.append([int(v) for v in b])
                slot_list.append(name)
                adopted = True
                if name in self._dropped:
                    self._dropped.remove(name)
                self.app.log(f"[客户端]检测到新客户端 {name}({size}),"
                             "已自动纳入观测")
            if adopted:
                self.set_auto_boxes(slots, slot_list)
                self._sync_scan_rects(found)   # 新槽位当拍就用上画布框
                mine = self.app.cfg.setdefault("keju", {})
                mine["regions"] = (slots
                                   + [None] * (core.MAX_CLIENTS - len(slots)))
                self.forget_region_memory()
            return
        if not missing and boxes == self._auto_boxes:
            return
        if missing or not core.can_follow_clients(boxes, self._auto_boxes):
            # 数量变了。逐老槽位认领「能认出是谁」的变化:改成不支持档的
            # 转入暂停观测(红名,改回支持档自动恢复);退出登录、关闭
            # (窗口没了;最小化在 minimized 里单独记账,原槽位保留等恢复)
            # 静默移除;与老区域交叠的支持客户端照常跟随;未被认领的
            # 新登录客户端自动纳入观测。
            removed, keep, moved = 0, [], False
            claimed = set()                # 一个窗口只归一个老槽位(改档的
            for i, ob in enumerate(self._auto_boxes):   # 窗口变大,可能同时
                old_name = (self._client_names[i]       # 压住隔壁老区域)
                            if i < len(self._client_names) else None)
                hit = None
                for j, (t, b) in enumerate(raw):
                    if j in claimed or not _boxes_overlap(b, ob):
                        continue
                    claimed.add(j)
                    if core.client_supported(b, t):
                        hit = (t, b, None)
                    elif core.client_name(t):
                        hit = (t, b, "tier")
                    else:
                        hit = (t, b, "logout")
                    break
                if hit is None:
                    # 窗口认领不到:最小化还在(角色名对得上)就保留原槽位
                    # 等恢复;真没了就是已关闭,静默移除
                    if (old_name and any(core.client_name(t) == old_name
                                         for t in minimized)):
                        keep.append((list(ob), old_name))
                    else:
                        removed += 1   # 关闭/退出登录:静默移除(2026-11-13)
                    continue
                t, b, why = hit
                if why:
                    # 不支持档/退出登录:槽位让出不再截图识别(慢、字小)。
                    # 被认领的观测槽位改档是**确凿证据** —— 名字当场记红名,
                    # 不等 _unsup_mark 配画布:切换分辨率那一拍画布经常配
                    # 不到本尊,等它配到之前名字就丢了(2026-11-16 用户
                    # 实测:切到不支持档后胶囊直接消失)。退出登录(标题
                    # 抠不出角色名)照旧静默移除。
                    removed += 1
                    nm = core.client_name(t) if why == "tier" else ""
                    if nm and nm not in self._unsup:
                        self._unsup.append(nm)
                        canvas = core.client_canvas(b, t)
                        reason = (f"游戏分辨率 {canvas[1][0]}×{canvas[1][1]}"
                                  " 不支持" if canvas else
                                  "游戏画面暂不可读(切换分辨率/加载中)")
                        self.app.log(f"[客户端]{nm}{reason},已暂停观测"
                                     "(红名显示,改回支持档自动恢复)")
                else:
                    bb = [int(v) for v in b]
                    moved = moved or bb != ob
                    nm = core.client_name(t)
                    if nm in self._unsup:      # 保险:恢复的不留红名
                        self._unsup.remove(nm)
                    keep.append((bb, nm))
            # 新客户端:未被老槽位认领、登录角色、档位支持 —— 自动纳入观测
            for j, (t, b) in enumerate(raw):
                if j in claimed:
                    continue
                name = core.client_name(t)
                if not name or not core.client_supported(b, t):
                    continue
                canvas = core.client_canvas(b, t)
                size = (f"{canvas[1][0]}×{canvas[1][1]}" if canvas else
                        core.client_size_info(b, t)[0])
                if len(keep) >= core.MAX_CLIENTS:
                    # 观测满员:新客户端直接丢弃(同一客户端只记一次),
                    # 槽位空出后走这条路自动接入
                    if name not in self._dropped:
                        self._dropped.append(name)
                        self.app.log(f"[客户端]已观测满 {core.MAX_CLIENTS} 个,"
                                     f"新客户端 {name}({size})已丢弃")
                    continue
                claimed.add(j)
                keep.append(([int(v) for v in b], name))
                if name in self._unsup:
                    self._unsup.remove(name)   # 保险:恢复的不留红名(heal 已摘)
                if name in self._dropped:
                    self._dropped.remove(name)   # 之前满员丢过:接入即摘标
                self.app.log(f"[客户端]检测到新客户端 {name}({size}),"
                             "已自动纳入观测")
            keep = keep[:core.MAX_CLIENTS]
            self.set_auto_boxes([bb for bb, _n in keep], [n for _bb, n in keep])
            self._sync_scan_rects(found)   # 槽位重排了,按新矩形重新对位
            mine = self.app.cfg.setdefault("keju", {})
            mine["regions"] = ([bb for bb, _n in keep]
                               + [None] * (core.MAX_CLIENTS - len(keep)))
            self.forget_region_memory()    # 区域变了,旧的画面/题目记忆全作废
            if keep and (removed or moved):
                self.app.log(f"[客户端]{len(keep)} 个客户端位置变了,已自动跟随")
            if not keep:
                self._readopt = True   # 全部退出登录/关闭:等重新登录就自动收编
            return
        names = [core.client_name(t)
                 for t, _b in found[:core.MAX_CLIENTS]]   # 跟随时名字一并刷新
        self.set_auto_boxes(boxes, names)
        self._sync_scan_rects(found)   # 槽位矩形变了,按新矩形重新对位
        mine = self.app.cfg.setdefault("keju", {})
        mine["regions"] = boxes + [None] * (core.MAX_CLIENTS - len(boxes))
        self.forget_region_memory()            # 位置变了,旧的画面/题目记忆全作废
        self.app.log(f"[客户端]{len(boxes)} 个客户端位置变了,已自动跟随")

    def _frame_gate(self, key, crop) -> bool:
        """是否需要OCR。

        - 首次看到这个区域 -> 直接识别
        - 与上次识别过的画面一致 -> 只做超时复检(15 秒)
        - 内容有变化(含截图尺寸变化)-> 立即放行,不等"稳定帧"
        """
        now = time.time()

        def diff(a, b):
            if a is None or b is None or a.shape != b.shape:
                return 999.0        # 尺寸变了(换分辨率等)也算"内容变了"
            return float(np.abs(a - b).mean())

        g = crop.mean(axis=2).astype("int16")
        prev = self._region_frame.get(key)
        ref = self._region_ref.get(key)
        d = diff(g, ref) if ref is not None else None    # 差值一次算好,下面共用
        if d is not None and d > 4.0:
            self._region_dirty.setdefault(key, now)   # 本轮变化起点
        self._region_frame[key] = g

        if prev is None:
            self._region_ref[key] = g          # 首次:直接识别
            self._region_ocr_ts[key] = now
            self._region_t_detect[key] = now
            return True
        if d <= 4.0:                           # 与上次OCR画面一致:无新内容
            if now - self._region_ocr_ts.get(key, 0) > 15:
                # 复检过就把时钟拨回去:不然后面每轮都判"离上次 OCR 很久了",
                # 门控对这个区域就永久失效,静止画面也会一直 OCR
                self._region_ocr_ts[key] = now
                return True
            return False
        # 内容与上次 OCR 不同 -> 立即识别(去掉"等一轮稳定确认",省约 0.2 秒;
        # 若真遇到淡入动画,读到半成品最多浪费一次,下一轮会自然重识别)
        started = self._region_dirty.pop(key, now)
        self._region_ref[key] = g
        self._region_ocr_ts[key] = now
        self._region_t_detect[key] = started
        return True


BAND_PAD = 8          # 对话框带四周的留白(px)

