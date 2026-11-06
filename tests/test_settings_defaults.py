"""
Проверки файла значений по умолчанию (py_modules/settings_defaults.json).

Файл — единственный источник умолчаний для бэкенда и фронтенда, поэтому
ошибка в нём бьёт по обоим сразу и молча: опечатка в "left" или строка
вместо числа пройдут и json.load, и сборку фронтенда. Здесь проверяется:

1. каждое значение — правильного типа и из допустимых значений;
2. набор ключей совпадает с набором настроек, которые main.py отдаёт
   фронтенду (get_all_settings): новая настройка без умолчания или
   забытый ключ в JSON — ошибка;
3. в main.py не осталось литералов умолчаний вида get_setting("x", 1000):
   иначе дубли появятся снова;
4. каждый ключ описан в settings_defaults.md (иначе описание отстанет);
5. поля, которые хранят копию настройки (атрибуты Plugin, поля
   ProviderManager и GeminiVisionProvider), начинаются не с литерала;
6. фронтенд (src/settingsDefaults.ts) берёт каждый ключ JSON ровно один раз
   и в поле с соответствующим именем.

main.py не импортируется — ему нужны decky_plugin и evdev, которых нет вне
Деки. Он, как и остальные файлы, разбирается как текст (ast или разрезание
строки): так тест видит ровно код, а не результат его выполнения.
"""

import ast
import json
import os
import time
import unittest
from typing import Any, Callable, Dict, List, Set, Tuple

from settings_defaults import DEFAULTS, DEFAULTS_PATH

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(ROOT, "main.py")
FRONTEND_DEFAULTS_TS = os.path.join(ROOT, "src", "settingsDefaults.ts")
# Файлы, где поля хранят копию пользовательской настройки.
MIRROR_FILES = [
    MAIN_PY,
    os.path.join(ROOT, "py_modules", "providers", "__init__.py"),
    os.path.join(ROOT, "py_modules", "providers", "gemini_vision.py"),
]
# Описание ключей JSON (сам JSON — DEFAULTS_PATH из settings_defaults.py).
DEFAULTS_DOC = os.path.join(ROOT, "documentation", "settings_defaults.md")

# Допустимые значения настроек-перечислений. Списки — копии из кода
# (Settings в src/SettingsContext.tsx, InputMode в src/Input.tsx,
# FontStyleOption в src/fonts/fontPresets.ts, методы в
# src/tabs/TabTranslation.tsx); тест — то место, где их сверяют.
OCR_PROVIDERS = {"chromescreenai", "rapidocr", "ocrspace", "googlecloud", "gemini_vision"}
TRANSLATION_PROVIDERS = {
    "mymemory", "freegoogle", "googlecloud", "yandex",
    "gigachat", "gigachat_plus", "ct2", "opusmt",
}
TEXT_ALIGNMENTS = {"left", "right", "center", "justify"}
FONT_STYLES = {"normal", "bold", "italic", "bolditalic"}
INPUT_MODES = set(range(0, 10))  # InputMode: L4_BUTTON = 0 … L3_R3_COMBO = 9


def is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def is_number(value: Any) -> bool:
    # bool в Python — подкласс int: True прошёл бы как 1, поэтому исключаем явно.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def one_of(allowed: Set[Any]) -> Callable[[Any], bool]:
    return lambda value: not isinstance(value, bool) and value in allowed


def number_between(low: float, high: float) -> Callable[[Any], bool]:
    return lambda value: is_number(value) and low <= value <= high


def int_between(low: int, high: int) -> Callable[[Any], bool]:
    return lambda value: is_int(value) and low <= value <= high


def language_code(value: Any) -> bool:
    # "en", "ru", "zh-CN" и т. п.: непустая строка без пробелов. Полный список
    # языков у каждого метода свой, здесь важно поймать пустое или мусор.
    return isinstance(value, str) and value != "" and " " not in value


def string(value: Any) -> bool:
    return isinstance(value, str)


# Правило для каждого ключа. Диапазоны — как у ползунков в настройках
# (src/tabs/TabControls.tsx, src/tabs/TabTranslation.tsx): значение вне
# ползунка пользователь не смог бы выставить сам.
RULES: Dict[str, Callable[[Any], bool]] = {
    "enabled": is_bool,
    "input_language": language_code,
    "target_language": language_code,
    "input_mode": one_of(INPUT_MODES),
    "hold_time_translate": int_between(100, 3000),   # ползунок 0.1–3 с, хранится в мс
    "hold_time_dismiss": int_between(100, 3000),
    "ocr_provider": one_of(OCR_PROVIDERS),
    "translation_provider": one_of(TRANSLATION_PROVIDERS),
    "gemini_model": lambda value: isinstance(value, str) and value != "",
    "confidence_threshold": number_between(0.0, 1.0),
    "custom_recognition_settings": is_bool,
    "rapidocr_confidence": number_between(0.0, 1.0),
    "rapidocr_box_thresh": number_between(0.1, 1.0),
    "rapidocr_unclip_ratio": number_between(1.0, 3.0),
    "rapidocr_persistent_mode": is_bool,
    "chromescreenai_persistent_mode": is_bool,
    "ct2_persistent_mode": is_bool,
    "pause_game_on_overlay": is_bool,
    "quick_toggle_enabled": is_bool,
    "debug_mode": is_bool,
    "font_scale": number_between(1.0, 3.0),
    "grouping_power": number_between(0.25, 1.0),
    "translated_text_alignment": one_of(TEXT_ALIGNMENTS),
    "translated_text_font_family": string,
    "translated_text_font_style": one_of(FONT_STYLES),
    "hide_identical_translations": is_bool,
    "allow_label_growth": is_bool,
    "translation_cache_enabled": is_bool,
}

# Поля get_all_settings, у которых умолчания в JSON нет — намеренно
# (см. шапку py_modules/settings_defaults.py).
NOT_DEFAULTS = {
    "google_api_key", "google_vision_api_key", "google_translate_api_key",
    "gemini_api_key", "yandex_api_key", "yandex_folder_id",
    "gigachat_api_key", "mymemory_email",
    "use_free_providers",
}

# get_setting с литералом, которые разрешены: это не настройки с умолчанием.
# - key / name: ключи API читаются по имени, "" значит «не введено»;
# - default_ocr_provider_migrated_v1: служебный флаг разовой миграции.
ALLOWED_LITERAL_READS = {"key", "name", "default_ocr_provider_migrated_v1"}


# Поля-копии настроек, чьё имя не совпадает с "_<ключ>": имя поля → ключ JSON.
MIRROR_ALIASES = {
    "_ocr_provider_preference": "ocr_provider",
    "_translation_provider_preference": "translation_provider",
    "_gemini_target_language": "target_language",
    "DEFAULT_MODEL": "gemini_model",
}


def is_literal(node: Any) -> bool:
    """Литерал: 1000, "left", True — и -500 (в ast это UnaryOp над Constant)."""
    if isinstance(node, ast.Constant):
        return True
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and isinstance(node.operand, ast.Constant)
    )


def mirrored_key(field_name: str) -> str:
    """Ключ JSON, копию которого хранит поле, или "" — если поле не копия."""
    if field_name in MIRROR_ALIASES:
        return MIRROR_ALIASES[field_name]
    if field_name.startswith("_") and field_name[1:] in DEFAULTS:
        return field_name[1:]
    return ""


def assigned_names(node: ast.AST) -> list:
    """Имена слева в `x = ...`, `x: T = ...` и `self.x = ...`."""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return []
    names = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
            names.append(target.attr)
    return names


def initial_assignments(tree: ast.Module) -> list:
    """Присваивания, задающие начальное значение: уровень модуля, тело класса, __init__.

    Остальные методы не смотрим: там литерал — это правило, а не умолчание
    (например, «при OCR googlecloud и перевод googlecloud» или разовая
    миграция на конкретный метод).
    """
    statements = list(tree.body)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        statements.extend(node.body)
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == "__init__":
                statements.extend(member.body)
    return statements


def literal_mirror_fields(tree: ast.Module) -> Dict[str, int]:
    """Поля-копии настроек с литералом в начальном значении: имя → строка."""
    found = {}
    for statement in initial_assignments(tree):
        value = getattr(statement, "value", None)
        if not is_literal(value):
            continue
        for name in assigned_names(statement):
            if mirrored_key(name):
                found[name] = statement.lineno
    return found


# Поля фронтенда, чьё имя не получается из ключа простым camelCase: ключ → поле.
FRONTEND_FIELD_ALIASES = {
    "chromescreenai_persistent_mode": "chromeScreenAiPersistentMode",
}


def camel_case(key: str) -> str:
    """hold_time_translate → holdTimeTranslate."""
    first, *rest = key.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def expected_frontend_field(key: str) -> str:
    return FRONTEND_FIELD_ALIASES.get(key, camel_case(key))


def leading_identifier(text: str) -> str:
    """Начало строки до первого символа, который не буква, цифра или "_"."""
    length = 0
    while length < len(text) and (text[length].isalnum() or text[length] == "_"):
        length += 1
    return text[:length]


def frontend_bindings(source: str) -> List[Tuple[str, str]]:
    """Пары (поле, ключ JSON) из строк вида `holdTimeTranslate: rawDefaults.hold_time_translate,`.

    Без регулярного выражения: строку режем по "//" (комментарий не считается
    чтением ключа), поле — текст до первого ":", ключ — имя после
    "rawDefaults.". Блочные комментарии /* */ не разбираются: в этом файле
    их нет. Строка без ":" даёт пустое поле — тест на сверку имён её поймает.
    """
    bindings = []
    for line in source.splitlines():
        code = line.split("//", 1)[0]
        if "rawDefaults." not in code:
            continue
        field = code.split(":", 1)[0].strip() if ":" in code else ""
        for piece in code.split("rawDefaults.")[1:]:
            key = leading_identifier(piece)
            if key:
                bindings.append((field, key))
    return bindings


def unquote_code(cell: str) -> str:
    """"`text`" → "text"; ячейка не в обратных кавычках → ""."""
    cell = cell.strip()
    if len(cell) > 2 and cell.startswith("`") and cell.endswith("`"):
        return cell[1:-1]
    return ""


def documented_values(markdown: str) -> Dict[str, str]:
    """Строки таблиц вида "| `key` | `value` | ...": ключ → значение как текст.

    Без регулярного выражения: строку проще разрезать по "|" и проверить
    ячейки по отдельности.
    """
    values = {}
    for line in markdown.splitlines():
        cells = line.split("|")
        # "| `key` | `value` | ..." → ["", " `key` ", " `value` ", ...]
        if len(cells) < 4 or cells[0].strip() != "":
            continue
        key = unquote_code(cells[1])
        if key:
            values[key] = unquote_code(cells[2])
    return values


def documented_keys(markdown: str) -> Set[str]:
    return set(documented_values(markdown))


def parse_main() -> ast.Module:
    with open(MAIN_PY, encoding="utf-8") as main_file:
        return ast.parse(main_file.read(), filename=MAIN_PY)


def find_function(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"main.py: function {name} not found")


def all_settings_keys(tree: ast.Module) -> Set[str]:
    """Ключи словаря `settings = {...}` в Plugin.get_all_settings."""
    function = find_function(tree, "get_all_settings")
    for node in ast.walk(function):
        is_settings_assignment = (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Dict)
            and any(isinstance(target, ast.Name) and target.id == "settings" for target in node.targets)
        )
        if is_settings_assignment:
            keys = set()
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
            return keys
    raise AssertionError("main.py: `settings = {...}` not found in get_all_settings")


def setting_name(call_argument: ast.AST) -> str:
    """Имя настройки из первого аргумента: строка или имя переменной."""
    if isinstance(call_argument, ast.Constant):
        return str(call_argument.value)
    if isinstance(call_argument, ast.Name):
        return call_argument.id
    return ast.dump(call_argument)


def literal_default_reads(tree: ast.Module) -> Dict[str, int]:
    """get_setting(x, <литерал>) и load_setting(x, <литерал>): имя → строка.

    Умолчание может быть передано и позиционно, и как default=<литерал>.
    """
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        function_name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if function_name not in ("get_setting", "load_setting"):
            continue
        default = node.args[1] if len(node.args) >= 2 else None
        for keyword in node.keywords:
            if keyword.arg == "default":
                default = keyword.value
        if default is not None and is_literal(default):
            found[setting_name(node.args[0])] = node.lineno
    return found


class SettingsDefaultsFileTest(unittest.TestCase):
    def test_file_is_a_json_object(self):
        with open(DEFAULTS_PATH, encoding="utf-8") as defaults_file:
            self.assertIsInstance(json.load(defaults_file), dict)

    def test_every_key_has_a_rule(self):
        # Ключ без правила не проверяется — значит, его нужно сюда добавить.
        self.assertEqual(set(DEFAULTS) - set(RULES), set(), "keys without a rule in RULES")
        self.assertEqual(set(RULES) - set(DEFAULTS), set(), "rules for keys missing from the JSON")

    def test_values_are_valid(self):
        for key, value in DEFAULTS.items():
            with self.subTest(key=key):
                self.assertTrue(RULES[key](value), f"{key}: invalid default {value!r}")

    def test_every_key_is_documented(self):
        with open(DEFAULTS_DOC, encoding="utf-8") as doc_file:
            documented = documented_keys(doc_file.read())
        self.assertEqual(set(DEFAULTS) - documented, set(), "keys without a row in settings_defaults.md")
        self.assertEqual(documented - set(DEFAULTS), set(), "rows in settings_defaults.md for unknown keys")

    def test_documented_values_match_json(self):
        # Колонка «Значение» — копия для читателя; она не должна расходиться
        # с JSON. Значение записано так же, как в JSON: 500, true, "left".
        with open(DEFAULTS_DOC, encoding="utf-8") as doc_file:
            documented = documented_values(doc_file.read())
        mismatched = {
            key: (documented[key], json.dumps(value))
            for key, value in DEFAULTS.items()
            if key in documented and documented[key] != json.dumps(value, ensure_ascii=False)
        }
        self.assertEqual(mismatched, {}, "settings_defaults.md value differs from JSON (key: (doc, json))")

    def test_defaults_are_read_only(self):
        with self.assertRaises(TypeError):
            DEFAULTS["hold_time_translate"] = 1  # type: ignore[index]


class SettingsDefaultsCoverMainTest(unittest.TestCase):
    def setUp(self):
        self.tree = parse_main()

    def test_keys_match_get_all_settings(self):
        sent_to_frontend = all_settings_keys(self.tree) - NOT_DEFAULTS
        self.assertEqual(sent_to_frontend - set(DEFAULTS), set(),
                         "settings sent by get_all_settings without a default in the JSON")
        self.assertEqual(set(DEFAULTS) - sent_to_frontend, set(),
                         "JSON defaults that get_all_settings does not send")

    def test_no_literal_defaults_in_main(self):
        offenders = {
            name: line
            for name, line in literal_default_reads(self.tree).items()
            if name not in ALLOWED_LITERAL_READS
        }
        self.assertEqual(offenders, {}, "use DEFAULTS[...] instead of a literal default (name: line)")


class SettingsDefaultsMirrorsTest(unittest.TestCase):
    def setUp(self):
        with open(FRONTEND_DEFAULTS_TS, encoding="utf-8") as source_file:
            self.bindings = frontend_bindings(source_file.read())

    def test_mirror_fields_have_no_literals(self):
        for path in MIRROR_FILES:
            with self.subTest(file=os.path.relpath(path, ROOT)):
                with open(path, encoding="utf-8") as source_file:
                    tree = ast.parse(source_file.read(), filename=path)
                self.assertEqual(literal_mirror_fields(tree), {},
                                 "use DEFAULTS[...] instead of a literal (field: line)")

    def test_frontend_takes_every_key_once(self):
        uses: Dict[str, int] = {}
        for _field, key in self.bindings:
            uses[key] = uses.get(key, 0) + 1
        self.assertEqual(set(DEFAULTS) - set(uses), set(), "JSON keys the frontend does not read")
        self.assertEqual(set(uses) - set(DEFAULTS), set(), "rawDefaults.<key> for keys missing from the JSON")
        read_twice = {key: count for key, count in uses.items() if count != 1}
        self.assertEqual(read_twice, {}, "each key must be read exactly once (key: count)")

    def test_frontend_field_matches_key(self):
        # Ловит перестановку: holdTimeDismiss: rawDefaults.hold_time_translate.
        wrong = {
            field: key
            for field, key in self.bindings
            if field != expected_frontend_field(key)
        }
        self.assertEqual(wrong, {}, "frontend field bound to another key (field: key)")


class CheckersCatchBadValuesTest(unittest.TestCase):
    """Сами проверки должны ловить плохие значения, а не пропускать всё."""

    def test_bad_values_are_rejected(self):
        bad_values = {
            "translated_text_alignment": "centre",
            "ocr_provider": "google",          # id метода OCR — "googlecloud"
            "hold_time_translate": 0.5,        # секунды вместо миллисекунд
            "hold_time_dismiss": 5000,         # дольше, чем позволяет ползунок (3 с)
            "font_scale": 0.5,                 # ползунок начинается с 1
            "rapidocr_box_thresh": 0.0,        # ползунок начинается с 0.1
            "enabled": "true",
            "input_mode": True,                 # bool не должен проходить как 1
            "confidence_threshold": 60,
            "target_language": "",
        }
        for key, value in bad_values.items():
            with self.subTest(key=key):
                self.assertFalse(RULES[key](value), f"{key}: {value!r} should be rejected")

    def test_documented_keys_parser(self):
        markdown = "| Ключ | Значение |\n|---|---|\n| `enabled` | `true` | что это |\nтекст с `debug_mode`\n"
        self.assertEqual(documented_values(markdown), {"enabled": "true"})

    def test_literal_mirror_field_is_found(self):
        tree = ast.parse(
            "DEFAULT_MODEL = 'x'\n"
            "class Plugin:\n"
            "    _hold_time_translate: int = 1000\n"
            "    _hold_time_dismiss = -500\n"            # отрицательное число — тоже литерал
            "    _screenshot_path = '/tmp'\n"           # не настройка — не ловим
            "    def __init__(self):\n"
            "        self._ocr_provider_preference = 'x'\n"
            "    def configure(self):\n"
            "        self._ocr_provider = 'googlecloud'\n"  # правило, не умолчание
        )
        self.assertEqual(literal_mirror_fields(tree), {
            "DEFAULT_MODEL": 1, "_hold_time_translate": 3, "_hold_time_dismiss": 4,
            "_ocr_provider_preference": 7,
        })

    def test_frontend_bindings_parser(self):
        source = (
            "import rawDefaults from './x.json';\n"
            "    holdTimeTranslate: rawDefaults.hold_time_translate,\n"
            "    inputMode: inputMode('input_mode', rawDefaults.input_mode),\n"
            "    // enabled comes from rawDefaults.enabled\n"   # комментарий — не чтение
            "    enabled: rawDefaults.enabled, // see rawDefaults.debug_mode\n"
        )
        self.assertEqual(frontend_bindings(source), [
            ("holdTimeTranslate", "hold_time_translate"),
            ("inputMode", "input_mode"),
            ("enabled", "enabled"),
        ])

    def test_swapped_frontend_field_is_found(self):
        self.assertEqual(expected_frontend_field("hold_time_dismiss"), "holdTimeDismiss")
        self.assertNotEqual(expected_frontend_field("hold_time_translate"), "holdTimeDismiss")
        self.assertEqual(expected_frontend_field("chromescreenai_persistent_mode"), "chromeScreenAiPersistentMode")

    def test_long_input_is_handled(self):
        # Разбор строки — циклом, без регулярных выражений; длинный вход
        # проверяет, что время линейное и ничего не падает.
        long_name = "a" * 1_000_000
        source = "x: rawDefaults." + long_name + "\n" + "y: rawDefaults.b,\n" * 100_000
        started = time.monotonic()
        bindings = frontend_bindings(source)
        elapsed = time.monotonic() - started
        # Сравниваем короткие значения, а не список целиком: при падении
        # unittest иначе напечатал бы ключ длиной в миллион символов.
        self.assertEqual(len(bindings), 100_001)
        self.assertEqual(len(bindings[0][1]), 1_000_000)
        self.assertEqual(bindings[1], ("y", "b"))
        self.assertLess(elapsed, 5.0)

    def test_literal_default_is_found(self):
        tree = ast.parse(
            'self._settings.get_setting("hold_time_translate", 1000)\n'
            'self._settings.get_setting("debug_mode", default=False)\n'
            'self._settings.get_setting("hold_time_dismiss", -500)\n'
            'self._settings.get_setting("font_scale", DEFAULTS["font_scale"])\n'   # так правильно
        )
        self.assertEqual(literal_default_reads(tree), {
            "hold_time_translate": 1, "debug_mode": 2, "hold_time_dismiss": 3,
        })


if __name__ == "__main__":
    unittest.main()
