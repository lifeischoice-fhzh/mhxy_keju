# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

datas = [('data/keju_questions.json', 'data'),
         ('data/keju_metadata.json', 'data')]
# 语音缓存是运行时生成的(答过的题才有),目录可整删;缺失时跳过不阻断打包
if os.path.isdir('voice_cache'):
    datas.append(('voice_cache', 'voice_cache'))
binaries = []
hiddenimports = ['PIL', 'rapidfuzz', 'mss']
tmp_ret = collect_all('rapidocr_onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('edge_tts')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# 图标由 package.py 打包前生成(installer/app.ico,运行时画的状元帽);
# 单独手动跑 spec 没有它就退回默认图标,不阻断
exe_icon = 'installer/app.ico' if os.path.isfile('installer/app.ico') else None


a = Analysis(
    ['keju.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'matplotlib', 'scipy', 'pandas'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='mhxy_keju',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=exe_icon,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='mhxy_keju',
)
