"""窗口摆位 / 动效 —— 主窗与迷你窗共用这一份,不各写各的。"""
import tkinter as tk

FADE_STEP_MS = 25                   # 淡入每帧间隔(Windows 的定时器粒度就是
#                                     ~16ms,帧太密反而拖长总时长)
POP_PX, POP_MS = 18, 200             # 弹出时从偏下这么多像素滑到位 / 时长
TITLE_H = 28                         # 大致标题栏高度:居中时往上让一点


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


# ---- 圆角文字按钮:ttk 做不了圆角与三态,Canvas 画 ----

def round_pts(x1, y1, x2, y2, r):
    """圆角矩形轮廓点列(create_polygon + smooth=True 一次画圆)。"""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    return (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)


class TextButton(tk.Canvas):
    """圆角文字按钮:悬停变浅、按下更深,松手还在按钮上才触发回调。

    palette 由调用方按当前皮肤给(_btn_pal),本组件不自带配色。
    trail:贴右缘的小标记(模式下拉用 ✓),正文照常居中 —— 名字不因标记挪位。
    align:"c" 居中 / "w" 靠左(左内边距 12px,模式钮和下拉行用同一对齐)。
    """

    def __init__(self, parent, text, command, palette,
                 width=88, height=32, radius=9, bg=None, trail="",
                 align="c"):
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0,
                         bg=bg if bg is not None else parent["bg"])
        self._text, self._command = text, command
        self._pal = palette
        self._trail = trail
        self._align = align
        # 注意:_w/_h 是 tkinter 的控件路径属性,不能挪用,这里叫 _bw/_bh
        self._bw, self._bh, self._br = width, height, radius
        self.bind("<Enter>", lambda _e: self._paint("hover"))
        self.bind("<Leave>", lambda _e: self._paint("normal"))
        self.bind("<ButtonPress-1>", lambda _e: self._paint("press"))
        self.bind("<ButtonRelease-1>", self._release)
        self._paint("normal")

    def _paint(self, state):
        pal = self._pal
        self.delete("all")
        key = {"normal": "bg", "hover": "hover", "press": "press"}[state]
        self.create_polygon(round_pts(1, 1, self._bw - 1, self._bh - 1,
                                      self._br), smooth=True,
                            fill=pal[key], outline="")

        def mid(item):
            bb = self.bbox(item)
            if bb:
                self.move(item, 0, self._bh / 2 - (bb[1] + bb[3]) / 2)

        tx, anchor = ((12, "w") if self._align == "w"
                      else (self._bw / 2, "c"))
        mid(self.create_text(tx, self._bh / 2, text=self._text,
                             font=pal["font"], fill=pal["fg"], anchor=anchor))
        if self._trail:
            mid(self.create_text(self._bw - 10, self._bh / 2,
                                 text=self._trail, font=pal["font"],
                                 fill=pal["fg"], anchor="e"))

    def _release(self, e):
        inside = 0 <= e.x <= self._bw and 0 <= e.y <= self._bh
        self._paint("hover" if inside else "normal")
        if inside:
            self._command()

    def set_text(self, text):
        """换文案(模式/静音这类状态会变的钮用),重画回常态。"""
        self._text = text
        self._paint("normal")

    def set_trail(self, trail):
        """换右缘小标记(模式下拉的 ✓),重画回常态。"""
        self._trail = trail
        self._paint("normal")

    def set_palette(self, pal):
        """换配色(静音钮用红色系标记静音状态),重画回常态。"""
        self._pal = pal
        self._paint("normal")

    def label(self):
        """当前文案(测试与状态判断用)。"""
        return self._text

    def trail(self):
        """右缘标记文案(空串 = 没有标记)。"""
        return self._trail

    def invoke(self):
        self._command()


DOT_GAP = 1           # 状态点与文字的间距(px)
