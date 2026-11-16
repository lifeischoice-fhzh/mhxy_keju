# mhxy_keju(单功能独立应用)

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
python keju.py --selftest / --stats / --uncollected / --update-bank

# 回归测试(自 keju-answerer 移植;降妖伏魔两组已随功能裁掉)
python tests.py            # 全量(含界面/引擎慢用例,约 1~2 分钟)
python tests.py --fast     # 快速模式:只跑代表用例,改完代码自检用
python tests.py --only gui # 只跑某几组(名字带不带 sec_ 都行)
```

**单实例**:GUI 已有实例在跑时再次启动,只弹「已经在运行」框并退场
(命名互斥体 `Global\\MHXY_KEJU_MUTEX`,`keju._single_instance`;
命令行工具模式不拦,可与 GUI 并用)。测试进程内调 `keju.main` 的用例
记得桩 `_single_instance`(tests 有先例)。

**注意**:Bash 工具走 Git Bash,Windows 原生命令的参数要写双斜杠(`taskkill //F //IM`)。

## 目录结构与职责

```
入口  keju.py        ~130 行  argparse + 命令行分支
命令行 cli.py        ~640 行  cmd_*(自检/统计/补题/音频预合成;自检夹具也在这)
界面  gui.py         ~930 行  App:单窗口生命周期 + 按钮回调 + 引擎看门狗
布局  ui/keju.py     ~540 行  主窗(place 定尺寸)/迷你窗纯布局 + 角色名框绘制
部件  ui/widgets.py  ~270 行  摆位/动效 + 圆角钮/状态灯(Canvas 组件)
逻辑  tools/keju.py ~1310 行  KejuTool:自己的线程/状态/循环(识别、选项采集、事件落盘)
数据  bank.py        ~440 行  题库子系统(QuestionBank + 官方源更新)
地基  core.py        ~570 行  配置 / Engine 调度 / 事件(基础设施经它 re-export)
截屏  ocr.py         ~210 行  Screen 截屏 + Ocr 识别(单文件可复用,顶层不 import core)
窗口  client_common.py ~410 行  客户端窗口/画布枚举 + 分辨率折算 + 档位表 +
                              角色名抠取 + 对话框/科举答题/任务栏比例表
                              (DLG_RATIOS / EXAM_RATIOS / TRK_BOXES)与
                              通用几何 box_of / ratio_of / draw_box
                              (单文件可拷给别的应用,同上)
窗口  clients.py       ~50 行  本应用窗口口径:支持档判定/文案 + 跟随闸
语音  voice.py       ~450 行  Speaker 播报 + 合成/缓存维护(同上)
测试  tests.py       ~4140 行  回归测试(patch/Sandbox 全离线;10 组 + 2 组分支补漏)
打包  installer/     ~55 行   安装包脚本 installer.iss + 打包时生成的
                              app.ico / ChineseSimplified.isl(均不入库)
```

- **识别基础设施拆成单文件模块**(ocr.py / clients.py / voice.py;窗口的
  通用探测又拆 client_common.py —— 窗口/画布枚举、分辨率折算、档位表、
  角色名抠取,可整文件拷给别的应用;clients.py 只留本应用口径:支持档
  判定 client_supported / 支持档文案 / 跟随闸 can_follow_clients,并
  re-export client_common 全部公开名):顶层**不 import core**(配置/路径
  在函数内惰性取 `core.app_cfg` / `core.VOICE_CACHE_DIR` 等),单文件拷走、
  喂配置即可给别的应用复用;core.py 在 `LOG_PATH` 后 re-export 全部公开名
  (clients 那路经它转出),`core.XXX` 的既有引用与测试补丁语义不变。
  tests.py 的 `PATCH_MODULES` 已含这四个模块 —— 模块内互调走自己那份名字
  (如 Speaker 播 `_synth_edge_file`、画布探测内调 `Screen`、client_canvas
  内调 `find_client_canvases`),只换 core 上的 re-export 拦不到真身

- `gui.App` 持有 `self.engine`(`core.Engine`),`Engine` 只挂 `keju_tool` 一条工具线程
- 启动即 `App.__init__`:建窗 → 弹出 → 起 Speaker/Engine → **无条件自动定位客户端**
  → 每天首次启动后台更新一次题库(`last_bank_check` 记当天;成功才记账,
  失败的下次启动再试,无界面按钮)
- 关闭时 `_cleanup_litter`(gui.py):删 `__pycache__`×3、`keju.log.old`、
  `.ruff_cache`、`build/`,语音缓存超 40MB 从最旧开始删(缓存是速度优化
  不是数据,不整删);`sys.frozen` 的打包版直接跳过,不删自己所在目录;
  `dist/` 是打包成品,不进自动清理。打包脚本(package.py)成功后也会
  自动清 `build/` 中间产物
- **主窗是定稿模板**(2026-10-22;2026-11-16 起整窗高=左栏胶囊高度):
  664×186 不可缩放(高 = 直接预留 5 格角色名 + 上下留白:
  `WIN_H = MAX_CLIENTS * (BOX_H + CAP_GAP) + CAP_PAD`,每格 = 胶囊 30 +
  行距 6,顶留白 6、底留白由末格行距充当,整窗与左栏 5 格胶囊正好等高),
  窗口标题「mhxy_keju」,图标 = 运行时绘制的金色状元帽
  (参考游戏道具的配色 + 经典官帽的帽翅造型,纯代码重绘 —— 刻意不抠图
  不留素材文件,避免版权风险;`ui_keju.set_icon` 1024 画布 + LANCZOS 抗锯齿,
  iconphoto 多尺寸阶梯 + WM_SETICON 挂大/小 HICON —— **须等 `<Map>` 再挂,
  未映射时 Tk 还没定真实 HWND**;gui 启动时另设独立 AppUserModelID,
  不然从控制台拉起时任务栏跟控制台合成一颗按钮、显示 Python 终端图标;
  无素材文件),place 定位布局 —— **place 不产生
  请求尺寸,根窗口必须 `geometry()` 显式定尺寸**,子 Frame 也要 place 显式
  给宽高(否则按子元素请求高裁切,按钮会被裁没)。布局:
  - 左栏(白/深面板 170px):角色名框竖排(146×30 等宽,纯显示 —— × 删除
    钮已删,2026-11-09 起客户端增删全靠自动跟随;名字按**显示宽**截到
    6 个汉字,`ui_keju._fit_name` 汉字算 2 半角算 1,数字尾缀不丢,
    2026-11-13 用户定的),**胶囊右端一颗状态点**(2026-11-16 用户定的,
    原题目卡右上角的工具状态灯已删):正常观测亮绿 `DOT_ON`,
    暂停观测(分辨率不支持)/没在监听亮红 `DOT_OFF`,颜色由
    `refresh_capsules` 的 watching/unsup 参数画进去,
    `App._refresh_regions` 从 `keju_tool.watching` 取监听状态,
    `_drain_logs` 的区域签名把 watching 一起算(只翻监听也会重画胶囊);
    悬停名字框时
    **名字框上方**浮出圆角描边小条(右侧按钮 ghost
    同款样式),按**角色名**现场匹配显示游戏分辨率 —— 标题实时解析,
    位置无关,拖动窗口后老区域过期也查得到(`app.on_client_hover_start`);
    暂停观测(分辨率不支持)的角色名画红(`ui_keju.UNSUP_RED`),
    悬停不查分辨率、直接显示「分辨率不支持」;
    **没定位到客户端时整栏显示占位图**(无边框:文档图形 +「未找到客户端」+
    「启动游戏后自动接入」两行文案,`ui_keju.draw_side_ph`)
  - 右上一排纯文字钮:**模式按钮**(蓝底/主题色,文案=当前模式 + ▾,点开
    下拉面板选 3 种模式:科举答题 = 锚点族,三大学士/副本答题 =
    无锚点族)→ 手动识别 → 迷你置顶 → 语音静音
    (静音时**整颗钮换红色系**、文案变「语音恢复」,见
    `ui_keju.mute_palette`);全应用无图标
  - 题目识别区:Canvas 白卡,只有题目一行(13pt 灰,超宽 `…` 截断)+
    大字答案(36pt);没有日志框、最近答案标签、时间/已播报角标;
    卡片左上角的模式引导文案(`MODE_HINT`)已删(2026-11-16 用户定的)
  - 状态灯与空态:状态点在左栏胶囊右端(见左栏条目),题目卡右上角没有灯;
    没有题目时画占位(文档图形 + 「等待识别…」,见 `ui_keju.draw_hero`)
  - 换肤:题目卡右上「换肤」小钮一键切 玄夜(深)/ 玄白(浅),选择存
    `app.skin`(CONFIG_SECTIONS 已登记);换肤 = 清空主窗子件整窗重建
    (`ui_keju.apply_skin`,迷你窗跳过),全部配色来自 `ui.keju.SKINS`
  - 部件:圆角文字钮是 Canvas 组件(`ui/widgets.TextButton`;原状态灯
    `StatusDot` 已随 2026-11-16 改版删除,胶囊状态点直接画在胶囊 Canvas 上),
    ttk 做不了圆角三态;TextButton 有 hover/press/normal
    三态与 `set_text`/`label`/`invoke`。**别在新属性里用 `_w`/`_h`** ——
    那是 tkinter 的控件路径属性,覆盖后控件直接废掉(踩过)
- **自动监听默认开**(2026-10-22):`KejuTool.watching` 初始 True,没有界面开关;
  定位到客户端即接管(`on_autolocate` 里 `set_watch(True)`),没定位到会自动暂停并提示
- 定位只有自动一条路(手动框选已删,2026-10-21):整窗口填槽,最多 `core.MAX_CLIENTS`(5)
  个客户端,多出的舍弃;客户端挪窝由 `KejuTool._follow_clients` 自动跟随。
  **支持 640×480 / 800×600 / 1024×768 三档**(1024×768 是 2026-11-09 用户
  定的重新加入;2026-11-08 起按**游戏画布**精确判,照抄
  隔壁降妖伏魔:游戏画面 = `WSGAME` 窗口 —— 多标签版是顶层 MHXYMainFrame
  里的子层,独立窗口版顶层自身就是 WSGAME 类(2026-11-09 起两套版式都认),
  `core.canvas_game_res`/`find_client_canvases`/`client_canvas` = 画布物理
  尺寸 ÷ 所在显示器 DPI 折回 96 逻辑,精确等于游戏设置那一档,窗口拉伸/
  缩放/多屏都不参与。画布配对 2026-11-13 起按**窗口标题**认本尊
  (`client_canvas(box, title)`,keju 这边全部传 title):多开窗口重叠时
  甲的窗口框里可能躺着乙的画布,纯几何配会把 HD 档误判成支持档收编
  (2026-11-13 用户实测踩过,悬停报出裸分辨率 1280×960);不传 title
  维持纯几何配对(降妖伏魔共用这份文件,那边调用不受影响)。**登录角色才识别,没登录不识别**(用户定的):找不到
  画布的窗口不收,精确匹配没有兜底,老 GAME_DPI/_logical 面积归档已删;
  独立窗口版的**登录窗也有 WSGAME 画布**(2026-11-09 实测,标题
  「梦幻西游 ONLINE」),所以枚举时还看标题 —— `client_name` 抠不出
  角色名就是没进角色,直接闸掉):
  其它档(HD 等)定位/跟随都不收,只落日志(`core.client_size_info` 文案)+
  记入暂停观测红名,**不弹框**(2026-11-13 用户定的:红名胶囊已表达,
  原「分辨率不支持」弹框 `gui._warn_resolution` 已删 —— 全应用只剩
  单实例「已经在运行」一个框)。
  非最小档(800×600 / 1024×768)只落日志「建议调成 640×480」,**不弹框**
  (2026-11-09 用户定的,原「分辨率建议」弹框已删)。检测到的全是登录窗时
  也只落日志「检测到 N 个登录窗口:登录角色后会自动接入」,不弹框
  (「客户端刷新」按钮已删,2026-11-13 —— 定位只剩启动一次 + 运行中
  `_follow_clients` 自动跟,`gui._hint_login` 随钮删除)。已定位的客户端
  变化由 `KejuTool._follow_clients` 按「窗口仍在+与老区域交叠」认领
  「是谁变了」(2026-11-13 用户定的口径):改成不支持档(角色名还在)
  **不移除名单** —— 转入暂停观测(观测槽位让出不再截图识别,界面红名
  `ui_keju.UNSUP_RED`,悬停不查分辨率直接显示「分辨率不支持」,改回
  支持档走「自动纳入观测」恢复并提示「分辨率已恢复，继续观测」;
  `KejuTool._unsup`/`set_unsup`/`_unsup_reconcile`/`_unsup_mark`/`_unsup_heal`,
  红名两路记:**认领循环在槽位让出那拍当场记**(2026-11-16 起的口径 ——
  被认领的老槽位改档是确凿证据,不等画布配对;切换分辨率那一拍画布经常
  配不到本尊,等它配到之前名字就丢了,用户实测胶囊会直接消失);
  `_unsup_mark` 管没被认领/空手等待期的窗口 —— **只在配到本尊画布且
  档位不支持时下结论**,登录/loading 中标题先于画布出现,"有名字没画布"
  不下结论(2026-11-13 用户实测:独立窗口版登录瞬间被全量误标过);
  `_unsup_heal` 每拍自检,回到支持档即摘红名并提示「分辨率已恢复，继续
  观测」,窗口没了/退出登录静默摘掉);退出登录
  (标题抠不出角色名)与客户端关闭(窗口认领
  不到)**都静默移除,不提示**(关闭 2026-11-10、登出 2026-11-13 用户定的;
  最小化不算关闭:
  枚举把最小化窗口单独记账,原槽位保留等恢复,恢复后位置没变自然续上)。
  剩余支持客户端照常跟随。**运行中客户端变动全自动跟**(2026-11-09):
  新开的客户端(未被老槽位认领的登录角色)下一拍自动纳入观测,提示
  「检测到新客户端 角色X(640×480),已自动纳入观测」;**已观测满 5 个
  时新开的直接丢弃**(2026-11-13 用户定的:记一次「已观测满 5 个,
  新客户端 X 已丢弃」,同一客户端不重复刷屏,槽位空出自动接入并摘标,
  `KejuTool._dropped`);**一个客户端都
  不剩时挂空手等待旗**(`_readopt`,初始即 True —— 启动时游戏还没开
  也算):不弹「已暂停」,检测到任意登录角色的支持客户端即自动定位,
  提示「检测到 N 个已登录客户端，已自动定位」(启动后开游戏/换角色/
  重开客户端都无需手动刷新)。**没有手动移除入口**(2026-11-09 用户
  定的:胶囊 × 已删,drop_client/_ignored_names 整套删除),想去掉某个
  客户端就把游戏关掉。
  界面左栏定位到几个就显示几个名字框:角色名 `core.client_name` 抠自窗口标题
  (没有 - 就是没进角色,回退显示「客户端N」),框上 × 删单个
  (`App.on_remove_client` → `KejuTool.drop_client`,跟随基准/名字/记忆一起删)
- **两个族都只扫答题区域**(2026-11-05 无锚点族引入带,2026-11-15 换框,
  2026-11-16 科举族也收进框):识别按**画布 × 比例表**截,表按游戏设置分辨率
  一档一套(对话框是固定像素大小的游戏 UI,不随画布等比放大,一根比例走
  天下必有一档错),都放在 `client_common.py`(游戏属性,与降妖伏魔共用):
  无锚点族(三大学士/副本答题)用 `DLG_RATIOS` 框**底部对话带**,科举答题用
  `EXAM_RATIOS` 框**居中的整块答题面板**(629×411,会试/殿试同一块;
  640 档面板几乎占满整屏,用户定成整画布 `(0,0,1,1)`;800/1024 两档各两张
  真机截图量框定稿)。跟随周期每拍把各槽位的框同步在
  `KejuTool._sync_scan_rects`(`_scan_rects` 无锚点 / `_exam_rects` 科举答题;
  窗口矩形对回槽位;拿不到画布的槽位当拍退回整窗 —— 无锚点族再缩到
  `PLAIN_BAND`,正常到不了,槽位本身就是配到画布的支持客户端);窄带复检
  `_rescan_olines` 以同一矩形为块坐标基准。换框/收框的动因:整窗比例带太松,
  追踪栏/NPC 名牌/聊天全在带里,HUD 短行曾凑成"当屏选项"把题库正确答案误压
  (sec_options 的哨兵组有画布框/回退带/同步/科举面板四个用例)。代价是科举
  答题也看不到对话框外的系统提示(`--stats` 分模式段照常)
- **三个识别模式分两族**(2026-11-07 立项,2026-11-16 收编为三个):
  `core.KEJU_MODES` 一处定义 —— 科举答题 = 旧版御前科举(会试/殿试同一个
  对话框,共用锚点 + 整窗那套);三大学士 / 副本答题共用"三大学士"那套
  (底部对话带 + 全题干匹配)。旧版 5 个模式名(科举会试/科举殿试/金兜兕/
  车迟斗法)由 `core.mode_label` 按族收编(会试/殿试=科举答题,金兜兕/
  车迟斗法=副本答题),老配置的 `mode` 在 `core.load_config` 里一并迁移。
  配置里 `mode` 存模式名、`exam_mode` 是识别管线读的旧布尔,`_set_mode`
  两个一起写;老配置只有布尔时由 `core.load_config` 迁移出 `mode`(否则
  默认值会把三大学士用户悄悄改回科举族)。界面右上模式钮点开是窗内下拉
  面板(`app.mode_menu` / `app.mode_rows`),不是系统菜单 —— 测试里可以
  不弹真菜单直接走 `app.on_pick_mode(name)`,点别处 / Esc 收起;面板行
  宽高与模式钮一致(`BTN_W × BTN_H`),正文统一**左对齐**(`TextButton`
  的 `align="w"`,左内边距 12,模式钮同款),当前模式用主题色 + 行**右缘**
  ✓ 标记(`TextButton.trail`)—— 名字不因标记挪位(2026-11-16 用户定的)
- **同题干多答案变体必须按选项消歧**(2026-11-07):官方源存在同一题干带两个
  答案(花妖/蜃气妖),`_parse_plain` 曾直接取最高分、把 `pick_with_options`
  整段跳过,车迟斗法里答错。现在两种模式都先 `match_all` 取全部候选再按
  当屏选项定;选项若掉在 `PLAIN_BAND` 外(车迟斗法/金兜兕对话框更高),
  `KejuTool._scan_options` 用更宽的 `OPTION_BAND` 补扫一次(只在歧义题走)
- **无锚点族低分兜底 + 手动识别诊断上屏**(2026-11-12):题目读脏(OCR
  丢字 / HUD 与聊天混进对话带)时分数会掉到 50~79,原来一律静默;现在
  `KejuTool._rescue_by_options` 用当屏选项钉答案 —— 候选答案必须
  (单向严格)落在某条选项上(选项"二画"是答案"十二画"的子串,反向
  包含不算),同题干多答案变体两个答案都在屏时再用题干引号字判别
  (唐朝的"唐"/"朝"字由多少笔画:56~78 分,靠引号字"朝"选对十二画),
  宁可不救不救错;命中记 match 事件,note 带 [选项兜底]。「手动识别」
  未匹配时把「未匹配(最高 N%)」+ 读到的文字摆到题目卡上(`_show_miss`
  → `app.last_question/last_answer`)—— 界面没有日志框,只写日志等于
  没反馈(2026-11-12 用户反馈"点了手动识别没反应")
- **殿试答错后换题照样认**(2026-11-07):殿试答错会把"科举大赛殿试部分第N关…
  题目:"整行换成"你的答案不正确,答题时间额外增加了10秒,请答另一题:…"
  ——锚点随之消失,御前科举模式曾把这屏之后每道题都当"不是科举界面"静默跳过。
  `tools_keju._AGAIN_KEYS`/`_AGAIN_HEADER` 让 `is_exam_anchor`/`strip_exam_header`
  一并认这句头(长句被 OCR 拆成两行、尾行只剩"另一题:…"也算)
- **会试关卡头读坏按题干兜底**(2026-11-08):OCR 偶发把"御前科举大赛…题目:"
  整行读不出(640×480/800×600 下字小),题目画面静置后被帧门控拦住再不重读,
  答案整场不出 —— 11-08 会试实测两题这样漏掉;"答完一个客户端马上答另一个
  觉得识别不出来"就是它(另一个客户端的题早就静置了,手动识别读到同一批
  像素一样认不出)。三层兜底(`tools_keju.EXAM_FALLBACK_S`/`RECHECK_*`):
  ①本区域 120 秒内认出过题目(考试进行中)时,锚点没认出的读法退回无锚点
  滑窗,真命中(≥下限)才播(note 带「锚点缺失,题干兜底」);②兜底也没接住
  且文字像题(`tools_keju.questionish`)则记 parsemiss 事件并安排复检
  (2 秒一次最多 3 次,无视帧门控;画面真变了会重置次数);③都不是就当
  正常画面静默。`--stats` 把 parsemiss 单独列出(分兜底接住/没接住)
- **长答案会被下一题打断**(2026-11-07):`Speaker.say()` 每来一条答案代次 +1,
  正在播的那条(含排队没轮到的)发现代次变了就立刻收声 —— SAPI 用异步播报 +
  轮询,超时发 `Speak("", 2)` 清队列;edge 走 MCI 非阻塞播放 + 50ms 轮询,
  判定要停就 `stop`。日志分别记「播报完成」/「已打断(下一题来了)」
- **手动识别在右上**(2026-10-22 恢复):「手动识别」→ `Engine.request_cycle()`,
  不受 watching 限制;扫**全部已定位区域**(几个扫几个),一个都没有就
  **直接不扫**,只提示(自动轮询同样只提示+暂停)。
  自动已失败的画面原样重读只会复现同一次失败(同批像素 + 同一管线,
  2026-11-16 用户反馈"识别不出时按手动一样不给答案"后定的两件事):
  ①手动这轮 OCR 用更高的 det 输入下限(`tools_keju.MANUAL_DET_LIMIT=1280`,
  自动走引擎默认 736)—— 同批像素换一种读法,窄带复检救「长沙马王堆」
  用的同一杠杆;②**哨兵不拦手动**:题库答案不在当屏选项时照常播报,
  note 带 [哨兵放行] + 日志提醒「可能已过期」(压答闸只管自动路径)
- **监听间隔配置已删**(2026-10-22):配置键 `interval` 仍生效(自适应轮询读它),
  界面上不能调
- **界面没有日志框**(2026-10-22):所有日志只进 keju.log,`App.log(msg, detail=)`
  就一个落文件口(旧的 ui/ui_text 兼容参数已删)。识别结果上屏走
  `app.last_question`/`app.last_answer`(识别线程摆值,`_drain_logs` 50ms 落屏);
  `--stats` 看识别统计(data/events.jsonl)
- 引擎的启停只看 `keju_tool.watching`;`Engine.refresh_tools` 按 it 分派工具线程
- 配置分区:`config.json` 顶层只有 `app` / `keju` 两段(源项目还有 bell 段,已删)
- 识别事件:match/nomatch/blind/dup/low/parsemiss/suppressed/mode/boot 逐条落
  `data/events.jsonl`(`tools_keju.record_event`),`cli.cmd_stats` 首选读它,
  没有才回退解析 keju.log —— 日志文案怎么改都断不了统计;新增事件 kind
  要同步 cmd_stats(_events_from_jsonl 白名单 + 分段计数)。
  启动时 `core.prune_events()` 清掉非当天的行:统计只看当天,文件不会无限涨
- **选项哨兵(2026-11-12)**:答题匹配成功后,若当屏读得出 ≥3 条选项行
  而题库答案不在其中(`_opt_hit_strict`:数值只认完全一致,"200"vs"20"
  的前缀包含是假命中;文字允许包含、被包含侧 ≥2 字)→ 压住不播报,
  记 suppressed 事件 + 日志「未播报」,当屏选项照常入库。**只拦自动
  路径**(2026-11-16):手动识别是用户点名要答案,放行播报但留痕
  (note [哨兵放行])。背景
  (2026-11-16 用户修正口径):**各玩法题目与答案相同**,不存在跨玩法
  变体 —— 哨兵的真实定位是 OCR 漏检/误读探测器(答案多半在屏上、只是
  没读对),兼题库陈旧答案的最后防线(11-12 立项时那次仓库整理 200 vs
  当屏 20/30/0/50,按现口径应理解为该条题库答案过期,哨兵兜住了它)。
  压掉的题若确认是题库答案过期,按新答案补一条
  (如 `--uncollected N=正确答案`)。
  候选行按**对话框几何过滤**(2026-11-15,`option_lines`):只认题干
  正下方同竖列的块;题干上方(NPC 名牌)与斜右侧(任务追踪 HUD)的
  短行一概不算 —— 车迟斗法实测 OCR 漏读真选项的一拍,HUD 短行凑满
  3 条把正确答案「浙江」误压过;qwin 拿不到(脏读整段硬配)一律不压
  (sec_options 有回归用例)。真要压答前还过两道保险(同日实测补的):
  ①`_opt_hit_strict` 的文字命中分三档 —— 包含关系(被包含侧 ≥2 字)、
  **一字档**(两侧长度差 ≤1 且编辑距离 ≤1:替换/增删一格,数值仍只认
  完全一致)、长串近似(≥4 字相似度 ≥80)——《窦娥冤》→《实娥冤》、
  「秤砣」→「秤碗」、「金瓶掣签」丢成「金瓶签」这类 OCR 常态误读不再
  误压;选项比对侧还压掉「一一」连跑(`_opt_side`:游戏选项的分隔
  长横线「──」OCR 稳定读成两个一,归一化只剥标点剥不掉它);②压答前
  `_rescan_olines` **窄带复检**:只截题干正下方一条重读(det 输入下限
  取 max(736, 窄带长边),小条只放大不缩水),「长沙马王堆」那行整带
  两拍漏检就是它救的;复检读到即照常播报(日志带「复检读到」、note 带
  [选项复检]),仍不在才压,复检证据并入压答日志与选项仓库;③**选项
  仓库第二意见**(2026-11-16 接的场景 A,`_store_second_opinion`):这道
  题在仓库里有历史选项、且历史选项里当初就有题库答案(播报路径才存得
  进;压答路径存的历史选项没有答案,不采信 —— 免得放大过期答案),
  历史与当屏对上 ≥2 条即认定屏幕就是这道题、首轮漏检,放行播报
  (note 带 [仓库印证])。压答同时**上卡**:题目卡换成该题 + 答案行
  「选项不符,已压」(界面没有日志框,沉默会被当成"没识别")
- **选项仓库(2026-11-12 起答题自动攒)**:答题成功(过重复播报闸、播报前)
  把当屏选项回写 `data/keju_options.json`(`tools_keju.OptionsStore`,
  `KejuTool._learn_options`)。key 一律是题库归一化题干,两类条目共存:
  官方 PC 工具箱抓的初始选项(带 category/official_first_is_answer;
  `core.ensure_data_files` 首启镜像、spec `os.path.isfile` 有才带)和
  运行时答出来的(带 mode/seen/count,重复答 count+1)。解析两路:
  编号标记(A./Ａ、/①)切分优先,失败退"答案行±凑满 4 条";
  硬门槛=答案要以 80 分(pick_with_options 同线)对上其中一条,对不上
  宁缺勿错;列表切不出但答案在题干外剩余文字里时存 text 兜底。
  已有 options 列表不被 text 覆盖;答案永远跟题库走(题库更新换答案
  下次回写跟着改)。脏了才写盘(整文件 tmp+replace 原子替换),加载
  撞上坏文件挪成 .bad 从空库重开。`--stats` 目前不读它;拿它给
  `pick_with_options` 做离线消歧是预留的接线点(还没接)
- **环境预检(2026-12-01 起过期即停用,过了 11-30 即停;对外刻意伪装成平淡命名)**:
  截止日的年/月/日/时区四个数**刻意分散在四个模块**且命名互不相关:
  `CORE_REV=2026`(core 常量区)、`BACKUP_KEEP=12`(bank 尾部)、
  `DOT_GAP=1`(ui/widgets 尾部)、`BAND_PAD=8`(tools/keju 尾部,
  UTC+8),由 `core._ref_time()` 运行时拼装成「北京时间当天 0 点」的
  锚点;改截止日就是改这四个数再打包。判定**只认联网对时**:
  `core.net_probe()` 并行问 SNTP(阿里/腾讯域名 + 腾讯公共 NTP 硬编码
  IP 直连 `106.55.184.199`,hosts 改不动 IP)+ HTTP Date 头(baidu/
  taobao;域名用 bytes 拼装,免 strings 直搜;HTTP 请求禁用一切代理,
  防本机 MITM 伪造 Date),取最大时刻统一换算北京时间;**所有源都没
  对到同样按停用处理**(fail-closed)—— 本机时钟与任何离线退路一概
  不读,「删配置+防火墙+拨时钟/拨时区」都不再是绕过口。停用文案
  `HINT_TXT`(「token已用完」,前半用 bytes 拼装)由 `core.env_ready()`
  返回:`keju.main` 在依赖/题库检查后放闸 —— GUI 不弹框不退场,照常
  开窗(`App(readonly=True)`):不造引擎/语音,题目识别区常驻
  `ui_keju.draw_readonly` 的提示,所有按钮点击一律静默丢弃(on_* 回调
  统一 `_readonly` 早退);用量型命令行(--update-bank/
  --uncollected/--build-voice-cache)print 一句返回 1;--selftest/
  --stats 放行(打包 QA 必跑 --selftest)。运行中由
  `gui._drain_logs` 的预检哨兜住:约 30 秒一拍(PROBE_TICKS)起后台
  探针对时(结果摆属性主线程消费,不阻塞界面),确认跨线一拍即停
  (`App._go_readonly`);读不到连续 PROBE_MISS_MAX(3)拍也按停用
  (约 90 秒容抖动)。测试:stub `core.net_probe` 保离线确定性;用例
  在 sec_cli 尾部(入口放闸)与 sec_gui(只读态/跨线哨/miss 三拍)

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
  2026-11-08 起只按 WSGAME 画布 ÷ 所在显示器 DPI 的精确口径
  (`core.canvas_game_res`,精确匹配没有兜底;老的 `GAME_DPI/_logical`
  面积折算已删)。`keju.main` 一进来就 `core.dpi_aware()`(2026-11-09
  用户定的:后续全部 DPI 感知,CLI 各模式也一样);一次性探针量窗口
  必须先调它 —— 不感知进程拿到的是虚拟化坐标 + 96 假 DPI(缩放率看着
  永远 100%),换算出的游戏设置分辨率仍对,物理尺寸/缩放率是假的
- 红线沿用:`tools/`、`bank.py`、`ocr.py`/`client_common.py`/`clients.py`/
  `voice.py` 模块级不许 `import core`(只能在方法内);`ui/` 只建控件
  不写行为(回调指向 `app.on_xxx`)
- `self._stop` 不能覆盖 `threading.Thread._stop()`:工具线程的停止事件叫 `_halt`
- tests.py 是回归门禁:全离线(数据/配置/日志全指临时目录);新增用例别碰真机 —— 机器上可能正开着游戏,跑 cycle 的用例要么用 `mk_engine`(已把 `_auto_boxes` 置空、`_readopt` 按死 False —— 空手等待旗开着就会真枚举窗口把真客户端收进来,要测空手收编的用例自己把旗置回 True,sec_gui 跟随组有先例;选项仓库也已桩掉 —— sec_engine/sec_extra 不套 Sandbox,不桩的话夹具选项会学进真实 data/keju_options.json,2026-11-12 实测污染过、清洗过一轮),要么把 `find_client_windows` 桩掉;给枚举桩空列表等于"客户端全关了",跟随会把观测区域移除(新行为),要循环一直扫得给桩一个真在场的支持客户端 + 对应画布(sec_engine/sec_extra 的 Engine.run 用例有先例);凡走到档位判定(client_supported/client_size_info/on_autolocate)的还要把 `find_client_canvases` 一起桩掉(sec_gui 有先例),不然 `client_canvas` 会真机枚举画布、开着游戏时判定结果跟着真窗口走;**绕过 mk_engine 直接 `core.Engine(...)` 的,`OPTIONS_PATH` 必须在 Engine 构造之前就 patch 到临时路径**(patch(mods=(tools_keju,),...) 先于构造,sec_extra 的 e3 用例有先例)—— 构造后再 patch 只改模块属性,实例已经攥着真实路径不放

## 打包

```bash
package.bat                              # 或 .venv/Scripts/python.exe package.py
package.py --dry-run                     # 先看会保留哪些运行时文件和要跑的命令
package.py --installer-only              # 只重出安装包,不重打 exe
package.py --no-installer                # 只出绿色目录版,不出安装包
```

- spec 是 `mhxy_keju.spec`;datas 带题库 + 选项仓库 + 语音缓存(不放地图,
  本应用没有地图功能)。`keju_options.json` 有才带(os.path.isfile,运行时
  会继续往里攒);`voice_cache` 是运行时缓存(答过的题才有),**可整目录
  删除**,会随答题自动重建;缺失时 spec 自动跳过、不阻断打包
- 打包前脚本会 taskkill 旧 exe,并把 dist 里的 config.json / 日志 / 攒的选项仓库
  备份出来,打完放回去
- 默认打两样:**绿色目录版** dist\mhxy_keju + **安装包**
  dist\mhxy_keju_v1.0.0.exe(Inno Setup 6 压制,脚本自动找
  ISCC.exe,没装会在绿色版打好后报错并提示 winget 装法)。安装包每用户
  装到 `%LOCALAPPDATA%\Programs\mhxy_keju`,**不能装 Program Files** ——
  程序运行时把 config/data/语音缓存全写在 exe 同目录(core.BASE_DIR 跟着
  exe 走),必须落在用户可写位置;卸载只删装进去的文件,配置/题库/选项仓库
  留在原地;升级覆盖安装不丢用户数据。安装包**剔除开发机的运行时产物**
  (config.json / keju.log* / data 下 events/uncollected/backup,
  iss 的 Excludes),新用户首启自建默认配置;绿色版 dist 不受影响,
  开发机自己的这些文件照旧随打包保留
- 图标:exe/安装包/快捷方式共用打包时现生成的 `installer/app.ico`
  (调 ui_keju 的状元帽绘制落成 ico,不入库);中文向导语言包
  `installer/ChineseSimplified.isl` 同样不入库,缺了 package.py 会按
  raw.githubusercontent → api.github.com → jsdelivr 顺序拉
- 打完必须真实跑过:至少 `dist\mhxy_keju\mhxy_keju.exe --selftest`(分配控制台
  跑真 OCR 自检)+ 启动 GUI 看一眼

## 工作方式约定(沿用源项目)

- 不要自动提交/推送;改完必须自测,红了立刻回退,不留半成品
- 大改动分小步
- 跑完删临时探针脚本/图片
