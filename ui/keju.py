"""mhxy_keju 主窗口(定稿模板 + 双皮肤)+ 迷你答案窗(归科举)。

只负责建界面;按钮动作仍然回到 App 上的同名方法。
"""
import core  # 间隔区间(MIN/MAX_INTERVAL)取自地基
from tkinter import font as tkfont
from ui import widgets as ui_widgets
from ui.widgets import StatusDot, TextButton, round_pts

# ---- 迷你置顶窗 ----
MINI_FONT, MINI_FONT_MIN = 22, 11   # 默认字号 / 超长答案缩到的下限
MINI_LABEL_MAX_W = 430              # 文案超过这个宽度就逐步缩字号
MINI_MARGIN, MINI_TOP = 24, 36      # 默认停靠:屏幕右上角,留边距

# ---- 主窗:左竖排角色名 + 右上操作钮 + 题目识别区 ----
WIN_W, WIN_H = 664, 232
SIDE_W = 170                        # 左栏宽
BOX_W, BOX_H = 146, 30              # 角色名框 / 客户端刷新按钮(同宽对齐)
BOX_STEP = 36                       # 竖排步进(框高 30 + 间距 6)
BTN_W, BTN_H = 110, 36            # 右上操作钮:等宽大钮,4 颗正好铺满右区
MAIN_X = 182                        # 右区起始 x(左栏 170 + 12 间距)
MODE_MENU_X, MODE_MENU_Y = MAIN_X, 50
MODE_MENU_W = 160                   # 模式下拉面板:压在题目卡左上角
MODE_ROW_H, MODE_ROW_GAP = 28, 2
MODE_MENU_PAD = 6
MODE_MENU_H = (MODE_MENU_PAD * 2
               + len(core.KEJU_MODES) * (MODE_ROW_H + MODE_ROW_GAP)
               - MODE_ROW_GAP)
MODE_HINT = "点上方模式按钮可切换模式"
YH = "Microsoft YaHei UI"
Q_FONT = (YH, 13)
A_FONT = (YH, 36, "bold")

# ---- 双皮肤:M1 玄夜(深)/ M4 玄白(浅),换肤按钮一键切换 ----
SKINS = {
    "dark": {   # 玄夜:近黑蓝底 + 翡翠绿
        "bg": "#10141b", "panel": "#151a23", "card": "#151a23",
        "line": "#232b38", "row": "#1b212c", "row_line": "#39455a",
        "name": "#dfe5ee",
        "txt": "#eef2f8", "sub": "#8b96a8",
        "primary": {"bg": "#34b07c", "hover": "#3bc28a", "press": "#2b9a6d",
                    "fg": "#0c1712"},
        "ghost": {"bg": "#1b212c", "hover": "#232b38", "press": "#2a3342",
                  "fg": "#aeb8c6"},
        "answer": "#eef2f8",
        "ph_doc": "#1d2430", "ph_line": "#333e4e",
        "ph_text": "#5f6b80", "ph_sub": "#414c5c",
        "mute": {"bg": "#3a1f24", "hover": "#46262c", "press": "#522b32",
                 "fg": "#ff9b9b"},
        "card_r": 12, "row_r": 9, "btn_r": 9,
    },
    "light": {  # 玄白:白纸黑字强对比
        "bg": "#ffffff", "panel": "#f5f5f2", "card": "#ffffff",
        "line": "#e6e6e0", "row": "#ffffff", "row_line": "#c8c8bd",
        "name": "#141414",
        "txt": "#141414", "sub": "#8a8a84",
        "primary": {"bg": "#141414", "hover": "#333333", "press": "#000000",
                    "fg": "#ffffff"},
        "ghost": {"bg": "#ffffff", "hover": "#f0f0ec", "press": "#e4e4de",
                  "fg": "#333333"},
        "answer": "#141414",
        "ph_doc": "#f2f2ee", "ph_line": "#d9d9d2",
        "ph_text": "#9a9a92", "ph_sub": "#b5b5ae",
        "mute": {"bg": "#fdeaea", "hover": "#f9dcdc", "press": "#f4cccc",
                 "fg": "#c0392b"},
        "card_r": 6, "row_r": 15, "btn_r": 16,
    },
}


def _btn_pal(colors, font_size):
    """从皮肤色生成 TextButton 调色板(含字号)。"""
    pal = dict(colors)
    pal["font"] = ((YH, font_size, "bold") if font_size >= 10
                   else (YH, font_size))
    return pal


def mute_palette(skin, muted):
    """静音钮的调色板:静音 = 红色系提醒,未静音 = 普通 ghost。"""
    return _btn_pal(SKINS[skin]["mute" if muted else "ghost"], 10)


def tip_palette(skin):
    """悬停分辨率小条的调色板:ghost 同款,与右侧按钮样式一致。"""
    return _btn_pal(SKINS[skin]["ghost"], 10)


def mode_row_palette(skin, selected):
    """模式下拉里的一行:当前模式用主题色,其余 ghost。"""
    return _btn_pal(SKINS[skin]["primary" if selected else "ghost"], 11)


def _draw_zhuangyuan(d):
    """在 1024×1024 画布上画扁平状元帽(金色帽体 + 花瓣帽翅,缩到 256 抗锯齿)。"""
    gold, gold_hi = (250, 200, 70, 255), (255, 228, 130, 255)
    brim, red = (206, 138, 36, 255), (214, 60, 44, 255)
    line, hi = (74, 46, 14, 255), (255, 240, 200, 255)

    def petal(p0, p1, p2, w0, w1, color, steps=16):
        # 花瓣形帽翅:贝塞尔曲线上铺一排渐大的圆,并集即两端圆头的瓣
        for i in range(steps + 1):
            t = i / steps
            u = 1 - t
            x = u * u * p0[0] + 2 * t * u * p1[0] + t * t * p2[0]
            y = u * u * p0[1] + 2 * t * u * p1[1] + t * t * p2[1]
            r = (w0 + (w1 - w0) * t) / 2
            d.ellipse((x - r, y - r, x + r, y + r), fill=color)

    # 帽翅:勾边打底再盖色,近水平胖瓣自帽身两侧外捅、微上翘,根部埋进帽体
    for pts in (((345, 495), (250, 428), (128, 400)),
                ((679, 495), (774, 428), (896, 400))):
        petal(*pts, 198, 188, line)
        petal(*pts, 170, 160, gold)
    # 帽体:勾边打底 + 半圆帽顶 + 外撇帽身(金圈出边),下沿藏进帽沿后面
    d.pieslice((238, 234, 786, 782), 180, 360, fill=line)
    d.polygon([(238, 508), (786, 508), (828, 762), (198, 762)], fill=line)
    d.pieslice((252, 248, 772, 768), 180, 360, fill=gold)
    d.polygon([(252, 508), (772, 508), (812, 762), (212, 762)], fill=gold)
    d.ellipse((330, 306, 650, 546), fill=gold_hi)   # 冠面柔光(斜上)
    # 顶珠:半沉进帽顶的小金球
    d.ellipse((462, 204, 562, 304), fill=line)
    d.ellipse((470, 212, 554, 296), fill=gold)
    d.ellipse((488, 232, 516, 260), fill=gold_hi)
    # 帽沿 + 红宝
    d.rounded_rectangle((182, 722, 842, 826), radius=52, fill=line)
    d.rounded_rectangle((196, 736, 828, 812), radius=38, fill=brim)
    d.ellipse((472, 734, 552, 814), fill=line)
    d.ellipse((482, 744, 542, 804), fill=red)
    d.ellipse((492, 756, 512, 776), fill=hi)


def set_icon(root):
    """窗口/任务栏图标:状元帽(科举夺魁),运行时画,无素材文件。

    1024 高分辨率绘制 + LANCZOS 缩放抗锯齿,两路下发:
    - iconphoto(True) 给多尺寸阶梯(16~256):设窗口类图标,连之后创建的
      Toplevel(迷你窗)一起覆盖,各处取最贴近的尺寸,不让 Tk 现缩出脏边;
    - WM_SETICON 直挂大/小 HICON:任务栏按钮只认这个,iconphoto 改的
      类图标对已建好的按钮不刷新(按钮会一直显示 Python 终端图标);
      尺寸按系统 DPI 的 SM_CXICON/SM_CXSMICON 取,高分屏不糊。
    """
    try:
        from PIL import Image, ImageDraw, ImageTk
        img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
        _draw_zhuangyuan(ImageDraw.Draw(img))
    except Exception:
        return
    photos = [ImageTk.PhotoImage(img.resize((s, s), Image.LANCZOS))
              for s in (16, 20, 24, 32, 48, 64, 256)]
    root._icon_imgs = photos          # 留引用,防 PhotoImage 被回收
    root.iconphoto(True, *photos)
    _set_taskbar_icon(root, img)


def _set_taskbar_icon(root, img):
    """WM_SETICON 给窗口挂大/小 HICON(任务栏真正认的口)。

    时机:窗口未映射时 Tk 还没定真实 HWND,所以挂图标等 <Map> 事件
    (pop_in 的 deiconify 会触发);已映射(换肤重建)则立即挂。
    独占控制台启动时控制台窗也是我们的(双击/脚本拉起、没有终端共用),
    顺手一起换成状元帽 —— 否则任务栏把控制台和主窗合成一颗按钮,显示
    Python 终端图标。换肤重建时旧句柄先替换再回收。
    """
    import ctypes
    from ctypes import wintypes

    class _ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    try:
        from PIL import Image
        user32, gdi32, kernel32 = (ctypes.windll.user32, ctypes.windll.gdi32,
                                   ctypes.windll.kernel32)
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateIconIndirect.restype = wintypes.HICON
        user32.CreateIconIndirect.argtypes = [ctypes.POINTER(_ICONINFO)]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        gdi32.CreateBitmap.restype = wintypes.HBITMAP
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        kernel32.GetConsoleProcessList.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]

        def hicon(size):
            rgba = img.resize((size, size), Image.LANCZOS)
            bi = _BMIH(ctypes.sizeof(_BMIH), size, -size, 1, 32, 0)
            bits = ctypes.c_void_p()
            color = gdi32.CreateDIBSection(None, ctypes.byref(bi), 0,
                                           ctypes.byref(bits), None, 0)
            if not color or not bits:
                return 0
            ctypes.memmove(bits, rgba.tobytes("raw", "BGRA"), size * size * 4)
            mask = gdi32.CreateBitmap(size, size, 1, 1, None)
            hc = user32.CreateIconIndirect(
                ctypes.byref(_ICONINFO(True, 0, 0, mask, color)))
            gdi32.DeleteObject(color)
            gdi32.DeleteObject(mask)
            return hc

        big = hicon(max(user32.GetSystemMetrics(11), 16))    # SM_CXICON
        small = hicon(max(user32.GetSystemMetrics(49), 12))  # SM_CXSMICON

        def apply_to(hwnd):
            if hwnd:
                user32.SendMessageW(hwnd, 0x80, 1, big)    # WM_SETICON 大
                user32.SendMessageW(hwnd, 0x80, 0, small)  # WM_SETICON 小

        old = getattr(root, "_icon_hicons", [])
        root._icon_hicons = [big, small]

        def fire(_event=None):
            if root._icon_done:
                return
            root._icon_done = True
            apply_to(user32.GetAncestor(root.winfo_id(), 2))  # GA_ROOT
            con = kernel32.GetConsoleWindow()
            if con:
                pids = (wintypes.DWORD * 4)()
                if (kernel32.GetConsoleProcessList(pids, 4) == 1
                        and pids[0] == kernel32.GetCurrentProcessId()):
                    apply_to(con)   # 独占控制台:任务栏合成按钮显示的是它

        # 窗口未映射时 Tk 还没定真实 HWND(winfo_id 是临时句柄,映射即销毁),
        # 直接挂会挂到死窗上 —— 必须等 <Map>;换肤重建时已映射,立即挂
        root._icon_done = bool(root.winfo_ismapped())
        if root._icon_done:
            fire()
        root.bind("<Map>", fire)
        for h in old:
            user32.DestroyIcon(h)
    except Exception:
        pass


def build(app, skin="dark"):
    """按皮肤建主窗(换肤 = 清空子件后整窗重建,见 apply_skin)。"""
    import tkinter as tk

    th = SKINS[skin]
    app.skin = skin
    app.root.title("mhxy_keju")
    set_icon(app.root)
    app.root.resizable(False, False)
    app.root.geometry(f"{WIN_W}x{WIN_H}")   # place 布局不产生请求尺寸,窗要显式定
    ui_widgets.set_place(app.root, size=(WIN_W, WIN_H))
    app.root.configure(bg=th["bg"])

    # 左栏:角色名竖排 + 客户端刷新(替代原定位按钮)
    side = tk.Frame(app.root, width=SIDE_W, height=WIN_H, bg=th["panel"],
                    highlightthickness=0)
    side.place(x=0, y=0, width=SIDE_W, height=WIN_H)  # 显式定高:刷新钮才会露出
    app.client_capsules = []
    for i in range(core.MAX_CLIENTS):
        cv = tk.Canvas(side, width=BOX_W, height=BOX_H,
                       highlightthickness=0, bg=th["panel"])
        cv.grid(row=i, column=0, padx=(12, 12), pady=(12 if i == 0 else 0, 6))
        app.client_capsules.append(cv)
    app.refresh_btn = TextButton(
        side, "客户端刷新", app.on_refresh_clients,
        palette=_btn_pal(th["primary"], 10), width=BOX_W, height=BOX_H,
        radius=th["btn_r"], bg=th["panel"])
    app.refresh_btn.place(x=12, y=12 + core.MAX_CLIENTS * BOX_STEP)
    # 左栏空态占位:没定位到客户端时显示(见 refresh_capsules / draw_side_ph)
    app.side_ph = tk.Canvas(side, width=BOX_W, height=150,
                            highlightthickness=0, bg=th["panel"])
    app.side_ph.place(x=12, y=18)
    app._refresh_regions()

    # 右上:操作按钮依次排开(模式钮 = 当前模式 + ▾,点开下拉切模式)
    pal_p = _btn_pal(th["primary"], 11)
    pal_g = _btn_pal(th["ghost"], 11)
    app.mode_btn = TextButton(app.root, "模式", app.on_toggle_mode,
                              palette=pal_p, width=BTN_W, height=BTN_H,
                              radius=th["btn_r"])
    app.manual_btn = TextButton(app.root, "手动识别", app.on_manual_scan,
                                palette=pal_g, width=BTN_W, height=BTN_H,
                                radius=th["btn_r"])
    app.mini_btn = TextButton(app.root, "迷你置顶", app.on_mini,
                              palette=pal_g, width=BTN_W, height=BTN_H,
                              radius=th["btn_r"])
    app.mute_btn = TextButton(app.root, "语音静音", app.on_toggle_mute,
                              palette=pal_g, width=BTN_W, height=BTN_H,
                              radius=th["btn_r"])
    # 4 颗钮在右区(MAIN_X ~ 窗右留 12)等间距平铺:110×36,间距 10
    btns = (app.mode_btn, app.manual_btn, app.mini_btn, app.mute_btn)
    span = (WIN_W - 12 - MAIN_X - BTN_W) / (len(btns) - 1)
    for k, btn in enumerate(btns):
        btn.place(x=int(MAIN_X + k * span), y=10)
    app._refresh_mode_btn()
    app._refresh_mute_btn()

    # 题目识别区:白卡 + 题目/大字答案;没信息时画占位(见 draw_hero)
    app.hero_canvas = tk.Canvas(app.root, width=WIN_W - 12 - MAIN_X,
                                height=WIN_H - 12 - 52, bg=th["bg"],
                                highlightthickness=0)
    app.hero_canvas.place(x=MAIN_X, y=52)
    # 状态灯:监听中绿点闪烁 / 未监听红点常亮(叠在题目卡右上角)
    app.status_dot = StatusDot(app.root, d=10, bg=th["card"])
    app.status_dot.place(x=WIN_W - 28, y=62)
    # 换肤:玄夜 / 玄白 一键切换(选择记进配置,下次启动沿用)
    app.skin_btn = TextButton(
        app.root, "换肤", app.on_toggle_skin,
        palette=_btn_pal(th["ghost"], 9), width=52, height=22,
        radius=7, bg=th["card"])
    app.skin_btn.place(x=572, y=58)
    draw_hero(app.hero_canvas, "", "", skin)
    # 题目卡左上角的引导:点一下也能直接开模式下拉
    app.hero_canvas.tag_bind("mode_hint", "<Button-1>",
                             lambda _e: app.on_toggle_mode())
    build_mode_menu(app, skin)
    app._refresh_mode_btn()
    app._refresh_mode_menu()


def build_mode_menu(app, skin):
    """模式下拉面板:默认藏着,点模式钮才放下(app.on_toggle_mode)。

    行是圆角钮(和别处一致),当前模式由 app._refresh_mode_menu 上主题色。
    """
    import tkinter as tk

    th = SKINS[skin]
    panel = tk.Frame(app.root, bg=th["line"], highlightthickness=0)
    inner = tk.Frame(panel, bg=th["card"], highlightthickness=0)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    app.mode_rows = []
    for i, name in enumerate(core.KEJU_MODE_NAMES):
        btn = TextButton(
            inner, name, lambda n=name: app.on_pick_mode(n),
            palette=mode_row_palette(skin, False),
            width=MODE_MENU_W - 2 * MODE_MENU_PAD, height=MODE_ROW_H,
            radius=th["row_r"], bg=th["card"])
        btn.place(x=MODE_MENU_PAD,
                  y=MODE_MENU_PAD + i * (MODE_ROW_H + MODE_ROW_GAP))
        app.mode_rows.append((name, btn))
    panel.place_forget()
    app.mode_menu = panel
    app._mode_menu_open = False


def fit_text(text, font, maxw):
    """按像素宽度截断文本,超出部分以 … 结尾。"""
    f = font if isinstance(font, tkfont.Font) else tkfont.Font(font=font)
    if f.measure(text) <= maxw:
        return text
    while text and f.measure(text + "…") > maxw:
        text = text[:-1]
    return text + "…"


def draw_hero(cv, question, answer, skin="dark"):
    """重画题目识别区:卡片 + 题目(超宽加 …)+ 大字答案。

    没有任何信息时画占位:小文档图形 + 「等待识别…」。
    """
    th = SKINS[skin]
    cv.delete("all")
    w, h = int(cv["width"]), int(cv["height"])
    cv.create_polygon(round_pts(1, 1, w - 1, h - 1, th["card_r"]), smooth=True,
                      fill=th["card"], outline=th["line"], width=1)
    # 模式引导常驻:很多用户不知道右上那颗钮是能点的
    cv.create_text(24, 15, text=MODE_HINT, font=(YH, 9), fill=th["sub"],
                   anchor="w", tags="mode_hint")
    if not question and not answer:
        cx, gy = w / 2, h / 2 - 60
        cv.create_polygon(round_pts(cx - 21, gy, cx + 21, gy + 54, 6),
                          smooth=True, fill=th["ph_doc"],
                          outline=th["ph_line"], width=1)
        for k, ln in enumerate((16, 27, 38)):
            cv.create_line(cx - 11, gy + ln, cx + (11 if k == 2 else 13),
                           gy + ln, fill=th["ph_line"], width=2,
                           capstyle="round")
        cv.create_text(cx, gy + 76, text="等待识别…",
                       font=(YH, 11, "bold"), fill=th["ph_text"])
        cv.create_text(cx, gy + 96, text="识别到题目后会自动显示在这里",
                       font=(YH, 9), fill=th["ph_sub"], tags="ph")
        return
    q = fit_text(question, Q_FONT, w - 48)
    cv.create_text(24, 42, text=q, font=Q_FONT, fill=th["sub"], anchor="w",
                   tags="q")
    a = fit_text(answer or "—", A_FONT, w - 48)
    cv.create_text(24, 96, text=a, font=A_FONT, fill=th["answer"],
                   anchor="w", tags="a")


def draw_readonly(cv, skin="dark"):
    # 识别区常驻「token已用完」(文案在 core.HINT_TXT);窗口照常在,只是不能再答题
    """只读模式:识别区常驻停用提示,窗口照常开、只是不能再答题。"""
    th = SKINS[skin]
    cv.delete("all")
    w, h = int(cv["width"]), int(cv["height"])
    cv.create_polygon(round_pts(1, 1, w - 1, h - 1, th["card_r"]), smooth=True,
                      fill=th["card"], outline=th["line"], width=1)
    cx = w / 2
    cv.create_text(cx, h / 2 - 40, text="已超过使用期限",
                   font=(YH, 11, "bold"), fill=th["sub"])
    cv.create_text(cx, h / 2 + 10, text="token已用完",
                   font=A_FONT, fill=th["answer"])


def apply_skin(app, skin):
    """换肤:清掉主窗全部子件按新皮肤重建;迷你窗若开着也重开成新配色。

    迷你窗是 root 的子窗,destroy 一并没了;重建时保留用户拖过的位置
    (_mini_user_pos),只重置自己刚记的锚点,免得把旧锚误判成用户挪过。
    """
    app.skin = skin
    for w in app.root.winfo_children():
        w.destroy()
    build(app, skin)
    app._hero_shown = None          # 让刷新链立刻按新皮肤重画题目区
    if getattr(app, "_readonly", False):
        draw_readonly(app.hero_canvas, skin)   # 只读模式:重建后提示照样常驻
    if getattr(app, "mini", None) is not None:
        app._mini_anchor = None
        app.mini = None
        build_mini(app)
        app._anchor_mini()


def build_mini(app):
    """建迷你答案窗(app.mini / app.mini_var / app.mini_label),归科举。

    无边框(overrideredirect):系统标题栏在 DPI 感知进程里拖不动
    (Tk 8.6 的非客户区事件进不了绑定,探针实测),索性去掉 ——
    整个窗面抓着就能拖,还原走窗上的「还原」按钮。
    配色跟主窗皮肤(SKINS):卡片底 + 1px 描边 + 圆角钮,没有 ttk 默认灰。
    """
    import tkinter as tk

    th = SKINS[getattr(app, "skin", "light")]
    m = tk.Toplevel(app.root)
    m.attributes("-topmost", True)
    m.overrideredirect(True)
    m.resizable(False, False)
    m.configure(bg=th["row_line"])     # 1px 描边:深色场景里分得清窗界
    body = tk.Frame(m, bg=th["row"], cursor="fleur")
    body.pack(fill="both", expand=True, padx=1, pady=1)
    app.mini_var = tk.StringVar(value="—")
    app.mini_label = tk.Label(body, textvariable=app.mini_var,
                              font=("Microsoft YaHei", MINI_FONT, "bold"),
                              fg=th["answer"], bg=th["row"],
                              padx=14, pady=7, cursor="fleur")
    app.mini_label.pack(side="left")
    TextButton(body, "还原", app.on_restore,
               palette=_btn_pal(th["primary"], 10),
               width=64, height=30, radius=th["btn_r"],
               bg=th["row"]).pack(side="left", padx=(0, 10), pady=8)
    # 抓着窗面拖动(body 空白 / 大字都算;按钮自己吃掉点击,不受影响)
    for w in (body, app.mini_label):
        w.bind("<Button-1>", app.on_mini_drag_start)
        w.bind("<B1-Motion>", app.on_mini_drag_move)
    app.mini = m


def _draw_capsule(cv, i, name, th):
    """重画第 i 个角色名框(只画已定位的):全名,无按钮。

    多边形内缩 1px:贴边画的描边下半像素会被画布裁掉(下边框消失);
    文字按 bbox 实测垂直居中 —— 中文字形在行框里偏上,固定偏移猜不准。
    """
    w, h = int(cv["width"]), int(cv["height"])
    cv.delete("all")
    cv.create_polygon(round_pts(1, 1, w - 1, h - 1, th["row_r"]), smooth=True,
                      fill=th["row"], outline=th["row_line"], width=1)

    def _vcenter(item):
        bb = cv.bbox(item)
        if bb:
            cv.move(item, 0, h / 2 - (bb[1] + bb[3]) / 2)

    label = (name or f"客户端{i + 1}")[:6]
    _vcenter(cv.create_text(12, h // 2, text=label, font=(YH, 10),
                            fill=th["name"], tags="name", anchor="w"))


def draw_side_ph(cv, th):
    """左栏空态占位:没定位到客户端时,画文档图形 + 提示文字。"""
    cv.delete("all")
    w = int(cv["width"])
    cv.create_polygon(round_pts(1, 1, w - 1, int(cv["height"]) - 1, 10),
                      smooth=True, fill=th["ph_doc"], outline=th["ph_line"],
                      width=1)
    cx = w / 2
    cv.create_polygon(round_pts(cx - 14, 18, cx + 14, 54, 5), smooth=True,
                      fill=th["card"], outline=th["ph_line"], width=1)
    for k, ln in enumerate((28, 35, 42)):
        cv.create_line(cx - 7, ln, cx + (7 if k == 2 else 9), ln,
                       fill=th["ph_line"], width=2, capstyle="round")
    cv.create_text(cx, 72, text="未找到客户端", font=(YH, 10, "bold"),
                   fill=th["ph_text"])
    cv.create_text(cx, 90, text="启动游戏后自动接入", font=(YH, 8),
                   fill=th["ph_sub"], tags="ph")


def refresh_capsules(app, regions, names):
    """App 调:定位到几个客户端就画几个名字框(最多 MAX_CLIENTS)。

    一个都没有时名字框全藏,显示左栏占位图。区域列表在跟随/自动定位
    后始终是"非空前缀",所以数量 = 非空个数。只画不改状态。
    """
    th = SKINS[getattr(app, "skin", "dark")]
    regions = list(regions or []) + [None] * core.MAX_CLIENTS
    n = sum(1 for r in regions if r)
    ph = getattr(app, "side_ph", None)
    if n == 0:
        for cv in getattr(app, "client_capsules", []):
            cv.delete("all")
            cv.grid_remove()
        if ph:
            draw_side_ph(ph, th)
            ph.place(x=12, y=18)
        return
    if ph:
        ph.place_forget()
    for i, cv in enumerate(getattr(app, "client_capsules", [])):
        if i >= n:
            cv.delete("all")
            cv.grid_remove()
            continue
        cv.grid()
        name = names[i] if i < len(names or []) else None
        _draw_capsule(cv, i, name, th)
        # 悬停角色名框 = 浮出该角色的游戏画面分辨率(行为在 app.on_client_hover_*)
        cv.bind("<Enter>", lambda _e, i=i: app.on_client_hover_start(i))
        cv.bind("<Leave>", lambda _e, i=i: app.on_client_hover_end(i))
