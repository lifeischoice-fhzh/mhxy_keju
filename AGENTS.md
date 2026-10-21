# mhxy_keju —— 科举答题器(单功能独立应用)

从 `keju-answerer` 抽出的**科举单功能版**。降妖伏魔(铃铛/地图)不在本应用;
工具箱那层窗口也没有 —— **启动直接进答题器主窗,只有一个窗口**。

## 环境

```bash
# Python 一律用项目 venv
E:/Project/mhxy_keju/.venv/Scripts/python.exe

# 启动应用
./.venv/Scripts/python.exe keju.py

# 日志
keju.log          # 格式 [HH:MM:SS][模块]内容,按天轮转 .old

# 命令行
python keju.py --selftest / --rewards / --stats / --uncollected / --update-bank
```

**注意**:Bash 工具走 Git Bash,Windows 原生命令的参数要写双斜杠(`taskkill //F //IM`)。

## 目录结构与职责

```
入口  keju.py        ~100 行  argparse + 命令行分支
界面  gui.py          ~560 行  App:单窗口生命周期 + 按钮回调 + 引擎看门狗
布局  ui/keju.py      ~290 行  主窗/迷你窗纯布局 + 收益明细窗;ui/widgets.py 主题/摆位/动效
逻辑  tools/keju.py  ~1160 行  KejuTool:自己的线程/状态/循环(识别、收益统计)
数据  bank.py         ~400 行  题库子系统(QuestionBank + 官方源更新)
地基  core.py        ~1630 行  配置 / OCR / 截图 / 语音 / 窗口枚举 / Engine 调度 + CLI
```

- `gui.App` 持有 `self.engine`(`core.Engine`),`Engine` 只挂 `keju_tool` 一条工具线程
- 启动即 `App.__init__`:建窗 → 弹出 → 起 Speaker/Engine → 无区域就自动定位
- 引擎的启停只看 `keju_tool.watching`(自动监听开关);没有 active_tool 那套功能切换
- 配置分区:`config.json` 顶层只有 `app` / `keju` 两段(源项目还有 bell 段,已删)

## 从源项目(keju-answerer)带过来的约定

- 界面刷新只认 `KejuTool.ui_state()` 只读口,别下钻摸工具字段
- 日志按功能分家:`core.log_scope()` 按消息里的 `[模块]` 前缀判归属;
  界面日志框只显示 keju + 公共的行
- 动效 `pop_in` / `fade_out` / `stop_anim`(`ui/widgets.py`):销毁窗口前先 `stop_anim`;
  `pop_in` 负责"显示"的顺序是死的(先 -alpha 0、再 deiconify、再逐帧淡入)
- DPI:Tk 的 `winfo_*` 是逻辑像素,截图(mss)和窗口矩形是物理像素
- 红线沿用:`tools/`、`bank.py` 模块级不许 `import core`(只能在方法内);
  `ui/` 只建控件不写行为(回调指向 `app.on_xxx`)
- `self._stop` 不能覆盖 `threading.Thread._stop()`:工具线程的停止事件叫 `_halt`

## 打包

```bash
package.bat                              # 或 .venv/Scripts/python.exe package.py
package.py --dry-run                     # 先看会保留哪些运行时文件
```

- spec 是 `科举答题器.spec`;datas 带题库 + 语音缓存(不放地图,本应用没有地图功能)
- 打包前脚本会 taskkill 旧 exe,并把 dist 里的 config.json / 日志 / 收益记录
  备份出来,打完放回去
- 打完必须真实跑过:至少 `dist\科举答题器\科举答题器.exe --selftest`(分配控制台
  跑真 OCR 自检)+ 启动 GUI 看一眼

## 工作方式约定(沿用源项目)

- 不要自动提交/推送;改完必须自测,红了立刻回退,不留半成品
- 大改动分小步
- 跑完删临时探针脚本/图片
