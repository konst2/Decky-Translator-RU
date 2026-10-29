# providers/opusmt_downloader.py
# Manages the Opus-MT en→ru CTranslate2 model download, storage, and lifecycle

import hashlib
import logging
import os
import shutil
import threading
from typing import Dict

logger = logging.getLogger(__name__)

# Ready-made CTranslate2 int8 conversion of Helsinki-NLP opus-mt-en-ru (Marian).
# A narrow en→ru model: ~82 MB instead of the 1.4 GB NLLB all-pairs model.
# Third-party conversion, so the revision is pinned and every file is checked
# against a known sha256 (docs/PLAN.md, "model pack" keeps the same idea).
HF_REPO = "manancode/opus-mt-en-ru-ctranslate2-android"
HF_REVISION = "8693481face72247976d2a7da8610ed4c75d9e83"
HF_BASE_URL = f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}/{{filename}}"
MODEL_DIR_NAME = "opus-mt-en-ru"
MODEL_APPROX_MB = 82

# filename -> (size in bytes, sha256)
MODEL_FILES = {
    "model.bin": (
        78276475,
        "f5ae1da30bcb61e877c549be8fb5a434193893eb97509ccd0b954465f9a8f80c",
    ),
    "source.spm": (
        802781,
        "16bebef1389a0b8ab452772c4e35b9e605e5713f8ac7baa71ca701394eaa086d",
    ),
    "target.spm": (
        1080169,
        "745998e51ba5b058e38b7ac7765c25c43ed5c1c39cc92b27163b9b2e323c9d7c",
    ),
    "shared_vocabulary.json": (
        2300269,
        "bf1812383ed402163a2ed1b553e175e7db96a54ed68b6c15566018fdf945ed99",
    ),
    "config.json": (
        223,
        "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
    ),
}

REQUIRED_MODEL_FILES = ["model.bin", "source.spm", "target.spm"]
OPTIONAL_MODEL_FILES = ["shared_vocabulary.json", "config.json"]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OpusMtDownloader:
    """Manages the single Opus-MT en→ru model download and storage."""

    def __init__(self, models_dir: str):
        self._models_dir = models_dir
        self._downloading = False
        self._download_progress = 0.0
        self._download_error = None
        self._download_cancel = False
        self._download_thread = None
        self._lock = threading.Lock()

        os.makedirs(models_dir, exist_ok=True)
        self._cleanup_partial_downloads()

    def _cleanup_partial_downloads(self):
        """Remove any leftover .downloading directories from interrupted downloads."""
        try:
            for item in os.listdir(self._models_dir):
                if item.endswith(".downloading"):
                    path = os.path.join(self._models_dir, item)
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                        logger.info(f"Cleaned up partial download: {item}")
        except Exception as e:
            logger.error(f"Error cleaning up partial downloads: {e}")

    def get_model_dir(self) -> str:
        return os.path.join(self._models_dir, MODEL_DIR_NAME)

    def is_model_downloaded(self) -> bool:
        model_dir = self.get_model_dir()
        if not os.path.isdir(model_dir):
            return False
        return all(
            os.path.exists(os.path.join(model_dir, f))
            for f in REQUIRED_MODEL_FILES
        )

    def get_model_size(self) -> int:
        model_dir = self.get_model_dir()
        if not os.path.isdir(model_dir):
            return 0
        total = 0
        for f in os.listdir(model_dir):
            fp = os.path.join(model_dir, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
        return total

    def get_approx_size_mb(self) -> int:
        return MODEL_APPROX_MB

    def start_download(self) -> bool:
        with self._lock:
            if self._downloading:
                return False
            self._downloading = True
            self._download_progress = 0.0
            self._download_error = None
            self._download_cancel = False

        self._download_thread = threading.Thread(
            target=self._download_model, daemon=True
        )
        self._download_thread.start()
        return True

    def _download_model(self):
        """Download model files from HuggingFace. Runs in background thread."""
        import requests

        temp_dir = os.path.join(self._models_dir, f"{MODEL_DIR_NAME}.downloading")
        final_dir = self.get_model_dir()

        try:
            os.makedirs(temp_dir, exist_ok=True)

            all_files = REQUIRED_MODEL_FILES + OPTIONAL_MODEL_FILES
            total_expected = sum(MODEL_FILES[f][0] for f in all_files)
            downloaded_total = 0

            for filename in all_files:
                if self._download_cancel:
                    raise Exception("Загрузка отменена")

                url = HF_BASE_URL.format(filename=filename)
                dest = os.path.join(temp_dir, filename)
                is_required = filename in REQUIRED_MODEL_FILES
                expected_size, expected_sha = MODEL_FILES[filename]

                try:
                    resp = requests.get(url, stream=True, timeout=30)

                    if resp.status_code == 404:
                        if is_required:
                            raise Exception(f"Файл не найден на сервере: {filename}")
                        downloaded_total += expected_size
                        continue
                    elif resp.status_code == 429:
                        raise Exception("Слишком много запросов. Попробуйте позже.")
                    elif resp.status_code != 200:
                        if is_required:
                            raise Exception(f"Ошибка загрузки {filename} (код {resp.status_code})")
                        downloaded_total += expected_size
                        continue

                    with open(dest, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            if self._download_cancel:
                                raise Exception("Загрузка отменена")
                            f.write(chunk)
                            downloaded_total += len(chunk)
                            with self._lock:
                                self._download_progress = min(
                                    downloaded_total / total_expected, 0.99
                                )

                except requests.ConnectionError:
                    raise Exception("Сервер моделей недоступен. Проверьте подключение к интернету.")
                except requests.Timeout:
                    raise Exception("Время ожидания загрузки истекло. Проверьте подключение к интернету.")

                if not os.path.exists(dest):
                    if is_required:
                        raise Exception(f"После загрузки не найден файл: {filename}")
                    continue
                if os.path.getsize(dest) != expected_size:
                    raise Exception(f"Размер файла {filename} не совпадает с ожидаемым")
                if _sha256(dest) != expected_sha:
                    raise Exception(f"Контрольная сумма файла {filename} не совпадает")

            for req_file in REQUIRED_MODEL_FILES:
                if not os.path.exists(os.path.join(temp_dir, req_file)):
                    raise Exception(f"После загрузки не найден файл: {req_file}")

            if os.path.exists(final_dir):
                shutil.rmtree(final_dir)
            os.rename(temp_dir, final_dir)

            with self._lock:
                self._download_progress = 1.0
                self._downloading = False

            logger.info("Opus-MT en→ru model downloaded successfully")

        except Exception as e:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

            with self._lock:
                self._download_error = str(e)
                self._downloading = False

            logger.error(f"Opus-MT model download failed: {e}")

    def cancel_download(self):
        with self._lock:
            self._download_cancel = True

    def get_download_status(self) -> Dict:
        with self._lock:
            return {
                "downloading": self._downloading,
                "progress": self._download_progress,
                "error": self._download_error,
            }

    def clear_download_error(self):
        with self._lock:
            self._download_error = None

    def delete_model(self) -> bool:
        model_dir = self.get_model_dir()
        if os.path.isdir(model_dir):
            try:
                shutil.rmtree(model_dir)
                logger.info("Deleted Opus-MT en→ru model")
                return True
            except Exception as e:
                logger.error(f"Error deleting Opus-MT model: {e}")
                return False
        return True
