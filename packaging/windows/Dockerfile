# Сборочный образ для Windows-дистрибутива.
#
# PyInstaller не умеет кросс-компиляцию: exe для Windows собирается только
# Windows-интерпретатором. Ставить что-либо на хост правилами проекта запрещено,
# поэтому Windows-Python живёт внутри образа и работает под Wine.
#
# Почему trixie, а не bookworm. В bookworm Wine 8.0, в котором нет
# KERNEL32.CopyFile2 — функции, которой пользуется Python 3.13: pip падает уже
# на `ensurepip`. В trixie Wine 10.0, и та же связка работает. Оба тега явные,
# `latest` не используется нигде.
FROM debian:trixie-slim

# Версия Python зафиксирована. Сумма — SHA-256, опубликованная python.org на
# странице релиза, и проверенная на скачанном файле; при смене версии её нужно
# взять оттуда же, а не вычислить у себя (иначе проверка сверяет файл сам с
# собой и ничего не доказывает).
ARG PYTHON_VERSION=3.13.14
ARG PYTHON_SHA256=c54d9b9bbb8a36e6489363ddd01139707fd781d72f1f9e90c7ec65d0061368e0
ARG PYTHON_DIR="C:\\Python313"
# PyInstaller тоже закреплён: релизный exe должен собираться одинаково сегодня
# и через полгода.
ARG PYINSTALLER_VERSION=6.21.0

# UID/GID того, кто собирает. Wine отказывается работать с префиксом, который
# ему не принадлежит («'/opt/wine' is not owned by you»), — прав на запись мало,
# нужна именно принадлежность. Поэтому префикс создаётся сразу под пользователем
# сборки, а не под root: контейнер потом запускается с этим же UID, и артефакты
# в смонтированном dist/ достаются хозяину репозитория, а не root.
ARG BUILD_UID=1000
ARG BUILD_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    WINEARCH=win64 \
    WINEPREFIX=/opt/wine \
    WINEDEBUG=-all \
    HOME=/opt/wine-home \
    WINEPYTHON="C:\\Python313\\python.exe"

# wine32:i386 нужен не ради 32-битных программ, а потому что установщик Python
# с python.org — 32-битный загрузчик даже в amd64-варианте: без него он
# завершается с ошибкой загрузки модуля.
# xvfb+xauth: установщик и PyInstaller создают окна, а контейнер без дисплея.
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wine \
        wine64 \
        wine32:i386 \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

# Пользователь сборки. `|| true` — на случай, если такой UID/GID в образе уже
# занят системным пользователем: тогда достаточно того, что он существует.
RUN set -eux; \
    groupadd -g "$BUILD_GID" builder || true; \
    useradd -u "$BUILD_UID" -g "$BUILD_GID" -M -s /usr/sbin/nologin builder || true; \
    mkdir -p "$WINEPREFIX" "$HOME" /build; \
    chown -R "$BUILD_UID:$BUILD_GID" "$WINEPREFIX" "$HOME" /build

USER $BUILD_UID:$BUILD_GID

RUN set -eux; \
    xvfb-run -a wineboot --init >/dev/null 2>&1; \
    wineserver -w

# Установщик Python отказывается ставиться на Windows младше 8.1, а Wine по
# умолчанию представляется более старой версией.
RUN set -eux; \
    xvfb-run -a winecfg -v win10 >/dev/null 2>&1; \
    wineserver -w

RUN set -eux; \
    installer=/tmp/python-installer.exe; \
    curl -fsSLo "$installer" \
        "https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-amd64.exe"; \
    echo "${PYTHON_SHA256}  $installer" | sha256sum -c -; \
    xvfb-run -a wine "$installer" /quiet \
        InstallAllUsers=1 \
        TargetDir="${PYTHON_DIR}" \
        Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 \
        AssociateFiles=0 Shortcuts=0 PrependPath=0 CompileAll=0 >/dev/null 2>&1; \
    wineserver -w; \
    rm -f "$installer"; \
    wine "$WINEPYTHON" -VV; \
    wine "$WINEPYTHON" -m pip --version

# Зависимости берутся из pyproject.toml, а не переписываются сюда руками:
# второй список разошёлся бы с первым. Сам пакет `maxub` в образ не ставится —
# исходники монтируются при сборке, и образ не устаревает от каждого коммита.
COPY pyproject.toml /build/pyproject.toml
COPY packaging/windows/requirements.py /build/requirements.py
RUN set -eux; \
    wine "$WINEPYTHON" 'Z:\build\requirements.py' 'Z:\build\pyproject.toml' 'Z:\build\requirements.txt'; \
    wine "$WINEPYTHON" -m pip install --no-cache-dir --no-input \
        "pyinstaller==${PYINSTALLER_VERSION}" -r 'Z:\build\requirements.txt'; \
    wine "$WINEPYTHON" -m PyInstaller --version

COPY --chmod=0755 packaging/windows/build-inside.sh /usr/local/bin/maxub-winbuild

WORKDIR /repo
ENTRYPOINT ["/usr/local/bin/maxub-winbuild"]
