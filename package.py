# -*- coding: utf-8 -*-
"""一键打包:重建 dist/科举答题器,并把用户运行时数据补回去。

用法:
    package.bat                # 双击或命令行(推荐)
    .venv/Scripts/python.exe package.py --dry-run

PyInstaller 会整个重建 dist/科举答题器;配置、日志、收益和补题记录都在那个
目录里。脚本打包前把它们抄到临时目录,打完再放回去。
"""
import argparse
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
APP_NAME = "科举答题器"
SPEC = ROOT / f"{APP_NAME}.spec"
DIST = ROOT / "dist" / APP_NAME
EXE = DIST / f"{APP_NAME}.exe"

# 运行时产物(每台机器/每次使用都不一样,打包不能丢)
KEEP_FILES = (
    "config.json",
    "data/rewards.json",
    "data/uncollected.jsonl",
    "data/keju_questions.backup.json",
)
KEEP_GLOBS = ("keju.log*",)

# 打包结果里必须有的资源(少一个就是 spec 漏了)
REQUIRED = (
    "data/keju_questions.json",
    "data/keju_metadata.json",
)


def say(msg):
    print(f"[打包] {msg}", flush=True)


def runtime_files():
    """dist 里需要保留的运行时文件(打包前还不存在就返回空)。"""
    out = []
    for rel in KEEP_FILES:
        p = DIST / rel
        if p.is_file():
            out.append(p)
    for pat in KEEP_GLOBS:
        out.extend(p for p in DIST.glob(pat) if p.is_file())
    seen, uniq = set(), []
    for p in out:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def backup(files, target):
    for p in files:
        dst = target / p.relative_to(DIST)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)


def restore(target):
    n = 0
    for p in target.rglob("*"):
        if not p.is_file():
            continue
        dst = DIST / p.relative_to(target)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        n += 1
    return n


def stop_running():
    """打包前关掉正在运行的打包版,不然 PyInstaller 删不掉旧目录。"""
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/IM", EXE.name],
            capture_output=True, text=True,
            encoding=locale.getpreferredencoding(False), errors="replace")
    except OSError:
        return False
    if r.returncode == 0:
        say(f"已关闭正在运行的 {EXE.name}")
        return True
    return False


def pyinstaller_ready():
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        say("缺少 PyInstaller。先装一下:")
        say(f'  "{VENV_PY}" -m pip install pyinstaller')
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="一键打包科举答题器")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印要保留的文件和打包命令,不真的打包")
    ap.add_argument("--no-clean", action="store_true",
                    help="保留 PyInstaller 缓存(更快,但依赖变动后可能打进旧东西)")
    ap.add_argument("--no-backup", action="store_true",
                    help="不保留 dist 里的运行时数据(配置/日志等)")
    args = ap.parse_args(argv)

    if not SPEC.is_file():
        say(f"找不到打包配置: {SPEC}")
        return 2
    if VENV_PY.is_file() and Path(sys.executable).resolve() != VENV_PY.resolve():
        say(f"请用项目 venv 运行: {VENV_PY}")
        say(f"  (或者直接双击 {ROOT / 'package.bat'})")
        return 2
    if not pyinstaller_ready():
        return 2

    files = [] if args.no_backup else runtime_files()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if not args.no_clean:
        cmd.append("--clean")
    cmd.append(str(SPEC))

    say(f"程序目录: {DIST}")
    if files:
        say(f"将保留 {len(files)} 个运行时文件:")
        for p in files:
            say(f"  + {p.relative_to(DIST)}")
    else:
        say("没有可保留的运行时文件(首次打包或已指定 --no-backup)")

    if args.dry_run:
        say("dry-run:不执行打包")
        say("将执行: " + " ".join(cmd))
        return 0

    stop_running()
    tmp = Path(tempfile.mkdtemp(prefix="keju_pkg_")) if files else None
    ok = False
    try:
        if tmp is not None:
            backup(files, tmp)
        t0 = time.time()
        say("开始打包…")
        say("  " + " ".join(cmd))
        rc = subprocess.call(cmd, cwd=str(ROOT))
        if rc != 0:
            say(f"PyInstaller 失败(exit {rc})")
            return rc
        if not EXE.is_file():
            say(f"打包结束但没找到 {EXE}")
            return 3
        if tmp is not None:
            n = restore(tmp)
            say(f"已恢复 {n} 个运行时文件")
        missing = [rel for rel in REQUIRED
                   if not ((DIST / rel).exists()
                           or (DIST / "_internal" / rel).exists())]
        if missing:
            say("警告:打包结果缺少这些资源:" + ", ".join(missing))
            say("     请检查 科举答题器.spec 的 datas")
        size = EXE.stat().st_size / 1024 / 1024
        say(f"完成: {EXE}")
        say(f"  {size:.1f} MB,耗时 {time.time() - t0:.0f}s")
        ok = True
        return 0
    finally:
        if tmp is not None:
            if ok:
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                say(f"未完成,运行时备份留在: {tmp}")


if __name__ == "__main__":
    sys.exit(main())
