# Сборочный образ для Windows-дистрибутива.
#
# PyInstaller не умеет кросс-компиляцию: exe для Windows собирается только
# Windows-интерпретатором. Ставить что-либо на хост правилами проекта запрещено,
# поэтому Windows-Python живёт внутри образа и работает под Wine.
#
# Почему trixie, а не bookworm. В bookworm Wine 8.0, в котором нет
# KERNEL32.CopyFile2 — функции, которой пользуется Python начиная с 3.13: pip
# падает уже на `ensurepip`. В trixie Wine 10.0, и та же связка работает.
#
# Закреплено по digest, а не по тегу: за `debian:trixie-slim` со временем встаёт
# другой образ (в него въезжают обновления пакетов), и пересборка того же
# коммита через полгода дала бы другой Wine — то есть другую среду, в которой
# собирается exe. Digest снят с фактически скачанного образа
# (`docker buildx imagetools inspect`) и указывает на манифест-список, поэтому
# остаётся верным на любой архитектуре. Тег рядом — чтобы было видно, что
# закреплено.
#
# Чего digest сам по себе не закрепляет: `apt-get install` ниже ходит в
# репозиторий Debian, а тот изменяемый — по тем же именам пакетов через полгода
# встал бы другой Wine, то есть exe собрался бы в другой среде. Поэтому
# источники apt ниже переключаются на снимок репозитория (snapshot.debian.org).
FROM debian@sha256:fac46bff2e02f51425b6e33b0e1169f55dfb053d83511ca28aa50c09fd5ed7a4
# debian:trixie-slim

# Версия Python зафиксирована. Сумма — SHA-256, опубликованная python.org на
# странице релиза, и проверенная на скачанном файле; при смене версии её нужно
# взять оттуда же, а не вычислить у себя (иначе проверка сверяет файл сам с
# собой и ничего не доказывает).
#
# Каталог установки назван по версии, как это делает сам установщик Python. Он
# третье место, где версия прошита, — и путь к интерпретатору ниже собирается из
# него, а не пишется заново: раньше `C:\Python313` стоял в двух местах, и при
# обновлении версии второе молча осталось бы с прежним номером.
ARG PYTHON_VERSION=3.14.6
ARG PYTHON_SHA256=14b3e9a710a3fcf0bd9b55ab6b60412bd91227563f813fc49040cabc0209e0bd
ARG PYTHON_DIR="C:\\Python314"

# Дата снимка репозитория Debian. Взята не на глаз: официальный образ Debian сам
# записывает в /etc/apt/sources.list.d/debian.sources комментарий с тем снимком,
# из которого собран, и у образа под digest выше это ровно 20260713T000000Z.
# Совпадение с digest обязательно в одну сторону: снимок РАНЬШЕ базового слоя
# означал бы, что apt видит версии ниже уже установленных в образе, и
# зависимости перестают разрешаться на ровном месте. При смене digest дату надо
# перечитать из самого образа, а не подставить сегодняшнюю:
#   docker run --rm debian@<digest> cat /etc/apt/sources.list.d/debian.sources
ARG DEBIAN_SNAPSHOT=20260713T000000Z

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
    WINEPYTHON="${PYTHON_DIR}\\python.exe"

# Снимок вместо живого зеркала. Выбран он, а не закрепление номеров версий
# (`apt-get install wine=10.0~repack-6`): номер уезжает из репозитория при первом
# же обновлении, и такая «фиксация» не воспроизводит сборку, а ломает её. Снимок
# же отдаёт тот же индекс пакетов сколько угодно лет спустя, и списку пакетов
# ниже не нужно знать ни одной версии — их выбирает apt, но из застывшего
# состояния репозитория.
#
# Check-Valid-Until: no — Release в снимке подписан датой снимка, а apt считает
# индексы протухшими примерно через неделю. Подпись при этом проверяется как и
# раньше (Signed-By на штатный keyring), отключена только проверка свежести —
# ровно та, которая по смыслу снимка всегда будет провалена.
#
# Чем платим: snapshot.debian.org — один сервис без зеркал, он заметно медленнее
# deb.debian.org и периодически отдаёт 5xx под нагрузкой. Отсюда Acquire::Retries.
#
# Сверка снимка с базовым образом делается здесь, а не «глазами при правке»:
# свой снимок официальный образ записывает комментарием в тот самый файл,
# который мы сейчас перезапишем, — после перезаписи проверять будет нечего.
# Так замена digest без замены даты падает сразу и с объяснением, а не всплывает
# через полгода конфликтом зависимостей.
RUN set -eux; \
    sources=/etc/apt/sources.list.d/debian.sources; \
    for archive in debian debian-security; do \
        grep -q "archive/${archive}/${DEBIAN_SNAPSHOT}" "$sources" || { \
            echo "DEBIAN_SNAPSHOT=${DEBIAN_SNAPSHOT} расходится с базовым образом;" >&2; \
            echo "в образе записано:" >&2; \
            grep -F snapshot.debian.org "$sources" >&2; \
            exit 1; \
        }; \
    done; \
    printf '%s\n' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
        'Suites: trixie trixie-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
        'Suites: trixie-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
        'Check-Valid-Until: no' \
        > "$sources"; \
    printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-snapshot-retries

# wine32:i386 нужен не ради 32-битных программ, а потому что установщик Python
# с python.org — 32-битный загрузчик даже в amd64-варианте: без него он
# завершается с ошибкой загрузки модуля.
# xvfb+xauth: установщик и PyInstaller создают окна, а контейнер без дисплея.
#
# Версий в списке нет намеренно: их закрепляет снимок выше. Дублировать номера
# здесь значило бы завести второй источник правды, который разъедется с датой
# снимка при первой же правке.
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

# Проверка не косметическая. В Wine 8 нет KERNEL32.CopyFile2, которой пользуется
# Python начиная с 3.13, и `ensurepip` падает; сама ошибка всплывает минут через
# двадцать, уже на установке Python. Если дату снимка когда-нибудь передвинут на
# репозиторий, где Wine ниже 10, сборка должна остановиться здесь и назвать
# причину, а не заставлять разбираться в чужом стектрейсе pip.
#
# Сравнение отдано `dpkg --compare-versions`, а не шаблону по строке: версии
# Debian не сравниваются лексикографически. `10.0~repack-6` меньше `10.0`, а
# epoch (`1:10.0`) сдвигает всю строку — самодельная проверка на таких формах
# либо промахивается, либо запрещает валидную версию.
RUN set -eux; \
    wine_version="$(dpkg-query -W -f='${Version}' wine)"; \
    echo "wine ${wine_version}"; \
    dpkg --compare-versions "$wine_version" ge 10 \
        || { echo "нужен Wine >= 10, из снимка встал ${wine_version}" >&2; exit 1; }

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
#
# Версии — из packaging/constraints.txt, оттуда же берётся и версия PyInstaller.
# Раньше она стояла здесь отдельным ARG и вторым числом в
# .github/workflows/windows-build.yml: два значения, обязанные совпадать, но
# ничем не связанные, — правка одного молча оставляла второе. Теперь `pip` зовут
# без номера версии, номер приходит из ограничений.
#
# Путь в переменной — в виде, понятном Windows-pip: он запущен под Wine, и
# POSIX-путь /build он не откроет.
COPY pyproject.toml /build/pyproject.toml
COPY packaging/constraints.txt /build/constraints.txt
COPY packaging/windows/requirements.py /build/requirements.py
RUN set -eux; \
    export PIP_CONSTRAINT='Z:\build\constraints.txt'; \
    export PIP_BUILD_CONSTRAINT='Z:\build\constraints.txt'; \
    wine "$WINEPYTHON" 'Z:\build\requirements.py' 'Z:\build\pyproject.toml' 'Z:\build\requirements.txt'; \
    wine "$WINEPYTHON" -m pip install --no-cache-dir --no-input \
        pyinstaller -r 'Z:\build\requirements.txt'; \
    wine "$WINEPYTHON" -m PyInstaller --version

COPY --chmod=0755 packaging/windows/build-inside.sh /usr/local/bin/maxub-winbuild

WORKDIR /repo
ENTRYPOINT ["/usr/local/bin/maxub-winbuild"]
