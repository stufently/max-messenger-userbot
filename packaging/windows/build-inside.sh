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

ls -l "$DIST_DIR"
