"""界面层:单窗口应用 —— 启动即科举答题器,窗口生命周期与各按钮回调(布局在 ui/)。"""
import os
import queue
import time

import bank                 # 题库子系统:只有"检查/更新题库"这条路用它
import core                 # 地基:配置/截图/语音/引擎与键名归属表
                            # (一律 core.xxx 现取,别再拷名字过来)
from tools import keju as tools_keju   # 收益文案(纯函数,只读)
from ui import keju as ui_keju
from ui.keju import (MINI_FONT, MINI_FONT_MIN, MINI_LABEL_MAX_W,
                     MINI_MARGIN, MINI_TOP)
from ui.widgets import fade_out, pop_in, stop_anim


ENGINE_RESTART_MAX = 3    # 看门狗连续重建上限:到顶就停手,别每 10 秒重建一次刷日志


# ---------------- GUI ----------------

POLL_MS = 50      # 界面刷新间隔:日志框落屏(50ms 人眼看不出延迟)

class App:
    """组合 Tk 根窗口与各工作线程。"""

    LOG_BOX_TOOL = "keju"     # 界面上的滚动日志框只显示科举+公共的行

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
            self.log(f"[配置]键「{key}」不在归属表里,已暂存到 app 分区", ui=False)
        return "app"

    def _get(self, key, default=None):
        return self.cfg.setdefault(self._section(key), {}).get(key, default)

    def _set(self, key, val):
        self.cfg.setdefault(self._section(key), {})[key] = val

    def __init__(self):
        import tkinter as tk

        self.root = tk.Tk()
        # 按钮回调里没接住的异常,默认只打到 stderr(用户根本看不到);记进日志
        self.root.report_callback_exception = self._on_tk_error
        self.cfg = core.load_config()
        self.bank = bank.QuestionBank()
        self.last_answer = ""
        self._logs: queue.Queue = queue.Queue()
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
            n = core.prune_events()    # 事件只留当天:统计口径与收益一致
            if n:
                self.log(f"[事件]已清掉 {n} 条非当天的识别记录", ui=False)
        except Exception:
            pass
        self.log(f"[应用]启动 · 题库 {len(self.bank)} 题")
        tools_keju.record_event("boot")     # --stats 的分模式段从这里切开
        try:
            n = core.prune_voice_cache(self.cfg, self.bank)
            if n:
                self.log(f"[语音]已清理 {n} 个与当前语音参数不符的旧缓存",
                         ui=False)
        except Exception:
            pass

        ui_keju.build(self)
        # 主窗只有一个:摆在 build 里定好的位置,浮入并提到前面
        xy = getattr(self.root, "p_xy", None)
        pop_in(self.root, xy=xy)
        try:
            self.root.lift()
        except Exception:
            pass

        self.speaker = core.Speaker(self.cfg, lambda m: self.log(m, ui=False))
        self.speaker.start()
        self._engine_bad = 0           # 看门狗连续不健康次数(退避用,启动归零)
        self.engine = core.Engine(self)
        self.engine.start()

        self._auto_bank_check()
        self.log("[应用]就绪 —— 打开「自动监听」开始答题")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(POLL_MS, self._drain_logs)
        # 启动即自动定位:坐标不留在配置里,每次启动都按当前窗口重新找
        self.on_autolocate()

    # ---- 界面回调 ----
    def _refresh_watch_btn(self):
        """监听按钮文案/高亮跟引擎状态走(按钮可能已销毁,异常都兜住)。"""
        on = bool(getattr(self.engine.keju_tool, "watching", False))
        try:
            self.watch_btn.configure(
                text=("自动监听: 开" if on else "自动监听: 关"))
            self.watch_btn.configure(style="Accent.TButton" if on
                                     else "TButton")
        except Exception:
            pass

    def _refresh_mode_btn(self):
        """两个模式按钮:当前生效的高亮为蓝色。"""
        keju = bool(self._get("exam_mode", True))
        try:
            self.mode_keju_btn.configure(
                style="Accent.TButton" if keju else "TButton")
            self.mode_plain_btn.configure(
                style="TButton" if keju else "Accent.TButton")
        except Exception:
            pass

    def _set_exam_mode(self, keju: bool):
        if bool(self._get("exam_mode", True)) == keju:
            return
        self._set("exam_mode", keju)
        core.save_config(self.cfg)
        self._refresh_mode_btn()
        self.log(f"[识别模式]{'御前科举' if keju else '三大学士'}")
        tools_keju.record_event("mode", mode="御前科举" if keju else "三大学士")

    def _refresh_regions(self):
        """刷新各客户端槽位的坐标显示。"""
        regions = ((self._get("regions") or [])
                   + [None] * core.MAX_CLIENTS)
        for i in range(core.MAX_CLIENTS):
            r = regions[i]
            self.region_vars[i].set(
                f"({r[0]},{r[1]})-({r[2]},{r[3]})" if r else "未定位")

    def on_reset_stats(self):
        self.engine.keju_tool.reset_stats()
        self.log("[统计]本局已重置")

    def on_earn_detail(self):
        """收益明细:每一轮的用时/银两/物品(窗口在 ui/keju)。"""
        ui_keju.open_earn_detail(self)

    def on_earn_detail_refresh(self):
        """收益明细窗的内容:每轮一行(没数据给空态提示)。

        ui/keju 只建控件;读状态、填文本是行为,归 App(红线)。
        """
        try:
            rounds = self.engine.keju_tool.ui_state()["rounds"]
        except Exception:
            rounds = []                  # 状态口不在:窗开着但内容空,不拖垮刷新
        lines = tools_keju.reward_round_lines(rounds)
        txt = getattr(self, "earn_detail_text", None)
        if txt is None:
            return
        try:
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.insert("1.0", "\n".join(lines) if lines
                       else "还没有完成的轮次。\n"
                            "(一轮 = 游戏里那句“恭喜你完成会试，共用时X分Y秒”)")
            txt.configure(state="disabled")
        except Exception:
            pass

    def on_earn_detail_close(self):
        """明细窗关闭:跑完淡出再销毁(和别的窗口一个动效)。"""
        win = getattr(self, "earn_detail_win", None)
        if win is not None and win.winfo_exists():
            fade_out(win, then=win.destroy)

    def on_toggle_mute(self):
        self._set("mute", not bool(self._get("mute")))
        core.save_config(self.cfg)
        self._refresh_mute_btn()
        self.log("[语音]已静音(界面照旧显示答案)"
                 if self._get("mute") else "[语音]已恢复")

    def _refresh_mute_btn(self):
        on = bool(self._get("mute"))
        try:
            self.mute_btn.configure(text=("静音: 开" if on else "静音: 关"),
                                    style=("Accent.TButton" if on else "TButton"))
        except Exception:
            pass

    def on_clear_log(self):
        """清空界面滚动框(keju.log 文件不动)。"""
        try:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass

    def on_autolocate(self):
        """按窗口枚举,把每个梦幻西游客户端"整窗口"填成一条题目对话框区域。

        整窗口覆盖=不漏对话框;窗口移动/换分辨率/多开位置不同都能一次到位。
        (实测:整窗口 640x480 档 OCR 中位 0.30s,与只框对话框持平)
        每次都会先把旧坐标全部清掉:客户端挪过位置后,旧坐标只会误导。
        最多定位 core.MAX_CLIENTS 个客户端,多出的舍弃。
        """
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
            if minimized:      # 最小化时截不到画面,得让用户把窗口显示出来
                self.log("[客户端]客户端是最小化的,定位不到 —— "
                         "把游戏窗口显示出来,再点一次「自动定位客户端」")
            else:
                self.log("[客户端]没找到梦幻西游客户端窗口:"
                         "确认游戏已启动,再点一次「自动定位客户端」")
            return
        self._set("regions", [])
        regs = self._get("regions")
        for _title, box in wins[:core.MAX_CLIENTS]:
            regs.append([int(v) for v in box])
        while len(regs) < core.MAX_CLIENTS:
            regs.append(None)
        self.engine.keju_tool.set_auto_boxes([r for r in regs if r])
        core.save_config(self.cfg)
        self._refresh_regions()
        for i, (_title, box) in enumerate(wins[:core.MAX_CLIENTS]):
            size, _is_min = core.client_size_info(box)
            self.log(f"[客户端{i + 1}]整个客户端 "
                     f"({box[0]},{box[1]})-({box[2]},{box[3]}) {size}")
        n_set = len([r for r in regs if r])
        extra = (f";最多支持 {core.MAX_CLIENTS} 个,"
                 f"多出的 {len(wins) - n_set} 个舍弃"
                 if len(wins) > core.MAX_CLIENTS else "")
        self.log(f"[客户端]发现 {len(wins)} 个客户端,已定位 {n_set} 个{extra}")
        sizes = [core.client_size_info(w[1]) for w in wins[:core.MAX_CLIENTS]]
        if any(not min_ok for _t, min_ok in sizes):
            self.log(f"[提示]不是最小分辨率 —— 游戏分辨率调成 "
                     f"{core.MIN_GAME_RES[0]}×{core.MIN_GAME_RES[1]} 效果最佳"
                     "(截图更快、字也清楚)")
        self.log("[提示]挪动客户端或改了分辨率后,会自动跟随")
        if not bool(self._get("exam_mode", True)):
            self.log("[提示]整窗口区域在「三大学士」模式下噪声大,"
                     "建议切「御前科举」模式")

    def _forget_region_memory(self):
        """清掉各区域记住的上一题/画面指纹(区域变了,旧记忆作废)。"""
        self.engine.keju_tool.forget_region_memory()

    def _clear_all_regions(self):
        """清空全部客户端坐标 + 对应的画面/题目记忆(不落盘、不记日志)。"""
        self._set("regions", [None] * core.MAX_CLIENTS)
        self.engine.keju_tool.set_auto_boxes(None)   # 清空了就不再跟随客户端
        self._forget_region_memory()
        self._refresh_regions()

    def on_clear_regions(self):
        self._clear_all_regions()
        core.save_config(self.cfg)
        self.log("[客户端]全部清除")

    def on_toggle_watch(self):
        self._engine_bad = 0        # 用户重新点「自动监听」= 重新给看门狗机会重建
        self.engine.toggle_watch()
        self._refresh_watch_btn()

    def _save_interval(self):
        try:
            self._set("interval", min(core.MAX_INTERVAL, max(
                core.MIN_INTERVAL, float(self.interval_var.get()))))
        except ValueError:
            return
        self.interval_var.set(f"{self._get('interval'):.1f}")  # 回显夹紧后的值

    def on_manual_cycle(self):
        self.engine.request_cycle()

    def on_fullscan(self):
        self.engine.request_fullscan()

    def on_update_bank(self):
        """手动触发题库更新(后台线程)。"""
        if getattr(self, "_bank_updating", False):
            return
        self._bank_updating = True
        self.log("[题库]正在检查更新…")
        import threading as _th
        _th.Thread(target=self._bank_update_worker, daemon=True).start()

    def _bank_update_worker(self):
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
                        workers=4, log=lambda m: self.log(m, ui=False))
                    self.log(f"[语音]新答案音频合成完成: 成功 {done},失败 {failed}")
                else:
                    self.log("[语音]所有答案音频均已就绪")
            else:
                self.log(f"[题库]{msg}")
        except Exception as e:
            self.log(f"[题库]更新失败(继续用原题库): {e}")
        finally:
            self._bank_updating = False

    def _auto_bank_check(self):
        """启动时静默检查题库(每天一次);有新版本就自动更新。"""
        import datetime
        today = datetime.date.today().isoformat()
        if self._get("last_bank_check") == today:
            return
        self._set("last_bank_check", today)
        try:
            core.save_config(self.cfg)
        except Exception:
            pass

        def work():
            try:
                cur = bank.official_chunk_hash()
                if not cur:
                    return                                  # 网络不可用:静默跳过
                if cur == bank.local_meta().get("chunk_hash"):
                    self.log("[题库]检查: 已是最新", ui=False)
                    return
                self.log("[题库]检测到新版本,正在自动更新…")
                self._bank_updating = True
                self._bank_update_worker()
            except Exception as e:
                self.log(f"[题库]自动检查失败(忽略): {e}", ui=False)

        import threading as _th
        _th.Thread(target=work, daemon=True).start()

    def on_test_tts(self):
        self.speaker.say(core.SAY_TTS_TEST)

    def on_mini(self):
        """缩成小窗置顶,不遮挡游戏;大号字显示最近答案。"""
        if getattr(self, "mini", None) and self.mini.winfo_exists():
            self.mini.lift()
            return
        self.root.withdraw()
        self._mini_anchor = None       # 我们最近一次设定的位置
        self._mini_user_pos = None     # 用户拖过之后记住的位置
        ui_keju.build_mini(self)
        self._anchor_mini()
        pop_in(self.mini, offset=None)     # 迷你窗只淡入:它的位置由 _anchor_mini 随时管,不碰 geometry

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
            self.log(f"[界面]回调出错({exc.__name__}):{val}{where}", ui=False)
        except Exception:
            pass

    def log(self, msg: str, ui: bool = True, detail: str = "",
            ui_text=None):
        """ui=False 的消息只写 keju.log 文件,不进界面滚动框。

        detail: 只写进文件、界面不显示的补充信息(如 OCR/匹配耗时),
        方便事后按日志定位问题。
        ui_text: 界面上换一段短文案(文件里还是完整的 msg)。
        消息统一成 [时间][模块]内容;万一哪条忘了写模块,兜底成 [其他]。
        """
        if not msg.startswith("["):
            msg = "[其他]" + msg
        self._logs.put((msg, ui))
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
                     f"{type(err).__name__}: {err}", ui=False)
        except Exception:
            pass

    def _drain_logs(self):
        # 这一趟的几步各自兜一层:这个方法是 `after` 自排自的整条界面刷新链的
        # 入口,只要有一次异常漏出去,链就断了 —— 窗口还能拖、引擎还在扫,
        # 但日志框再也不更新,看起来就是"卡死不动"。
        try:
            while True:
                try:
                    msg, ui = self._logs.get_nowait()
                except queue.Empty:
                    break
                # 日志分家:这个滚动框只显示科举 + 公共的行;
                # ui=False 的只写文件,不进滚动框。
                if not ui:
                    continue
                scope = core.log_scope(msg)
                if scope and scope != self.LOG_BOX_TOOL:
                    continue
                stamp = time.strftime("%H:%M:%S")
                tag = ""
                if "匹配" in msg and "【" in msg:
                    tag = "match"          # 认出题目、给出答案的那一行
                self.log_box.configure(state="normal")
                self.log_box.insert("end", f"[{stamp}]{msg}\n", tag)
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except Exception as e:
            self._note_broken("日志落屏", e)
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
        # 本局统计 + 今日收益刷新:只认工具的只读状态口(ui_state)
        try:
            st = self.engine.keju_tool.ui_state()
        except Exception as e:
            self._note_broken("本局统计", e)
            st = None                  # 状态口不在/坏了:这一拍就不动界面
        if st is not None:
            try:
                s = st.get("stats") or {}
                n = s.get("n", 0)
                avg = (s.get("resp_sum", 0.0) / n) if n else 0.0
                um = s.get("unmatched", 0)
                txt = (f"本局: {n} 题 · 平均响应 {avg:.2f}s"
                       + (f" · 未匹配 {um}" if um else ""))
                if self.round_var.get() != txt:
                    self.round_var.set(txt)
            except Exception as e:
                self._note_broken("本局统计行", e)
            try:
                txt = tools_keju.reward_earn_text(st.get("silver"))
                if self.reward_var.get() != txt:
                    self.reward_var.set(txt)
                sig = (tuple((n, c) for n, c in (st.get("items") or [])),
                       self.reward_items_canvas.winfo_width())
                if sig != getattr(self, "_earn_sig", None):
                    self._earn_sig = sig
                    ui_keju.draw_reward_items(self, st.get("items") or [])
            except Exception as e:
                self._note_broken("收益物品行", e)
        # 区域被引擎自动跟随更新了 -> 同步界面上的坐标显示
        try:
            sig = tuple(tuple(r) if r else None
                        for r in (self._get("regions") or []))
            if sig != getattr(self, "_region_sig", None):
                self._region_sig = sig
                self._refresh_regions()
        except Exception as e:
            self._note_broken("区域显示", e)
        # 监听按钮与引擎真实状态保持同步(引擎才是唯一事实来源)
        try:
            self._refresh_watch_btn()
        except Exception as e:
            self._note_broken("监听按钮", e)
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
                                 "请检查显卡驱动/依赖,"
                                 "修好后重新点「自动监听」或重启应用")
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

    def on_close(self):
        # 关闭即清空坐标:下次启动会重新自动定位,旧坐标不留在配置里误导
        try:
            self._clear_all_regions()
            core.save_config(self.cfg)
        except Exception:
            pass
        if getattr(self, "mini", None):
            stop_anim(self.mini)          # 没跑完的动效先停,别跟销毁撞上
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
                self.log(f"[引擎]重排工具线程失败: {e}", ui=False)
        set_watch = getattr(self.engine, "set_watch", None)
        if keep["watching"] and set_watch is not None:
            try:
                set_watch(True)
            except Exception as e:
                self.log(f"[引擎]恢复自动监听失败: {e}", ui=False)
        elif keep["watching"]:
            # 没有 set_watch 的桩:至少把标志摆对,别替用户静默关掉
            keju = getattr(self.engine, "keju_tool", None)
            if keju is not None:
                keju.watching = True
                keju.enabled = True
        self.log("[引擎]用户开关已按重启前恢复:自动监听 "
                 f"{'开' if keep['watching'] else '关'}", ui=False)

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
                self.log(f"[引擎]旧引擎关闭失败(忽略): {e}", ui=False)

        _th.Thread(target=_bg, name="引擎回收", daemon=True).start()


# ---------------- 命令行(见 keju.py) ----------------
