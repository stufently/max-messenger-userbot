#!/usr/bin/env bash
# Проверка собранного образа перед публикацией: демон поднимается, отвечает на
# /health и отдаёт токен.
#
# Почему до публикации, а не после. `docker push` уже выкладывает образ, и
# проверка после него ничего не предотвращает: тег с неисправным образом уже
# виден всем, кто его тянет. Поэтому порядок такой: собрать локально, проверить,
# и только потом отправить те же байты. Пересборки между проверкой и отправкой
# нет — иначе публиковалось бы не то, что проверено.
#
# Проверяются два способа хранить данные, потому что оба описаны в README и
# ведут себя по-разному. Named volume Docker создаёт по правам каталога из
# образа, и владелец получается верным сам. Bind-mount с хоста приносит своего
# владельца, образу он не подчиняется, и запускать надо от того же UID — вот эта
# рекомендация здесь и проверяется, а не только пересказывается в документации.
#
# Запуск: bash packaging/docker/smoke.sh <образ>
set -euo pipefail

image="${1:?укажите образ, например max-userbot:prod}"
name="maxub-smoke-$$"
volume="maxub-smoke-$$"
bind_dir="$(mktemp -d)"

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm -f "$volume" >/dev/null 2>&1 || true
    rm -rf "$bind_dir"
}
trap cleanup EXIT

# Ожидание идёт по состоянию HEALTHCHECK из образа, а не по своему запросу:
# проверяется в том числе то, что healthcheck в образе рабочий — по нему
# оркестраторы решают, жив ли контейнер.
wait_healthy() {
    local deadline=$((SECONDS + 90))
    while [ "$SECONDS" -lt "$deadline" ]; do
        case "$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null)" in
            healthy) return 0 ;;
            unhealthy)
                echo "контейнер признан больным" >&2
                docker logs "$name" >&2 || true
                return 1
                ;;
        esac
        if [ "$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null)" = "false" ]; then
            echo "контейнер завершился, не дождавшись проверки:" >&2
            docker logs "$name" >&2 || true
            return 1
        fi
        sleep 2
    done
    echo "контейнер не стал healthy за 90 секунд" >&2
    docker logs "$name" >&2 || true
    return 1
}

check_token() {
    local token
    # Токен создаётся при первом обращении и лежит в каталоге данных. Пустой
    # ответ означал бы, что каталог недоступен для записи, — то есть ровно ту
    # поломку, из-за которой затевалась вся правка каталога по умолчанию.
    # `|| true`, чтобы отказ команды не увёл скрипт из-за `set -e` до сообщения
    # ниже: пустой ответ и есть тот исход, о котором надо доложить словами.
    token="$(docker exec "$name" maxub token || true)"
    if [ -z "${token//[[:space:]]/}" ]; then
        echo "демон не отдал токен API" >&2
        return 1
    fi
    echo "токен выдан, длина ${#token}"
}

echo "--- named volume, пользователь из образа ---"
docker volume create "$volume" >/dev/null
docker run -d --name "$name" -v "$volume:/data" "$image" >/dev/null
wait_healthy
check_token
docker exec "$name" id
docker rm -f "$name" >/dev/null

echo "--- bind-mount, UID хоста ---"
docker run -d --name "$name" \
    --user "$(id -u):$(id -g)" \
    -v "$bind_dir:/data" \
    "$image" >/dev/null
wait_healthy
check_token
# Файлы на смонтированном каталоге обязаны принадлежать хостовому пользователю:
# иначе он потеряет доступ к своим же данным, как только контейнер их создаст.
if [ ! -O "$bind_dir/api_token" ]; then
    echo "файлы на bind-mount созданы не от имени хостового пользователя" >&2
    ls -ln "$bind_dir" >&2
    exit 1
fi

echo "образ проверен: $image"
