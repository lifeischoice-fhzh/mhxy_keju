"""梦幻西游客户端窗口通用探测(单文件可拷走复用)。

枚举客户端窗口/游戏画面(WSGAME 画布)、折算游戏设置分辨率、档位表、
抠角色名 —— 只认游戏不掺应用口径;keju 这边的特有口径(认哪几档、
支持档文案、跟随闸)在 clients.py。依赖:ctypes(Win32 枚举/DPI,函数内
惰性 import);窗口类名与档位表等常量都在本文件,画布探测纯枚举窗口
不截图。画布口径与隔壁降妖伏魔同源(canvas_game_res 两边逐字一致),
改口径两边要一起改。
"""

CLIENT_WINDOW_CLASS = "MHXYMainFrame"
CLIENT_WINDOW_TITLE = "梦幻西游 ONLINE"
CLIENT_MIN_W, CLIENT_MIN_H = 400, 300     # 比这还小就不可能是客户端画面

# 游戏画面子窗口的类名(照抄隔壁降妖伏魔,那边 2026-11-07 实测)。多标签版
# ("多开合一")是一个顶层窗口(MHXYMainFrame)挂 N 个角色标签,每个角色一层
# WSGAME 子窗口,全部叠在同一矩形,只有当前角色的那层 IsWindowVisible,
# WSGAME 子窗口的矩形就是**纯游戏画面**(顶层标题条/角色标签条都不在里面)。
# 独立窗口版(2026-11-09 实测)顶层窗口**本身就是 WSGAME 类**,客户区=纯
# 游戏画面(外框多一圈系统框)—— 两种版式都认这个类名。
CANVAS_WINDOW_CLASS = "WSGAME"


def canvas_game_res(size, dpi):
    """WSGAME 画布尺寸 + 所在显示器有效 DPI -> 游戏设置分辨率(96-DPI 逻辑)。

    游戏是 DPI-unaware 的:它眼里的"800×600",在 125% 缩放的屏幕上物理是
    1000×750。dpi 是 GetDpiForMonitor 的返回值——**感知进程拿到真值(120=
    125%),不感知进程被虚拟化成 96**;两种情况这条公式都成立:
      感知进程:  物理 = 逻辑 × dpi/96        -> 逻辑 = 物理 × 96/dpi
      不感知进程: 虚拟 = 逻辑(DWM 已经除过)  -> 逻辑 = 虚拟原值
    所以 dpi <= 96 原值返回,dpi > 96 折回。纯函数,好离线测;与降妖伏魔
    那边的同名函数逐字一致,两边口径不变。
    """
    w, h = int(size[0]), int(size[1])
    if dpi and dpi > 96:
        return (round(w * 96 / dpi), round(h * 96 / dpi))
    return (w, h)


def find_client_canvases():
    """枚举客户端的**游戏画面**(WSGAME 窗口),并判出游戏设置分辨率。

    返回 ([(标题, 游戏分辨率(w,h), 画布矩形[x1,y1,x2,y2], 倍率, 版式), ...],
    [已最小化的窗口标题, ...])。版式 "tab" = 多标签版、"win" = 独立窗口版
    (只用于日志和存档文件名:对话框比例表/任务栏框表两版式共用一套,不按
    版式分档)。画布矩形是
    **本进程坐标系**:DPI 不感知
    进程拿到的是逻辑值(截图同坐标系,直接裁);DPI 感知进程拿到的是物理值
    (截图物理,裁前乘 倍率 = 画布宽/游戏宽)。游戏分辨率 = 画布尺寸 ÷ 所在
    显示器倍率,精确等于游戏设置的那一档(640×480 / 800×600 / 1024×768),
    **不需要容差去猜** —— 窗口怎么缩放都影响不到它。多标签版(多开合一,
    一个窗口
    N 个角色标签)只报当前可见的那层;独立窗口版(2026-11-09 起支持)顶层
    自身就是 WSGAME 类,画布 = 客户区,几个窗口报几个。标题都是顶层窗口
    的标题。照抄自降妖伏魔(2026-11-08 口径):本方法自包含,只依赖
    CLIENT_WINDOW_CLASS/CLIENT_WINDOW_TITLE/CANVAS_WINDOW_CLASS 三个常量。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    shcore = getattr(ctypes.windll, "shcore", None)
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def text_of(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value

    def cls_of(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def canvas_of(hwnd):
        """这扇顶层窗口的游戏画布矩形 + 版式(本进程坐标系);没有返回 None。

        独立窗口版(顶层自身就是 WSGAME 类):客户区就是画布 —— 顶层带系统框,
        必须量客户区,不能用 GetWindowRect;版式 "win"。
        多标签版(顶层 MHXYMainFrame):画布 = 里面当前可见角色的那层 WSGAME
        子窗口(隐藏的层是别的角色标签);版式 "tab"。
        """
        if cls_of(hwnd) == CANVAS_WINDOW_CLASS:
            r = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(r)):
                return None
            pt = wintypes.POINT(0, 0)
            if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
                return None
            return [pt.x, pt.y, pt.x + r.right, pt.y + r.bottom], "win"
        prev = None
        while True:
            hk = user32.FindWindowExW(hwnd, prev, None, None)
            if not hk:
                return None
            prev = hk
            if cls_of(hk) != CANVAS_WINDOW_CLASS:
                continue
            if not user32.IsWindowVisible(hk):
                continue                       # 隐藏的层是别的角色标签
            r = wintypes.RECT()
            if not user32.GetWindowRect(hk, ctypes.byref(r)):
                return None
            return [r.left, r.top, r.right, r.bottom], "tab"

    found, minimized = [], []

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = text_of(hwnd)
        cls = cls_of(hwnd)
        if (cls != CLIENT_WINDOW_CLASS and cls != CANVAS_WINDOW_CLASS
                and title != CLIENT_WINDOW_TITLE):
            return True
        if user32.IsIconic(hwnd):
            minimized.append(title or "梦幻西游")      # 最小化:截不到画面
            return True
        # 窗口可能在枚举列出它之后、查询到这里之前销毁(关窗竞态):
        # canvas_of 返回 None 就跳过。回调里抛异常会被 ctypes 吞掉,但
        # EnumWindows 收到 FALSE 就提前收工 —— 当拍在场窗口全会漏掉。
        got = canvas_of(hwnd)
        if not got:
            return True
        canvas, mode = got
        if (canvas[2] - canvas[0] < CLIENT_MIN_W
                or canvas[3] - canvas[1] < CLIENT_MIN_H
                or canvas[0] < -10000):               # 最小化窗口的坐标是 -32000
            return True
        # 登录窗/没进角色不认(用户定的「登录角色才识别」):标题抠不出角色名
        # 就是没进角色。独立窗口版的登录窗也有 WSGAME 画布(2026-11-09 实测,
        # 标题「梦幻西游 ONLINE」),光看画布分不出登录/进游戏,得看标题。
        if not client_name(title):
            return True
        # 所在显示器的有效 DPI。**GetDpiForMonitor 在不感知进程里会被虚拟化成
        # 96**(那边探针实测:同一块 125% 的屏,感知模式 120、不感知模式 96)
        # —— 但虚拟化是"连坐标带 DPI 一起除",所以两种进程里 canvas_game_res
        # 这条公式都成立,不用关心自己是什么感知模式。GetDpiForWindow 不行:
        # 游戏 DPI-unaware,它对自己窗口恒报 96,是假话。
        dpi = 96
        # MonitorFromPoint 的 POINT 是传值参数,不声明 argtypes 会访问越界(踩过)
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        pt = wintypes.POINT((canvas[0] + canvas[2]) // 2,
                            (canvas[1] + canvas[3]) // 2)
        mon = user32.MonitorFromPoint(pt, 2)      # MONITOR_DEFAULTTONEAREST
        if mon and shcore is not None:
            shcore.GetDpiForMonitor.argtypes = [
                ctypes.c_void_p, ctypes.c_int,
                ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
            shcore.GetDpiForMonitor.restype = ctypes.c_long
            dx, dy = wintypes.UINT(), wintypes.UINT()
            if shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx),
                                       ctypes.byref(dy)) == 0:
                dpi = dx.value or 96
        res = canvas_game_res((canvas[2] - canvas[0], canvas[3] - canvas[1]),
                              dpi)
        ratio = (canvas[2] - canvas[0]) / max(1, res[0])
        found.append((title or "梦幻西游", res, canvas, ratio, mode))
        return True

    try:
        user32.EnumWindows(proto(visit), 0)
    except Exception:
        return [], []
    found.sort(key=lambda t: (t[2][1], t[2][0]))       # 先上后左
    return found, minimized


def client_canvas(box, title=None):
    """客户区矩形 -> 里面那层游戏画面的 [画布矩形, 游戏分辨率, 版式];没有 None。

    画布按"矩形落在窗口里"配对(box 是客户区、画布是它里面的子窗口,同一
    进程枚举,坐标系一致)。**传 title 时只认标题相同的那块画布**:多开
    窗口互相压住时,甲的窗口框里可能躺着乙的画布,纯几何配会配错台
    (HD 档被邻居的 640×480 画布误判成支持),按标题配才是本尊;不传
    title 维持纯几何配对(降妖伏魔同款取舍,那边调用不受影响)。游戏
    分辨率由 find_client_canvases 按画布尺寸 ÷ 倍率算出,精确等于游戏
    设置的那一档(见 canvas_game_res);版式 "tab"/"win" 随画布一起给,
    只用于日志/存档文件名(比例表/框表不按版式分)。
    """
    try:
        canvases, _minimized = find_client_canvases()
    except Exception:
        return None
    x0, y0, x1, y1 = [int(v) for v in box]
    for wtitle, res, rect, _ratio, mode in canvases:
        if title is not None and wtitle != title:
            continue
        cx0, cy0, cx1, cy1 = (int(v) for v in rect)
        if cx0 >= x0 and cy0 >= y0 and cx1 <= x1 and cy1 <= y1:
            return [[cx0, cy0, cx1, cy1], (int(res[0]), int(res[1])), mode]
    return None


# ---- 通用:画布 × 占画比 ↔ 像素框(比例表见下面 DLG_RATIOS / TRK_BOXES) ----
# 比例表是**游戏属性**,真机量一次就固定;这三个纯工具别的应用可以直接拿去用
# —— 运行时 `box_of`(画布 × 比例)、量新档 `ratio_of`(实测框 ÷ 画布)、
# 复核/存档 `draw_box`(在截图上画框)。不认识"对话框/任务栏",只认几何。

def box_of(canvas, ratio):
    """画布矩形 + 占画比(left, top, right, bottom) -> 绝对框;比例 None 返回 None。"""
    if ratio is None:
        return None
    x0, y0 = int(canvas[0]), int(canvas[1])
    cw, ch = int(canvas[2]) - x0, int(canvas[3]) - y0
    rl, rt, rr, rb = ratio
    return [x0 + int(cw * rl), y0 + int(ch * rt),
            x0 + int(cw * rr), y0 + int(ch * rb)]


def ratio_of(canvas, box):
    """绝对框 ÷ 画布尺寸 -> 占画比(三位小数);画布尺寸为 0 返回 None。

    量新档用:真机框(或实测框)的像素四边 ÷ 画布宽高,结果直接填
    DLG_RATIOS / TRK_BOXES —— 与 Windows 缩放无关,换显示器照样成立。
    """
    x0, y0 = int(canvas[0]), int(canvas[1])
    cw, ch = int(canvas[2]) - x0, int(canvas[3]) - y0
    if cw <= 0 or ch <= 0:
        return None
    return tuple(round((int(box[i]) - (x0 if i % 2 == 0 else y0))
                       / (cw if i % 2 == 0 else ch), 3) for i in range(4))


def draw_box(img, canvas, ratio, color, width=2):
    """在截图(numpy RGB 数组)上画一圈"画布 × 比例"框;原地改并返回 img。

    纯切片、不依赖 PIL:各应用把自家那张截图(同坐标系的 RGB ndarray)传进来
    即可(量框复核、存标注图都用它)。color = (r, g, b),0~255。
    """
    box = box_of(canvas, ratio)
    if box is None:
        return img
    ix0, iy0 = int(canvas[0]), int(canvas[1])
    ix1, iy1 = int(canvas[2]), int(canvas[3])
    bx0, by0, bx1, by1 = box
    bx0, bx1 = max(ix0, min(bx0, ix1)), max(ix0, min(bx1, ix1))
    by0, by1 = max(iy0, min(by0, iy1)), max(iy0, min(by1, iy1))
    if bx1 - bx0 <= 0 or by1 - by0 <= 0:
        return img
    w = max(1, int(width))
    img[by0:by0 + w, bx0:bx1] = color
    img[by1 - w:by1, bx0:bx1] = color
    img[by0:by1, bx0:bx0 + w] = color
    img[by0:by1, bx1 - w:bx1] = color
    return img


# ---- 对话框框:游戏画面(WSGAME 画布)× 定稿比例(降妖伏魔在用) ----
# 这是**游戏属性**(对话框贴在画布哪里由游戏决定),不是应用口径 —— 真机
# 量一次放这里共享,别的应用用不上可以不调,但不必每个应用自己重画重框。
# **两种窗口版式(多标签 / 独立窗口)共用这一套**(2026-11-12 用户定的:
# 「多标签和独立的共用一套,不用分开」—— 画布都是纯游戏画面,对话框贴哪
# 不随版式变),所以表只按**游戏设置分辨率**分档;没量的档 `dialog_rect`
# 返回 None。
# 640/800(2026-11-08 用户定稿):两档 × 100/125/150% Windows 缩放共 6 档
# 组合真机画框验证,全部贴住对话框。按**游戏设置分辨率**分档:对话框是固定
# 像素大小(640×480 上 547×182、800×600 上 547×181,水平居中),不随画布
# 等比放大 —— 一根比例走天下必有一档错(640 的比例拿到 800 档左右各多包
# ~70px,反向则切字),所以一档一套;档内与缩放无关(画布物理尺寸随倍率
# 等比,比例不变)。left/right/bottom 是真框锚定值,top 48% 略高于引导框
# 实测顶 46.5~47.3%(切的是边框不是字)。
# **1024×768 档**(2026-11-12 用户定的:17 / 48 / 84 / 76):真机按面板底色
# 投影量得 0.172 / 0.475 / 0.842 / 0.759(1024×768、Windows 缩放 100%),用户
# 取整定稿;这档不跟 640/800 的固定像素口径比,每档记住自己的占画比即可。
# 改 Windows 缩放后按定稿比例重画框复核(存档在 docs/layout/),没量的档
# `dialog_rect` 返回 None(以后加新档,先量再填)。
DLG_RATIOS = {
    (640, 480): (0.08, 0.48, 0.936, 0.846),
    (800, 600): (0.16, 0.48, 0.85, 0.78),
    (1024, 768): (0.17, 0.48, 0.84, 0.76),
}


def dialog_rect(canvas, res):
    """游戏画布矩形 + 游戏设置分辨率 -> 对话框框(绝对坐标);没量返回 None。

    比例表 `DLG_RATIOS`(上面,真机画框定稿,两版式共用)。纯几何,不知道
    "识别"要用它做什么 —— 够不够暗、读不读、日志说什么,都是各应用自己的事。
    """
    return box_of(canvas, DLG_RATIOS.get((int(res[0]), int(res[1]))))


# ---- 任务栏框(「任务追踪」那块):游戏画面(WSGAME 画布)× 定稿框(降妖伏魔在用) ----
# 与对话框框同一条道理:任务面板画在游戏画面里的固定位置,是**游戏属性**,
# 真机框一次放这里两边共用,别的应用用不上可以不调。**两种版式共用**(同
# 对话框框),只按**游戏设置分辨率**分档;值 = **占画布的比例**(与
# `DLG_RATIOS` 同构、同样与 Windows 缩放无关;2026-11-12 用户定的口径:手框
# 四边 ÷ 画布尺寸,别再存像素 —— 像素只在量的那一档缩放上成立)。
# **2026-11-12 起旧表整体退役**:老 bell_layouts.json 是相对 GetWindowRect
# 窗口左上角的像素,客户区/画布口径下对不上号,已随表整体退役(git 历史可
# 找回)。框好一档填一行;没框的档 `trk_rect` 返回 None,任务栏那支不读
# (没有兜底,同对话框框)。
TRK_BOXES = {           # {(宽,高): (left, top, right, bottom)} 占画比
    (640, 480): (0.60, 0.10, 1.0, 0.66),   # 2026-11-12 用户定的
    (800, 600): (0.70, 0.08, 1.0, 0.53),   # 2026-11-12 用户定的
    (1024, 768): (0.76, 0.06, 1.0, 0.42),  # 2026-11-12 用户定的
}


def trk_rect(canvas, res):
    """游戏画布矩形 + 游戏设置分辨率 -> 任务栏框(绝对坐标);没框返回 None。

    框表 `TRK_BOXES`(上面,占画比,两版式共用)。纯几何,同 `dialog_rect`:
    什么时候读、读到之后信不信,都是各应用自己的事。
    """
    return box_of(canvas, TRK_BOXES.get((int(res[0]), int(res[1]))))


# 游戏分辨率档(用户口径):640×480 / 800×600 / 1024×768 受支持(2026-11-09
# 用户定的:1024×768 重新加入),HD 等更大档不支持。
# 档位判定 2026-11-08 起只按**游戏画布(WSGAME)**精确算(见 canvas_game_res,
# 照抄隔壁降妖伏魔):画布物理尺寸 ÷ 所在显示器 DPI = 游戏设置那一档,窗口
# 拉伸/Windows 缩放/多屏都不参与。登录角色才有画布 —— 找不到画布的窗口
# (登录窗/没进角色)一律不认,**精确匹配没有兜底**(用户定的,老面积归档已删)
GAME_RES_STEPS = ((640, 480), (800, 600), (1024, 768))
MIN_GAME_RES = GAME_RES_STEPS[0]          # 最小档:整窗口截图最快,实测最划算


def client_size_info(box, title=None):
    """客户端窗口 -> (尺寸说明文字, 是否已经是最小分辨率)。

    文案按调用场景写全(定位日志只拼头,跳过原因全靠这段):配到画布且
    支持 -> 游戏设置分辨率的精确值(画布口径,见 client_canvas);配到
    画布但不支持 -> 不支持+已跳过;没画布按标题分两说 —— 抠得出角色名
    是"已登录但画布还没出来"(登录/加载过渡),抠不出才是真没登录。
    title 透传 client_canvas(按标题配对,防多开窗口重叠时配错台)。
    """
    found = client_canvas(box, title)
    if found:
        w, h = found[1]
        if (w, h) in SUPPORTED_RES:
            return f"{w}×{h}", (w, h) == tuple(MIN_GAME_RES)
        return f"游戏分辨率 {w}×{h} 不支持,已跳过", False
    w = max(1, int(box[2]) - int(box[0]))
    h = max(1, int(box[3]) - int(box[1]))
    if title and client_name(title):
        return f"角色已登录,还没读到游戏画面(客户区 {w}×{h}),已跳过", False
    return f"未登录角色,没有游戏画面(客户区 {w}×{h}),已跳过", False


SUPPORTED_RES = set(GAME_RES_STEPS)    # (640,480) / (800,600) / (1024,768)


def client_name(title):
    """从客户端窗口标题抠角色名:「… - (区服[服] - 名字[id])」取最后一段。

    「梦幻西游 ONLINE - (湖北2区[黄鹤楼] - 祭花′[29024311])」->「祭花′」:
    取最后一个 - 之后,截到第一个 [,再去掉两端的括号和空格。
    标题里没有 -(还没进角色的登录窗口就只有「梦幻西游 ONLINE」)或抠出来
    是空的,返回空串 —— 界面那格退回显示「客户端N」。
    """
    t = str(title or "")
    if "-" not in t:
        return ""
    seg = t.rsplit("-", 1)[1].split("[", 1)[0].strip()
    while seg and seg[0] in "()()「」":
        seg = seg[1:]
    while seg and seg[-1] in "()()「」":
        seg = seg[:-1]
    return seg


def find_client_windows():
    """枚举梦幻西游客户端窗口(返回**客户区**矩形,整窗填槽的区域用)。

    返回 ([(标题, [x1,y1,x2,y2]), ...], [已最小化的窗口标题, ...])。
    两种版式都认:多标签版(MHXYMainFrame 顶层)和独立窗口版(顶层自身
    就是 WSGAME 类)。量的是**客户区**(GetClientRect + ClientToScreen);
    用 GetWindowRect 会把系统标题栏/边框也算进去(新版客户端带系统框,
    量出 812×663 这种数)。档位判定不走窗口尺寸 —— 按 WSGAME 画布精确算
    (见 client_canvas),这里量出的矩形只当定位区域。结果按屏幕位置
    (先上后左)排序,多开时区域编号稳定。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found, minimized = [], []

    def text_of(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value

    def cls_of(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = text_of(hwnd)
        cls = cls_of(hwnd)
        if (cls != CLIENT_WINDOW_CLASS and cls != CANVAS_WINDOW_CLASS
                and title != CLIENT_WINDOW_TITLE):
            return True
        if user32.IsIconic(hwnd):
            minimized.append(title or "梦幻西游")      # 最小化:截不到画面
            return True
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return True
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return True
        box = [pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom]
        if (box[2] - box[0] < CLIENT_MIN_W or box[3] - box[1] < CLIENT_MIN_H
                or box[0] < -10000):                  # 最小化窗口的坐标是 -32000
            return True
        found.append((title or "梦幻西游", box))
        return True

    try:
        user32.EnumWindows(proto(visit), 0)
    except Exception:
        return [], []
    found.sort(key=lambda t: (t[1][1], t[1][0]))       # 先上后左
    return found, minimized
