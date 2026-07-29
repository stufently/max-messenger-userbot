#!/usr/bin/env bash
# Собирает Windows-дистрибутив из Docker и кладёт результат в dist/windows/.
#
# Запускается от обычного пользователя (`bash packaging/windows/build.sh`).
# `sudo` не нужен и вреден: контейнер пишет в смонтированный репозиторий, и от
# root артефакты достались бы root — пользователь потерял бы к ним доступ.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="$REPO_ROOT/packaging/windows/Dockerfile"

# Тег фиксированный и говорит, из чего образ собран: `latest` со временем
# начинает означать что угодно. Дата в теге — снимок репозитория Debian, из
# которого приходит системная часть: Wine и его окружение теперь такой же
# закреплённый вход сборки, как версия Python.
#
# Числа для тега вычитываются из Dockerfile, а не пишутся здесь ещё раз. Иначе
# это два одинаковых значения в разных файлах, обязанных совпадать и ничем не
# связанных: правка снимка оставила бы тег с прежней датой, и образ называл бы
# содержимое, которого в нём нет. Мажор Wine в теге не вычисляется — его
# гарантирует проверка в самом Dockerfile, которая не даст собрать образ с
# версией ниже.
dockerfile_arg() {
    local value
    value="$(sed -n "s/^ARG $1=//p" "$DOCKERFILE")"
    if [ -z "$value" ]; then
        echo "build.sh: в Dockerfile не найден ARG $1" >&2
        return 1
    fi
    printf '%s\n' "$value"
}

PYTHON_VERSION="$(dockerfile_arg PYTHON_VERSION)"
DEBIAN_SNAPSHOT="$(dockerfile_arg DEBIAN_SNAPSHOT)"
IMAGE="${MAXUB_WINBUILD_IMAGE:-maxub-winbuild:py${PYTHON_VERSION}-wine10-deb${DEBIAN_SNAPSHOT%%T*}}"
DIST_DIR="$REPO_ROOT/dist/windows"

if [ "$(id -u)" -eq 0 ]; then
    echo "build.sh: запускать нужно от обычного пользователя, не от root" >&2
    exit 1
fi

echo "==> сборочный образ $IMAGE"
# UID/GID передаются в образ: префикс Wine должен принадлежать тому, кто потом
# запускает контейнер, иначе Wine откажется его открывать.
docker build \
    --build-arg "BUILD_UID=$(id -u)" \
    --build-arg "BUILD_GID=$(id -g)" \
    -t "$IMAGE" \
    -f "$REPO_ROOT/packaging/windows/Dockerfile" \
    "$REPO_ROOT"

mkdir -p "$DIST_DIR"

echo "==> сборка exe"
docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$REPO_ROOT:/repo" \
    --workdir /repo \
    "$IMAGE"

echo "==> готово: $DIST_DIR"
ls -l "$DIST_DIR"
