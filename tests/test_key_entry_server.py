"""
Страница ввода ключей с другого устройства (py_modules/providers/key_entry_server.py).

Сервер поднимается по-настоящему и слушает сеть; запросы идут на
127.0.0.1. Сохранение подменено словарём: проверяется, что и с какими
значениями страница передаёт на сохранение, а не сам set_setting.
"""

import base64
import http.client
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from providers import key_entry_server
from pathlib import Path

from providers.key_entry_server import (
    DEFAULT_ASSETS_DIR,
    MAX_BODY_BYTES,
    METHOD_FIELDS,
    PREFERRED_PORT,
    KeyEntryServer,
    load_method_logo,
)


def preferred_port_is_free() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Как у сервера: соединения прошлых тестов в TIME_WAIT порт не занимают.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", PREFERRED_PORT))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def key_entry_threads():
    return [t for t in threading.enumerate() if t.name == "key-entry-server" and t.is_alive()]


class FakeStorage:
    """Подмена set_setting: запоминает сохранённое, может «сломаться»."""

    def __init__(self):
        self.saved = {}
        self.fail = False

    def save(self, key, value):
        if self.fail:
            raise RuntimeError("storage is broken")
        # Как настоящий set_setting, возвращаем значение в «очищенном» виде —
        # отличимом от присланного, чтобы проверить, что фронтенд получит
        # именно сохранённое.
        stored = f"stored:{value}"
        self.saved[key] = stored
        return stored


class KeyEntryServerTest(unittest.TestCase):

    def setUp(self):
        self.storage = FakeStorage()
        self.server = KeyEntryServer(self.storage.save)

    def tearDown(self):
        self.server.stop()

    # --- помощники ---

    def request(self, method, path="/", body=None, headers=None):
        port = self.server.port
        self.assertIsNotNone(port, "server is not running")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def post_form(self, fields):
        body = urlencode(fields).encode("utf-8")
        return self.request(
            "POST",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    # --- страница ---

    def test_page_shows_only_fields_of_selected_method(self):
        self.server.start("yandex")

        status, page = self.request("GET")

        self.assertEqual(status, 200)
        self.assertIn("name='yandex_api_key'", page)
        self.assertIn("name='yandex_folder_id'", page)
        self.assertNotIn("gigachat_api_key", page)
        self.assertNotIn("google_api_key", page)

    def test_page_never_shows_saved_values(self):
        # Иначе любой в сети прочитал бы ключ, открыв страницу.
        self.server.start("gigachat")
        self.post_form({"gigachat_api_key": "secret-key-123"})

        status, page = self.request("GET")

        self.assertEqual(status, 200)
        self.assertNotIn("secret-key-123", page)

    def test_unknown_path_is_404(self):
        self.server.start("gigachat")

        status, _ = self.request("GET", "/settings")

        self.assertEqual(status, 404)

    def test_phone_keyboard_does_not_alter_key(self):
        # Телефон по умолчанию делает первую букву заглавной — ключ сломается.
        self.server.start("gigachat")

        _, page = self.request("GET")

        self.assertIn("autocapitalize='off'", page)
        self.assertIn("autocorrect='off'", page)

    # --- сохранение ---

    def test_saves_fields_trimmed_and_reports_them_once(self):
        self.server.start("yandex")

        status, page = self.post_form({
            "yandex_api_key": "  AQVN-key  ",
            "yandex_folder_id": "b1g-folder",
        })

        self.assertEqual(status, 200)
        self.assertIn("Сохранено", page)
        self.assertEqual(self.storage.saved, {
            "yandex_api_key": "stored:AQVN-key",
            "yandex_folder_id": "stored:b1g-folder",
        })
        # Фронтенд получает сохранённое значение, а не присланное.
        self.assertEqual(self.server.pop_saved(), {
            "yandex_api_key": "stored:AQVN-key",
            "yandex_folder_id": "stored:b1g-folder",
        })
        # Второй опрос пустой: окно на Деке не подставит то же самое снова.
        self.assertEqual(self.server.pop_saved(), {})

    def test_empty_field_keeps_saved_value(self):
        # Вставили только folder_id — ключ не должен стереться.
        self.server.start("yandex")

        status, _ = self.post_form({"yandex_api_key": "", "yandex_folder_id": "b1g"})

        self.assertEqual(status, 200)
        self.assertEqual(self.storage.saved, {"yandex_folder_id": "stored:b1g"})

    def test_all_fields_empty_is_rejected(self):
        self.server.start("yandex")

        status, page = self.post_form({"yandex_api_key": " ", "yandex_folder_id": ""})

        self.assertEqual(status, 400)
        self.assertIn("Все поля пустые", page)
        self.assertEqual(self.storage.saved, {})

    def test_field_of_other_setting_is_rejected(self):
        # Через страницу ключа нельзя поменять, например, язык перевода.
        self.server.start("gigachat")

        status, _ = self.post_form({
            "gigachat_api_key": "key",
            "target_language": "de",
        })

        self.assertEqual(status, 400)
        self.assertEqual(self.storage.saved, {})

    def test_field_of_other_method_is_rejected(self):
        self.server.start("gigachat")

        status, _ = self.post_form({"yandex_api_key": "key"})

        self.assertEqual(status, 400)
        self.assertEqual(self.storage.saved, {})

    def test_storage_error_is_reported_on_page(self):
        self.server.start("gigachat")
        self.storage.fail = True

        status, page = self.post_form({"gigachat_api_key": "key"})

        self.assertEqual(status, 500)
        self.assertIn("Не удалось сохранить", page)
        self.assertEqual(self.server.pop_saved(), {})

    def test_too_large_body_is_not_read(self):
        self.server.start("gigachat")
        body = b"gigachat_api_key=" + b"a" * MAX_BODY_BYTES

        status, _ = self.request("POST", body=body)

        self.assertEqual(status, 413)
        self.assertEqual(self.storage.saved, {})

    def test_long_key_within_limit_is_saved(self):
        # Ключи бывают длинными (у GigaChat — Base64 от Client ID и Secret).
        self.server.start("gigachat")
        long_key = "k" * 4000

        status, _ = self.post_form({"gigachat_api_key": long_key})

        self.assertEqual(status, 200)
        self.assertEqual(self.storage.saved["gigachat_api_key"], f"stored:{long_key}")

    # --- запуск и остановка ---

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(ValueError):
            self.server.start("freegoogle")
        self.assertIsNone(self.server.port)

    def test_restart_switches_method(self):
        self.server.start("yandex")
        self.server.start("gigachat")

        _, page = self.request("GET")

        self.assertIn("gigachat_api_key", page)
        self.assertNotIn("yandex_api_key", page)

    def test_stop_frees_port_for_next_start(self):
        # Адрес должен оставаться тем же от раза к разу.
        if not preferred_port_is_free():
            self.skipTest(f"port {PREFERRED_PORT} is already busy on this machine")
        first_port = self.server.start("gigachat")
        self.server.stop()

        second_port = self.server.start("gigachat")

        self.assertEqual(first_port, second_port)

    def test_stop_closes_page_and_is_idempotent(self):
        port = self.server.start("gigachat")
        self.server.stop()
        self.server.stop()

        self.assertIsNone(self.server.port)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_busy_preferred_port_falls_back_to_free_one(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Без SO_REUSEADDR занять порт не дадут соединения предыдущих
        # тестов, ещё висящие в TIME_WAIT. Слушающий сокет на том же порту
        # это всё равно не разрешает, так что сервер порт не получит.
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            try:
                blocker.bind(("0.0.0.0", PREFERRED_PORT))
            except OSError:
                self.skipTest(f"port {PREFERRED_PORT} is already busy on this machine")
            blocker.listen(1)

            port = self.server.start("gigachat")

            self.assertNotEqual(port, PREFERRED_PORT)
            status, _ = self.request("GET")
            self.assertEqual(status, 200)
        finally:
            blocker.close()


class KeyEntryLogoTest(unittest.TestCase):
    """Логотип метода в шапке страницы."""

    def setUp(self):
        self.assets = tempfile.TemporaryDirectory()
        self.assets_dir = Path(self.assets.name)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.stop()
        self.assets.cleanup()

    def get_page(self, method):
        self.server = KeyEntryServer(FakeStorage().save, assets_dir=self.assets_dir)
        self.server.start(method)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            conn.request("GET", "/")
            return conn.getresponse().read().decode("utf-8")
        finally:
            conn.close()

    def test_logo_found_by_name_with_build_hash(self):
        # Сборка добавляет к имени хеш — файл ищется по началу имени.
        logo_bytes = b"\x89PNG fake yandex logo"
        (self.assets_dir / "yandex-translate-logo-261fe9e5.png").write_bytes(logo_bytes)
        expected = "data:image/png;base64," + base64.b64encode(logo_bytes).decode()

        page = self.get_page("yandex")

        self.assertIn(f"<img src='{expected}'", page)
        self.assertIn("<h1>Яндекс Облако</h1>", page)
        self.assertIn("Decky Translator RU", page)

    def test_page_works_without_logo(self):
        # Фронтенд не собран — страница без иконки, но рабочая.
        page = self.get_page("gigachat")

        self.assertNotIn("<img", page)
        self.assertIn("<h1>GigaChat</h1>", page)
        self.assertIn("name='gigachat_api_key'", page)

    def test_logo_of_other_method_is_not_taken(self):
        # googlecloud-logo и googletranslate-logo начинаются одинаково.
        (self.assets_dir / "googletranslate-logo-2f4d4d7c.png").write_bytes(b"x")

        self.assertIsNone(load_method_logo("googlecloud", self.assets_dir))

    def test_every_method_has_logo_in_real_build(self):
        # Сверка с настоящей сборкой: переименовали логотип или добавили
        # метод без него — тест скажет об этом.
        if not DEFAULT_ASSETS_DIR.is_dir():
            self.skipTest("frontend is not built (no dist/assets)")
        missing = [m for m in METHOD_FIELDS if load_method_logo(m, DEFAULT_ASSETS_DIR) is None]

        self.assertEqual(missing, [])


class KeyEntryLifecycleTest(unittest.TestCase):
    """Гонки запуска и остановки и запросы, пережившие остановку."""

    def setUp(self):
        self.storage = FakeStorage()
        self.server = KeyEntryServer(self.storage.save)

    def tearDown(self):
        self.server.stop()

    def slow_bind(self, delay=0.3):
        """Подменяет _bind: запуск сервера длится delay секунд."""
        real_bind = key_entry_server._bind

        def bind_slowly(handler):
            time.sleep(delay)
            return real_bind(handler)

        return mock.patch.object(key_entry_server, "_bind", bind_slowly)

    def test_stop_during_start_leaves_no_server(self):
        # Окно на Деке закрыли, пока шёл «Запуск…»: остановка пришла
        # посреди запуска. Страница не должна остаться работать.
        with self.slow_bind():
            starter = threading.Thread(target=self.server.start, args=("gigachat",))
            starter.start()
            time.sleep(0.05)  # запуск уже внутри, сервер ещё не записан
            self.server.stop()
            starter.join()

        self.assertIsNone(self.server.port)
        self.assertEqual(key_entry_threads(), [])

    def test_concurrent_starts_leave_one_server(self):
        # Два запуска сразу (двойное нажатие): после остановки не должно
        # остаться ни одного сервера — раньше первый терялся без остановки.
        with self.slow_bind(0.1):
            starters = [
                threading.Thread(target=self.server.start, args=("gigachat",))
                for _ in range(2)
            ]
            for starter in starters:
                starter.start()
            for starter in starters:
                starter.join()

        self.server.stop()

        self.assertEqual(key_entry_threads(), [])

    def send_headers_only(self, body: bytes) -> socket.socket:
        """POST, у которого пока отправлены только заголовки."""
        conn = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        conn.sendall(
            b"POST / HTTP/1.1\r\nHost: deck\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        time.sleep(0.1)  # обработчик принял запрос и ждёт тело
        return conn

    def finish_request(self, conn: socket.socket, body: bytes) -> str:
        conn.sendall(body)
        response = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            response += chunk
        conn.close()
        return response.decode("utf-8", errors="replace")

    def test_request_started_before_stop_saves_nothing(self):
        # Заголовки пришли, окно на Деке закрыли, потом пришло тело.
        self.server.start("gigachat")
        body = b"gigachat_api_key=LATEKEY"
        conn = self.send_headers_only(body)

        self.server.stop()
        response = self.finish_request(conn, body)

        self.assertIn(" 410 ", response.split("\r\n")[0])
        self.assertEqual(self.storage.saved, {})
        self.assertEqual(self.server.pop_saved(), {})

    def test_request_from_previous_start_of_same_method_saves_nothing(self):
        # Перезапуск с тем же методом не должен «оживить» старый запрос:
        # сверяется номер запуска, а не метод.
        self.server.start("gigachat")
        body = b"gigachat_api_key=OLDKEY"
        conn = self.send_headers_only(body)

        self.server.start("gigachat")
        response = self.finish_request(conn, body)

        self.assertIn(" 410 ", response.split("\r\n")[0])
        self.assertEqual(self.storage.saved, {})

    def test_stop_returns_values_not_polled_yet(self):
        # Окно закрыли сразу после сохранения на телефоне, до опроса.
        self.server.start("yandex")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request(
            "POST", "/",
            body=urlencode({"yandex_api_key": "K", "yandex_folder_id": ""}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

        leftovers = self.server.stop()

        self.assertEqual(leftovers, {"yandex_api_key": "stored:K"})

    def test_stop_waits_for_save_in_progress(self):
        # Сохранение, начатое до остановки, доходит до конца и попадает
        # в то, что вернула остановка, — а не теряется на полпути.
        save_started = threading.Event()

        def slow_save(key, value):
            save_started.set()
            time.sleep(0.3)
            return self.storage.save(key, value)

        self.server = KeyEntryServer(slow_save)
        self.server.start("gigachat")
        poster = threading.Thread(
            target=lambda: urlopen_post(self.server.port, {"gigachat_api_key": "K"})
        )
        poster.start()
        self.assertTrue(save_started.wait(5))

        leftovers = self.server.stop()
        poster.join()

        self.assertEqual(leftovers, {"gigachat_api_key": "stored:K"})

    def test_silent_connection_is_closed_by_timeout(self):
        # Иначе соединение без запроса навсегда занимает поток обработчика.
        with mock.patch.object(key_entry_server, "REQUEST_TIMEOUT_SECONDS", 0.5):
            self.server.start("gigachat")
        conn = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        started = time.monotonic()

        data = conn.recv(1024)  # сервер закрывает соединение — recv вернёт b""
        elapsed = time.monotonic() - started
        conn.close()

        self.assertEqual(data, b"")
        self.assertLess(elapsed, 3)


def urlopen_post(port, fields):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST", "/",
            body=urlencode(fields),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return conn.getresponse().status
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
