#!/usr/bin/env bash
# Сборка exe внутри контейнера: вызывается из build.sh, руками не запускается.
#
# Репозиторий смонтирован в /repo. Пути для Windows-программы получаются через
# `winepath`, а не склейкой строк: так они остаются верными и если каталог
# вынесут за пределы /repo.
set -euo pipefail

DIST_DIR="${MAXUB_DIST_DIR:-/repo/dist/windows}"
# Рабочий каталог PyInstaller — временный, а не в репозитории: он нужен только
# на время сборки и не должен оседать в дереве пользователя.
WORK_DIR="${TMPDIR:-/tmp}/maxub-pyinstaller"
SPEC="/repo/packaging/windows/maxub.spec"
CHECK="/repo/packaging/windows/check_bundle.py"
SMOKE="/repo/packaging/windows/smoke_run.sh"

mkdir -p "$DIST_DIR" "$WORK_DIR"

# Вывод идёт в конвейер, а не в файл: у Wine перенаправление stdout Python-а в
# обычный файл ломает инициализацию потоков («Invalid handle»).
xvfb-run -a wine "$WINEPYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --log-level INFO \
    --distpath "$(winepath -w "$DIST_DIR")" \
    --workpath "$(winepath -w "$WORK_DIR")" \
    "$(winepath -w "$SPEC")" 2>&1

# Ждём, пока Wine отпустит собранные файлы: без этого следующая сборка
# спотыкается о ещё живой процесс.
wineserver -w

# Тот же скрипт, что зовёт CI: собранный exe без библиотеки транспорта выглядит
# исправным и почти столько же весит, а падает только у пользователя. Проверка
# идёт и здесь, а не только на раннере, потому что расхождение между локальной
# сборкой и релизной — ровно та ошибка, которую эта сборка призвана исключать.
wine "$WINEPYTHON" "$(winepath -w "$CHECK")" \
    "$(winepath -w "$DIST_DIR/maxub.exe")" \
    "$(winepath -w "$DIST_DIR/maxubctl.exe")" 2>&1
wineserver -w

# Запуск собранного exe, а не только разбор его содержимого. Тот же скрипт зовёт
# CI — расхождение между локальной сборкой и релизной эта сборка и призвана
# исключать; что проверяется, написано в нём самом.
#
# Только консольный `maxubctl.exe`: `maxub.exe` собран без консоли и без
# аргументов — он поднял бы демон и остался жить.
bash "$SMOKE" xvfb-run -a wine "$(winepath -w "$DIST_DIR/maxubctl.exe")"
wineserver -w

ls -l "$DIST_DIR"
