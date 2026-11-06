"""
Значения настроек по умолчанию — единственный источник для бэкенда и фронтенда.

Значения лежат в settings_defaults.json рядом с этим модулем, что значит
каждый ключ — в documentation/settings_defaults.md (в корне репозитория).
JSON выбран
потому, что его без посредников читают обе стороны: здесь — json.load, во
фронтенде — import при сборке (rollup встраивает значения в dist, сам файл
в рантайме фронтенду не нужен). Раньше умолчания были переписаны вручную
в нескольких местах на Python и TypeScript и расходились.

Ключи — имена настроек бэкенда (snake_case), как в файле настроек плагина.
Фронтенд переводит их в свои camelCase-имена в src/settingsDefaults.ts.

Чего здесь нет и почему:
- ключи API и email MyMemory: это данные пользователя, а не умолчания;
  пустая строка у них значит «не введено»;
- use_free_providers: устаревшее поле, вычисляется из ocr_provider.

Правила:
- Новая настройка — сначала значение сюда, потом чтение через DEFAULTS.
  Литерал умолчания в main.py (get_setting("x", 1000)) тест не пропустит.
- Допустимые значения и типы проверяет tests/test_settings_defaults.py.

Почему JSON лежит в py_modules/:
- Decky CLI (и сборка магазина Decky, которая идёт через него) упаковывает
  py_modules/ целиком, а незнакомые каталоги вроде settings/ — нет. Файл
  в py_modules/ попадает в плагин при любой сборке, без наших шагов.
- py_modules/ почти целиком в .gitignore (туда ставятся зависимости);
  этот файл и загрузчик внесены туда исключениями.
- Загрузчик лежит в py_modules/, а не в providers/: это настройки плагина,
  а не провайдеров.
"""

import json
import os
from types import MappingProxyType
from typing import Any, Mapping

# Файл рядом с этим модулем: так путь одинаков в репозитории и на Деке.
DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_defaults.json")


def _load_defaults() -> Mapping[str, Any]:
    with open(DEFAULTS_PATH, encoding="utf-8") as defaults_file:
        loaded = json.load(defaults_file)
    if not isinstance(loaded, dict):
        raise ValueError(f"{DEFAULTS_PATH}: expected a JSON object, got {type(loaded).__name__}")
    # Только для чтения: умолчания общие на весь процесс, и случайная запись
    # (DEFAULTS["x"] = ...) молча поменяла бы их для всех, кто читает после.
    return MappingProxyType(loaded)


# Ошибка чтения здесь — это сломанная сборка, а не ситуация для обработки:
# без умолчаний плагин не может стартовать корректно, поэтому исключение
# не глушим — оно окажется в логе Decky с понятным путём к файлу.
DEFAULTS: Mapping[str, Any] = _load_defaults()
