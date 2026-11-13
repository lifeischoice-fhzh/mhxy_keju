"""界面层:单窗口应用 —— 启动即 mhxy_keju,窗口生命周期与各按钮回调(布局在 ui/)。"""
import os
import time
from tkinter import messagebox

import bank                 # 题库子系统:只有"检查/更新题库"这条路用它
import core                 # 地基:配置/截图/语音/引擎与键名归属表
                            # (一律 core.xxx 现取,别再拷名字过来)
from tools import keju as tools_keju   # 识别事件(纯函数,只读)
from ui import keju as ui_keju
from ui.keju import (MINI_FONT, MINI_FONT_MIN, MINI_LABEL_MAX_W,
                     MINI_MARGIN, MINI_TOP, mute_palette)
from ui.widgets import pop_in, stop_anim


def _cleanup_litter():
    """关闭后清掉可再生的杂物:字节码/日志轮转残留/ruff 缓存/打包中间产物;
    语音缓存超上限时从最旧开始删(缓存是速度优化,不是数据)。

    只在源码运行时做:打包版(sys.frozen)不动自己所在目录。
    """
    import shutil
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        return
    root = Path(__file__).parent
    for rel in ("build", ".ruff_cache", "__pycache__", "tools/__pycache__",
                "ui/__pycache__", "keju.log.old"):
        p = root / rel
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception:
            pass
    cache, cap = root / "voice_cache", 40 * 1024 * 1024
    try:
        files = sorted(cache.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= cap:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
    except Exception:
        pass


ENGINE_RESTART_MAX = 3    # 看门狗连续重建上限:到顶就停手,别每 10 秒重建一次刷日志


def _set_appusermodelid():
    """给进程设独立 AppUserModelID(须在首个窗口创建前调)。

    不设的话,从控制台/脚本拉起时任务栏会把本应用和控制台合成一颗按钮,
    按钮图标显示成 Python 终端;设了之后应用按独立程序显示,图标才认
    `ui_keju.set_icon` 挂上去的状元帽。
    """
    import ctypes
    try:
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [
            ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID("mhxy_keju")
    except Exception:
        pass


# ---------------- GUI ----------------

POLL_MS = 50      # 界面刷新间隔:答案上屏(50ms 人眼看不出延迟)
PROBE_TICKS = 600          # 环境预检哨:每拍 POLL_MS,×600 ≈ 30 秒一次
PROBE_MISS_MAX = 3         # 连续几拍预检不到才判停用(≈90 秒容抖动)

class App:
    """组合 Tk 根窗口与各工作线程。"""

    # ---- 配置读写:按归属表归到对应分区 ----
    def _section(self, key):
        """查归属表;认不出的键记一条警告(打错键别静默落进 app 分区)。"""
        section = core.key_section(key)
        if section is not None:
            return section
        seen = getattr(self, "_unknown_keys", None)
        if seen is None:
            seen = self._unknown_keys = set()
        if key not in seen:
            seen.add(key)
            self.log(f"[配置]键「{key}」不在归属表里,已暂存到 app 分区")
        return "app"

    def _get(self, key, default=None):
        return self.cfg.setdefault(self._section(key), {}).get(key, default)

    def _set(self, key, val):
        self.cfg.setdefault(self._section(key), {})[key] = val

    def __init__(self, readonly=False):
        import tkinter as tk

        _set_appusermodelid()
        self.root = tk.Tk()
        # 按钮回调里没接住的异常,默认只打到 stderr(用户根本看不到);记进日志
        self.root.report_callback_exception = self._on_tk_error
        self._readonly = bool(readonly)   # 只读模式:窗口照常开,只是不能答题
        self.cfg = core.load_config()
        # 新老配置对齐:mode 是界面/统计认的模式名,exam_mode 是识别管线认的旧键
        self._set("mode", self._mode())
        self._set("exam_mode", core.mode_exam(self._mode()))
        self.bank = bank.QuestionBank()
        self.last_answer = ""
        self.last_question = ""
        self.skin = self._get("skin", "dark")
        try:
            if os.path.exists(core.LOG_PATH):
                import datetime
                mtime_day = datetime.date.fromtimestamp(
                    os.path.getmtime(core.LOG_PATH))
                too_big = os.path.getsize(core.LOG_PATH) > 2_000_000
                if mtime_day != datetime.date.today() or too_big:
                    # 非当天日志(或过大):轮转成 .old 保留一份,不累积
                    os.replace(core.LOG_PATH, core.LOG_PATH + ".old")
        except Exception:
            pass
        try:
            n = core.prune_events()    # 事件只留当天:统计只看当天
            if n:
                self.log(f"[事件]已清掉 {n} 条非当天的识别记录")
        except Exception:
            pass
        self.log(f"[应用]启动 · 题库 {len(self.bank)} 题")
        tools_keju.record_event("boot")     # --stats 的分模式段从这里切开
        if not self._readonly:
            try:
                n = core.prune_voice_cache(self.cfg, self.bank)
                if n:
                    self.log(f"[语音]已清理 {n} 个与当前语音参数不符的旧缓存")
            except Exception:
                pass

        ui_keju.build(self, self.skin)
        if self._readonly:
            ui_keju.draw_readonly(self.hero_canvas, self.skin)
        # 模式下拉:点窗内别处 / Esc 收起(展开由模式钮自己管)
        self.root.bind("<Button-1>", self._on_root_click, add="+")
        self.root.bind("<Escape>", lambda _e: self._close_mode_menu())
        # 主窗只有一个:摆在 build 里定好的位置,浮入并提到前面
        xy = getattr(self.root, "p_xy", None)
        pop_in(self.root, xy=xy)
        try:
            self.root.lift()
        except Exception:
            pass

        if self._readonly:
            # 只读模式:不造引擎不造语音 —— 答题相关的路全不通,识别区常驻提示
            self.speaker = None
            self.engine = None
        else:
            self.speaker = core.Speaker(self.cfg, lambda m: self.log(m))
            self.speaker.start()
            self._engine_bad = 0       # 看门狗连续不健康次数(退避用,启动归零)
            self.engine = core.Engine(self)
            self.engine.start()
            self._auto_bank_update()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(POLL_MS, self._drain_logs)
        if self._readonly:
            try:
                self.status_dot.set_state(False)    # 红点常亮
            except Exception:
                pass
            self.log(f"[应用]{core.HINT_TXT} —— 应用已停用,不能识别与播报")
        else:
            self.log("[应用]就绪 —— 已默认开启自动监听,定位到客户端即开始答题")
            # 启动即自动定位:坐标不留在配置里,每次启动都按当前窗口重新找
            self.on_autolocate()

    # ---- 界面回调 ----
    def _mode(self):
        """当前识别模式名:新配置读 mode;只有老 exam_mode 布尔时按族推。"""
        name = self._get("mode", "")
        if name in core.KEJU_MODE_NAMES:
            return name
        return (core.DEFAULT_MODE if bool(self._get("exam_mode", True))
                else "三大学士")

    def _refresh_mode_btn(self):
        """右上模式钮:文案=当前模式 + ▾(点开下拉切换)。"""
        try:
            self.mode_btn.set_text(f"{self._mode()} ▾")
        except Exception:
            pass

    def _refresh_mode_menu(self):
        """下拉面板里把当前模式标出来(✓ + 主题色),其余回普通色。"""
        for name, btn in getattr(self, "mode_rows", []):
            try:
                cur = name == self._mode()
                btn.set_text(("✓ " if cur else "") + name)
                btn.set_palette(ui_keju.mode_row_palette(self.skin, cur))
            except Exception:
                pass

    def on_toggle_mode(self):
        """点右上模式钮:收起/放下识别模式下拉。"""
        if getattr(self, "_readonly", False):
            return
        if getattr(self, "_mode_menu_open", False):
            self._close_mode_menu()
        else:
            self._open_mode_menu()

    def _open_mode_menu(self):
        menu = getattr(self, "mode_menu", None)
        if menu is None:
            return
        self._refresh_mode_menu()
        menu.place(x=ui_keju.MODE_MENU_X, y=ui_keju.MODE_MENU_Y,
                   width=ui_keju.MODE_MENU_W, height=ui_keju.MODE_MENU_H)
        menu.lift()
        self._mode_menu_open = True

    def _close_mode_menu(self):
        menu = getattr(self, "mode_menu", None)
        self._mode_menu_open = False
        if menu is None:
            return
        try:
            menu.place_forget()
        except Exception:
            pass

    def _on_root_click(self, event):
        """点主窗别处时收起下拉(模式钮和面板自己的点击不关)。"""
        if not getattr(self, "_mode_menu_open", False):
            return
        w = event.widget
        if w is getattr(self, "mode_btn", None):
            return
        menu = getattr(self, "mode_menu", None)
        while w is not None:
            if w is menu:
                return
            w = getattr(w, "master", None)
        self._close_mode_menu()

    def on_pick_mode(self, name):
        """下拉里选中一个模式。"""
        if getattr(self, "_readonly", False):
            return
        self._close_mode_menu()
        self._set_mode(name)

    def _set_mode(self, name):
        if name not in core.KEJU_MODE_NAMES or self._mode() == name:
            return
        self._set("mode", name)
        # exam_mode 是识别管线读的旧键,一直跟着模式走,别让它和 mode 打架
        self._set("exam_mode", core.mode_exam(name))
        core.save_config(self.cfg)
        self._refresh_mode_btn()
        self._refresh_mode_menu()
        self.log(f"[识别模式]{name}")
        tools_keju.record_event("mode", mode=name)

    def _set_exam_mode(self, keju: bool):
        """旧接口:只表达识别族,映射到该族的默认模式(测试/老调用用)。"""
        self._set_mode(core.DEFAULT_MODE if keju else "三大学士")

    def _refresh_regions(self):
        """刷新客户端胶囊(角色名/未定位);引擎还没起来时也安全。"""
        regions = ((self._get("regions") or [])
                   + [None] * core.MAX_CLIENTS)
        names = []
        if getattr(self, "engine", None) is not None:
            try:
                names = self.engine.keju_tool.ui_state().get("names") or []
            except Exception:
                names = []
        ui_keju.refresh_capsules(self, regions, names)

    def on_toggle_mute(self):
        if getattr(self, "_readonly", False):
            return
        self._set("mute", not bool(self._get("mute")))
        core.save_config(self.cfg)
        self._refresh_mute_btn()
        self.log("[语音]已静音(界面照旧显示答案)"
                 if self._get("mute") else "[语音]已恢复")

    def on_autolocate(self, manual=False):
        """按窗口枚举,把每个梦幻西游客户端"整窗口"填成一条题目对话框区域。

        整窗口覆盖=不漏对话框;窗口移动/换分辨率/多开位置不同都能一次到位。
        (实测:整窗口 640x480 档 OCR 中位 0.30s,与只框对话框持平)
        每次都会先把旧坐标全部清掉:客户端挪过位置后,旧坐标只会误导。
        最多定位 core.MAX_CLIENTS 个客户端,多出的舍弃。
        manual=True 是「客户端刷新」按钮的手动刷新:检测到的全是登录窗时
        弹「请先登录角色」;启动自动定位只落日志,别一开应用就弹框。
        """
        if getattr(self, "_readonly", False):
            return    # 停用态:按钮点击一律静默丢弃(不处理、不记日志)
        self._clear_all_regions()          # 先无条件清空,再重新填
        try:
            wins, minimized = core.find_client_windows()
        except Exception as e:
            core.save_config(self.cfg)
            self.log(f"[客户端]自动定位失败: {e}")
            return
        for t in minimized:
            self.log(f"[客户端]客户端已最小化,跳过(截不到画面): {t}")
        if not wins:
            core.save_config(self.cfg)
            if minimized:      # 最小化时截不到画面,窗口显示出来就自动接入
                self.log("[客户端]客户端是最小化的,定位不到 —— "
                         "把游戏窗口显示出来后会自动接入")
            else:
                self.log("[客户端]没找到梦幻西游客户端窗口:"
                         "游戏启动并登录角色后会自动接入")
            return
        # 只定位支持的三档(640×480 / 800×600 / 1024×768,按画布精确判):其它档
        # 识别慢、字也小,跳过并弹框说明;混合时支持的照常定位
        ok = [(t, b) for t, b in wins if core.client_supported(b)]
        bad = [(t, b) for t, b in wins if not core.client_supported(b)]
        if not ok:
            if bad and all(not core.client_name(t) for t, _b in bad):
                # 全是登录窗(标题抠不出角色名,独立版/多标签版同判):
                # 提示登录后再刷新,别拿「分辨率不支持」误导人
                self._hint_login(len(bad), manual)
            else:
                self._log_unsupported(bad)
                self._warn_resolution()
            core.save_config(self.cfg)
            return
        self._set("regions", [])
        regs = self._get("regions")
        for _title, box in ok[:core.MAX_CLIENTS]:
            regs.append([int(v) for v in box])
        while len(regs) < core.MAX_CLIENTS:
            regs.append(None)
        names = [core.client_name(t) for t, _box in ok[:core.MAX_CLIENTS]]
        self.engine.keju_tool.set_auto_boxes([r for r in regs if r], names)
        core.save_config(self.cfg)
        self._refresh_regions()
        for i, (_title, box) in enumerate(ok[:core.MAX_CLIENTS]):
            size, _is_min = core.client_size_info(box)
            tail = f" 角色「{names[i]}」" if names[i] else ""
            self.log(f"[客户端{i + 1}]整个客户端 "
                     f"({box[0]},{box[1]})-({box[2]},{box[3]}) {size}{tail}")
        self._log_unsupported(bad)
        n_set = len([r for r in regs if r])
        extra = (f";最多支持 {core.MAX_CLIENTS} 个,"
                 f"多出的 {len(ok) - n_set} 个舍弃"
                 if len(ok) > core.MAX_CLIENTS else "")
        self.log(f"[客户端]发现 {len(wins)} 个客户端,已定位 {n_set} 个{extra}")
        if bad:
            self._warn_resolution()
        sizes = [core.client_size_info(w[1]) for w in ok[:core.MAX_CLIENTS]]
        if any(not min_ok for _t, min_ok in sizes):
            self.log(f"[提示]不是最小分辨率 —— 游戏分辨率调成 "
                     f"{core.MIN_GAME_RES[0]}×{core.MIN_GAME_RES[1]} 效果最佳"
                     "(截图更快、字也清楚)")
        self.log("[提示]挪动客户端或改了分辨率后,会自动跟随")
        if not self.engine.keju_tool.watching:
            self.engine.set_watch(True)   # 自动监听默认开:定位到客户端就(重新)接管
        if not core.mode_exam(self._mode()):
            self.log(f"[提示]「{self._mode()}」没有结构锚点,靠题库比对认题,"
                     "科举会试/殿试带锚点更稳")

    def _log_unsupported(self, bad):
        """逐个记一行不支持的客户端(跳过原因落在文件里,弹框只报总数)。

        原因文案在 core.client_size_info 里按场景写全(分辨率不支持 /
        未登录角色没有游戏画面),这里只补支持档。
        """
        for _title, box in bad:
            self.log(f"[客户端]{core.client_size_info(box)[0]}"
                     f"(只支持 {core.supported_res_text()})")

    def _warn_resolution(self):
        """定位到了不支持分辨率档的客户端:弹一次框说明(测试里桩掉)。

        跳过了哪些窗口走日志(_log_unsupported),框里只说规则一句话。
        """
        messagebox.showwarning(
            "分辨率不支持",
            f"目前只支持 {core.supported_res_text()} 分辨率,"
            f"{core.MIN_GAME_RES[0]}×{core.MIN_GAME_RES[1]} 效果最佳")

    def on_refresh_clients(self):
        """「客户端刷新」按钮:手动刷新(全是登录窗时弹「请先登录角色」)。"""
        self.on_autolocate(manual=True)

    def _hint_login(self, count, manual):
        """检测到的客户端全是登录窗:提示登录角色后再刷新。

        独立窗口版和多标签版的登录窗都靠「标题抠不出角色名」认出来
        (见 core.find_client_canvases 的登录闸)。手动点「客户端刷新」
        才弹框;启动时的自动定位只落日志,别一开应用就弹框。
        """
        self.log(f"[客户端]检测到 {count} 个登录窗口:"
                 "请登录角色后再点「客户端刷新」")
        if manual:
            messagebox.showinfo("请先登录角色",
                                "请先登录角色,登录后再点「客户端刷新」")

    def on_client_hover_start(self, i):
        """鼠标悬停角色名框:浮出该角色的游戏分辨率小条。

        按**角色名**现场匹配(标题实时解析,与窗口位置无关 —— 拖动窗口后
        定位时的区域框会过期,名字不会);没跟踪到名字的槽位退回区域框
        匹配;都查不到(确实退回登录界面)就地写明,不弹框。
        """
        names = []
        if getattr(self, "engine", None) is not None:
            try:
                names = self.engine.keju_tool.ui_state().get("names") or []
            except Exception:
                names = []
        name = names[i] if i < len(names) else None
        if name:
            for title, res, _rect, _ratio, _mode in \
                    core.find_client_canvases()[0]:
                if core.client_name(title) == name:
                    self._hover_tip(i, f"{res[0]}×{res[1]}")
                    return
            self._hover_tip(i, "未登录角色,没有游戏画面")
            return
        regions = [r for r in (self._get("regions") or []) if r]
        if i >= len(regions):
            self._hover_tip(i, "未定位")
            return
        canvas = core.client_canvas([int(v) for v in regions[i]])
        if canvas:
            res = canvas[1]
            self._hover_tip(i, f"{res[0]}×{res[1]}")
        else:
            self._hover_tip(i, "未登录角色,没有游戏画面")

    def on_client_hover_end(self, _i=0):
        tip = getattr(self, "_client_tip", None)
        if tip is not None:
            tip.destroy()
            self._client_tip = None

    def _hover_tip(self, slot, text):
        """悬停小条:角色名框**上方**浮出,圆角+描边与右侧按钮同款(ghost 色)。

        Tk 窗口画不出真圆角,四角用透明色抠掉(Windows 的
        -transparentcolor,应用只跑 Windows);宽度按文案实测自适应
        (tkfont 量宽),重建比改字稳 —— 文案长短不一,画布裁字。
        """
        import tkinter as tk
        from tkinter import font as tkfont
        self.on_client_hover_end()          # 先收旧的:宽度要跟着文案重算
        th = ui_keju.SKINS[getattr(self, "skin", "dark")]
        pal = ui_keju.tip_palette(getattr(self, "skin", "dark"))
        key = "#010101" if th["bg"] != "#010101" else "#020202"
        tip = tk.Toplevel(self.root)
        try:
            tip.wm_overrideredirect(True)   # 无边框小条
            tip.wm_attributes("-topmost", True)
            tip.wm_attributes("-transparentcolor", key)
        except Exception:
            pass
        tip.configure(bg=key)
        text_w = int(tkfont.Font(root=self.root, font=pal["font"]).measure(text))
        w, h = max(72, text_w + 26), 30
        cv = tk.Canvas(tip, width=w, height=h, highlightthickness=0, bg=key)
        cv.pack()
        cv.create_polygon(
            ui_keju.round_pts(1, 1, w - 1, h - 1, th["btn_r"]),
            smooth=True, fill=pal["bg"], outline=th["row_line"], width=1)
        tid = cv.create_text(w / 2, h / 2, text=text, font=pal["font"],
                             fill=pal["fg"], anchor="c")
        bb = cv.bbox(tid)
        if bb:
            cv.move(tid, 0, h / 2 - (bb[1] + bb[3]) / 2)
        tip.tip_cv, tip.tip_tid = cv, tid
        self._client_tip = tip
        caps = getattr(self, "client_capsules", [])
        if slot < len(caps):
            cv0 = caps[slot]
            tip.wm_geometry(f"+{cv0.winfo_rootx()}"
                            f"+{cv0.winfo_rooty() - h - 6}")

    def _forget_region_memory(self):
        """清掉各区域记住的上一题/画面指纹(区域变了,旧记忆作废)。"""
        self.engine.keju_tool.forget_region_memory()

    def _clear_all_regions(self):
        """清空全部客户端坐标 + 对应的画面/题目记忆(不落盘、不记日志)。"""
        self._set("regions", [None] * core.MAX_CLIENTS)
        self.engine.keju_tool.set_auto_boxes(None)   # 清空了就不再跟随客户端
        self._forget_region_memory()
        self._refresh_regions()

    def on_manual_scan(self):
        """手动识别:立刻投递一次识别任务(不受自动监听开关限制)。"""
        if getattr(self, "_readonly", False):
            return
        self.engine.request_cycle()
        self.log("[手动识别]已手动触发一次识别")

    def on_toggle_skin(self):
        """换肤:玄夜(深)/ 玄白(浅)一键切换,选择落盘下次启动沿用。"""
        if getattr(self, "_readonly", False):
            return
        skin = "light" if self._get("skin", "dark") == "dark" else "dark"
        self._set("skin", skin)
        core.save_config(self.cfg)
        ui_keju.apply_skin(self, skin)
        self.log("[界面]已切换到"
                 + ("玄夜" if skin == "dark" else "玄白") + "皮肤")

    def _refresh_mute_btn(self):
        """静音钮文案+配色跟状态走:静音=红色系「语音恢复」,未静音=普通。"""
        try:
            muted = bool(self._get("mute"))
            self.mute_btn.set_text(
                "语音恢复" if muted else "语音静音")
            self.mute_btn.set_palette(mute_palette(self.skin, muted))
        except Exception:
            pass

    def _bank_update_worker(self, mark_day=None):
        try:
            updated, msg = bank.update_bank()
            if updated:
                self.bank = bank.QuestionBank()     # 热加载
                n = core.prune_voice_cache(self.cfg, self.bank)
                self.log(f"[题库]{msg};已清理 {n} 个失效音频缓存")
                missing = core.count_missing_voice(self.cfg, self.bank)
                if missing:
                    self.log(f"[语音]检测到 {missing} 条答案没有音频缓存,后台自动合成中…")
                    done, failed, _ = core.build_voice_cache(
                        workers=4, log=lambda m: self.log(m))
                    self.log(f"[语音]新答案音频合成完成: 成功 {done},失败 {failed}")
                else:
                    self.log("[语音]所有答案音频均已就绪")
            else:
                self.log(f"[题库]{msg}")
        except Exception as e:
            self.log(f"[题库]更新失败(继续用原题库): {e}")
        else:
            if mark_day:            # 成功(含"已是最新")才记当天:失败的下次再试
                self._set("last_bank_check", mark_day)
                try:
                    core.save_config(self.cfg)
                except Exception:
                    pass
        finally:
            self._bank_updating = False

    def _auto_bank_update(self):
        """每天首次启动后台更新一次题库,当天再启动直接跳过。

        更新成功(含"已是最新")才记 last_bank_check;失败的当天不记,
        下次启动会再试。没有界面按钮。
        """
        import datetime
        today = datetime.date.today().isoformat()
        if self._get("last_bank_check") == today:
            return
        import threading as _th
        _th.Thread(target=self._bank_update_worker, args=(today,),
                   daemon=True).start()

    def on_mini(self):
        """缩成小窗置顶,不遮挡游戏;大号字显示最近答案。"""
        if getattr(self, "_readonly", False):
            return
        if getattr(self, "mini", None) and self.mini.winfo_exists():
            self.mini.lift()
            return
        self.root.withdraw()
        self._mini_anchor = None       # 我们最近一次设定的位置
        self._mini_user_pos = None     # 用户拖过之后记住的位置
        ui_keju.build_mini(self)
        self._anchor_mini()
        pop_in(self.mini, offset=None)     # 迷你窗只淡入:它的位置由 _anchor_mini 随时管,不碰 geometry

    def on_mini_drag_start(self, e):
        """抓着大字开始拖迷你窗(记下鼠标与窗口的相对位置)。"""
        m = self.mini
        if m and m.winfo_exists():
            self._mini_drag = (e.x_root, e.y_root, m.winfo_x(), m.winfo_y())

    def on_mini_drag_move(self, e):
        """拖动中:按开场记的相对位置挪窗;松手后 _anchor_mini 会把新位置
        认作用户位置(照常沿用,不会被拉回右上角)。"""
        at = getattr(self, "_mini_drag", None)
        m = self.mini
        if not at or not m or not m.winfo_exists():
            return
        m.geometry(f"+{at[2] + e.x_root - at[0]}+{at[3] + e.y_root - at[1]}")

    def _anchor_mini(self):
        """定位小窗:默认钉在屏幕右上角;用户自己拖过之后,就按他放的位置来。

        位置不是我们上次设的 -> 判定为用户拖动,记下来一直沿用;
        只有当窗口因为答案变长会伸出屏幕右边时,才往左夹一点。
        """
        try:
            m = self.mini
            if not m or not m.winfo_exists():
                return
            m.update_idletasks()
            sw = m.winfo_screenwidth()
            w = min(m.winfo_reqwidth(), max(200, sw - MINI_MARGIN * 2))
            pos = (m.winfo_x(), m.winfo_y())
            if self._mini_anchor and pos != self._mini_anchor:
                self._mini_user_pos = pos           # 用户挪过:以后照他放的位置
            user = self._mini_user_pos
            if user:
                x = min(max(user[0], 0), max(0, sw - w - MINI_MARGIN))
                y = max(0, user[1])
            else:
                x, y = max(0, sw - w - MINI_MARGIN), MINI_TOP
            if pos != (x, y):
                m.geometry(f"+{x}+{y}")
            self._mini_anchor = (x, y)
        except Exception:
            pass

    def on_restore(self):
        if getattr(self, "_readonly", False):
            return
        if getattr(self, "mini", None):
            try:
                self.mini.destroy()
            except Exception:
                pass
            self.mini = None
        self.root.deiconify()

    # ---- 线程安全日志 ----
    def _on_tk_error(self, exc, val, tb):
        """界面回调里没接住的异常:Tk 默认只写到 stderr,用户看不见,记进日志。"""
        where = ""
        try:
            import traceback
            frames = traceback.extract_tb(tb) if tb else []
            if frames:
                where = f" @ {frames[-1].name}:{frames[-1].lineno}"
        except Exception:
            where = ""
        try:
            self.log(f"[界面]回调出错({exc.__name__}):{val}{where}")
        except Exception:
            pass

    def log(self, msg: str, detail: str = ""):
        """写一条日志进 keju.log(界面上没有日志框,全部只落文件)。

        detail: 补充信息(如 OCR/匹配耗时),同样进文件。
        消息统一成 [时间][模块]内容;万一哪条忘了写模块,兜底成 [其他]。
        """
        if not msg.startswith("["):
            msg = "[其他]" + msg
        try:
            with open(core.LOG_PATH, "a", encoding="utf-8") as f:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"{stamp} {msg}{detail}\n")
        except Exception:
            pass

    def _note_broken(self, key, err):
        """刷新链里某个兜底分支第一次失败时记进日志(同类失败只记一次)。

        这些分支静默是为了"刷新链不能断"(界面还能用);但完全无声的话,
        坏了没人知道 —— 首错记一条进文件,后面同类失败不再重复。
        """
        seen = getattr(self, "_broken_seen", None)
        if seen is None:
            seen = self._broken_seen = set()
        if key in seen:
            return
        seen.add(key)
        try:
            self.log(f"[界面]刷新项「{key}」失败(同类失败不再记): "
                     f"{type(err).__name__}: {err}")
        except Exception:
            pass

    def _drain_logs(self):
        # 这一趟的几步各自兜一层:这个方法是 `after` 自排自的整条界面刷新链的
        # 入口,只要有一次异常漏出去,链就断了 —— 窗口还在、引擎还在扫,
        # 但答案再也不上屏,看起来就是"卡死不动"。
        # 环境预检哨:只认联网对时,本机时间一概不看。30 秒一拍起一个
        # 后台探针(结果摆属性、主线程消费,绝不阻塞界面):确认跨过锚点
        # 一拍就停;预检不到连续 PROBE_MISS_MAX 拍也按停用处理(约 90 秒,
        # 免得一次网络抖动打死正在用的会话;要更严改小常数即可)
        try:
            self._probe_ticks = getattr(self, "_probe_ticks", 0) + 1
            if self._probe_ticks >= PROBE_TICKS:
                self._probe_ticks = 0
                if not getattr(self, "_probing", False):
                    self._probing = True
                    import threading as _th

                    def _probe():
                        self._probe_net = core.net_probe()
                        self._probing = False

                    _th.Thread(target=_probe, daemon=True,
                               name="环境预检").start()
            net = getattr(self, "_probe_net", "?")
            if net != "?":                 # 有一拍结果:主线程消费
                self._probe_net = "?"
                if self._probe_tick(net):
                    self._go_readonly()
                    self.root.after(POLL_MS, self._drain_logs)
                    return
        except Exception as e:
            self._note_broken("环境预检", e)
        if getattr(self, "_readonly", False):
            # 停用态:窗口留着只显示提示,引擎相关的整条刷新链都跳过
            self.root.after(POLL_MS, self._drain_logs)
            return
        # 状态灯:监听中 = 绿点闪烁;未监听/未定位 = 红点常亮
        try:
            watching = bool(getattr(self.engine.keju_tool, "watching", False))
            self.status_dot.set_state(watching)
        except Exception as e:
            self._note_broken("状态灯", e)
        # 题目识别区:内容变了才重画(识别线程只摆 last_question/last_answer)
        try:
            qa = (self.last_question or "", self.last_answer or "")
            if qa != getattr(self, "_hero_shown", None):
                self._hero_shown = qa
                ui_keju.draw_hero(self.hero_canvas, *qa, self.skin)
        except Exception as e:
            self._note_broken("答案上屏", e)
        # 迷你窗显示最近答案(单行大号字;过长自动缩字号)
        try:
            if getattr(self, "mini", None) and self.mini.winfo_exists():
                txt = self.last_answer if self.last_answer else "—"
                if self.mini_var.get() != txt:
                    self.mini_var.set(txt)
                    self.root.update_idletasks()
                    # 先还原默认字号,再按需缩小:否则长答案缩过之后,
                    # 后面的短答案会一直用那个小字号
                    size = MINI_FONT
                    self.mini_label.configure(
                        font=("Microsoft YaHei", size, "bold"))
                    self.root.update_idletasks()
                    while (self.mini_label.winfo_reqwidth() > MINI_LABEL_MAX_W
                           and size > MINI_FONT_MIN):
                        size -= 2
                        self.mini_label.configure(
                            font=("Microsoft YaHei", size, "bold"))
                        self.root.update_idletasks()
                    self._anchor_mini()
        except Exception as e:
            self._note_broken("迷你窗", e)
        # 区域被引擎自动跟随更新了(或角色名变了)-> 同步胶囊显示
        try:
            names = self.engine.keju_tool.ui_state().get("names") or []
            sig = (tuple(tuple(r) if r else None
                         for r in (self._get("regions") or [])),
                   tuple(names))
            if sig != getattr(self, "_region_sig", None):
                self._region_sig = sig
                self._refresh_regions()
        except Exception as e:
            self._note_broken("区域显示", e)
        # 引擎线程看门狗:线程退出、或 OCR 假就绪(warmup 推理失败)都重建(10 秒冷却)
        try:
            alive = self.engine.is_alive()
            # 线程活着不等于识别可用:健康标志不正常时同样重建
            healthy = alive and bool(getattr(self.engine, "ocr_ok", True))
            if healthy:
                self._engine_bad = 0            # 起来了:退避计数归零
            elif time.time() - getattr(self, "_engine_restart_ts", 0) > 10:
                # 每 10 秒最多算一次,免得计数被刷屏的每一拍顶上去
                self._engine_restart_ts = time.time()
                self._engine_bad = getattr(self, "_engine_bad", 0) + 1
                if self._engine_bad > ENGINE_RESTART_MAX:
                    # 到顶就停手:这台机器重建多少次也起不来,再重建只是白刷日志
                    if self._engine_bad == ENGINE_RESTART_MAX + 1:   # 只提示这一次
                        self.log(f"[引擎]连续 {ENGINE_RESTART_MAX} 次重建都没恢复"
                                 "(OCR 起不来或线程退出),已停止自动重建:"
                                 "请检查显卡驱动/依赖,修好后重启应用")
                else:
                    self.log("[引擎]线程已退出,正在自动重启…" if not alive
                             else "[引擎]OCR 不健康,正在重建引擎…")
                    old = self.engine   # 先停掉旧引擎的活,别放出僵尸工具线程
                    keep = self._engine_switches(old)  # 开关先记下,重建后摆回去
                    self._retire_engine(old)    # 停识别 + 后台回收:Tk 线程不等
                    self.engine = core.Engine(self)
                    self.engine.start()
                    self._restore_engine_switches(keep)
        except Exception as e:
            self._note_broken("引擎看门狗", e)
        self.root.after(POLL_MS, self._drain_logs)

    def _probe_tick(self, net):
        """消费一拍预检结果:返回 True=该停用。None 记一次 miss,连拍满
        PROBE_MISS_MAX 按停用处理;有时刻则确认跨线即停、没跨清零重计。"""
        if net is None:
            self._probe_miss = getattr(self, "_probe_miss", 0) + 1
        else:
            self._probe_miss = 0
            if core.env_state(net)[0]:
                return True
        return self._probe_miss >= PROBE_MISS_MAX

    def _go_readonly(self):
        """预检不过(启动传入或运行中跨线):停识别播报,识别区常驻提示。

        窗口不关:用户还能看到发生了什么,换肤这类纯界面操作照常,答题
        相关的路(引擎/监听/手动识别/定位/模式切换)全部不通。
        """
        if getattr(self, "_readonly", False):
            return
        self._readonly = True
        try:
            self._clear_all_regions()      # 引擎还在时先清坐标(它要动工具)
        except Exception:
            pass
        eng = getattr(self, "engine", None)
        if eng is not None:
            self._retire_engine(eng)       # 停识别 + 后台回收,不等线程
            self.engine = None
        try:
            core.save_config(self.cfg)
        except Exception:
            pass
        self.log(f"[应用]{core.HINT_TXT},已停止识别与播报")
        try:
            ui_keju.draw_readonly(self.hero_canvas, self.skin)
            self.status_dot.set_state(False)
        except Exception as e:
            self._note_broken("环境预检", e)

    def on_close(self):
        # 关闭即清空坐标:下次启动会重新自动定位,旧坐标不留在配置里误导
        try:
            self._clear_all_regions()
            core.save_config(self.cfg)
        except Exception:
            pass
        _cleanup_litter()         # 顺手清掉可再生杂物(见函数说明)
        if getattr(self, "mini", None):
            stop_anim(self.mini)          # 没跑完的动效先停,别跟销毁撞上
        if getattr(self, "speaker", None):
            self.speaker.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


    # ---- 引擎看门狗:重启时用户的开关要原样搬过去 ----
    def _engine_switches(self, engine):
        """记下旧引擎上的"自动监听"开关。

        它不在配置里:watching 只活在工具对象上。引擎重建会造出全新的工具,
        不搬就等于替用户静默关掉。
        """
        keju = getattr(engine, "keju_tool", None)
        return {"watching": bool(getattr(keju, "watching", False))}

    def _restore_engine_switches(self, keep):
        """把记下的开关还给新引擎,再让 refresh_tools 重新分派工具线程。

        自动监听只在"原来就开着"时走 set_watch(True)(内部会再刷一次);
        原来就关着的不调 set_watch(False):状态没变不该多打一条"已停止"。
        """
        refresh = getattr(self.engine, "refresh_tools", None)
        if refresh is not None:
            try:
                refresh()
            except Exception as e:
                self.log(f"[引擎]重排工具线程失败: {e}")
        set_watch = getattr(self.engine, "set_watch", None)
        if keep["watching"] and set_watch is not None:
            try:
                set_watch(True)
            except Exception as e:
                self.log(f"[引擎]恢复自动监听失败: {e}")
        elif keep["watching"]:
            # 没有 set_watch 的桩:至少把标志摆对,别替用户静默关掉
            keju = getattr(self.engine, "keju_tool", None)
            if keju is not None:
                keju.watching = True
                keju.enabled = True
        self.log("[引擎]用户开关已按重启前恢复:自动监听 "
                 f"{'开' if keep['watching'] else '关'}")

    def _retire_engine(self, old):
        """回收旧引擎:先停它的识别,再把 stop+join 丢后台,Tk 线程绝不在这等。

        直接 shutdown() 会 join 工具线程(最坏几秒),而看门狗跑在 Tk 主线程上,
        界面会真冻住。所以:
        ① 先 deactivate:旧工具立刻不再识别播报,旧/新两套短暂并存时同一题
           不会播两遍(两边的 recent_answered 不共享,防重播拦不住);
        ② stop+join 交给一条临时线程:旧线程照样会退,只是界面不等它。
        """
        tool = getattr(old, "keju_tool", None)
        off = getattr(tool, "deactivate", None)
        if off is not None:
            try:
                off()
            except Exception:
                pass
        shutdown = getattr(old, "shutdown", None)
        if shutdown is None:
            return
        import threading as _th

        def _bg():
            try:
                shutdown()
            except Exception as e:
                self.log(f"[引擎]旧引擎关闭失败(忽略): {e}")

        _th.Thread(target=_bg, name="引擎回收", daemon=True).start()


# ---------------- 命令行(见 keju.py) ----------------
