# Базовый образ закреплён по digest, а не по тегу: `python:3.14-slim` — движущаяся
# ссылка, за ней стоит то, что выложили сегодня, и пересборка того же коммита
# через полгода дала бы другой интерпретатор и другой набор системных библиотек.
# Digest снят с фактически скачанного образа (`docker buildx imagetools inspect`)
# и указывает на манифест-список, то есть остаётся верным и на amd64, и на arm64.
# Тег рядом — чтобы при обновлении было видно, что именно закреплено.
FROM python@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6
# python:3.14-slim (3.14.6)

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MAXUB_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY packaging/constraints.txt ./packaging/constraints.txt
COPY src ./src

# Состав зависимостей по-прежнему берётся из pyproject, версии — из файла
# ограничений (почему они разделены, объяснено в самом файле). Переменные, а не
# флаг `--constraint`: колесо проекта собирается hatchling-ом в отдельном
# изолированном окружении, куда флаг не доходит, и версия самого hatchling
# осталась бы плавающей. Вторая переменная — запас на будущее, подробности там
# же. Пути абсолютные: pip окружения сборки работает из своего временного
# каталога.
#
# Почему `pymax` ставится в этот же образ, а не отдельной целью сборки.
# Транспорт выбирается переменной `MAXUB_TRANSPORT` в рантайме, то есть решение
# принимает тот, кто запускает контейнер, — а образ к тому моменту уже собран.
# Без библиотеки внутри выбор `pymax` упирался бы в совет «выполните pip
# install», невыполнимый в неизменяемом контейнере. Отдельная цель (`stub` и
# `full`) означала бы два образа, которые обязаны совпадать во всём остальном:
# CI гонял бы тесты в одном, а пользователь запускал другой, и расхождение
# всплыло бы только в проде. Цена одного образа — десяток лишних пакетов
# (aiohttp, websockets, zstandard) и пара мегабайт; цена двух — вторая матрица
# сборки и проверок. `dev` тут же по той же причине: в этом образе CI гоняет
# весь набор тестов.
RUN PIP_CONSTRAINT=/app/packaging/constraints.txt \
    PIP_BUILD_CONSTRAINT=/app/packaging/constraints.txt \
    pip install --no-cache-dir .[dev,pymax]

# Каталог данных монтируется снаружи; владельца задаёт `user:` в compose.
RUN mkdir -p /data

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8765/health', timeout=2).status_code==200 else 1)"

CMD ["maxub", "daemon"]
