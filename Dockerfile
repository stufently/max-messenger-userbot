# Базовый образ закреплён по digest, а не по тегу: `python:3.12-slim` — движущаяся
# ссылка, за ней стоит то, что выложили сегодня, и пересборка того же коммита
# через полгода дала бы другой интерпретатор и другой набор системных библиотек.
# Digest снят с фактически скачанного образа (`docker buildx imagetools inspect`)
# и указывает на манифест-список, то есть остаётся верным и на amd64, и на arm64.
# Тег рядом — чтобы при обновлении было видно, что именно закреплено.
FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
# python:3.12-slim

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
RUN PIP_CONSTRAINT=/app/packaging/constraints.txt \
    PIP_BUILD_CONSTRAINT=/app/packaging/constraints.txt \
    pip install --no-cache-dir .[dev]

# Каталог данных монтируется снаружи; владельца задаёт `user:` в compose.
RUN mkdir -p /data

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8765/health', timeout=2).status_code==200 else 1)"

CMD ["maxub", "daemon"]
