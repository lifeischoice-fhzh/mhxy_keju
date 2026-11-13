"""语音播报:合成(edge 云端/SAPI 本地)+ 播放 + 缓存仓库维护。

可独立复用的单文件模块:给文本出声音。依赖:Windows SAPI(pywin32) /
edge-tts(云端合成,联网时);缓存目录与配置(音量/音色/静音)走 core,
方法内惰性 import —— 本文件顶层不碰 core,别的应用直接把本文件拷走、
按需喂配置即可。
"""
import os
import time
import queue
import threading


ZH_VOICE_KEYS = ("zh-cn", "zh_cn", "zh-hans", "huihui", "kangkang",
                 "yaoyao", "tracy", "minxi", "yunxi", "yunyang")

CLOUD_VOICES = {
    "云端·云希(男声)": "zh-CN-YunxiNeural",
    "云端·晓晓(女声)": "zh-CN-XiaoxiaoNeural",
}

def _play_mp3_blocking(path: str, should_stop=None):
    """用 Windows MCI(winmm)同步播放 mp3,无需额外依赖。

    should_stop: 可选的"要不要中断"回调(返回 True 立刻停)。
    不给就走 MCI 自带的 wait 一直播完;给了就非阻塞播放 + 每 50ms 轮询,
    长答案播到一半来了下一题能马上收声。
    """
    from ctypes import windll, create_unicode_buffer

    winmm = windll.winmm

    def send(cmd):
        buf = create_unicode_buffer(256)
        if winmm.mciSendStringW(cmd, buf, 256, 0) != 0:
            raise RuntimeError(f"MCI failed: {cmd}")
        return buf.value

    try:
        send(f'open "{path}" alias ttsclip')
        if should_stop is None:
            send("play ttsclip wait")
            return
        send("play ttsclip")
        buf = create_unicode_buffer(256)
        seen_playing = False
        t0 = time.time()
        while True:
            if should_stop():
                send("stop ttsclip")
                return
            if winmm.mciSendStringW("status ttsclip mode", buf, 256, 0) != 0:
                # 查不到播放状态(极少见):当它播完,别在这里死等 —— close 会收声
                return
            mode = buf.value.strip().lower()
            if mode == "playing":
                seen_playing = True
            elif seen_playing:
                return                  # 播完了(stopped)或者被暂停
            elif time.time() - t0 > 3:
                return                  # 迟迟没进入播放态:别死转
            time.sleep(0.05)
    finally:
        try:
            send("close ttsclip")
        except Exception:
            pass




RATE_PCT = 0  # 固定语速(不对外提供配置,避免缓存反复失效)


def edge_params(cfg: dict):
    """当前云端合成参数(语速固定)。音量/音调在 app 分区。"""
    import core
    app = core.app_cfg(cfg)
    volume = max(-100, min(0, round((float(app.get("volume", 1.0)) - 1) * 100)))
    pitch = max(-50, min(50, int(app.get("pitch", 15))))
    return RATE_PCT, volume, pitch


def _voice_cache_path(text: str, voice: str, rate: int, volume: int,
                      pitch: int = 0) -> str:
    import core
    import hashlib
    key = f"{voice}|{rate:+d}%|{volume:+d}%|{pitch:+d}Hz|{text}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    os.makedirs(core.VOICE_CACHE_DIR, exist_ok=True)
    return os.path.join(core.VOICE_CACHE_DIR, f"{h}.mp3")


def _synth_edge_file(text: str, voice: str, rate: int, volume: int,
                     out_path: str, timeout: int = 15, retries: int = 2,
                     pitch: int = 0) -> bool:
    """合成一条答案音频到 out_path。

    源码运行:起子进程跑 edge-tts(隔离崩溃,可强杀超时)。
    打包版(sys.frozen):sys.executable 是应用自己,不是 python 解释器,
    "-c" 只会被 argparse 当未知参数拒掉(实测 exit=2)——edge_tts 本来
    就随包分发,改为进程内合成;套临时线程 + 超时,卡住就放弃,别拖住
    播报线程(同 Ocr.warmup 的模式;代价是"放弃"杀不死卡住的线程,
    而子进程方案能强杀)。
    """
    import sys

    def synth_inline() -> bool:
        import asyncio

        async def _save():
            import edge_tts
            c = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%",
                                     volume=f"{volume:+d}%",
                                     pitch=f"{pitch:+d}Hz")
            await c.save(out_path)

        box: dict = {}
        done = threading.Event()

        def work():
            try:
                asyncio.run(asyncio.wait_for(_save(),
                                             timeout=max(1, timeout - 1)))
                box["ok"] = True
            except BaseException:
                box["ok"] = False
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        if not done.wait(timeout):
            return False                  # 超时:这次算失败,别无限等
        return (bool(box.get("ok")) and os.path.exists(out_path)
                and os.path.getsize(out_path) > 0)

    for attempt in range(retries + 1):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            if getattr(sys, "frozen", False):
                ok = synth_inline()
            else:
                import subprocess
                script = (
                    "import sys, asyncio, edge_tts\n"
                    "async def main():\n"
                    "    c = edge_tts.Communicate(sys.argv[1], sys.argv[2],"
                    " rate=sys.argv[3], volume=sys.argv[4], pitch=sys.argv[6])\n"
                    "    await c.save(sys.argv[5])\n"
                    "asyncio.run(asyncio.wait_for(main(), timeout=14))\n"
                )
                subprocess.run(
                    [sys.executable, "-c", script, text, voice,
                     f"{rate:+d}%", f"{volume:+d}%", out_path,
                     f"{pitch:+d}Hz"],
                    timeout=timeout, check=True, creationflags=0x08000000)
                ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            if ok:
                return True
        except Exception:
            pass
        if attempt < retries:
            time.sleep(0.8 + attempt)  # 服务抖动,稍作退避再试
    try:
        os.remove(out_path)
    except Exception:
        pass
    return False

REQUIRED_MODULES = ["numpy", "mss", "rapidocr_onnxruntime", "rapidfuzz",
                    "PIL", "win32com"]




class Speaker(threading.Thread):
    """独立线程跑 Windows SAPI(SpVoice),避免阻塞界面。"""

    def __init__(self, cfg: dict, log):
        super().__init__(daemon=True)
        self.cfg = cfg                 # 完整分区配置:语音只读 app 分区
        self._log = log
        self.q: queue.Queue = queue.Queue()
        self._tts = None
        self._ready = threading.Event()
        self.dead = False                # 语音引擎起不来:say() 直接丢弃
        self.voice_name = ""
        # 播报代次:say() 每来一条新答案就 +1,正在播的那条发现代次变了就停
        self._gen = 0
        self._gen_lock = threading.Lock()

    def _setting(self, key, default=None):
        """语音相关的设置全在 app 分区:音量/音色/静音/合成后端。"""
        import core
        return core.app_cfg(self.cfg).get(key, default)

    def run(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            import win32com.client
            self._tts = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception as e:
            self._log(f"[语音]引擎初始化失败: {e}")
            # 置位并标记:否则每次播报都会让识别线程在这里等满 10 秒
            self.dead = True
            self._ready.set()
            return
        self._apply()
        self._ready.set()
        while True:
            item = self.q.get()
            if item[0] == "quit":
                break
            if item[0] != "say":
                continue
            text, gen = item[1], item[2]

            def stale(g=gen):
                """排队/播报期间又来了新答案(长答案常见):这条不用再播了。"""
                return self._gen != g

            if stale():
                continue
            if str(self._setting("tts_backend")) == "edge":
                t0 = time.time()
                if self._speak_edge(text, stale):
                    if stale():
                        self._log("[语音]已打断(下一题来了)")
                    else:
                        tag = ("本地缓存" if getattr(self, "_cache_hit", False)
                               else "云端合成")
                        self._log(f"[语音]播报完成({tag} {time.time() - t0:.1f}s)")
                    continue
            if stale():
                continue            # 云端失败但已被新题顶掉:别再用旧答案回退
            self._speak_sapi(text, stale)
        # 正确释放 COM:先丢弃 SpVoice 引用,再反初始化公寓,避免退出时崩溃
        try:
            self._tts = None
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            pass

    def _speak_sapi(self, text: str, stale=None):
        """系统语音:异步播报 + 轮询,来了下一题就清队列立刻收声。"""
        t0 = time.time()
        try:
            # SVSFlagsAsync=1:Speak 立刻返回,下面自己轮询,才能中途打断
            self._tts.Speak(text, 1)
            while not self._tts.WaitUntilDone(50):
                if stale is not None and stale():
                    self._tts.Speak("", 2)      # SVSFPurgeBeforeSpeak
                    self._log("[语音]已打断(下一题来了)")
                    return
            self._log(f"[语音]播报完成({time.time() - t0:.1f}s)")
        except Exception as e:
            self._log(f"[语音]出错: {e}")

    def _speak_edge(self, text: str, stale=None) -> bool:
        try:
            voice = self._setting("voice_hint") or "zh-CN-YunxiNeural"
            rate, volume, pitch = edge_params(self.cfg)
            cpath = _voice_cache_path(text, voice, rate, volume, pitch)
            if os.path.exists(cpath) and os.path.getsize(cpath) > 0:
                self._cache_hit = True
                if stale is None:
                    _play_mp3_blocking(cpath)   # 缓存命中:零网络,直接播
                else:
                    _play_mp3_blocking(cpath, stale)
                return True
            self._cache_hit = False
            last_err = "unknown"
            for _ in range(2):  # 服务偶发抖动,重试一次
                if stale is not None and stale():
                    return True             # 已有新题:这次合成/播报直接作废
                if _synth_edge_file(text, voice, rate, volume, cpath,
                                    timeout=7, retries=1, pitch=pitch):
                    if stale is not None and stale():
                        return True
                    if stale is None:
                        _play_mp3_blocking(cpath)
                    else:
                        _play_mp3_blocking(cpath, stale)
                    return True
                last_err = "合成失败"
            self._log(f"[语音]云端失败({last_err}),回退系统语音")
            return False
        except Exception as e:
            self._log(f"[语音]云端出错({e}),回退系统语音")
            return False

    def _apply(self):
        tts = self._tts
        if tts is None:
            return
        try:
            # SAPI Rate 取值 -10..10;语速固定(RATE_PCT=0,界面不提供语速控件,
            # 固定才能让音频缓存不因改速反复失效)
            tts.Rate = max(-10, min(10, round(RATE_PCT / 10)))
            tts.Volume = max(0, min(100,
                                    int(float(self._setting("volume", 1.0)) * 100)))
        except Exception:
            pass
        hint = (self._setting("voice_hint") or "").strip()
        try:
            voices = tts.GetVoices()
            chosen_idx = None
            for i in range(voices.Count):
                desc = voices.Item(i).GetDescription()
                low = desc.lower()
                if hint and hint.lower() in low:
                    chosen_idx = i
                    break
                if chosen_idx is None and any(k in low for k in ZH_VOICE_KEYS):
                    chosen_idx = i
            if chosen_idx is None and voices.Count:
                chosen_idx = 0
            if chosen_idx is not None:
                tts.Voice = voices.Item(chosen_idx)  # 始终选好中文音,作为断网回退
                self.voice_name = voices.Item(chosen_idx).GetDescription()
            else:
                self.voice_name = "默认"
        except Exception:
            self.voice_name = "默认"
        if str(self._setting("tts_backend")) == "edge":
            self.voice_name = next((n for n, v in CLOUD_VOICES.items()
                                    if v == hint), "云端语音")
            return
        if self.voice_name != "默认" and not hint and not any(
                k in self.voice_name.lower() for k in ZH_VOICE_KEYS):
            self._log(f"[语音]未找到中文语音(当前 {self.voice_name}),"
                      "可在 设置→时间和语言→语音 中添加中文语音包")

    def say(self, text: str):
        if self.dead or self._setting("mute"):  # 起不来/静音都不拖住识别线程
            return
        if not self._ready.wait(timeout=10):
            self._log("[语音]引擎未能启动,无法播报")
            return
        with self._gen_lock:
            self._gen += 1
            gen = self._gen
        # 带上代次:已经在播的那条(以及排队没轮到的旧答案)看到代次变了就停
        self.q.put(("say", text, gen))

    def quit(self):
        self.q.put(("quit", None))




def prune_voice_cache(cfg, bank) -> int:
    """删除与当前合成参数不匹配的缓存音频(改语速/音色后留下的失效文件)。"""
    import core
    voice = core.app_cfg(cfg).get("voice_hint") or "zh-CN-YunxiNeural"
    rate, volume, pitch = edge_params(cfg)
    texts = {e["a"] for e in bank.entries}
    valid = {os.path.basename(_voice_cache_path(t, voice, rate, volume, pitch))
             for t in texts}
    removed = 0
    try:
        for f in os.listdir(core.VOICE_CACHE_DIR):
            if f.endswith(".mp3") and f not in valid:
                try:
                    os.remove(os.path.join(core.VOICE_CACHE_DIR, f))
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


def count_missing_voice(cfg, bank) -> int:
    """统计题库答案里还没有本地音频缓存的数量。"""
    import core
    voice = core.app_cfg(cfg).get("voice_hint") or "zh-CN-YunxiNeural"
    rate, volume, pitch = edge_params(cfg)
    n = 0
    for e in bank.entries:
        a = e["a"].strip()
        pth = _voice_cache_path(a, voice, rate, volume, pitch)
        if not (os.path.exists(pth) and os.path.getsize(pth) > 0):
            n += 1
    return n


def build_voice_cache(limit: int = 0, workers: int = 4, log=print):
    """并发合成答案音频。log 为进度回调(默认 print)。

    返回 (新合成, 失败, 已有)。可中断,重跑续传。
    """
    import bank
    import core
    import threading
    from concurrent.futures import ThreadPoolExecutor

    bank = bank.QuestionBank()
    cfg = core.load_config()
    voice = core.app_cfg(cfg).get("voice_hint") or "zh-CN-YunxiNeural"
    rate, volume, pitch = edge_params(cfg)
    seen, items = set(), []
    for e in bank.entries:
        a = e["a"].strip()
        if a and a not in seen:
            seen.add(a)
            items.append(a)
    if limit > 0:
        items = items[:limit]
    todo = []
    for a in items:
        pth = _voice_cache_path(a, voice, rate, volume, pitch)
        if not (os.path.exists(pth) and os.path.getsize(pth) > 0):
            todo.append(a)
    skipped = len(items) - len(todo)
    log(f"待合成 {len(todo)} 条(共 {len(items)} 条,已有 {skipped} 条,"
        f"{workers} 路并行)")
    done = failed = 0
    lock = threading.Lock()
    t0 = time.time()

    def work(a):
        nonlocal done, failed
        cpath = _voice_cache_path(a, voice, rate, volume, pitch)
        ok = _synth_edge_file(a, voice, rate, volume, cpath,
                              timeout=10, retries=1, pitch=pitch)
        with lock:
            if ok:
                done += 1
            else:
                failed += 1
            n = done + failed
            if n % 25 == 0 or n == len(todo):
                el = time.time() - t0
                eta = el / max(1, n) * (len(todo) - n) / 60
                log(f"音频合成进度 {n}/{len(todo)} | 成功 {done} 失败 {failed} | "
                    f"已用 {el/60:.1f} 分钟,预计还需 {eta:.0f} 分钟")

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))
    except KeyboardInterrupt:
        log("已中断,下次重跑会继续。")
    return done, failed, skipped


