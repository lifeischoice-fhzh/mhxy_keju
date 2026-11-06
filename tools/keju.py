"""科举大赛:一个完全独立的功能。

自己一条线程、自己的区域和状态,匹配(锚点/滑窗/全屏聚类)和收益统计都在这里。
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

# 三大学士(无锚点)只扫底部题面对话框:整窗其余全是噪声(任务追踪/聊天/名字),
# 还拖慢 OCR。对话框是固定 UI,按客户端占比框一条带,四周留足余量
# (按 860×600 档实测对话框约横 6%~88%、纵 45%~70%)。
PLAIN_BAND = (0.04, 0.42, 0.91, 0.73)   # (左, 上, 右, 下) 占整窗比例


def plain_band(x1, y1, x2, y2):
    """整窗矩形 -> 三大学士模式下真正要 OCR 的对话框带(对应 PLAIN_BAND)。"""
    w, h = x2 - x1, y2 - y1
    return (int(x1 + w * PLAIN_BAND[0]), int(y1 + h * PLAIN_BAND[1]),
            int(x1 + w * PLAIN_BAND[2]), int(y1 + h * PLAIN_BAND[3]))


# 锚点触发:块里有"科举",且出现"大赛"/"题目",或"第X关"的结构。
# 不要求"御前"在场 —— 日志实测"御前科举大赛第16关"常被 OCR 读成
# "前科举大赛第16关""何以前科举大赛第15关",死认那四个字会让整个区域静默失效。
_ANCHOR_STAGE = re.compile(r"第\s*[0-9一二三四五六七八九十百零两]*\s*关")
# 剥离头:同样容忍"御""大赛"被读丢(前面的杂字一并切掉,如被叠进来的聊天/名字)
_EXAM_HEADER = re.compile(r".*?科举(?:大赛)?.*?题目[:：]?")


def is_exam_anchor(text: str) -> bool:
    """这块文字是不是"御前科举大赛第X关:…题目:"的准考证头(容忍 OCR 错字)。"""
    t = text or ""
    if "科举" not in t:
        return False
    return ("大赛" in t or "题目" in t
            or _ANCHOR_STAGE.search(t) is not None)


def strip_exam_header(text: str) -> str:
    """剥掉"御前科举大赛第X关:这一关考的是XXX。题目:"头,返回裸题干。"""
    return _EXAM_HEADER.sub("", text, count=1)


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
    looks_like_question = len(norm) >= MIN_QUESTION_CHARS and (
        "？" in text or "?" in text
        or any(k in text for k in ("作者", "出自", "是谁", "哪位", "什么",
                                   "哪一", "哪个", "正确的是", "指的是")))
    if not looks_like_question:
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


# 当天答题收益(银两/经验/物品)按天累计,跨启动保留
REWARD_PATH = os.path.join(BASE_DIR, "data", "rewards.json")

# 滑窗匹配的窗口数上限:只防极端花屏把单次扫描拖成秒级。普通整窗 OCR 只有
# 几十个块,远到不了这里;预筛是单指标,但窗口数真到 3000 时也会到数百毫秒级。
# 600 的旧上限曾在 300+ 块干扰的屏幕上静默截断,把屏幕下方的真题漏掉。
MAX_SCAN_WINDOWS = 3000

# ---- 奖励提示的识别(文案取自真实日志) ----
#   "你得到了66580点经验，6831两银子"
#   "连续答对3题，鬼王令+1你得到了61358点经验，7320两银子"
#   "你获得了2点成就点。你获得了定魂珠"
_RE_EXP = re.compile(r"([\d,]{2,})\s*点\s*经验")
_RE_SILVER = re.compile(r"([\d,]{2,})\s*两\s*银")
_RE_ITEM_N = re.compile(r"([\u4e00-\u9fa5·]{2,6})\s*[×xX+＋]\s*(\d{1,3})")
# 物品用"你获得了X"这类系统提示;要求"你"在前,免得把玩家聊天里的
# "我获得了XX"也当成自己的收益
_RE_ITEM_GOT = re.compile(r"你(?:获得|得到|恭喜(?:你)?(?:获得)?|奖励)[了]?\s*"
                          r"([\u4e00-\u9fa5·]{2,8})")
_REWARD_VERBS = ("获得", "得到", "奖励", "恭喜")
_REWARD_STOP = ("成就点", "经验", "银子", "点数", "技能", "速度", "等级", "帮贡",
                # 科举活动道具(连对奖励),不算收益物品
                "鬼王令", "天王令", "地王令")
# "恭喜你完成会试，共用时9分1秒。" —— 这行落轮:用时 + 是否拿到物品奖励
_RE_ROUND_SEC = re.compile(r"共用时\s*(?:(\d+)\s*分)?\s*(\d+)\s*秒")
_RE_ROUND_MIN = re.compile(r"共用时\s*(\d+)\s*分")
ROUND_PASS_SECONDS = 600     # 10 分钟内答完 -> 固定物品奖励
# 同一条奖励消息会一直挂在聊天区,被反复 OCR 到 —— 靠"同一帧里出现几次"
# 这个上限来去重:多开时同一条消息会在同一帧里出现 N 次(几个客户端就几次)
REWARD_TTL = 180.0          # 秒:超过这个时间同样的一行算新的一次奖励


def scan_rewards(blocks) -> dict:
    """从这一轮 OCR 的文字块里挑奖励信息。

    返回 {"exp": {数值: 帧内次数}, "silver": {...},
          "items": {"名字": {件数: 帧内次数}}, "round_sec": 秒数|None}。
    只认有把握的数字(经验/银两)、明确的物品文案,以及"共用时X分Y秒"这种落轮提示。
    """
    exp: dict = {}
    silver: dict = {}
    items: dict = {}
    round_sec = None

    def bump(d, k, n=1):
        d[k] = d.get(k, 0) + n

    for b in blocks:
        text = b.get("text") or ""
        if "共用时" in text:                    # 落轮:用时/达标
            m = _RE_ROUND_SEC.search(text)
            if m:
                round_sec = int(m.group(1) or 0) * 60 + int(m.group(2))
            else:
                m = _RE_ROUND_MIN.search(text)
                if m:
                    round_sec = int(m.group(1)) * 60
        if not any(k in text for k in ("经验", "两银") + _REWARD_VERBS):
            continue
        for m in _RE_EXP.finditer(text):
            v = int(m.group(1).replace(",", ""))
            if v > 0:
                bump(exp, v)
        for m in _RE_SILVER.finditer(text):
            v = int(m.group(1).replace(",", ""))
            if v > 0:
                bump(silver, v)
        has_verb = any(k in text for k in _REWARD_VERBS)
        for m in _RE_ITEM_N.finditer(text):
            name, n = m.group(1), int(m.group(2))
            if has_verb and not any(s in name for s in _REWARD_STOP):
                bump(items.setdefault(name, {}), n)
        if has_verb:
            m = _RE_ITEM_GOT.search(text)
            if m:
                name = m.group(1)
                if not any(s in name for s in _REWARD_STOP) and len(name) >= 2:
                    bump(items.setdefault(name, {}), 1)
    return {"exp": exp, "silver": silver, "items": items,
            "round_sec": round_sec}


class RewardTally:
    """当天答题收益:银两 / 经验 / 物品(种类 + 件数),按天累计、跨启动保留。

    读的人(`--rewards`,可与 GUI 并用)会和科举线程的 feed 并发:
    不加锁就会撞上"遍历字典时它变了"(RuntimeError),读到的账目也是半截。
    读/写各自进这把锁,粒度只包住自己这几个办法,不碰工具线程的循环。
    用 RLock 是因为 feed() 内部还要调 rollover()/save(),普通 Lock 会自锁。
    """

    def __init__(self, path: str = REWARD_PATH):
        self._lock = threading.RLock()
        self.path = path
        self.day = time.strftime("%Y-%m-%d")
        self.exp = 0
        self.silver = 0
        self.items: dict[str, int] = {}
        self.rounds: list[dict] = []           # 已完成的轮次(按客户端分)
        self._cur: dict[str, dict] = {}        # 客户端 -> 本轮累计
        self._peak: dict[str, tuple] = {}      # key -> (帧内最多出现几次, 时间)
        self._load()

    # ---- 持久化 ----
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        if d.get("day") == self.day:           # 只认当天的,换天就从头算
            self.exp = int(d.get("exp", 0) or 0)
            self.silver = int(d.get("silver", 0) or 0)
            self.items = {k: int(v) for k, v in (d.get("items") or {}).items()}
            self.rounds = list(d.get("rounds") or [])[-20:]
            self._cur = {k: {"exp": int(v.get("exp", 0)),
                             "silver": int(v.get("silver", 0)),
                             "items": dict(v.get("items") or {})}
                          for k, v in (d.get("cur") or {}).items()}

    def save(self):
        try:
            with self._lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"day": self.day, "exp": self.exp,
                               "silver": self.silver, "items": self.items,
                               "rounds": self.rounds, "cur": self._cur},
                              f, ensure_ascii=False)
                os.replace(tmp, self.path)
        except Exception:
            pass

    def rollover(self):
        """跨天了就把当天那一笔清零重来。"""
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day = today
                self.exp = self.silver = 0
                self.items = {}
                self.rounds = []
                self._cur = {}
                self._peak = {}
                self.save()

    # ---- 计数 ----
    def _acc(self, key, n_now, now):
        """同一帧里出现 n_now 次:只把比"这轮窗口内见过的最大次数"多出来的算进去。

        这样同一条消息反复被 OCR 到不会重复计,而多开时同一条消息在一帧里
        出现几次就计几次。
        """
        peak, ts = self._peak.get(key, (0, now))
        if now - ts > REWARD_TTL:
            peak = 0                            # 窗口过期:重新起算
        grew = max(0, n_now - peak)
        self._peak[key] = (max(peak, n_now), now)
        return grew

    def feed(self, blocks, slot=1) -> list[str]:
        """喂一轮 OCR 结果,返回本帧新计入的说明(进日志)。

        slot: 这条画面属于哪个客户端(槽位号 1~5)。多开时每个客户端各算各的
        "单轮收益",互不串账;落轮提示("共用时X分Y秒")也只结自己这一轮的账。
        整段进锁:`--rewards` 等读方可能同时在读,不能读到写了一半的状态。
        """
        with self._lock:
            return self._feed(blocks, slot)

    def _feed(self, blocks, slot) -> list[str]:
        self.rollover()
        found = scan_rewards(blocks)
        now = time.time()
        news = []
        rk = str(slot)                   # 槽位号:1~5,多开各算各的
        cur = self._cur.setdefault(rk, {"exp": 0, "silver": 0, "items": {}})
        for v, n in found["exp"].items():
            grew = self._acc(f"{rk}|exp:{v}", n, now)
            if grew:
                self.exp += v * grew
                cur["exp"] += v * grew
                news.append(f"经验 +{v}" + (f"×{grew}" if grew > 1 else ""))
        for v, n in found["silver"].items():
            grew = self._acc(f"{rk}|silver:{v}", n, now)
            if grew:
                self.silver += v * grew
                cur["silver"] += v * grew
                news.append(f"银两 +{v}" + (f"×{grew}" if grew > 1 else ""))
        for name, counts in found["items"].items():
            for cnt, n in counts.items():
                grew = self._acc(f"{rk}|item:{name}x{cnt}", n, now)
                if grew:
                    self.items[name] = self.items.get(name, 0) + cnt * grew
                    cur["items"][name] = cur["items"].get(name, 0) + cnt * grew
                    news.append(f"{name}×{cnt * grew}")
        if found["round_sec"] is not None:
            # 这一轮结束了:把它记成"某客户端的一轮",用时/达标一并留档
            sec = int(found["round_sec"])
            if cur["exp"] or cur["silver"] or cur["items"]:
                self.rounds.append({
                    "slot": rk, "sec": sec,
                    "pass": sec <= ROUND_PASS_SECONDS,
                    "exp": cur["exp"], "silver": cur["silver"],
                    "items": dict(cur["items"]),
                    "ts": time.strftime("%H:%M:%S")})
                self.rounds = self.rounds[-20:]          # 只留最近 20 轮
                news.append(f"本轮完成:用时 {sec // 60}分{sec % 60}秒"
                            + ("(达标)" if sec <= ROUND_PASS_SECONDS
                               else "(超时)"))
            self._cur[rk] = {"exp": 0, "silver": 0, "items": {}}
        if len(self._peak) > 800:               # 防内存膨胀
            cut = sorted(self._peak.items(), key=lambda kv: kv[1][1],
                         reverse=True)[:400]
            self._peak = dict(cut)
        if news:
            self.save()
        return news

    def summary(self) -> str:
        """给 `--rewards` 看的一行:当天累计(银两 / 经验 / 物品种类数 + 明细)。"""
        with self._lock:
            if not (self.exp or self.silver or self.items):
                return "今日收益: 还没有记录(答题过程中会自动统计)"
            exp = (f"{self.exp / 10000:.1f}万" if self.exp >= 10000
                   else str(self.exp))
            parts = [f"银两 {self.silver:,} 两", f"经验 {exp}"]
            if self.items:
                top = sorted(self.items.items(), key=lambda kv: -kv[1])
                detail = "、".join(f"{k}×{v}" for k, v in top[:3])
                if len(top) > 3:  # 一行放不下:种类数前面已经写了
                    detail += "…"
                parts.append(f"物品 {len(self.items)} 种({detail})")
            else:
                parts.append("物品 0 种")
            return "今日收益: " + " · ".join(parts)

    # ---- 单轮(按客户端分开) ----
    def round_lines(self) -> list[str]:
        """每个客户端一行:最近一轮的用时/达标 + 该轮银两/经验/物品。

        多开时逐个客户端列,不混在一起;没数据的客户端不占行。
        """
        with self._lock:
            lines = []
            for rk in sorted(self._cur, key=lambda k: int(k) if k.isdigit()
                             else 99):
                cur = self._cur[rk]
                done = [r for r in self.rounds if r.get("slot") == rk]
                name = f"{rk}号"
                if cur["exp"] or cur["silver"] or cur["items"]:
                    parts = [f"银两 {cur['silver']:,}", f"经验 {cur['exp']}"]
                    if cur["items"]:
                        it = "、".join(f"{k}×{v}"
                                      for k, v in cur["items"].items())
                        parts.append(f"物品 {len(cur['items'])} 种({it})")
                    lines.append(f"{name} 本轮进行中: " + " · ".join(parts))
                if done:
                    r = done[-1]
                    mark = "达标" if r["pass"] else "超时"
                    parts = [f"用时 {r['sec'] // 60}分{r['sec'] % 60}秒({mark})",
                             f"银两 {r['silver']:,}", f"经验 {r['exp']}"]
                    if r["items"]:
                        it = "、".join(f"{k}×{v}"
                                      for k, v in r["items"].items())
                        parts.append(f"物品 {len(r['items'])} 种({it})")
                    lines.append(f"{name} 最近一轮: " + " · ".join(parts))
            return lines


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
        self._region_frame = {}        # 区域画面指纹,用于跳过未变化的OCR
        self._region_ocr_ts = {}       # 各区域上次OCR的时间(超时复检)
        self._region_ref = {}          # 各区域上次OCR时的画面(内容比对基准)
        self._region_dirty = {}        # 各区域画面开始变化的时间
        self._region_t_detect = {}     # 各区域最近一次'检测到变化'的时刻
        self._last_announce_ts = 0.0   # 最近一次播报时刻(自适应轮询用)
        # 显式传路径:REWARD_PATH 在测试里会被指到临时目录
        self.rewards = RewardTally(REWARD_PATH)  # 当天收益
        # "自动定位"当时记下的客户端矩形(引擎重建会造新工具:从配置把当前
        # 区域接过来,自动跟随不断线)
        boxes = [list(r) for r in (self.cfg("regions") or []) if r]
        self._auto_boxes = boxes or None
        self._client_names: list = []   # 各槽位的角色名(抠自窗口标题,见 set_auto_boxes)
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
        for d in (self._region_last, self._region_frame, self._region_ref,
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
        self._auto_boxes = ([[int(v) for v in b] for b in boxes]
                            if boxes else None)
        names = list(names or [])
        self._client_names = (names + [None] * core.MAX_CLIENTS
                              )[:core.MAX_CLIENTS]

    def drop_client(self, i):
        """界面上胶囊 × 删掉第 i 个槽位的客户端(工具侧的收尾)。

        从跟随基准和角色名里把这一位去掉(整体前移,和界面里区域列表的
        删法一致),那一格的画面/题目记忆一并作废。必须在界面改写配置里
        的 regions **之前**调:这里要按旧配置数出它是紧凑表里的第几个 box。
        """
        import core
        regions = [r for r in (self.cfg("regions") or [])][:core.MAX_CLIENTS]
        regions += [None] * (core.MAX_CLIENTS - len(regions))
        if not regions[i]:
            return
        k = sum(1 for r in regions[:i] if r)   # 前面非空的个数 = 紧凑表下标
        if self._auto_boxes and 0 <= k < len(self._auto_boxes):
            self._auto_boxes = (self._auto_boxes[:k]
                                + self._auto_boxes[k + 1:]) or None
        if 0 <= i < len(self._client_names):
            self._client_names.pop(i)
        self.forget_region_memory(key=tuple(regions[i]))

    # ---- 自己的配置 ----
    def cfg(self, key, default=None):
        """自己的配置只在 keju 分区取。"""
        return (self.app.cfg.get("keju") or {}).get(key, default)

    # ---- 只读状态口:gui 只认这个口,不摸下面的字段 ----
    def ui_state(self):
        """界面刷新要的只读快照:各槽位的角色名(客户端胶囊显示用)。"""
        return {"names": list(self._client_names)}

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

    @staticmethod
    def _leftover_text(rblocks, qwin):
        """题干块之外的文字 —— 通常就是四个选项;区域没框到选项时返回空串。"""
        import core
        qwin_set = set(qwin or [])
        rest = " ".join(b["text"] for i, b in enumerate(rblocks)
                        if i not in qwin_set)
        return rest if len(core.normalize(rest)) >= 6 else ""

    def _parse_keju(self, rblocks):
        """【御前科举模式】必须存在"御前科举大赛…题目:"结构锚点。

        找到 -> 剥头取裸题干,返回 (裸题干, 题干块下标集合, None);
        找不到 -> 返回 None(当前区域不是该界面的题,跳过)。
        """
        import core
        bare, qwin = extract_anchored_question(rblocks)
        if not qwin or len(core.normalize(bare)) < MIN_QUESTION_CHARS:
            return None
        return bare, qwin, None

    def _parse_plain(self, rblocks):
        """【三大学士模式】不要锚点:区域内滑窗直接全题干匹配。

        返回 (题干文本, 题干块下标集合或None, 预匹配结果或None)。
        """
        thr = self._match_floor()
        hits, _ = find_questions(self.app.bank, rblocks, thr, use_anchor=False)
        if hits:
            sc, en, tx, win = max(hits.values(), key=lambda v: v[0])
            return tx, win, (en, sc)
        return "".join(b["text"] for b in rblocks), None, None

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

        # 带槽位号(1~5):奖励统计要按客户端分开归轮
        regions = [(i + 1, r) for i, r in
                   enumerate(self.cfg("regions") or []) if r]
        if not regions:
            # 没有区域:直接不扫(手动也不扫),提示 + 暂停自动监听
            self._no_region_streak += 1
            if force or self._no_region_streak == 1:
                olog("还没有定位到客户端 —— 点「客户端刷新」定位后再识别")
            if self.watching:
                self.watching = False
                olog("没有定位到客户端,已暂停(定位后自动接管)")
            return
        self._no_region_streak = 0
        found_text = False
        _t_cycle0 = time.time()
        exam_mode = bool(self.cfg("exam_mode", True))
        for slot, region in regions:
            x1, y1, x2, y2 = region
            key = tuple(region)
            try:
                # 三大学士只扫底部对话框带(整窗其余全是噪声);御前科举整窗
                crop = self.screen.grab(region if exam_mode
                                        else plain_band(x1, y1, x2, y2))
            except Exception as e:
                olog(f"截图失败: {e}")
                return
            fresh = self._frame_gate(key, crop)
            if not force and not fresh:
                # 强制识别(手动按钮)也走一遍门控:只为更新参考帧,
                # 否则下一轮会把刚识别过的同一画面再识别一次
                continue  # 画面无新内容,跳过OCR
            _t_ocr0 = time.time()
            rblocks = self.ocr.recognize_blocks(crop)
            _t_ocr = time.time() - _t_ocr0
            if not rblocks:
                continue
            found_text = True
            # 顺手统计奖励(银两/经验/物品):跟题目识别无关,两种模式都算
            try:
                for gain in self.rewards.feed(rblocks, slot):
                    self.app.log(f"[奖励]{gain}")
            except Exception:
                pass
            # 两套解析逻辑,按模式选择(互不干扰)
            if exam_mode:
                parsed = self._parse_keju(rblocks)
            else:
                parsed = self._parse_plain(rblocks)
            if parsed is None:
                # 御前科举模式下未找到锚点:当前区域不是该界面
                if force:
                    olog("读到文字但没认出科举题目:"
                         f"{' '.join(b['text'] for b in rblocks)[:24]}…"
                         "(可切「三大学士」模式试试)")
                continue
            bare, qwin, pre = parsed
            n = core.normalize(bare)
            if len(n) < MIN_QUESTION_CHARS:
                if force:
                    olog(f"读到的文字太碎,凑不出题干(仅 {len(n)} 字):"
                         f"{bare[:20]}…")
                continue  # 碎片文字(如单个"是"),不可能是完整题干
            last = self._region_last.get(key, "")
            if not force and last and fuzz.ratio(n, last) >= 88:
                continue  # 该客户端还是同一道题,看下一个
            if is_exam_prompt(bare):
                # 考官开场对话("这里是御前科举大赛第N关…你作好答题的准备了吗")
                # 根本不是题目:直接跳过匹配,免得"科举大赛"几个字被配到
                # 《科举大赛的第一阶段是》这种题上报个假答案(曾误报"乡试")
                if force:
                    olog(f"当前是考官开场对话,不是题目: {bare[:20]}…")
                continue
            self._region_last[key] = n

            _t_m0 = time.time()
            if pre is not None:
                entry, score, note = pre[0], pre[1], " [三大学士]"
                candidates = []
            else:
                candidates = self.app.bank.match_all(bare, self._match_floor())
            # 只有"答案不同且分数咬得紧"的候选才算真歧义,才去消歧
            need_disamb = False
            if len(candidates) > 1:
                top_e, top_s = candidates[0]
                need_disamb = any(
                    e2["a"] != top_e["a"] and s2 >= top_s - 10
                    for e2, s2 in candidates[1:])
            # 消歧只用本次OCR的结果:题干块之外的文字(通常就是选项)。
            # 区域没框到选项时 options_text 为空,退化为取最高分候选。
            options_text = self._leftover_text(rblocks, qwin) if need_disamb else ""
            if pre is None:
                entry, score, note = self.app.bank.pick_with_options(
                    candidates, options_text)
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
                self.app.speaker.say(entry["a"])
                self._last_announce_ts = time.time()
                self._nomatch_streak = 0
                return  # 本轮已播报
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
        """客户端被挪动/换了分辨率:自动定位来的区域跟着更新。

        每秒最多查一次窗口;客户端最小化/关掉会让数量变化,那时宁可不跟,
        免得把别的槽位弄丢。不支持的分辨率档(640×480 / 860×600 之外)
        不在跟随范围:改大后被滤掉,数量对不上就不跟,等下次定位收口。
        """
        import core
        now = time.time()
        if now - getattr(self, "_follow_ts", 0.0) < 1.0:
            return
        self._follow_ts = now
        if not getattr(self, "_auto_boxes", None):
            return
        try:
            found, _minimized = core.find_client_windows()
        except Exception:
            return
        found = [(t, b) for t, b in found if core.client_supported(b)]
        boxes = [[int(v) for v in b] for _t, b in found[:core.MAX_CLIENTS]]
        if boxes == self._auto_boxes:
            return
        if not core.can_follow_clients(boxes, self._auto_boxes):
            return
        names = [core.client_name(t)
                 for t, _b in found[:core.MAX_CLIENTS]]   # 跟随时名字一并刷新
        self.set_auto_boxes(boxes, names)
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

