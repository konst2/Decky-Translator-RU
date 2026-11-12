# providers/capabilities.py
# Optional engines that may or may not be shipped in the current build.

import importlib.util

# RapidOCR needs OpenCV and onnxruntime, which are ~90 MB of the dependencies
# archive. The default OCR method is Chrome Screen AI, so the engine is not
# part of the base build; the provider code stays. To restore it, install
# requirements-rapidocr.txt (see requirements.txt).
RAPIDOCR_UNAVAILABLE_MESSAGE = (
    "RapidOCR недоступен в этой сборке плагина. "
    "Выберите другой метод распознавания"
)


def _installed(package: str) -> bool:
    """True only for a real package, not a leftover empty directory."""
    spec = importlib.util.find_spec(package)
    return spec is not None and spec.origin is not None


RAPIDOCR_AVAILABLE = _installed("rapidocr")
