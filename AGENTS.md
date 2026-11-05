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

**单实例**:GUI 已有实例在跑时再次启动,只弹「已经在运行」框并退场
(命名互斥体 `Global\\MHXY_KEJU_ANSWERER_MUTEX`,`keju._single_instance`;
命令行工具模式不拦,可与 GUI 并用)。测试进程内调 `keju.main` 的用例
记得桩 `_single_instance`(tests 有先例)。

**注意**:Bash 工具走 Git Bash,Windows 原生命令的参数要写双斜杠(`taskkill //F //IM`)。

## 目录结构与职责

```
入口  keju.py        ~120 行  argparse + 命令行分支
命令行 cli.py        ~670 行  cmd_*(自检/统计/补题/收益/音频预合成;自检夹具也在这)
界面  gui.py          ~680 行  App:单窗口生命周期 + 按钮回调 + 引擎看门狗
布局  ui/keju.py      ~480 行  主窗(place 定尺寸)/迷你窗纯布局 + 角色名框绘制
部件  ui/widgets.py   ~260 行  摆位/动效 + 圆角钮/状态灯(Canvas 组件)
逻辑  tools/keju.py  ~1090 行  KejuTool:自己的线程/状态/循环(识别、收益统计、事件落盘)
数据  bank.py         ~430 行  题库子系统(QuestionBank + 官方源更新)
地基  core.py        ~1120 行  配置 / OCR / 截图 / 语音 / 窗口枚举 / Engine 调度
测试  tests.py       ~3430 行  回归测试(patch/Sandbox 全离线;9 组 + 2 组分支补漏)
```

- `gui.App` 持有 `self.engine`(`core.Engine`),`Engine` 只挂 `keju_tool` 一条工具线程
- 启动即 `App.__init__`:建窗 → 弹出 → 起 Speaker/Engine → **无条件自动定位客户端**
  → 每天首次启动后台更新一次题库(`last_bank_check` 记当天;成功才记账,
  失败的下次启动再试,无界面按钮)
- 关闭时 `_cleanup_litter`(gui.py):删 `__pycache__`×3、`keju.log.old`、
  `.ruff_cache`、`build/`,语音缓存超 40MB 从最旧开始删(缓存是速度优化
  不是数据,不整删);`sys.frozen` 的打包版直接跳过,不删自己所在目录;
  `dist/` 是打包成品,不进自动清理。打包脚本(package.py)成功后也会
  自动清 `build/` 中间产物
- **主窗是定稿模板**(2026-10-22):
  664×232 不可缩放,窗口标题「风二与露娜」,图标 = 运行时绘制的金色状元帽
  (参考游戏道具的配色 + 经典官帽的帽翅造型,纯代码重绘 —— 刻意不抠图
  不留素材文件,避免版权风险;`ui_keju.set_icon` 1024 画布 + LANCZOS 抗锯齿,
  iconphoto 多尺寸阶梯 + WM_SETICON 挂大/小 HICON —— **须等 `<Map>` 再挂,
  未映射时 Tk 还没定真实 HWND**;gui 启动时另设独立 AppUserModelID,
  不然从控制台拉起时任务栏跟控制台合成一颗按钮、显示 Python 终端图标;
  无素材文件),place 定位布局 —— **place 不产生
  请求尺寸,根窗口必须 `geometry()` 显式定尺寸**,子 Frame 也要 place 显式
  给宽高(否则按子元素请求高裁切,按钮会被裁没)。布局:
  - 左栏(白/深面板 170px):角色名框竖排(146×30 等宽,× 删单个,悬停
    × 变红),末尾「客户端刷新」按钮(同宽对齐)替代原定位圆钮;
    **没定位到客户端时整栏显示占位图**(「未找到客户端」 +
    「启动游戏后点下方按钮」,`ui_keju.draw_side_ph`)
  - 右上一排纯文字钮:**御前科举**(蓝底/主题色,文案=当前模式,点一下切
    「三大学士」再点切回)→ 手动识别 → 迷你置顶 → 播报试听 → 语音静音
    (静音时**整颗钮换红色系**、文案变「语音恢复」,见
    `ui_keju.mute_palette`);全应用无图标
  - 题目识别区:Canvas 白卡,只有题目一行(13pt 灰,超宽 `…` 截断)+
    大字答案(36pt);没有日志框、最近答案标签、时间/已播报角标
  - 状态灯与空态:题目卡右上角一颗小圆点(`ui/widgets.StatusDot`,
    监听中=绿点 0.5s 呼吸闪烁,未监听/未定位=红点常亮,`_drain_logs` 每拍
    按 `keju_tool.watching` 刷新);没有题目时画占位(文档图形 +
    「等待识别…」,见 `ui_keju.draw_hero`)
  - 换肤:题目卡右上「换肤」小钮一键切 玄夜(深)/ 玄白(浅),选择存
    `app.skin`(CONFIG_SECTIONS 已登记);换肤 = 清空主窗子件整窗重建
    (`ui_keju.apply_skin`,迷你窗跳过),全部配色来自 `ui.keju.SKINS`
  - 部件:圆角文字钮/状态灯是 Canvas 组件(`ui/widgets.TextButton` /
    `StatusDot`),ttk 做不了圆角三态;TextButton 有 hover/press/normal
    三态与 `set_text`/`label`/`invoke`。**别在新属性里用 `_w`/`_h`** ——
    那是 tkinter 的控件路径属性,覆盖后控件直接废掉(踩过)
- **自动监听默认开**(2026-10-22):`KejuTool.watching` 初始 True,没有界面开关;
  定位到客户端即接管(`on_autolocate` 里 `set_watch(True)`),没定位到会自动暂停并提示
- 定位只有自动一条路(手动框选已删,2026-10-21):整窗口填槽,最多 `core.MAX_CLIENTS`(5)
  个客户端,多出的舍弃;客户端挪窝由 `KejuTool._follow_clients` 自动跟随。
  **只支持 640×480 / 800×600 两档**(2026-11-05,按面积就近归档,拉伸过也算;
  860×600 的窗口自然归到 800×600 档同样放行):
  其它档定位/跟随都不收,弹框提示(`gui._warn_resolution`,
  文案统一走 `core.supported_res_text()`;测试桩 `gui.messagebox`)。
  800×600 档能定位但会弹一次「分辨率建议」建议调 640×480
  (`gui._hint_resolution`,同样桩 `gui.messagebox`)。
  界面左栏定位到几个就显示几个名字框:角色名 `core.client_name` 抠自窗口标题
  (没有 - 就是没进角色,回退显示「客户端N」),框上 × 删单个
  (`App.on_remove_client` → `KejuTool.drop_client`,跟随基准/名字/记忆一起删);
  「重新定位」走「客户端刷新」按钮
- **三大学士模式只扫对话框带**(2026-11-05):`tools_keju.PLAIN_BAND` 按整窗占比
  把截图缩到底部题面对话框(御前科举仍整窗);代价是该模式看不到对话框外的
  系统提示,收益统计在三大学士下会缺(`--stats` 分模式段照常)
- **手动识别在右上**(2026-10-22 恢复):「手动识别」→ `Engine.request_cycle()`,
  不受 watching 限制;扫**全部已定位区域**(几个扫几个),一个都没有就
  **直接不扫**,只提示(自动轮询同样只提示+暂停)
- **监听间隔配置已删**(2026-10-22):配置键 `interval` 仍生效(自适应轮询读它),
  界面上不能调
- **界面没有日志框**(2026-10-22):所有日志只进 keju.log,`App.log(msg, detail=)`
  就一个落文件口(旧的 ui/ui_text 兼容参数已删)。识别结果上屏走
  `app.last_question`/`app.last_answer`(识别线程摆值,`_drain_logs` 50ms 落屏);
  rewards 统计照跑(data/rewards.json),用 `--rewards` / `--stats` 看
- 引擎的启停只看 `keju_tool.watching`;`Engine.refresh_tools` 按 it 分派工具线程
- 配置分区:`config.json` 顶层只有 `app` / `keju` 两段(源项目还有 bell 段,已删)
- 识别事件:match/nomatch/blind/dup/low/mode/boot 逐条落 `data/events.jsonl`
  (`tools_keju.record_event`),`cli.cmd_stats` 首选读它,没有才回退解析 keju.log
  —— 日志文案怎么改都断不了统计;新增事件 kind 要同步 cmd_stats。
  启动时 `core.prune_events()` 清掉非当天的行:统计只看当天,文件不会无限涨

## 从源项目(keju-answerer)带过来的约定

- 界面刷新只认 `KejuTool.ui_state()` 只读口(角色名),别下钻摸工具字段;
  题目/答案是例外:识别线程直写 `app.last_question`/`app.last_answer`
- 动效 `pop_in` / `stop_anim`(`ui/widgets.py`):销毁窗口前先 `stop_anim`;
  `pop_in` 负责"显示"的顺序是死的(先 -alpha 0、再 deiconify、再逐帧淡入)
- DPI:Tk 的 `winfo_*` 是逻辑像素,截图(mss)和窗口矩形是物理像素;
  per-monitor DPI 感知进程里 Tk 8.6 的标题栏原生拖动是死的(探针实测),
  迷你窗因此做成**无边框**(overrideredirect),拖动在客户区自己实现
  (`gui.on_mini_drag_*`,抓着窗面哪儿都能拖,还原走「还原」按钮)。
  高分屏(如 125%)下游戏窗口物理尺寸 = 游戏设置尺寸 × 缩放,档位判定
  按枚举时记下的显示器有效 DPI 折算回逻辑值(`core.GAME_DPI/_logical`)
- 红线沿用:`tools/`、`bank.py` 模块级不许 `import core`(只能在方法内);
  `ui/` 只建控件不写行为(回调指向 `app.on_xxx`)
- `self._stop` 不能覆盖 `threading.Thread._stop()`:工具线程的停止事件叫 `_halt`
- tests.py 是回归门禁:全离线(数据/配置/日志全指临时目录);新增用例别碰真机 —— 机器上可能正开着游戏,跑 cycle 的用例要么用 `mk_engine`(已把 `_auto_boxes` 置空),要么把 `find_client_windows` 桩掉,不然 `_follow_clients` 会把假区域"跟随"到真客户端上

## 打包

```bash
package.bat                              # 或 .venv/Scripts/python.exe package.py
package.py --dry-run                     # 先看会保留哪些运行时文件
```

- spec 是 `科举答题器.spec`;datas 带题库 + 语音缓存(不放地图,本应用没有
  地图功能)。`voice_cache` 是运行时缓存(答过的题才有),**可整目录删除**,
  会随答题自动重建;缺失时 spec 自动跳过、不阻断打包
- 打包前脚本会 taskkill 旧 exe,并把 dist 里的 config.json / 日志 / 收益记录
  备份出来,打完放回去
- 打完必须真实跑过:至少 `dist\科举答题器\科举答题器.exe --selftest`(分配控制台
  跑真 OCR 自检)+ 启动 GUI 看一眼

## 工作方式约定(沿用源项目)

- 不要自动提交/推送;改完必须自测,红了立刻回退,不留半成品
- 大改动分小步
- 跑完删临时探针脚本/图片
