# -*- coding: utf-8 -*-
"""一键打包:重建 dist/mhxy_keju,再压出双击安装包,运行时数据补回去。

用法:
    package.bat                # 双击或命令行(推荐)
    .venv/Scripts/python.exe package.py --dry-run
    .venv/Scripts/python.exe package.py --installer-only   # 只重出安装包

两步打包:
    1) PyInstaller 出绿色目录版 dist/mhxy_keju(配置、日志、收益和补题记录
       先抄到临时目录,打完再放回去);
    2) Inno Setup(installer/installer.iss)把目录版压成安装包
       dist/mhxy_keju_v*.exe —— 双击装到 %LOCALAPPDATA%/Programs,
       出桌面/开始菜单快捷方式,可卸载,全程不需要管理员。
"""
import argparse
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
APP_NAME = "mhxy_keju"
SPEC = ROOT / f"{APP_NAME}.spec"
DIST = ROOT / "dist" / APP_NAME
EXE = DIST / f"{APP_NAME}.exe"

# 安装包(Inno Setup):脚本 + 打包前生成的图标 + 中文向导语言包
INSTALLER_DIR = ROOT / "installer"
ISS = INSTALLER_DIR / "installer.iss"
ICON = INSTALLER_DIR / "app.ico"
ISL = INSTALLER_DIR / "ChineseSimplified.isl"
SETUP_NAME = "mhxy_keju_v*.exe"

ISCC_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
)
# 中文语言包 Inno 默认不带,缺了按顺序试这几条通道拉一份(题库更新同款网络环境)
ISL_URLS = (
    "https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_3"
    "/Files/Languages/Unofficial/ChineseSimplified.isl",
    "https://api.github.com/repos/jrsoftware/issrc/contents"
    "/Files/Languages/Unofficial/ChineseSimplified.isl?ref=is-6_7_3",
    "https://cdn.jsdelivr.net/gh/jrsoftware/issrc@is-6_7_3"
    "/Files/Languages/Unofficial/ChineseSimplified.isl",
)

# 运行时产物(每台机器/每次使用都不一样,打包不能丢)
KEEP_FILES = (
    "config.json",
    "data/rewards.json",
    "data/uncollected.jsonl",
    "data/events.jsonl",
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


def make_icon():
    """installer/app.ico:把运行时画的状元帽落成 ico(exe/安装包共用图标)。

    画不出来不阻断绿色版 —— spec 里缺图标自动退回默认;但安装包依赖它。
    """
    try:
        from PIL import Image, ImageDraw
        from ui.keju import _draw_zhuangyuan
    except Exception as exc:
        say(f"图标生成跳过(拿不到状元帽绘制): {exc}")
        return None
    img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    _draw_zhuangyuan(ImageDraw.Draw(img))
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    return ICON


def ensure_isl():
    """中文向导语言包:Inno 默认不带,缺了从官方仓库拉一份到 installer/。"""
    if ISL.is_file():
        return True
    say("缺中文向导语言包,下载 ChineseSimplified.isl …")
    for url in ISL_URLS:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "mhxy_keju",
                "Accept": "application/vnd.github.raw"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if data and b"Chinese" in data:
                INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
                ISL.write_bytes(data)
                say(f"已就位: {ISL.relative_to(ROOT)}")
                return True
        except OSError as exc:
            say(f"  失败({url.split('/')[2]}): {exc}")
    say("中文语言包拿不到,手动下载后放到 installer/ChineseSimplified.isl:")
    say("  " + ISL_URLS[0])
    return False


def find_iscc():
    for p in ISCC_CANDIDATES:
        if p.is_file():
            return p
    w = shutil.which("iscc")
    return Path(w) if w else None


def build_installer():
    """用 Inno Setup 把 dist/mhxy_keju 压成单文件安装包。"""
    if not ISS.is_file():
        say(f"找不到安装包脚本: {ISS}")
        return 2
    iscc = find_iscc()
    if iscc is None:
        say("没找到 Inno Setup 6(ISCC.exe),出不了安装包。装一下:")
        say("  winget install --id JRSoftware.InnoSetup -e")
        say("  (只出绿色目录版可改用 --no-installer)")
        return 2
    if not ensure_isl():
        return 2
    if not ICON.is_file():
        say(f"缺少安装包图标 {ICON.relative_to(ROOT)}(图标生成失败?),"
            "先排查 make_icon")
        return 2
    cmd = [str(iscc), str(ISS)]
    say("开始打安装包…")
    say("  " + " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        say(f"ISCC 失败(exit {rc})")
        return rc
    setups = sorted((ROOT / "dist").glob(SETUP_NAME),
                    key=lambda p: p.stat().st_mtime)
    if not setups:
        say("ISCC 成功但没找到安装包产物")
        return 3
    s = setups[-1]
    say(f"安装包完成: {s}")
    say(f"  {s.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="一键打包 mhxy_keju(exe + 安装包)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印要保留的文件和打包命令,不真的打包")
    ap.add_argument("--no-clean", action="store_true",
                    help="保留 PyInstaller 缓存(更快,但依赖变动后可能打进旧东西)")
    ap.add_argument("--no-backup", action="store_true",
                    help="不保留 dist 里的运行时数据(配置/日志等)")
    ap.add_argument("--no-installer", action="store_true",
                    help="只出绿色目录版,不打安装包")
    ap.add_argument("--installer-only", action="store_true",
                    help="不重打 exe,直接用现有 dist 出安装包")
    args = ap.parse_args(argv)

    if not SPEC.is_file():
        say(f"找不到打包配置: {SPEC}")
        return 2
    if VENV_PY.is_file() and Path(sys.executable).resolve() != VENV_PY.resolve():
        say(f"请用项目 venv 运行: {VENV_PY}")
        say(f"  (或者直接双击 {ROOT / 'package.bat'})")
        return 2

    if args.installer_only:
        if not EXE.is_file():
            say(f"没有现成的 {EXE.relative_to(ROOT)},先跑一次完整打包")
            return 2
        if args.dry_run:
            say("dry-run:不执行,将直接用现有 dist 出安装包")
            return 0
        return build_installer()

    if not pyinstaller_ready():
        return 2
    icon = None
    if not args.no_installer:
        icon = make_icon()      # 必须在 PyInstaller 前生成,spec 里要用

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
    if icon is not None:
        say(f"图标: {icon.relative_to(ROOT)}")

    if args.dry_run:
        say("dry-run:不执行打包")
        say("将执行: " + " ".join(cmd))
        if not args.no_installer:
            iscc = find_iscc()
            tail = " ".join([str(iscc), str(ISS)]) if iscc \
                else "ISCC 未找到(装 Inno Setup 6,见 README)"
            say("打完将执行: " + tail)
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
            say("     请检查 mhxy_keju.spec 的 datas")
        size = EXE.stat().st_size / 1024 / 1024
        say(f"完成: {EXE}")
        say(f"  {size:.1f} MB,耗时 {time.time() - t0:.0f}s")
        shutil.rmtree(ROOT / "build", ignore_errors=True)   # 中间产物不留
        ok = True
        if args.no_installer:
            return 0
        return build_installer()
    finally:
        if tmp is not None:
            if ok:
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                say(f"未完成,运行时备份留在: {tmp}")


if __name__ == "__main__":
    sys.exit(main())
