#!/usr/bin/env bash
# Собирает Windows-дистрибутив из Docker и кладёт результат в dist/windows/.
#
# Запускается от обычного пользователя (`bash packaging/windows/build.sh`).
# `sudo` не нужен и вреден: контейнер пишет в смонтированный репозиторий, и от
# root артефакты достались бы root — пользователь потерял бы к ним доступ.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Тег фиксированный и говорит, из чего образ собран: `latest` со временем
# начинает означать что угодно.
IMAGE="${MAXUB_WINBUILD_IMAGE:-maxub-winbuild:py3.13.14-wine10}"
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
