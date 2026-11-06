#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""梦幻西游 · mhxy_keju(入口)

逻辑拆分:
- core.py 配置 / OCR / 语音 / 识别引擎调度
- cli.py  命令行(自检 / 统计 / 补题 / 音频预合成)
- bank.py 题库子系统(读写/更新/匹配)
- gui.py  单窗口应用的生命周期与按钮回调
- ui/     窗口布局(keju / widgets)
- tools/  科举自己的线程与匹配(keju)
本文件只保留命令行入口:core / bank 里的名字一律调用时现取。
"""
import argparse
import os
import sys

import bank                 # 题库子系统:更新题库与题库文件路径
import cli                  # 命令行(cmd_*):自检/统计/补题/音频预合成
import core                 # 地基:配置/依赖检查与数据文件(dpi_aware/ensure_data_files)
from gui import App                 # 窗口入口


def _ensure_console():
    """打包为窗口程序后没有控制台;命令行模式临时分配一个,便于看输出。"""
    if sys.stdout is None:
        try:
            import ctypes
            ctypes.windll.kernel32.AllocConsole()
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)   # UTF-8,避免中文乱码
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
        except Exception:
            pass


def _single_instance() -> bool:
    """GUI 只许开一个:已有实例在跑时返回 False(弹框退路在 main)。

    命名互斥体由系统在进程退出时自动释放,这里刻意不 CloseHandle ——
    句柄得活满整个进程生命周期,提前关等于没锁。WinDLL(use_last_error
    =True)拿到的才是 CreateMutexW 自己的错误码(183=ERROR_ALREADY_EXISTS)。
    """
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, "Global\\MHXY_KEJU_MUTEX")
    return ctypes.get_last_error() != 183    # 183 = ERROR_ALREADY_EXISTS


def main():
    ap = argparse.ArgumentParser(description="梦幻西游 mhxy_keju")
    ap.add_argument("--selftest", action="store_true", help="OCR+匹配自检(合成图片)")
    ap.add_argument("--seed", type=int, default=cli.SELFTEST_SEED, metavar="N",
                    help=f"自检抽题种子(默认 {cli.SELFTEST_SEED},可复现;0=每次随机)")
    ap.add_argument("--tts-test", action="store_true", help="语音播报测试")
    ap.add_argument("--build-voice-cache", type=int, nargs="?", const=0, default=None,
                    metavar="N", help="预合成答案音频到本地缓存(不带N=全部,如 200=前200条)")
    ap.add_argument("--stats", action="store_true", help="统计每题识别/播报耗时")
    ap.add_argument("--uncollected", nargs="*", default=None, metavar="序号=答案",
                    help="列出疑似题库未收录的读法;如 --uncollected 1=常建 直接补进题库")
    ap.add_argument("--since", default="", metavar="HH:MM",
                    help="配合 --stats:只看该时刻之后的记录(按当日小时:分钟)")
    ap.add_argument("--rewards", action="store_true",
                    help="看今天的答题收益(银两/经验/物品)")
    ap.add_argument("--update-bank", action="store_true", help="检查并更新题库")
    args = ap.parse_args()

    cli_mode = (args.selftest or args.tts_test or args.stats or args.update_bank
                or args.uncollected is not None or args.rewards
                or args.build_voice_cache is not None)
    if cli_mode:
        _ensure_console()          # 打包版才有意义,源码运行无副作用
    else:
        # 工具只许开一个:已有实例就弹框退场(命令行工具模式不拦,方便并用)
        import ctypes
        if not _single_instance():
            ctypes.windll.user32.MessageBoxW(
                0, "mhxy_keju 已经在运行,不能重复打开。", "mhxy_keju", 0x40)
            return 0
    core.ensure_data_files()

    missing = core.check_deps()
    if missing:
        print("缺少依赖: " + ", ".join(missing))
        print("请先安装:  pip install -r "
              f"\"{os.path.join(core.BASE_DIR, 'requirements.txt')}\"")
        return 1
    if not os.path.exists(bank.BANK_PATH):
        print(f"题库文件不存在: {bank.BANK_PATH}")
        return 1

    # 环境预检不过即停(见 core 环境预检):预检不过的版本不再提供答题能力。
    # GUI 照常开窗,题目识别区常驻「token已用完」;--selftest/--stats/
    # --rewards 只是自检与翻旧记录,不具答题能力,放行(--selftest 是打包
    # QA 的必跑项);其余命令行模式拦下直接退场。
    readonly = False
    if not (args.selftest or args.stats or args.rewards):
        blocked = core.env_ready()
        if blocked:
            if cli_mode:
                print(blocked)
                return 1
            readonly = True

    if args.selftest:
        return cli.cmd_selftest(args.seed)
    if args.tts_test:
        return cli.cmd_tts_test()
    if args.update_bank:
        try:
            _, msg = bank.update_bank()
            print(msg)
            return 0
        except Exception as e:
            print(f"更新失败: {e}")
            return 1
    if args.stats:
        return cli.cmd_stats(args.since)
    if args.rewards:
        return cli.cmd_rewards()
    if args.uncollected is not None:
        return cli.cmd_uncollected(args.uncollected)
    if args.build_voice_cache is not None:
        return cli.cmd_build_voice_cache(args.build_voice_cache)

    core.dpi_aware()
    App(readonly=readonly).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
