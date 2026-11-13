"""屏幕截屏 + OCR 识别(RapidOCR 封装,GPU 可选)。

可独立复用的单文件模块:截一块屏幕区域出文字块。依赖:rapidocr_
onnxruntime / numpy / mss;GPU 开关等配置走 core,方法内惰性 import。
"""
import threading

try:
    import numpy as np
except ImportError:  # main() 会给出友好提示
    np = None


WARMUP_TIMEOUT = 30.0      # OCR 预热超时(本机首次 GPU 预热约 1 秒,给足余量)


class Ocr:
    """RapidOCR 封装(线程安全,懒加载)。"""

    def __init__(self, log=None):
        self._engine = None
        self._lock = threading.Lock()
        self._log = log or (lambda m: None)
        self.device = "CPU"
        self.ok = True                 # warmup 那次真实推理成功过吗(False=假就绪)
        self.err = ""                  # 不健康的原因(引擎/看门狗据此重建与报错)

    def warmup(self, timeout: float = WARMUP_TIMEOUT):
        """预热:建好引擎 + 真跑一次推理,结果记在 ok 上。

        GPU(DirectML) 首次推理要初始化显存/算子(约 1 秒),提前吃掉,
        否则第一道题会明显变慢。这次推理失败**不再静默**:GPU 上先按既有
        机制回退 CPU 再试,仍然不行就把 ok 标成 False(附 err)——引擎据此
        报"识别不可用"而不是"已就绪",看门狗也能发现假就绪。

        预热整段放进临时线程跑,主线程只等 timeout:万一卡在驱动/引擎里不
        返回,这里照样标 ok=False。否则引擎线程恒活 + ok 默认 True,看门狗
        永远不动作,日志还会说"引擎已就绪"。
        """
        box: dict = {}
        done = threading.Event()

        def _work():
            try:
                box["ok"], box["err"] = self._warmup_once()
            except BaseException as e:      # 临时线程里出事也得落到 ok 上
                box["ok"], box["err"] = False, f"{type(e).__name__}: {e}"
            finally:
                done.set()

        th = threading.Thread(target=_work, name="OCR预热", daemon=True)
        th.start()
        if not done.wait(timeout):
            # 超时:那次预热的结论不再采信(卡住的线程还在就让它去),按不可用报
            self.ok = False
            self.err = f"预热超时({timeout:.0f} 秒没返回,可能卡在显卡驱动)"
            self._log(f"[OCR]{self.err},识别不可用,看门狗将重建引擎")
            return
        self.ok = bool(box.get("ok", False))
        self.err = str(box.get("err") or "")

    def _warmup_once(self):
        """真跑一次预热推理,返回 (ok, err);不直接改 self.ok(超时那次不采信)。"""
        with self._lock:
            self._get()
        dummy = np.full((32, 160, 3), 255, dtype=np.uint8)
        try:
            self._infer(dummy)
        except Exception as err:
            self._log(f"[OCR]推理初始化失败({err}),设备可能不可用")
            if str(self.device).startswith("GPU"):
                self._fallback_to_cpu(err)     # 既有机制:GPU 出问题就重建为 CPU
            try:
                self._infer(dummy)             # 回退后再试一次
            except Exception as err2:
                self._log(f"[OCR]推理仍不可用({err2}):"
                          "识别会一直报错,看门狗将重建引擎")
                return False, str(err2)
        return True, ""

    def _get(self):
        import core
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self.device = "CPU"
            try:
                cfg = core.load_config()
                if core.app_cfg(cfg).get("use_gpu", True):
                    import onnxruntime as ort
                    if "DmlExecutionProvider" in ort.get_available_providers():
                        self._engine = RapidOCR(det_use_dml=True,
                                                rec_use_dml=True,
                                                cls_use_dml=True)
                        self.device = "GPU(DirectML)"
            except Exception:
                self._engine = None
            if self._engine is None:
                self._engine = RapidOCR()
        return self._engine

    def recognize(self, img_bgr) -> str:
        return "".join(b["text"] for b in self.recognize_blocks(img_bgr))

    def _infer(self, img_bgr, limit_side=None):
        """跑一次推理。limit_side 临时改 det 的输入下限(改完还原)。

        det 的下限是引擎实例上的一个属性,整段在锁里做 —— 两个线程同时识别也
        不会互相看到对方的临时值。取不到那个属性(库换实现)就按默认跑。
        """
        with self._lock:
            eng = self._get()
            det = getattr(eng, "text_det", None)
            old = getattr(det, "limit_side_len", None)
            if limit_side and old is not None:
                try:
                    det.limit_side_len = int(limit_side)
                except Exception:
                    old = None
            try:
                result, _ = eng(img_bgr, use_cls=False)
            finally:
                if old is not None:
                    det.limit_side_len = old
        return result or []

    def _fallback_to_cpu(self, err):
        """GPU 运行中出错时,重建为 CPU 引擎,保证识别不中断。

        重建也进锁:引擎的创建一律发生在 self._lock 里(_get 同理),两个
        线程同时走到这里也只会排队建一份,不会各建一份再互相覆盖(白占
        一份模型内存)。重建要一两秒,锁外等着的那条本来就是想用引擎的。
        """
        self._log(f"[OCR]GPU 识别异常({err}),自动回退 CPU")
        try:
            from rapidocr_onnxruntime import RapidOCR
            with self._lock:
                self._engine = RapidOCR()
                self.device = "CPU(已回退)"
        except Exception as e:
            self._log(f"[OCR]CPU 引擎重建失败: {e}")

    def recognize_blocks(self, img_bgr, limit_side=None):
        """返回带坐标的文本块列表,按 (y, x) 排序。

        limit_side: 这一次识别的 det 输入下限(默认跟着引擎配置 736)。
        小图会被 det 放大到这个下限再检测 —— 识别准一点,但**很贵**(一块
        860x240 的小区域放大到 2840x736 再跑 det)。只扫游戏界面里的一小块时
        调小它,又快又不掉准头(实测调小后连"朱紫国"都读得更对)。
        """
        try:
            result = self._infer(img_bgr, limit_side)
        except Exception as e:
            if str(self.device).startswith("GPU"):
                self._fallback_to_cpu(e)
                result = self._infer(img_bgr, limit_side)   # CPU 重试一次
            else:
                raise
        if not result:
            return []

        def sort_key(item):
            box = item[0]
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            return (sum(ys) / len(ys), sum(xs) / len(xs))

        blocks = []
        for box, text, conf in sorted(result, key=sort_key):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            blocks.append({
                "text": str(text),
                "x1": int(min(xs)), "y1": int(min(ys)),
                "x2": int(max(xs)), "y2": int(max(ys)),
                "conf": float(conf),
            })
        return blocks


class Screen:
    """mss 截图封装。region 为主显示器内的相对物理像素。

    mss 实例按线程各建一个:GDI DC 是线程亲和资源(实例创建时缓存),
    在 Engine 线程建、拿到工具线程里用会失效,所以谁第一次 grab 谁
    自己惰性建(threading.local 每线程各存一份)。
    """

    def __init__(self):
        self._local = threading.local()

    @property
    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss
            sct = mss.mss()
            self._local.sct = sct
        return sct

    def grab(self, region=None):
        sct = self._sct
        mon = sct.monitors[1]
        if region:
            x1, y1, x2, y2 = region
            box = {"left": mon["left"] + int(x1), "top": mon["top"] + int(y1),
                   "width": max(1, int(x2 - x1)), "height": max(1, int(y2 - y1))}
        else:
            box = dict(mon)
        shot = sct.grab(box)
        arr = np.asarray(shot)          # BGRA
        return np.ascontiguousarray(arr[:, :, :3])  # BGR


