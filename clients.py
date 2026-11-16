"""梦幻西游客户端窗口:本应用(科举答题器)特有的窗口口径。

通用探测(窗口/游戏画布枚举、游戏分辨率折算、档位表、角色名抠取)都在
client_common.py —— 那边是单文件可拷给别的应用的;本文件只留本应用特有
的口径:认哪几档(client_supported)、支持档文案(supported_res_text)、
跟随闸(can_follow_clients)。这里 re-export client_common 的全部公开名,
core.XXX 的既有引用与测试补丁语义不变。顶层不 import core。
"""

from client_common import (CANVAS_WINDOW_CLASS,  # noqa: F401
                           CLIENT_MIN_H, CLIENT_MIN_W,
                           CLIENT_WINDOW_CLASS, CLIENT_WINDOW_TITLE,
                           DLG_RATIOS, EXAM_RATIOS, GAME_RES_STEPS, MIN_GAME_RES,
                           SUPPORTED_RES, TRK_BOXES, box_of, canvas_game_res,
                           client_canvas, client_name, client_size_info,
                           dialog_rect, draw_box, exam_rect, find_client_canvases,
                           find_client_windows, ratio_of, trk_rect)


# 支持三档(2026-11-09 用户定的:1024×768 重新加入;2026-11-05 曾裁到两档)。
# HD 等其它尺寸不定位 —— 跳过并弹框说明。判定只认画布的精确值,没画布
# 不收,没有兜底。档位表本体(SUPPORTED_RES = set(GAME_RES_STEPS))与
# client_size_info 的跳过文案在 client_common,两边共用同一份口径。


def supported_res_text():
    """「640×480 / 800×600 / 1024×768」这样的支持档文案,日志与弹框共用。"""
    return " / ".join(f"{w}×{h}" for w, h in sorted(SUPPORTED_RES))


def client_supported(box, title=None):
    """这个窗口认不认:找得到游戏画布(已登录角色)且其**游戏设置分辨率**
    属于支持的三档(640×480 / 800×600 / 1024×768)。

    精确匹配没有兜底:分辨率来自 WSGAME 画布物理尺寸 ÷ 所在显示器 DPI
    (见 canvas_game_res),窗口拉伸/缩放都不参与;没画布(登录窗/没进
    角色)一律不认。title 透传 client_canvas —— 按标题配对本尊画布,
    多开窗口重叠时不借邻居的画布(否则 HD 档会被误判成支持档,
    2026-11-13 用户实测踩过)。
    """
    found = client_canvas(box, title)
    return bool(found) and tuple(found[1]) in SUPPORTED_RES


def can_follow_clients(boxes, auto_boxes):
    """区域能不能跟着客户端走:两次看到的客户端数量一致才跟。

    客户端最小化/关掉会让数量变化,那时宁可不跟,也别把别的槽位弄丢。
    """
    return bool(boxes) and bool(auto_boxes) and len(boxes) == len(auto_boxes)
