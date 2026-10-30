#!/usr/bin/env python3
"""
Subprocess OCR runner for Chrome Screen AI. Loads libchromescreenai.so via
ctypes, with the same JSON-line protocol used by rapidocr_subprocess.py.

  Oneshot: python chromescreenai_subprocess.py <image> <model_dir> <min_conf>
  Worker:  python chromescreenai_subprocess.py --worker
"""

import ctypes
import json
import os
import sys


_WORKER_MODE = '--worker' in sys.argv

# Debug aid for checking block grouping on a real Deck: appends " [block_id-1]"
# to every recognized line, so the overlay shows which lines were merged.
# The tag also goes to the translator. Keep False in releases.
_DEBUG_SHOW_BLOCK_ID = False


# Skia structs PerformOCR reads. Only populated fields matter.
class _SkColorInfo(ctypes.Structure):
    _fields_ = [
        ('fColorSpace', ctypes.c_void_p),
        ('fColorType', ctypes.c_int32),
        ('fAlphaType', ctypes.c_int32),
    ]


class _SkISize(ctypes.Structure):
    _fields_ = [
        ('fWidth', ctypes.c_int32),
        ('fHeight', ctypes.c_int32),
    ]


class _SkImageInfo(ctypes.Structure):
    _fields_ = [
        ('fColorInfo', _SkColorInfo),
        ('fDimensions', _SkISize),
    ]


class _SkPixmap(ctypes.Structure):
    _fields_ = [
        ('fPixels', ctypes.c_void_p),
        ('fRowBytes', ctypes.c_size_t),
        ('fInfo', _SkImageInfo),
    ]


class _SkBitmap(ctypes.Structure):
    _fields_ = [
        ('fPixelRef', ctypes.c_void_p),
        ('fPixmap', _SkPixmap),
        ('fFlags', ctypes.c_uint32),
    ]


# Callback signatures for SetFileContentFunctions.
_SizeCb = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_char_p)
_ContentCb = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p)


class ScreenAIEngine:
    """Wraps libchromescreenai.so and PerformOCR."""

    def __init__(self, model_dir):
        self.model_dir = model_dir

        # Anchor CFUNCTYPE refs on self; GC would dangle the C callbacks.
        @_SizeCb
        def get_size(p):
            try:
                full = os.path.join(self.model_dir, p.decode('utf-8'))
                return os.path.getsize(full) if os.path.exists(full) else 0
            except Exception:
                return 0

        @_ContentCb
        def get_content(p, n, out_ptr):
            try:
                full = os.path.join(self.model_dir, p.decode('utf-8'))
                if not os.path.exists(full):
                    return
                with open(full, 'rb') as f:
                    data = f.read(n)
                ctypes.memmove(out_ptr, data, len(data))
            except Exception:
                pass

        self._size_cb = get_size
        self._content_cb = get_content

        so_path = os.path.join(self.model_dir, 'libchromescreenai.so')
        if not os.path.exists(so_path):
            raise FileNotFoundError(f"libchromescreenai.so not found at {so_path}")

        dll_mode = getattr(os, 'RTLD_LAZY', 1)
        self.lib = ctypes.CDLL(so_path, mode=dll_mode)

        self.lib.SetFileContentFunctions.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib.SetFileContentFunctions.restype = None
        self.lib.InitOCRUsingCallback.restype = ctypes.c_bool
        self.lib.SetOCRLightMode.argtypes = [ctypes.c_bool]
        self.lib.SetOCRLightMode.restype = None
        self.lib.PerformOCR.argtypes = [
            ctypes.POINTER(_SkBitmap), ctypes.POINTER(ctypes.c_uint32)
        ]
        self.lib.PerformOCR.restype = ctypes.c_void_p
        self.lib.FreeLibraryAllocatedCharArray.argtypes = [ctypes.c_void_p]
        self.lib.FreeLibraryAllocatedCharArray.restype = None
        self.lib.GetMaxImageDimension.restype = ctypes.c_uint32

        self.lib.SetFileContentFunctions(
            ctypes.cast(self._size_cb, ctypes.c_void_p),
            ctypes.cast(self._content_cb, ctypes.c_void_p),
        )

        if not self.lib.InitOCRUsingCallback():
            raise RuntimeError("InitOCRUsingCallback returned False")

        # Light mode is the smaller/faster model; we want full quality.
        self.lib.SetOCRLightMode(False)

        self.max_dim = int(self.lib.GetMaxImageDimension()) or 2048

    def perform(self, rgba_bytes, width, height):
        bitmap = _SkBitmap()
        # Anchor the buffer on self so it isn't GCed during PerformOCR.
        self._buffer_bytes = rgba_bytes
        self._buffer = ctypes.c_char_p(rgba_bytes)
        bitmap.fPixmap.fPixels = ctypes.cast(self._buffer, ctypes.c_void_p)
        bitmap.fPixmap.fRowBytes = width * 4
        bitmap.fPixmap.fInfo.fColorInfo.fColorType = 4   # kRGBA_8888
        bitmap.fPixmap.fInfo.fColorInfo.fAlphaType = 1   # kOpaque
        bitmap.fPixmap.fInfo.fDimensions.fWidth = width
        bitmap.fPixmap.fInfo.fDimensions.fHeight = height

        out_len = ctypes.c_uint32(0)
        ptr = self.lib.PerformOCR(ctypes.byref(bitmap), ctypes.byref(out_len))
        if not ptr:
            return None
        try:
            data = ctypes.string_at(ptr, out_len.value)
        finally:
            self.lib.FreeLibraryAllocatedCharArray(ptr)
        return data


def _load_image_rgba(image_path, max_dim):
    """Returns (rgba, w, h, scale_x, scale_y); scale maps back to original."""
    from PIL import Image

    img = Image.open(image_path)
    orig_w, orig_h = img.size

    if max(orig_w, orig_h) > max_dim:
        factor = min(max_dim / orig_w, max_dim / orig_h)
        new_w = max(1, int(orig_w * factor))
        new_h = max(1, int(orig_h * factor))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    if img.mode != 'RGBA':
        img = img.convert('RGBA')

    w, h = img.size
    scale_x = orig_w / w if w else 1.0
    scale_y = orig_h / h if h else 1.0
    return img.tobytes(), w, h, scale_x, scale_y


def _proto_to_regions(proto_bytes, scale_x, scale_y, min_confidence):
    from providers.chromescreenai_protos.chrome_screen_ai_pb2 import VisualAnnotation

    ann = VisualAnnotation()
    ann.ParseFromString(proto_bytes)

    regions = []
    for line in ann.lines:
        text = (line.utf8_string or "").strip()
        if not text:
            continue
        # Per-line confidence is in [0,1]; drop low-confidence noise.
        confidence = float(line.confidence) if line.confidence else 0.0
        if confidence and confidence < min_confidence:
            continue

        # Treat the proto's rotated rect as axis-aligned; good enough for the overlay.
        bb = line.bounding_box
        x, y, w, h = bb.x, bb.y, bb.width, bb.height
        left = int(round(x * scale_x))
        top = int(round(y * scale_y))
        right = int(round((x + w) * scale_x))
        bottom = int(round((y + h) * scale_y))

        is_dialog = len(text) > 15 or any(p in text for p in '.?!,:;"')
        # Screen AI numbers blocks from 0, and the first block gets 0. The
        # frontend treats ids <= 0 as "no block" (that's how the other
        # providers look), so the first block was never merged. Shift by one
        # to keep every real block id positive.
        block_id = int(line.block_id) + 1
        # Added after is_dialog so the tag doesn't change dialog detection.
        # Written as "[shifted-1]" (first block: "[1-1]") as a reminder that
        # the id is shifted and the model itself sent one less.
        if _DEBUG_SHOW_BLOCK_ID:
            text = f"{text} [{block_id}-1]"
        regions.append({
            "text": text,
            "rect": {"left": left, "top": top, "right": right, "bottom": bottom},
            "confidence": confidence,
            "is_dialog": is_dialog,
            # Layout id from the full (non-light) model, shifted to start at 1.
            "block_id": block_id,
        })
    return regions


def run_oneshot(image_path, model_dir, min_confidence):
    debug = []
    try:
        debug.append(f"Python: {sys.version}")
        engine = ScreenAIEngine(model_dir)
        debug.append(f"max_dim: {engine.max_dim}")

        rgba, w, h, sx, sy = _load_image_rgba(image_path, engine.max_dim)
        debug.append(f"image: {w}x{h} (scale {sx:.3f},{sy:.3f})")

        proto = engine.perform(rgba, w, h)
        if proto is None:
            return {"error": "PerformOCR returned null", "regions": [], "debug": debug}

        regions = _proto_to_regions(proto, sx, sy, min_confidence)
        return {"error": None, "regions": regions, "debug": debug}

    except Exception as e:
        import traceback
        debug.append(traceback.format_exc())
        return {"error": str(e), "regions": [], "debug": debug}


def _write_line(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def worker_main():
    """Long-lived worker. Mirrors rapidocr_subprocess.worker_main protocol.

      >>> {"type":"init","model_dir":"...","min_confidence":0.5}
      <<< {"type":"ready","error":null}

      >>> {"type":"recognize","image_path":"/tmp/x.png"}
      <<< {"type":"result","error":null,"regions":[...]}

      >>> {"type":"shutdown"}
    """
    engine = None
    cfg = {"model_dir": "", "min_confidence": 0.5}

    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                _write_line({"type": "result", "error": f"bad json: {e}", "regions": []})
                continue

            mtype = msg.get("type")

            if mtype == "init":
                try:
                    for k in cfg.keys():
                        if k in msg:
                            cfg[k] = msg[k]
                    engine = ScreenAIEngine(cfg["model_dir"])
                    _write_line({"type": "ready", "error": None})
                except Exception as e:
                    import traceback
                    _write_line({
                        "type": "ready",
                        "error": f"{e}\n{traceback.format_exc()}",
                    })

            elif mtype == "recognize":
                try:
                    if engine is None:
                        _write_line({"type": "result", "error": "not initialized", "regions": []})
                        continue
                    image_path = msg.get("image_path", "")
                    if not image_path or not os.path.exists(image_path):
                        _write_line({"type": "result", "error": "image not found", "regions": []})
                        continue

                    rgba, w, h, sx, sy = _load_image_rgba(image_path, engine.max_dim)
                    proto = engine.perform(rgba, w, h)
                    if proto is None:
                        _write_line({"type": "result", "error": "PerformOCR null", "regions": []})
                        continue
                    regions = _proto_to_regions(proto, sx, sy, cfg["min_confidence"])
                    _write_line({"type": "result", "error": None, "regions": regions})
                except Exception as e:
                    import traceback
                    _write_line({
                        "type": "result",
                        "error": str(e),
                        "regions": [],
                        "trace": traceback.format_exc(),
                    })

            elif mtype == "shutdown":
                break

            else:
                _write_line({"type": "result", "error": f"unknown type: {mtype}", "regions": []})

    except KeyboardInterrupt:
        pass


def main():
    if _WORKER_MODE:
        worker_main()
        return

    if len(sys.argv) < 4:
        print(json.dumps({
            "error": "Usage: chromescreenai_subprocess.py <image> <model_dir> <min_confidence>",
            "regions": [],
        }))
        sys.exit(1)

    image_path = sys.argv[1]
    model_dir = sys.argv[2]
    min_confidence = float(sys.argv[3])
    print(json.dumps(run_oneshot(image_path, model_dir, min_confidence)))


if __name__ == '__main__':
    main()
