"""
Ввод ключей с другого устройства в локальной сети.

Набирать длинный API-ключ экранной клавиатурой с геймпада мучительно.
Поэтому окно ввода ключа на Деке может поднять маленькую веб-страницу
`http://<ip-деки>:<порт>`. Её открывают с телефона или компьютера в той же
сети и вставляют ключ из буфера обмена.

Как устроено:
- сервер работает, только пока на Деке открыто окно ввода ключа и в нём
  нажата кнопка: окно запускает его для одного метода и останавливает,
  когда закрывается;
- страница показывает поля только этого метода (`METHOD_FIELDS`) и
  принимает только их: посторонние настройки, например язык перевода,
  через неё не поменять;
- страница только принимает значения и никогда не показывает уже
  сохранённые: иначе любой в сети мог бы прочитать ключи;
- сам ключ сохраняет вызывающая сторона (`save_field`, в `main.py` это
  обычный `set_setting`). Поэтому чистка ключа от пробелов и маскировка
  в логах работают так же, как при вводе на Деке.

Защиты от чужих в сети нет: ни пароля, ни HTTPS. Это сознательное решение,
чтобы адрес оставался коротким, его легко было набрать с компьютера.
Пользователя предупреждают на Деке: не включать ввод в сетях, которым он
не доверяет.

Сервер — стандартный `http.server` в отдельном потоке, без новых
зависимостей. В процессе Decky Loader есть и `aiohttp`, но это зависимость
загрузчика, а не плагина: её версию плагин не контролирует.
"""

import base64
import html
import logging
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

# Постоянный порт, чтобы адрес был одним и тем же от раза к разу: его
# проще запомнить или оставить открытым во вкладке браузера. Если порт
# занят, берётся любой свободный (см. `_bind`).
PREFERRED_PORT = 8765

# Ключи — это десятки или сотни символов. Тело запроса больше этого —
# не форма с этой страницы; не читаем его, чтобы не держать в памяти
# что попало.
MAX_BODY_BYTES = 16 * 1024

# Сколько секунд соединение может молчать. Без тайм-аута клиент, который
# прислал заголовки и пропал (или открыл соединение и ничего не послал),
# навсегда занимает поток обработчика — остановка сервера такие соединения
# не закрывает. Форме с телефона хватает долей секунды.
REQUEST_TIMEOUT_SECONDS = 15


class SessionClosedError(Exception):
    """Запрос начался при одном запуске страницы, а сохранять пришлось после её остановки."""


@dataclass(frozen=True)
class Field:
    setting_key: str  # имя настройки в бэкенде, как в set_setting
    label: str  # подпись на странице


# Какие поля показывать для каждого метода. Имена методов — те же, что
# в настройках фронтенда (ocrProvider / translationProvider). GigaChat+
# пользуется ключом GigaChat, поэтому отдельной записи для него нет:
# фронтенд передаёт "gigachat".
METHOD_FIELDS: Dict[str, List[Field]] = {
    "googlecloud": [Field("google_api_key", "API-ключ Google Cloud")],
    "gemini_vision": [Field("gemini_api_key", "API-ключ Gemini")],
    "yandex": [
        Field("yandex_api_key", "API-ключ Яндекс Облака"),
        Field("yandex_folder_id", "folder_id"),
    ],
    "gigachat": [Field("gigachat_api_key", "Authorization Key GigaChat")],
    "mymemory": [Field("mymemory_email", "Email для MyMemory")],
}

# Логотипы методов — те же картинки, что в списках методов на Деке. Сборка
# фронтенда кладёт их в dist/assets/ с хешем в имени
# (`yandex-translate-logo-261fe9e5.png`), поэтому здесь только начало имени,
# а файл ищется по шаблону `<начало>-*.png`.
METHOD_LOGOS: Dict[str, str] = {
    "googlecloud": "googlecloud-logo",
    "gemini_vision": "gemini-logo",
    "yandex": "yandex-translate-logo",
    "gigachat": "gigachat-logo",
    "mymemory": "mymemory-logo",
}

# dist/assets плагина: этот файл лежит в <плагин>/py_modules/providers/.
DEFAULT_ASSETS_DIR = Path(__file__).resolve().parents[2] / "dist" / "assets"

METHOD_TITLES: Dict[str, str] = {
    "googlecloud": "Google Cloud",
    "gemini_vision": "Gemini",
    "yandex": "Яндекс Облако",
    "gigachat": "GigaChat",
    "mymemory": "MyMemory",
}

# Сохраняет одно поле и возвращает значение в том виде, в каком оно
# сохранено (после чистки). Ошибку сохранения сообщает исключением.
SaveField = Callable[[str, str], str]


def load_method_logo(method: str, assets_dir: Path) -> Optional[str]:
    """
    Логотип метода как data:-адрес для <img> или None, если файла нет
    (фронтенд не собран — например, в тестах; страница тогда без иконки).

    Картинка встраивается прямо в HTML, а не отдаётся отдельным адресом:
    логотипы весят 3–10 КБ, и серверу не нужен ещё один маршрут.
    """
    prefix = METHOD_LOGOS.get(method)
    if prefix is None:
        return None
    # sorted — чтобы при нескольких файлах (остатки старой сборки) выбор
    # был одним и тем же, а не зависел от порядка в файловой системе.
    candidates = sorted(assets_dir.glob(f"{prefix}-*.png"))
    if not candidates:
        logger.debug(f"No logo for {method} in {assets_dir}")
        return None
    try:
        data = candidates[0].read_bytes()
    except OSError as e:
        logger.warning(f"Failed to read logo {candidates[0]}: {e}")
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def detect_local_ip() -> Optional[str]:
    """
    IP Деки в локальной сети или None, если сети нет.

    Берётся адрес интерфейса, через который ушёл бы пакет наружу: на UDP-сокете
    делаем connect, и система выбирает маршрут. connect у UDP ничего
    не отправляет, поэтому адрес назначения может быть любым маршрутизируемым,
    а сеть может быть без интернета. Так мы не перебираем интерфейсы и не
    показываем адреса docker, которыми с телефона не воспользоваться.

    Ограничение: если весь трафик Деки идёт через VPN (например, Tailscale
    с exit node), маршрут наружу ведёт в VPN, и вернётся адрес VPN, а не
    домашней сети. С телефона в той же Wi-Fi по нему не зайти.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 192.0.2.1 — адрес из документационного диапазона (RFC 5737),
        # настоящего узла за ним нет.
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()

    if address.startswith("127.") or address == "0.0.0.0":
        return None
    return address


class KeyEntryServer:
    """Одна страница ввода ключей одного метода. Потокобезопасен."""

    def __init__(self, save_field: SaveField, assets_dir: Path = DEFAULT_ASSETS_DIR):
        self._save_field = save_field
        self._assets_dir = assets_dir

        # Три замка, у каждого своя работа. Порядок захвата всегда
        # _lifecycle_lock → _save_lock → _lock, поэтому взаимной блокировки нет.
        #
        # _lifecycle_lock — на весь start/stop. Без него остановка, пришедшая
        # посреди запуска (окно на Деке закрыли, пока шёл «Запуск…»), не
        # видела ещё не записанный сервер, а запуск потом записывал его:
        # страница оставалась работать без окна. Два одновременных запуска
        # так же теряли первый сервер, не остановив его.
        self._lifecycle_lock = threading.Lock()
        # _save_lock — на одно сохранение со страницы. stop ждёт его, чтобы
        # сохранение не разорвалось пополам и его результат попал в то, что
        # stop возвращает. Отдельный от _lock, потому что сохранение ждёт
        # цикл событий плагина, а pop_saved вызывается прямо из этого цикла:
        # держи сохранение _lock, цикл и поток ждали бы друг друга.
        self._save_lock = threading.Lock()
        # _lock — короткий, только на чтение и запись полей ниже.
        self._lock = threading.Lock()

        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._method: Optional[str] = None
        # Логотип текущего метода (data:-адрес) или None. Читается один раз
        # при запуске, а не на каждый запрос страницы.
        self._logo: Optional[str] = None
        # Номер запуска. Запрос запоминает его в начале и сохраняет, только
        # если номер не изменился: запрос, начатый до остановки, не должен
        # сохранить ключ после неё — даже если страницу уже перезапустили
        # с тем же методом.
        self._session = 0
        # Что сохранили со страницы и ещё не забрал фронтенд: имя настройки →
        # сохранённое значение. Фронтенд забирает это опросом (`pop_saved`)
        # и при остановке (`stop` возвращает остаток), чтобы окно на Деке
        # показало новый ключ и не затёрло его старым.
        self._saved: Dict[str, str] = {}

    @property
    def port(self) -> Optional[int]:
        with self._lock:
            if self._httpd is None:
                return None
            return self._httpd.server_address[1]

    def start(self, method: str) -> int:
        """
        Запускает страницу для метода и возвращает порт. Уже запущенный
        сервер сначала останавливается: одновременно работает одна страница.
        """
        if method not in METHOD_FIELDS:
            raise ValueError(f"Unknown method for key entry: {method}")

        with self._lifecycle_lock:
            # Остаток прошлого запуска выбрасывается: окно, которое его
            # запускало, уже закрыто и при закрытии забрало остаток само.
            self._stop_locked()

            logo = load_method_logo(method, self._assets_dir)
            httpd = _bind(self._make_handler())
            thread = threading.Thread(
                target=httpd.serve_forever,
                name="key-entry-server",
                daemon=True,  # не держать процесс плагина при выгрузке
            )
            with self._lock:
                self._httpd = httpd
                self._thread = thread
                self._method = method
                self._logo = logo
                self._session += 1
                self._saved = {}
            thread.start()

        port = httpd.server_address[1]
        logger.info(f"Key entry page started for {method} on port {port}")
        return port

    def stop(self) -> Dict[str, str]:
        """
        Останавливает страницу и возвращает сохранённое, что фронтенд ещё
        не забрал опросом: окно закрыли сразу после сохранения на телефоне.
        """
        with self._lifecycle_lock:
            return self._stop_locked()

    def pop_saved(self) -> Dict[str, str]:
        """Сохранённое со страницы с прошлого вызова; после вызова пусто."""
        with self._lock:
            saved = self._saved
            self._saved = {}
            return saved

    # --- внутреннее ---

    def _stop_locked(self) -> Dict[str, str]:
        """stop без захвата _lifecycle_lock: его уже держит вызывающий."""
        # Дождаться идущего сохранения и сменить номер запуска: всё, что
        # придёт дальше, сохранено уже не будет.
        with self._save_lock:
            with self._lock:
                httpd = self._httpd
                thread = self._thread
                leftovers = self._saved
                self._httpd = None
                self._thread = None
                self._method = None
                self._logo = None
                self._session += 1
                self._saved = {}
        if httpd is None:
            return leftovers

        # shutdown ждёт, пока serve_forever выйдет из цикла; server_close
        # освобождает порт, чтобы следующий запуск снова получил 8765.
        # Уже принятые соединения живут в своих потоках до ответа или
        # тайм-аута (REQUEST_TIMEOUT_SECONDS); сохранить они ничего не
        # смогут — номер запуска уже другой.
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        logger.info("Key entry page stopped")
        return leftovers

    def _current_session(self):
        """
        (метод, номер запуска, логотип); метод None, если страница
        остановлена.
        """
        with self._lock:
            return self._method, self._session, self._logo

    def _save(self, values: Dict[str, str], session: int) -> None:
        """
        Сохраняет поля по одному и запоминает их для фронтенда. Если с
        начала запроса страницу остановили, не сохраняет ничего.
        """
        with self._save_lock:
            with self._lock:
                if self._session != session:
                    raise SessionClosedError()
            for setting_key, value in values.items():
                stored = self._save_field(setting_key, value)
                with self._lock:
                    self._saved[setting_key] = stored

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            # StreamRequestHandler закрывает соединение, если клиент молчит
            # дольше этого (см. REQUEST_TIMEOUT_SECONDS).
            timeout = REQUEST_TIMEOUT_SECONDS

            def do_GET(self):
                method, _, logo = server._current_session()
                if method is None or self.path not in ("/", "/index.html"):
                    self._reply(404, _render_message("Страница не найдена."))
                    return
                self._reply(200, _render_form(method, logo))

            def do_POST(self):
                # Номер запуска — до чтения тела: тело может прийти после
                # того, как окно на Деке закрыли.
                method, session, logo = server._current_session()
                if method is None or self.path != "/":
                    self._reply(404, _render_message("Страница не найдена."))
                    return

                body = self._read_body()
                if body is None:
                    return

                values, error = _parse_form(body, METHOD_FIELDS[method])
                if error:
                    self._reply(400, _render_form(method, logo, error=error))
                    return

                try:
                    server._save(values, session)
                except SessionClosedError:
                    self._reply(
                        410,
                        _render_message(
                            "Окно ввода ключа на Деке уже закрыто — ничего не сохранено. "
                            "Откройте его снова и нажмите «Ввести с телефона или компьютера»."
                        ),
                    )
                    return
                except Exception as e:
                    logger.error(f"Key entry save failed: {e}")
                    self._reply(
                        500,
                        _render_form(method, logo, error="Не удалось сохранить. Попробуйте ещё раз."),
                    )
                    return

                saved_labels = [f.label for f in METHOD_FIELDS[method] if f.setting_key in values]
                self._reply(200, _render_saved(method, logo, saved_labels))

            def _read_body(self) -> Optional[bytes]:
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._reply(411, _render_message("Не указан размер запроса."))
                    return None
                if length < 0 or length > MAX_BODY_BYTES:
                    self._reply(413, _render_message("Слишком большой запрос."))
                    return None
                return self.rfile.read(length)

            def _reply(self, status: int, page: str) -> None:
                data = page.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                # Браузер не должен класть страницу с формой ключей в кэш.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                # По умолчанию http.server пишет каждый запрос в stderr.
                # В строке запроса ключей нет (они в теле POST), но и
                # пользы от этих строк в логе плагина мало — только debug.
                logger.debug(f"Key entry page: {self.address_string()} {format % args}")

        return Handler


class _KeyEntryHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind после bind ищет полное имя хоста
        # (socket.getfqdn → обратный DNS-запрос). Нам это имя не нужно, а при
        # плохом DNS запрос занимает секунды — всё это время на Деке
        # висит «Запуск…». Поэтому только bind, без поиска имени.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _bind(handler) -> ThreadingHTTPServer:
    """Сервер на PREFERRED_PORT, а если он занят — на любом свободном порту."""
    try:
        return _KeyEntryHTTPServer(("0.0.0.0", PREFERRED_PORT), handler)
    except OSError as e:
        logger.warning(f"Port {PREFERRED_PORT} is busy ({e}), using a random one")
        return _KeyEntryHTTPServer(("0.0.0.0", 0), handler)


def _parse_form(body: bytes, fields: List[Field]):
    """
    Значения полей метода из тела формы: (значения, текст ошибки или None).

    Пустое поле значит «не менять»: на странице обычно вставляют одно
    значение (например, только folder_id), и остальные не должны стереться.
    Стереть ключ можно на самой Деке.
    """
    try:
        form = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError:
        return {}, "Не удалось прочитать форму."

    allowed = {f.setting_key for f in fields}
    unknown = set(form) - allowed
    if unknown:
        # С нашей страницы такого не прийти: её поля ровно из `fields`.
        return {}, "В форме есть поля, которых у этого метода нет."

    values: Dict[str, str] = {}
    for field in fields:
        submitted = form.get(field.setting_key, [""])
        # Поле с одним именем может прийти несколько раз; берём первое.
        value = submitted[0].strip()
        if value:
            values[field.setting_key] = value

    if not values:
        return {}, "Все поля пустые — нечего сохранять."
    return values, None


# --- HTML ---

_PAGE_STYLE = """
body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
       background: #1b2838; color: #e6e6e6; }
main { max-width: 520px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; margin: 0; }
.head { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
.head img { width: 48px; height: 48px; object-fit: contain; flex-shrink: 0; }
.sub { color: #889; font-size: 14px; margin-top: 2px; }
p { color: #aab; line-height: 1.4; }
label { display: block; margin: 18px 0 6px; font-weight: 600; }
input { width: 100%; box-sizing: border-box; padding: 12px; font-size: 16px;
        border-radius: 6px; border: 1px solid #456; background: #0e1621;
        color: #fff; font-family: monospace; }
button { margin-top: 24px; width: 100%; padding: 14px; font-size: 17px;
         border: 0; border-radius: 6px; background: #4c9be8; color: #fff; }
.error { color: #ff8a8a; }
.ok { color: #7fd97f; }
"""


def _render_page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_PAGE_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _page_title(method: str) -> str:
    return f"Decky Translator RU — {METHOD_TITLES.get(method, method)}"


def _render_head(method: str, logo: Optional[str]) -> str:
    """
    Шапка: логотип, название метода крупно — чтобы сразу было видно, для
    чего ключ, — и подпись с названием плагина.
    """
    title = html.escape(METHOD_TITLES.get(method, method))
    # logo — data:-адрес, который собрали мы сами из base64; в нём нет
    # кавычек и угловых скобок, но экранируем по общему правилу.
    image = f"<img src='{html.escape(logo)}' alt=''>" if logo else ""
    return (
        f"<header class='head'>{image}<div>"
        f"<h1>{title}</h1>"
        "<div class='sub'>Decky Translator RU · ввод ключа</div>"
        "</div></header>"
    )


def _render_form(method: str, logo: Optional[str], error: Optional[str] = None) -> str:
    parts = [
        _render_head(method, logo),
        "<p>Вставьте значения и нажмите «Сохранить». Пустое поле не меняет "
        "то, что уже сохранено на Деке.</p>",
    ]
    if error:
        parts.append(f"<p class='error'>{html.escape(error)}</p>")

    parts.append("<form method='post' action='/'>")
    for field in METHOD_FIELDS[method]:
        name = html.escape(field.setting_key)
        # autocapitalize/autocorrect выключены: телефон иначе сделает первую
        # букву ключа заглавной или «исправит» его, и ключ перестанет
        # подходить. autocomplete=off — чтобы браузер не предлагал и не
        # запоминал ключи как обычный текст формы.
        parts.append(
            f"<label for='{name}'>{html.escape(field.label)}</label>"
            f"<input id='{name}' name='{name}' type='text' autocomplete='off' "
            "autocapitalize='off' autocorrect='off' spellcheck='false'>"
        )
    parts.append("<button type='submit'>Сохранить</button></form>")
    return _render_page(_page_title(method), "".join(parts))


def _render_saved(method: str, logo: Optional[str], saved_labels: List[str]) -> str:
    saved = ", ".join(html.escape(label) for label in saved_labels)
    body = (
        _render_head(method, logo) +
        f"<p class='ok'>Сохранено: {saved}.</p>"
        # Окно на Деке узнаёт о сохранении опросом, через секунду-две.
        "<p>Значения уже сохранены на Деке и через пару секунд появятся "
        "в окне ввода. Эту страницу можно закрыть.</p>"
        "<p><a href='/' style='color:#4c9be8'>Ввести ещё раз</a></p>"
    )
    return _render_page(_page_title(method), body)


def _render_message(text: str) -> str:
    return _render_page("Decky Translator RU", f"<p>{html.escape(text)}</p>")
