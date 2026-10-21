"""共享的对话框基础设施:全屏冻结框选、截图取点。

各功能窗口都从这里取,自己不重复实现。
"""


def region_from_drag(start, end):
    """拖拽起止点 -> 归一化区域 [x1,y1,x2,y2];没起始点或框太小返回 None。"""
    if not start:
        return None
    rx1, rx2 = sorted((start[0], end[0]))
    ry1, ry2 = sorted((start[1], end[1]))
    if rx2 - rx1 >= 20 and ry2 - ry1 >= 12:
        return [rx1, ry1, rx2, ry2]
    return None


def pick_region(parent, prompt=None):
    """全屏冻结截图,拖拽框选,返回相对主显示器左上角的 [x1,y1,x2,y2]。"""
    import tkinter as tk
    from PIL import Image, ImageTk
    import mss
    import numpy as np

    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
    arr = np.asarray(shot)
    pil_img = Image.fromarray(arr[:, :, :3][:, :, ::-1])  # -> RGB

    result = {"region": None}
    top = tk.Toplevel(parent)
    top.attributes("-fullscreen", True, "-topmost", True)
    top.configure(bg="black")
    canvas = tk.Canvas(top, highlightthickness=0, cursor="crosshair", bg="black")
    canvas.pack(fill="both", expand=True)
    photo = ImageTk.PhotoImage(pil_img)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.image = photo
    w, h = pil_img.size
    canvas.create_rectangle(0, 0, w, h, fill="black", stipple="gray50")
    canvas.create_text(w // 2, 40, fill="#00ff66",
                       font=("Microsoft YaHei", 16, "bold"),
                       text=prompt or "拖动鼠标框住【整个题目对话框】"
                                          "(含选项和坐标),按 Esc 取消")

    state = {"start": None, "rect": None}

    def on_press(e):
        state["start"] = (e.x, e.y)
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                outline="#00ff66", width=3)

    def on_drag(e):
        if state["start"] and state["rect"]:
            x1, y1 = state["start"]
            canvas.coords(state["rect"], x1, y1, e.x, e.y)

    def on_release(e):
        if not state["start"]:
            return
        region = region_from_drag(state["start"], (e.x, e.y))
        state["start"] = None
        if region:
            result["region"] = region
        top.destroy()

    top.bind("<Escape>", lambda e: top.destroy())
    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    top.focus_force()
    parent.wait_window(top)
    return result["region"]


def freeze_screen():
    """全屏冻结截图(主显示器),返回 PIL RGB 图。"""
    from PIL import Image
    import mss
    import numpy as np

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
    arr = np.asarray(shot)
    return Image.fromarray(arr[:, :, :3][:, :, ::-1])


# ---- 窗口摆位与动效:所有窗口都走这一份,切来切去才不会"到处乱飞" ----
FADE_MS, FADE_STEP_MS = 160, 25      # 淡入淡出时长 / 每帧间隔(Windows 的定时器
#                                      粒度就是 ~16ms,帧太密反而拖长总时长)
POP_PX, POP_MS = 18, 200             # 弹出时从偏下这么多像素滑到位 / 时长
TITLE_H = 28                         # 大致标题栏高度:居中时往上让一点


def win_origin(win):
    """窗口的"摆放坐标" (x, y) —— 和 `geometry("+x+y")` 同一套。

    别拿 `winfo_rootx/rooty` 当摆放坐标:那是**客户区**的屏幕坐标,和 geometry
    差一个标题栏+边框(这台机器 125% 缩放下差 9/38),拿它去摆窗每切一次就漂一点。
    """
    try:
        return (int(win.winfo_x()), int(win.winfo_y()))
    except Exception:
        return None


def window_box(win):
    """窗口的屏幕矩形 (x, y, 宽, 高);还没映射时用请求尺寸兜底。"""
    try:
        w, h = win.winfo_width(), win.winfo_height()
        if w <= 1 or h <= 1:
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        return (win.winfo_rootx(), win.winfo_rooty(), w, h)
    except Exception:
        return None


def centered_xy(size, screen, ref=None):
    """(宽,高) 要摆到屏幕(或 ref 矩形)正中的左上角坐标,自动夹进屏幕内。"""
    w, h = int(size[0]), int(size[1])
    sw, sh = int(screen[0]), int(screen[1])
    if ref:
        x = int(ref[0]) + (int(ref[2]) - w) // 2
        y = int(ref[1]) + (int(ref[3]) - h) // 2
    else:
        x = (sw - w) // 2
        y = (sh - h) // 2 - TITLE_H // 2
    return (max(0, min(x, max(0, sw - w))), max(0, min(y, max(0, sh - h))))


def place(win, xy):
    """把窗口摆到指定左上角(不改变大小)。"""
    try:
        win.geometry(f"+{int(xy[0])}+{int(xy[1])}")
    except Exception:
        pass


def set_place(win, size=None, ref=None, screen=None):
    """决定这个窗口摆哪(记在 win.p_xy 上,并立刻摆好),返回 (x, y)。

    size 不给就用窗口的请求尺寸。所有主窗都居中 —— 这是一个应用,不各摆各的。
    """
    if screen is None:
        screen = (win.winfo_screenwidth(), win.winfo_screenheight())
    if size is None:
        size = (win.winfo_reqwidth(), win.winfo_reqheight())
    xy = centered_xy(size, screen, ref)
    win.p_xy = xy
    place(win, xy)
    return xy


def stop_anim(win):
    """把还没跑完的动效停掉。

    销毁窗口前叫一声:`after` 的回调是挂在 Tk 命令上的,窗口先没了、回调还在排队,
    Tk 删命令时会抛 "can't delete Tcl command"。
    """
    aid = getattr(win, "_anim_after", None)
    if aid:
        try:
            win.after_cancel(aid)
        except Exception:
            pass
        win._anim_after = None


def _animate(win, frames, step_ms=FADE_STEP_MS, done=None):
    """逐帧跑 frames,跑完调 done。

    单帧出异常**不能**让整段动画断掉:断了的话收尾动作(隐藏/销毁窗口)就不执行,
    窗口会一直挂在屏幕上 —— 这种"偶尔没淡出"最容易被当成玄学。
    """
    stop_anim(win)

    def run(i):
        win._anim_after = None
        if not win.winfo_exists():
            return
        if i >= len(frames):
            if done:
                try:
                    done()
                except Exception:
                    pass
            return
        try:
            frames[i]()
        except Exception:
            pass
        win._anim_after = win.after(step_ms, lambda: run(i + 1))
    run(0)


def pop_in(win, xy=None, offset=(0, POP_PX), ms=POP_MS, show=True):
    """淡入 + 轻微滑入(默认从下方 offset 像素滑到位),像"浮"出来。

    show=True 时它负责把窗口显示出来,而且**顺序是"先全透明、再显示"** ——
    反过来(先显示再设透明)中间会画出一帧满不透明的窗,看起来就是"闪一下再淡入",
    窗口越大越明显(科举那种大窗一眼就能看出来,小窗几乎看不出来)。

    xy 不给就用窗口现在的位置;`offset=None` 是纯淡入,一个 geometry 都不动 ——
    迷你窗那种位置由别人随时校正的必须用这个。
    平台不支持 -alpha 就直接显示,不折腾。
    """
    if offset is None:
        if show:
            try:
                win.attributes("-alpha", 0.0)
            except Exception:
                pass
            try:
                win.deiconify()
            except Exception:
                pass
        try:
            win.attributes("-alpha", 0.0)
        except Exception:
            return
        steps = max(1, ms // FADE_STEP_MS)

        def fade(k=0):
            win.attributes("-alpha", min(1.0, (k + 1) / float(steps)))
        _animate(win, [lambda k=i: fade(k) for i in range(steps)])
        return
    if xy is None:
        try:
            xy = (win.winfo_rootx(), win.winfo_rooty())
        except Exception:
            xy = (0, 0)
    if show:
        try:
            win.attributes("-alpha", 0.0)
        except Exception:
            pass
        try:
            win.deiconify()
        except Exception:
            pass
    try:
        win.attributes("-alpha", 0.0)
    except Exception:                     # 不支持透明度:直接摆到位
        return
    steps = max(1, ms // FADE_STEP_MS)
    frames = []
    for i in range(1, steps + 1):
        k = i / float(steps)

        def frame(k=k):
            win.attributes("-alpha", min(1.0, k))
            win.geometry("+{}+{}".format(
                int(xy[0] + offset[0] * (1 - k)),
                int(xy[1] + offset[1] * (1 - k))))
        frames.append(frame)

    def done():
        try:
            win.geometry(f"+{int(xy[0])}+{int(xy[1])}")
        except Exception:
            pass
    _animate(win, frames, done=done)


def fade_out(win, then=None, ms=FADE_MS):
    """淡出,完了执行 then(一般是 destroy / withdraw)。"""
    try:
        if not win.winfo_exists():
            raise RuntimeError("窗口没了")
        win.attributes("-alpha", 1.0)
    except Exception:
        if then:
            then()
        return
    steps = max(1, ms // FADE_STEP_MS)
    frames = []
    for i in range(1, steps + 1):
        k = i / float(steps)

        def frame(k=k):
            win.attributes("-alpha", max(0.0, 1.0 - k))
        frames.append(frame)
    _animate(win, frames, done=then)


# ---- 统一外观:全套配色与 ttk 样式(扁平卡片风),所有窗口共用这一份 ----
BG, CARD = "#f0f3f8", "#ffffff"
ACCENT, TXT, SUB = "#2f6fb0", "#1f2a3a", "#7b8794"
BORDER = "#e2e8f0"


def apply_theme():
    """配好 ttk 主题与共用样式(扁平卡片风),返回调色板。

    ttk.Style 是全局的,但样式得有人在窗口显示**之前**配过;每个窗口
    各调一次(幂等)。
    """
    from tkinter import ttk
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    font_base = ("Microsoft YaHei UI", 10)
    style.configure(".", background=CARD, foreground=TXT, font=font_base)
    style.configure("TFrame", background=CARD)
    style.configure("TLabel", background=CARD, foreground=TXT)
    style.configure("Hint.TLabel", background=CARD, foreground=SUB,
                    font=("Microsoft YaHei UI", 9))
    style.configure("Section.TLabel", background=CARD, foreground=ACCENT,
                    font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("Big.TLabel", background=CARD, foreground=ACCENT,
                    font=("Microsoft YaHei UI", 17, "bold"))
    style.configure("Footnote.TLabel", background=CARD, foreground=SUB,
                    font=("Microsoft YaHei UI", 8))
    style.configure("OK.TLabel", background=CARD, foreground="#2f9e5f",
                    font=("Microsoft YaHei UI", 9))
    style.configure("TButton", background="#e9edf3", foreground=TXT,
                    borderwidth=0, focusthickness=0, padding=(10, 5))
    style.map("TButton", background=[("active", "#dbe3ee"),
                                     ("pressed", "#ccd6e4")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
    style.map("Accent.TButton", background=[("active", "#3c81c6"),
                                            ("pressed", "#255d97")])
    style.configure("Danger.TButton", background="#f6e8e8", foreground="#a03535")
    style.map("Danger.TButton", background=[("active", "#efd9d9")])
    style.configure("TCheckbutton", background=CARD, foreground=TXT,
                    focusthickness=0)
    style.map("TCheckbutton", background=[("active", CARD)])
    style.configure("TSpinbox", fieldbackground="#ffffff", borderwidth=1,
                    arrowsize=13)
    style.configure("Slot.TLabel", background="#f7f9fc", foreground=SUB,
                    font=("Microsoft YaHei UI", 9))
    style.configure("SlotClose.TButton", background="#f7f9fc",
                    foreground="#9aa4b2", borderwidth=0, padding=(2, 2))
    style.map("SlotClose.TButton",
              background=[("active", "#f0d9d9")],
              foreground=[("active", "#c0392b")])
    style.configure("Horizontal.TScale", background=CARD,
                    troughcolor="#dfe5ee", borderwidth=0)
    return {"bg": BG, "card": CARD, "accent": ACCENT, "txt": TXT, "sub": SUB,
            "border": BORDER}

