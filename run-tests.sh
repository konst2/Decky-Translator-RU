#!/usr/bin/env bash
# Юнит-тесты бэкенда (tests/) на Python 3.11.
#
# Почему 3.11: main.py и py_modules/providers/ работают внутри процесса
# Decky Loader, а это Python 3.11 (воркеры OCR и перевода — отдельно, на 3.13).
# На более новом Python тесты не заметят синтаксис или функции стандартной
# библиотеки, которых в 3.11 нет.
#
# Python берётся из pyenv. Сам скрипт его не ставит: молча устанавливать
# интерпретатор при прогоне тестов было бы неожиданно. Поставить один раз:
#   pyenv install 3.11
#
# Вместо pyenv интерпретатор можно указать явно: TEST_PYTHON=/путь/к/python3.11.
# Так скрипт запускается в CI — там pyenv нет, Python ставит actions/setup-python.
# Версия всё равно проверяется: без проверки окружение от другого Python
# молча пересоздалось бы, и тесты прошли бы не на 3.11.
#
# Зависимости — в отдельном окружении .venv-tests/ (в .gitignore). Ставится
# только то, что импортируют тесты: requests и urllib3 (их импортирует пакет
# providers), с версиями из requirements.txt. Тяжёлые onnxruntime и
# ctranslate2 работают в процессах-воркерах, здесь они не нужны.
#
# Запуск:
#   ./run-tests.sh                 # все тесты
#   ./run-tests.sh -v              # остальные аргументы уходят в unittest
#   rm -rf .venv-tests             # пересоздать окружение с нуля
#   TEST_PYTHON=python3.11 ./run-tests.sh   # без pyenv (так делает CI)
#
# В CI запускается перед сборкой (.github/workflows/build.yml, задача test):
# упавшие тесты останавливают и dev-сборку, и релиз.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

REQUIRED_PYTHON="3.11"
VENV_DIR="$ROOT/.venv-tests"
# Что сейчас стоит в окружении. Поменялись версии в requirements.txt —
# зависимости ставятся заново.
DEPS_STAMP="$VENV_DIR/.installed-deps"

fail() {
    echo "error: $*" >&2
    exit 1
}

find_python() {
    command -v pyenv >/dev/null 2>&1 \
        || fail "pyenv not found. Install it (brew install pyenv), then: pyenv install $REQUIRED_PYTHON"

    # Последняя установленная 3.11.x; пусто, если не установлено ни одной.
    # Без флага -q: в pyenv 2.8 его нет, и «-q» принимался за номер версии.
    local version
    version="$(pyenv latest "$REQUIRED_PYTHON" 2>/dev/null || true)"
    [ -n "$version" ] \
        || fail "Python $REQUIRED_PYTHON is not installed in pyenv. Install it: pyenv install $REQUIRED_PYTHON"

    local python="$(pyenv root)/versions/$version/bin/python3"
    [ -x "$python" ] || fail "pyenv reports $version, but $python is missing"
    echo "$python"
}

# Зависимости тестов с версиями из requirements.txt — чтобы версии жили
# в одном месте. Простое совпадение начала строки «имя==версия».
pinned_deps() {
    grep -E '^(requests|urllib3)==' requirements.txt \
        || fail "requests/urllib3 pins not found in requirements.txt"
}

# Интерпретатор из TEST_PYTHON: проверить, что он есть и что это 3.11.
use_explicit_python() {
    local python="$1"
    command -v "$python" >/dev/null 2>&1 || fail "TEST_PYTHON=$python: not found"

    local version
    version="$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    [ "$version" = "$REQUIRED_PYTHON" ] \
        || fail "TEST_PYTHON=$python is Python $version, need $REQUIRED_PYTHON"
    echo "$python"
}

venv_python_version() {
    "$VENV_DIR/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true
}

prepare_venv() {
    local python="$1"
    local deps
    deps="$(pinned_deps)"

    # Окружение от другого Python (например, созданное до установки 3.11)
    # проверяло бы не тот интерпретатор — пересоздаём.
    if [ -d "$VENV_DIR" ] && [ "$(venv_python_version)" != "$REQUIRED_PYTHON" ]; then
        echo "Rebuilding $VENV_DIR: it is not Python $REQUIRED_PYTHON"
        rm -rf "$VENV_DIR"
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating $VENV_DIR with $("$python" --version)"
        "$python" -m venv "$VENV_DIR"
    fi

    if [ ! -f "$DEPS_STAMP" ] || [ "$(cat "$DEPS_STAMP")" != "$deps" ]; then
        echo "Installing test dependencies:" $deps
        # shellcheck disable=SC2086  # каждый пакет — отдельный аргумент pip
        "$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check $deps
        echo "$deps" > "$DEPS_STAMP"
    fi
}

main() {
    local python
    if [ -n "${TEST_PYTHON:-}" ]; then
        python="$(use_explicit_python "$TEST_PYTHON")"
    else
        python="$(find_python)"
    fi
    prepare_venv "$python"

    echo "Running tests on $("$VENV_DIR/bin/python" --version)"
    # py_modules в пути импорта: тесты импортируют пакет providers так же,
    # как main.py на Деке. -s tests добавляет в путь и tests/ (для fakes.py).
    PYTHONPATH="$ROOT/py_modules" "$VENV_DIR/bin/python" -m unittest discover -s tests "$@"
}

main "$@"
