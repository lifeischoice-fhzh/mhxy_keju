"""科举答题器主窗口 + 迷你答案窗(迷你窗归科举)。

只负责建界面;按钮动作仍然回到 App 上的同名方法。
"""
import core  # 间隔区间(MIN/MAX_INTERVAL)取自地基
from ui import widgets as ui_widgets

# ---- 迷你置顶窗 ----
MINI_FONT, MINI_FONT_MIN = 22, 11   # 默认字号 / 超长答案缩到的下限
MINI_LABEL_MAX_W = 430              # 文案超过这个宽度就逐步缩字号
MINI_MARGIN, MINI_TOP = 24, 36      # 默认停靠:屏幕右上角,留边距

# ---- 收益物品:一行名称小标签 + 右下角 ×件数(图标那套已删,2026-10-16) ----
ITEM_W = 46                         # 每个标签的宽度(px)
ITEM_H = 22                         # 标签高度(px)
ITEM_GAP = 6                        # 标签之间的间距(px)
_BADGE_FONT = ("Microsoft YaHei UI", 8, "bold")


def build(app):
    """在 app.root 上建科举的界面。回调仍然指向 app 的方法。"""
    import tkinter as tk
    from tkinter import ttk

    app.root.title("科举答题器 · 梦幻西游")
    ui_widgets.set_place(app.root, size=(880, 680))     # 居中

    # ---- 配色与样式(扁平卡片风,用 ui/widgets 里那一份) ----
    th = ui_widgets.apply_theme()
    BG, CARD = th["bg"], th["card"]
    ACCENT, TXT, SUB = th["accent"], th["txt"], th["sub"]
    BORDER = th["border"]
    app.root.configure(bg=BG)

    def card(title):
        outer = tk.Frame(app.root, bg=BORDER, highlightthickness=0)
        outer.pack(fill="x", padx=12, pady=(10, 0))
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(inner, text=title, style="Section.TLabel",
                  padding=(12, 8, 12, 0)).pack(anchor="w")
        body = ttk.Frame(inner, padding=(12, 6, 12, 12))
        body.pack(fill="both", expand=True)
        return outer, body

    # ---- 客户端(自动定位,只读显示) ----
    _, sec1 = card("客户端")
    app.region_vars = []
    slots = ttk.Frame(sec1)
    slots.pack(fill="x")
    slots.columnconfigure(0, weight=1, uniform="slot")
    slots.columnconfigure(1, weight=1, uniform="slot")
    for i in range(core.MAX_CLIENTS):
        row, col = divmod(i, 2)
        box = tk.Frame(slots, bg="#f7f9fc", highlightbackground=BORDER,
                       highlightthickness=1)
        box.grid(row=row, column=col, sticky="ew",
                 padx=(0, 8) if col == 0 else (8, 0), pady=3)
        ttk.Label(box, text=f"客户端{i + 1}", style="Slot.TLabel").pack(
            side="left", padx=(10, 6), pady=5)
        var = tk.StringVar(value="未定位")
        ttk.Label(box, textvariable=var, style="Slot.TLabel").pack(
            side="left", fill="x", expand=True)
        app.region_vars.append(var)

    bar = ttk.Frame(sec1)
    bar.pack(fill="x", pady=(10, 0))
    app.autolocate_btn = ttk.Button(bar, text="自动定位客户端",
                                    command=app.on_autolocate)
    app.autolocate_btn.pack(side="left")
    ttk.Button(bar, text="清除全部", style="Danger.TButton",
               command=app.on_clear_regions).pack(side="left", padx=(8, 0))
    app.hint_label = ttk.Label(
        bar, style="Hint.TLabel",
        text=(f"定位整个客户端窗口;最多 {core.MAX_CLIENTS} 个,"
              "多出的舍弃;最小分辨率效率最高"))
    app.hint_label.pack(side="left", padx=(10, 0))

    mbar = ttk.Frame(sec1)
    mbar.pack(fill="x", pady=(10, 0))
    ttk.Label(mbar, text="识别模式:").pack(side="left")
    app.mode_keju_btn = ttk.Button(
        mbar, text="御前科举",
        command=lambda: app._set_exam_mode(True))
    app.mode_keju_btn.pack(side="left", padx=(8, 6))
    app.mode_plain_btn = ttk.Button(
        mbar, text="三大学士",
        command=lambda: app._set_exam_mode(False))
    app.mode_plain_btn.pack(side="left")
    app._refresh_mode_btn()
    app._refresh_regions()

    # ---- 识别与播报 ----
    _, sec2 = card("识别与播报")
    bar1 = ttk.Frame(sec2)
    bar1.pack(fill="x")
    ttk.Button(bar1, text="手动识别",
               command=app.on_manual_cycle).pack(side="left")
    ttk.Button(bar1, text="全屏扫描",
               command=app.on_fullscan).pack(side="left", padx=8)
    ttk.Button(bar1, text="迷你置顶",
               command=app.on_mini).pack(side="left")
    ttk.Button(bar1, text="播报试听",
               command=app.on_test_tts).pack(side="left", padx=(8, 0))
    ttk.Button(bar1, text="更新题库",
               command=app.on_update_bank).pack(side="left", padx=(8, 0))
    app.mute_btn = ttk.Button(bar1, text="静音: 关", width=8,
                              command=app.on_toggle_mute)
    app.mute_btn.pack(side="left", padx=(8, 0))
    app._refresh_mute_btn()
    bar2 = ttk.Frame(sec2)
    bar2.pack(fill="x", pady=(10, 0))
    app.watch_btn = ttk.Button(bar2, text="自动监听: 关",
                               command=app.on_toggle_watch)
    app.watch_btn.pack(side="left")
    ttk.Label(bar2, text="监听间隔").pack(side="left", padx=(18, 5))
    app.interval_var = tk.StringVar(
        value=f"{float(app._get('interval', core.MIN_INTERVAL)):.1f}")
    ttk.Spinbox(bar2, from_=core.MIN_INTERVAL, to=core.MAX_INTERVAL,
                increment=0.1,
                width=5, textvariable=app.interval_var,
                command=app._save_interval).pack(side="left")
    ttk.Label(bar2, text="秒").pack(side="left", padx=(4, 0))
    ttk.Label(bar2, style="Hint.TLabel",
              text="空闲时轮询,出现题目后自动加快到 0.1 秒;多开可调大省 CPU").pack(
                  side="left", padx=(16, 0))

    # ---- 日志 ----
    outer, sec4 = card("日志")
    outer.pack_configure(expand=True, fill="both")
    sbar = ttk.Frame(sec4)
    sbar.pack(fill="x", pady=(0, 6))
    app.round_var = tk.StringVar(value="本局: 0 题")
    ttk.Label(sbar, textvariable=app.round_var,
              style="Info.TLabel").pack(side="left")
    ttk.Button(sbar, text="重置统计", width=9, style="SlotClose.TButton",
               command=app.on_reset_stats).pack(side="right")
    app.clear_log_btn = ttk.Button(sbar, text="清空日志", width=9,
                                   style="SlotClose.TButton",
                                   command=app.on_clear_log)
    app.clear_log_btn.pack(side="right", padx=(0, 6))
    # 总收益:一行银两 + 一行物品图标(逐轮明细收进「收益明细」按钮)
    earn = ttk.Frame(sec4)
    earn.pack(fill="x", pady=(0, 2))
    app.reward_var = tk.StringVar(value="收益：—")
    ttk.Label(earn, textvariable=app.reward_var,
              style="Hint.TLabel").pack(side="left")
    app.earn_detail_btn = ttk.Button(earn, text="收益明细", width=9,
                                     command=app.on_earn_detail)
    app.earn_detail_btn.pack(side="right", padx=(6, 0))
    irow = ttk.Frame(sec4)
    irow.pack(fill="x", pady=(0, 4))
    ttk.Label(irow, text="物品：", style="Hint.TLabel").pack(side="left")
    app.reward_items_canvas = tk.Canvas(irow, height=ITEM_H + 6,
                                        highlightthickness=0, bg=CARD)
    app.reward_items_canvas.pack(side="left", fill="x", expand=True)
    app.log_box = tk.Text(sec4, height=12, wrap="word",
                          font=("Microsoft YaHei UI", 10),
                          relief="flat", background="#fafbfd",
                          foreground=TXT, insertbackground=ACCENT,
                          padx=10, pady=8,
                          highlightthickness=1,
                          highlightbackground=BORDER,
                          highlightcolor=BORDER)
    app.log_box.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(sec4, command=app.log_box.yview)
    sb.pack(side="right", fill="y")
    app.log_box.configure(yscrollcommand=sb.set, state="disabled")
    app.log_box.tag_config("rec", foreground="#6b7688")
    # 只有"认出题目、给出答案"那一行高亮:绿字加粗;其余全是默认色
    app.log_box.tag_config("match", foreground="#0a7d3f",
                           font=("Microsoft YaHei UI", 11, "bold"))

    tk.Label(app.root, bg=BG, fg=SUB, font=("Microsoft YaHei UI", 9),
             text="使用: 启动即自动定位客户端 → 打开自动监听即可;两种答题"
                  "界面用上面的“识别模式”按钮切换").pack(
                      fill="x", padx=14, pady=8)


def build_mini(app):
    """建迷你答案窗(app.mini / app.mini_var / app.mini_label),归科举。"""
    import tkinter as tk
    from tkinter import ttk

    m = tk.Toplevel(app.root)
    m.title("科举答案")
    m.attributes("-topmost", True)
    m.resizable(False, False)
    app.mini_var = tk.StringVar(value="—")
    app.mini_label = ttk.Label(m, textvariable=app.mini_var,
                               font=("Microsoft YaHei", MINI_FONT, "bold"),
                               foreground="#12603a", padding=(16, 8))
    app.mini_label.pack(side="left")
    side = ttk.Frame(m)
    side.pack(side="left", padx=(0, 12))
    ttk.Button(side, text="还原", width=6,
               command=app.on_restore).pack(pady=(6, 2))
    m.protocol("WM_DELETE_WINDOW", app.on_restore)
    app.mini = m


def draw_reward_items(app, items):
    """把「物品：」那行画出来:名称小标签 + 右下角 ×件数。

    items 是 [(物品名, 件数), ...](按件数降序)。图标那套(自动采集 + data/items)
    2026-10-16 按用户要求整个删掉了,只剩标签 —— 统计不受影响。
    """
    cv = getattr(app, "reward_items_canvas", None)
    if cv is None or not cv.winfo_exists():
        return
    cv.delete("all")
    # 一行放不下就换行(物品多的时候);宽度未知(窗口还没显示)按一行算
    width = max(cv.winfo_width(), ITEM_W * 2 + 4)
    x, y = 2, 3
    row_h = ITEM_H + 4
    for name, cnt in items or []:
        if x + ITEM_W > width and x > 2:
            x, y = 2, y + row_h
        cv.create_rectangle(x, y, x + ITEM_W, y + ITEM_H,
                            fill="#eef2f8", outline="#d7dee8")
        cv.create_text(x + ITEM_W // 2, y + ITEM_H // 2, text=name[:6],
                       width=ITEM_W - 2, font=("Microsoft YaHei UI", 8),
                       fill="#5b6675")
        txt = f"×{cnt}"
        bw = 8 + 6 * len(txt)
        cv.create_rectangle(x + ITEM_W - bw, y + ITEM_H - 12,
                            x + ITEM_W + 1, y + ITEM_H + 1,
                            fill="#12203a", outline="")
        cv.create_text(x + ITEM_W - 2, y + ITEM_H - 1, text=txt,
                       anchor="se", fill="#ffd54a", font=_BADGE_FONT)
        x += ITEM_W + ITEM_GAP
    cv.configure(height=y - 3 + ITEM_H + 3,
                 scrollregion=(0, 0, width, y - 3 + ITEM_H + 3))


def open_earn_detail(app):
    """收益明细窗:每一轮的用时 / 银两 / 物品。只建控件;刷新/关闭归 App。"""
    import tkinter as tk
    from tkinter import ttk

    win = getattr(app, "earn_detail_win", None)
    if win is not None and win.winfo_exists():
        win.lift()
        return
    win = tk.Toplevel(app.root)
    win.title("收益明细 · 每一轮")
    ui_widgets.set_place(win, size=(620, 320),
                         ref=ui_widgets.window_box(app.root))
    win.transient(app.root)
    app.earn_detail_win = win
    ui_widgets.pop_in(win, xy=win.p_xy)

    bar = ttk.Frame(win, padding=(10, 8))
    bar.pack(fill="x")
    ttk.Label(bar, text="每一轮的用时 / 银两 / 物品",
              style="Section.TLabel").pack(side="left")
    txt = tk.Text(win, wrap="none", relief="flat", background="#fafbfd",
                  foreground="#1f2a3a", padx=10, pady=8,
                  font=("Microsoft YaHei UI", 10))
    txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    ttk.Button(bar, text="刷新", width=6,
               command=app.on_earn_detail_refresh).pack(side="right")
    ttk.Button(bar, text="关闭", width=6,
               command=app.on_earn_detail_close).pack(side="right", padx=(0, 6))
    win.bind("<Escape>", lambda e: app.on_earn_detail_close())
    app.earn_detail_text = txt
    app.on_earn_detail_refresh()
