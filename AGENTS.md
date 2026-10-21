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

# 回归测试(自 keju-answerer 移植;降妖伏魔两组已随功能裁掉)
python tests.py            # 全量(含界面/引擎慢用例,约 1~2 分钟)
python tests.py --fast     # 快速模式:只跑代表用例,改完代码自检用
python tests.py --only gui # 只跑某几组(名字带不带 sec_ 都行)
```

**注意**:Bash 工具走 Git Bash,Windows 原生命令的参数要写双斜杠(`taskkill //F //IM`)。

## 目录结构与职责

```
入口  keju.py        ~100 行  argparse + 命令行分支
命令行 cli.py        ~650 行  cmd_*(自检/统计/补题/收益/音频预合成;自检夹具也在这)
界面  gui.py          ~660 行  App:单窗口生命周期 + 按钮回调 + 引擎看门狗
布局  ui/keju.py      ~280 行  主窗/迷你窗纯布局 + 收益明细窗;ui/widgets.py 主题/摆位/动效
逻辑  tools/keju.py  ~1200 行  KejuTool:自己的线程/状态/循环(识别、收益统计、事件落盘)
数据  bank.py         ~430 行  题库子系统(QuestionBank + 官方源更新)
地基  core.py        ~1030 行  配置 / OCR / 截图 / 语音 / 窗口枚举 / Engine 调度
测试  tests.py       ~3370 行  回归测试(patch/Sandbox 全离线;9 组 + 2 组分支补漏)
```

- `gui.App` 持有 `self.engine`(`core.Engine`),`Engine` 只挂 `keju_tool` 一条工具线程
- 启动即 `App.__init__`:建窗 → 弹出 → 起 Speaker/Engine → **无条件自动定位客户端**
- 定位只有自动一条路(手动框选已删,2026-10-21):整窗口填槽,最多 `core.MAX_CLIENTS`(5)
  个客户端,多出的舍弃;客户端挪窝由 `KejuTool._follow_clients` 自动跟随
- 引擎的启停只看 `keju_tool.watching`(自动监听开关);没有 active_tool 那套功能切换
- 配置分区:`config.json` 顶层只有 `app` / `keju` 两段(源项目还有 bell 段,已删)
- 识别事件:match/nomatch/blind/dup/low/mode/boot 逐条落 `data/events.jsonl`
  (`tools_keju.record_event`),`cli.cmd_stats` 首选读它,没有才回退解析 keju.log
  —— 日志文案怎么改都断不了统计;新增事件 kind 要同步 cmd_stats。
  启动时 `core.prune_events()` 清掉非当天的行:统计只看当天,文件不会无限涨

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
- tests.py 是回归门禁:全离线(数据/配置/日志全指临时目录);新增用例别碰真机 —— 机器上可能正开着游戏,跑 cycle 的用例要么用 `mk_engine`(已把 `_auto_boxes` 置空),要么把 `find_client_windows` 桩掉,不然 `_follow_clients` 会把假区域"跟随"到真客户端上

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
