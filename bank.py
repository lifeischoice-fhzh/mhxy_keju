#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题库子系统:题库文件的读写/合并/更新,以及加载后的模糊匹配。

科举的匹配(tools/keju.py)、命令行的 cmd_* 与自检、界面上"检查题库"那一条路
(gui.py)都用它,所以它自己单独一层,不归任何一个功能。

跟 core 只有一条单行道:归一化的题干、配置读取这些通用工具还留在 core,本模块
调用时才现取(import core 写在用到它的函数里)。这样 core 可以在模块级 import
本模块,反过来不成环。
"""
import json
import os
import re
import sys

# 数据目录的根:跟 core.BASE_DIR 同一套规则(打包后跟着可执行文件走)。
# 这里不能在模块级 from core import BASE_DIR —— core 要在模块级 import 本模块,
# 反过来在模块级取 core 的名字只会拿到一个还没加载完的 core(同 tools/*.py)。
if getattr(sys, "frozen", False):          # PyInstaller 打包后
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BANK_PATH = os.path.join(BASE_DIR, "data", "keju_questions.json")

BANK_META_PATH = os.path.join(BASE_DIR, "data", "keju_metadata.json")
BANK_BACKUP_PATH = os.path.join(BASE_DIR, "data", "keju_questions.backup.json")
_GH_API = "https://api.github.com/repos/PilyTang/XYQQuiz/contents/"
_GH_REPO_PAGE = "https://github.com/PilyTang/XYQQuiz"


def _gh_raw(path: str, timeout: int = 30) -> bytes:
    """从题库源仓库取文件(走 api.github.com 的 raw 模式)。"""
    import urllib.request
    req = urllib.request.Request(
        _GH_API + path,
        headers={"Accept": "application/vnd.github.raw",
                 "User-Agent": "mhxy_keju"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


OFFICIAL_PAGE = "https://w.163.com/h5/xyq/dtk/"
# 官方数据块(exp.js)内的模块号 → 活动名(同一文件里放了多个活动的题库)
OFFICIAL_MODULES = {"8733": "keju", "6419": "qiandao"}
OFFICIAL_ACTIVITY_NAMES = {"keju": "科举", "qiandao": "签到"}


def _http_get(url: str, timeout: int = 30) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _js_unescape(s: str) -> str:
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s


def _no_blank(s: str) -> str:
    """填空题的空格线(一串下划线)在题库两边比对处剥掉。

    题库把填空空格原样存成"____"(如 孔子谓季氏:八佾舞于庭,____),而
    OCR 读不出这条图形线(读了也是长短随机的几截),两边长度对不上会把
    相似度拖到阈值之下 —— 2026-11-15 车迟斗法实测:31 字的存 norm vs
    10 字的读数只有 44%,整道题静默漏答。只在 bank 的比对两侧剥,不进
    core.normalize:那是通用文本设施,选项仓库等处的键不能跟着变。
    """
    return s.replace("_", "") if "_" in s else s


def fetch_official_bank(activities=None):
    """【主源】直连网易官方答题器,按模块解析各活动题库。

    数据块里模块号区分活动:8733=/keju(科举)、6419=/qiandao(签到)。
    activities 指定收录哪些活动;默认读配置 bank_activities。
    返回 (entries, meta)。页面/块结构变化时抛异常,由调用方回退备源。
    """
    import core
    import datetime
    if activities is None:
        activities = (core.keju_cfg(core.load_config()).get("bank_activities")
                      or ["keju", "qiandao"])
    activities = [a for a in activities if a in OFFICIAL_ACTIVITY_NAMES]

    html = _http_get(OFFICIAL_PAGE).decode("utf-8", "replace")
    m = re.search(r"static/js/(app\.[a-f0-9]+\.js)", html)
    if not m:
        raise ValueError("官方页面结构变化:未找到 app.js")
    app_js = _http_get(OFFICIAL_PAGE + "static/js/" + m.group(1)).decode(
        "utf-8", "replace")
    hashes = list(dict.fromkeys(re.findall(r'306:"([a-f0-9]{8})"', app_js)))
    if not hashes:
        raise ValueError("官方页面结构变化:未找到数据块哈希")
    src, used = None, ""
    for h in hashes:
        try:
            data = _http_get(f"{OFFICIAL_PAGE}static/js/exp.{h}.js", timeout=60)
        except Exception:
            continue
        if b'Q:"' in data:
            src, used = data.decode("utf-8", "replace"), h
            break
    if src is None:
        raise ValueError("官方数据块下载失败")

    pat = re.compile(r'\{Id:"([^"]*)",Q:"((?:[^"\\]|\\.)*)",'
                     r'A:"((?:[^"\\]|\\.)*)"\}')

    # 按 webpack 模块边界切分,定位各活动的题库
    bounds = [(mm.start(), mm.group(1))
              for mm in re.finditer(r"(\d{3,5}):function\(", src)]
    segments = []           # [(活动名或None, 文本片段)]
    if bounds:
        for i, (pos, mid) in enumerate(bounds):
            end = bounds[i + 1][0] if i + 1 < len(bounds) else len(src)
            segments.append((OFFICIAL_MODULES.get(mid), src[pos:end]))
    else:                   # 结构变化:退化为全文扫描
        segments.append((None, src))

    entries, seen, per_act = [], set(), {}
    for act, seg in segments:
        if act is not None and act not in activities:
            continue
        for _, q, a in pat.findall(seg):
            q, a = _js_unescape(q).strip(), _js_unescape(a).strip()
            if len(q) < 2 or not a or (q, a) in seen:
                continue
            seen.add((q, a))
            entries.append({"source_id": str(len(entries) + 1), "question": q,
                            "answer": a, "activity": act or "keju",
                            "normalized_question": _no_blank(core.normalize(q))})
            per_act[act or "keju"] = per_act.get(act or "keju", 0) + 1
    # 跨活动去重:同一题干在多个活动里都出现时优先保留科举版本
    # (签到版的答案常带同义词后缀如"蒙古族,蒙古",科举版更干净)
    keju_qs = {e["normalized_question"] for e in entries
               if e.get("activity") == "keju"}
    if keju_qs and any(e.get("activity") != "keju" for e in entries):
        dropped = [e for e in entries if e.get("activity") != "keju"
                   and e["normalized_question"] in keju_qs]
        if dropped:
            entries = [e for e in entries
                       if e.get("activity") == "keju"
                       or e["normalized_question"] not in keju_qs]
            per_act = {}
            for e in entries:
                act = e.get("activity", "keju")
                per_act[act] = per_act.get(act, 0) + 1
    if len(entries) < 500:
        raise ValueError(f"官方源题目过少({len(entries)}),已放弃")
    meta = {"source": "official", "page_url": OFFICIAL_PAGE + "index.html#/keju",
            "chunk_url": f"{OFFICIAL_PAGE}static/js/exp.{used}.js",
            "chunk_hash": used, "record_count": len(entries),
            "activities": {OFFICIAL_ACTIVITY_NAMES.get(k, k): v
                           for k, v in per_act.items()},
            "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    return entries, meta


def fetch_github_bank():
    """【备源】社区镜像仓库。返回 (entries, meta)。"""
    cur = json.loads(_gh_raw("data/current.json").decode("utf-8"))
    gen = cur.get("generation_id", "")
    if not gen:
        raise ValueError("镜像仓库未返回版本号")
    base = f"data/generations/{gen}"
    meta = json.loads(_gh_raw(base + "/metadata.json").decode("utf-8"))
    raw = _gh_raw(base + "/keju_questions.json", timeout=60)
    entries = json.loads(raw.decode("utf-8"))
    if not isinstance(entries, list) or len(entries) < 500:
        raise ValueError(f"镜像源数据异常({len(entries)},已放弃)")
    meta = dict(meta)
    meta["source"] = "github"
    meta["repo"] = _GH_REPO_PAGE
    return entries, meta


def _bank_content_sha(entries) -> str:
    """题库内容的稳定指纹(与来源无关),用于判断是否真的变了。"""
    import hashlib
    key = sorted(f"{e.get('question','')}|~|{e.get('answer','')}"
                 for e in entries)
    return hashlib.sha256(chr(10).join(key).encode("utf-8")).hexdigest()


def _local_meta() -> dict:
    try:
        with open(BANK_META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_bank_raw() -> list:
    """读题库原始记录(没文件就读到空表)。"""
    try:
        with open(BANK_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def merge_bank_entries(old_entries: list, new_entries: list) -> tuple:
    """只加不减:旧题一条都不丢,新源里"新题目"才追加进来。

    判重按 (归一化题干, 答案) —— 这样同一题干不同答案的歧义题也会保留;
    官方源下架的题、手工补的题,都不会因为更新而消失。
    返回 (合并后的题库, 新源新增条数, 仅本地独有的条数)。
    """
    import core

    def key(it):
        q = _no_blank(core.normalize(it.get("question") or it.get("q") or ""))
        return (q, (it.get("answer") or it.get("a") or "").strip())

    old_keys = {key(it) for it in old_entries if key(it)[0]}
    added = sum(1 for it in new_entries if key(it) not in old_keys)
    seen = {key(it) for it in new_entries}
    merged, kept = list(new_entries), 0
    for it in old_entries:
        k = key(it)
        if k[0] and k not in seen:
            seen.add(k)
            merged.append(it)
            kept += 1
    for i, it in enumerate(merged, 1):          # source_id 重新编号,保持整洁
        it["source_id"] = str(i)
    return merged, added, kept


def _keep_activities(entries, meta):
    """按配置 bank_activities 过滤更新下来的题,meta 计数同步改写。

    官方源在 fetch 阶段就按配置取了,这层闸主要管镜像备源——它不带活动
    过滤,不拦的话切到镜像时签到题会被"只加不减"地合并回来。
    activity 缺失按 keju 算(与 fetch_official_bank 的口径一致);
    配置缺失/写成两个活动之外时不过滤,行为同从前。
    """
    import core
    acts = (core.keju_cfg(core.load_config()).get("bank_activities")
            or ["keju", "qiandao"])
    keep = {a for a in acts if a in OFFICIAL_ACTIVITY_NAMES}
    if len(keep) >= len(OFFICIAL_ACTIVITY_NAMES) or not keep:
        return entries, meta
    kept = [e for e in entries if e.get("activity", "keju") in keep]
    per: dict = {}
    for e in kept:
        a = e.get("activity", "keju")
        per[a] = per.get(a, 0) + 1
    meta = dict(meta)
    meta["record_count"] = len(kept)
    meta["activities"] = {OFFICIAL_ACTIVITY_NAMES.get(k, k): v
                          for k, v in per.items()}
    return kept, meta


def update_bank():
    """检查并更新题库:优先官方源,失败回退镜像源。返回 (是否更新, 说明)。

    更新是"只加不减":旧题库里的题(含官方已下架、你手工补的)一条都不会丢,
    只有新题目会被追加;收录哪些活动由配置 bank_activities 决定。
    想彻底重建就删掉 data/keju_questions.json 再更新。
    """
    errors = []
    try:
        entries, meta = fetch_official_bank()
    except Exception as e:
        errors.append(f"官方源失败({e})")
        try:
            entries, meta = fetch_github_bank()
        except Exception as e2:
            errors.append(f"镜像源失败({e2})")
            raise ValueError("; ".join(errors)) from e2
    entries, meta = _keep_activities(entries, meta)

    old_entries = load_bank_raw()
    merged, added, kept = merge_bank_entries(old_entries, entries)
    sha = _bank_content_sha(merged)
    meta["content_sha"] = sha
    meta["record_count"] = len(merged)
    if _local_meta().get("content_sha") == sha and os.path.exists(BANK_PATH):
        return False, f"题库已是最新({len(merged)} 题,来源 {meta['source']})"

    old_count = len(old_entries)
    if old_count:
        import shutil
        shutil.copyfile(BANK_PATH, BANK_BACKUP_PATH)      # 备份旧题库
    tmp = BANK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    os.replace(tmp, BANK_PATH)                            # 原子替换
    try:
        with open(BANK_META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    new_count = len(QuestionBank().entries)
    src = "网易官方" if meta["source"] == "official" else "社区镜像"
    notes = []
    if added:
        notes.append(f"新增 {added} 题")
    if kept:
        notes.append(f"保留本地独有 {kept} 题")
    tail = f",{'、'.join(notes)}" if notes else ",题目没变化"
    return True, (f"题库已更新: {old_count} → {new_count} 题"
                  f"(来源 {src}{tail})")


class QuestionBank:
    def __init__(self, path: str | None = None):
        import core

        # 默认路径必须调用时现取:BANK_PATH 是模块级名字,写成默认参数会在
        # 定义时就绑死,之后 patch(BANK_PATH=...) 换不掉它。
        path = path or BANK_PATH
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.entries = []
        for it in raw:
            q = (it.get("question") or "").strip()
            a = (it.get("answer") or "").strip()
            nq = _no_blank(core.normalize(it.get("normalized_question") or q))
            if q and a and nq:
                self.entries.append({"q": q, "a": a, "n": nq})
        self._norms = [e["n"] for e in self.entries]
        self._groups: dict[str, list] = {}
        for e in self.entries:
            self._groups.setdefault(e["n"], []).append(e)

    def __len__(self):
        return len(self.entries)

    def match(self, text: str, threshold: float):
        """返回 (entry|None, 最高相似度)。低于阈值时 entry 为 None。"""
        import core
        return self.match_norm(core.normalize(text), threshold)

    def match_norm(self, q_norm: str, threshold: float):
        """同 match,收归一化后的题干(调用方已算过就别重复归一化)。"""
        from rapidfuzz import fuzz, process

        q = _no_blank(q_norm)
        if not q or not self.entries:
            return None, 0.0
        if len(q) < 10:
            # 短文本(如单字"是")会被包含式算法虚抬到100%,只用严格整串比对
            r = process.extractOne(q, self._norms, scorer=fuzz.ratio)
            if r and r[1] >= max(threshold, 80):
                return self.entries[r[2]], r[1]
            return None, (r[1] if r else 0.0)
        best_entry, best_score = None, 0.0
        for scorer in (fuzz.ratio, fuzz.partial_ratio, fuzz.token_set_ratio):
            r = process.extractOne(q, self._norms, scorer=scorer,
                                   score_cutoff=int(best_score))
            if r:
                _, score, idx = r
                if score > best_score:
                    best_entry, best_score = self.entries[idx], score
        if best_score < threshold:
            return None, best_score
        return best_entry, best_score

    def prefilter(self, q_norm: str, cutoff: float):
        """滑窗找题的单指标低成本预筛:fuzz.ratio 快速排除明显不沾边的窗口。

        返回 (score, entry) 或 None。通过预筛的仍要走 match/match_norm 三指标
        精匹配,这里只负责把"肯定到不了阈值"的窗口挡在门外(整窗几百个窗口时
        省大头)。q_norm 传归一化后的文本(调用方在滑窗里已经算过,不在这里
        重复算)。
        """
        from rapidfuzz import fuzz, process

        if not q_norm or not self.entries:
            return None
        r = process.extractOne(_no_blank(q_norm), self._norms,
                               scorer=fuzz.ratio, score_cutoff=int(cutoff))
        if not r:
            return None
        return r[1], self.entries[r[2]]

    def match_all(self, text: str, threshold: float, top_k: int = 6):
        """返回 [(entry, score)] 按相似度降序,含并列候选(用于选项消歧)。"""
        import core
        from rapidfuzz import fuzz, process

        q = _no_blank(core.normalize(text))
        if not q or not self.entries:
            return []
        if len(q) < 10:
            # 短文本防虚高:只用严格整串比对
            r = process.extractOne(q, self._norms, scorer=fuzz.ratio)
            if r and r[1] >= max(threshold, 80):
                return [(self.entries[r[2]], r[1])]
            return []
        merged: dict[int, float] = {}
        for scorer in (fuzz.ratio, fuzz.partial_ratio, fuzz.token_set_ratio):
            for _, score, idx in process.extract(q, self._norms, scorer=scorer,
                                                 limit=top_k):
                if score > merged.get(idx, -1):
                    merged[idx] = score
        out = [(self.entries[i], s) for i, s in merged.items() if s >= threshold]
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    def closest(self, text: str) -> float:
        """最接近的候选分数(不限阈值)。

        只在"没答出"的失败路径上用一次:用来分辨是"真题没收录"(分数接近)
        还是"屏幕上根本不是题"(分数很低,如聊天/任务栏)。
        """
        return self.match(text, 101)[1]

    @staticmethod
    def pick_with_options(candidates, options_text: str):
        """候选中答案出现在选项文本里的优先。返回 (entry|None, score, note)。

        80 是"答案清清楚楚在选项里"的硬线;够不到时回退第一名 —— 但题干
        得分打平的真歧义组(官方源同一题干带两个答案变体,如 花妖/蜃气妖)
        回退只是掷硬币,所以退一档开 65 的近似线:OCR 把「蜃气妖」读成
        「屋气妖」也有 67,认字总比掷硬币强。近似线只在第一名打平时生效。
        """
        import core
        from rapidfuzz import fuzz

        if not candidates:
            return None, 0.0, ""
        if not options_text.strip() or len(candidates) == 1:
            e, s = candidates[0]
            return e, s, ""
        onorm = core.normalize(options_text)
        best, best_vs = None, 0
        for e, s in candidates:
            vs = fuzz.partial_ratio(core.normalize(e["a"]), onorm)
            if vs > best_vs:
                best, best_vs = (e, s), vs
        if best is not None and best_vs >= 80:
            e, s = best
            return e, s, " [按选项消歧]"
        tied = len(candidates) > 1 and candidates[0][1] == candidates[1][1]
        if best is not None and best_vs >= 65 and tied:
            e, s = best
            return e, s, " [按选项消歧·近似]"
        e, s = candidates[0]
        return e, s, ""


BACKUP_KEEP = 12       # 题库备份保留份数(超过就轮转掉最旧的)
