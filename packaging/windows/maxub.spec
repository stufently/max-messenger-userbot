# -*- mode: python ; coding: utf-8 -*-
"""Спека PyInstaller: автономная сборка под Windows.

Одна спека собирает два exe и используется обоими способами сборки — Wine в
контейнере (`build.sh`) и нативный раннер GitHub Actions. Дублировать список
скрытых импортов в двух местах нельзя: они разъедутся, и релизный артефакт
перестанет совпадать с тем, что проверяли локально.

Собирается:

* ``maxub.exe``    — оконный (``console=False``): пользователь запускает его
  двойным щелчком, консольное окно на экране ему незачем;
* ``maxubctl.exe`` — консольный: машинный клиент, весь смысл которого в выводе
  в stdout, а у оконного процесса Windows потоков вывода нет.
"""

import re
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 — SPECPATH даёт PyInstaller
SRC_DIR = REPO_ROOT / "src"
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = PYPROJECT["project"]["version"]
DESCRIPTION = PYPROJECT["project"]["description"]

# --- статика веб-интерфейса --------------------------------------------------
# Каталог кладётся в сборку по тому же относительному пути, по которому его
# ищет `api/routes/web.py` (`.../api/static` рядом с модулем): у распакованного
# onefile `__file__` указывает внутрь временного каталога, и относительный путь
# сходится сам. Каталог может отсутствовать (веб-панель ещё дописывается) —
# сборка от этого падать не должна, exe просто останется без страницы.
STATIC_DIR = SRC_DIR / "maxub" / "api" / "static"
datas = []
if STATIC_DIR.is_dir():
    datas.append((str(STATIC_DIR), "maxub/api/static"))
else:
    print("maxub.spec: каталог статики не найден, собираю без веб-интерфейса", file=sys.stderr)

# --- скрытые импорты ---------------------------------------------------------
# Проверено эмпирически на собранном exe, а не взято из памяти: uvicorn
# подбирает реализации цикла, протокола и lifespan строкой в рантайме
# (`uvicorn.loops.auto` и соседние), поэтому анализатор их не видит и без
# явного перечисления exe падает на старте с ModuleNotFoundError.
# Раньше рядом с `websockets.auto` не стояло ни одной реализации, и это было
# верно: websockets и wsproto в зависимостях не было, авто-выбор отдавал
# заглушку, а панель обходилась без сокета. Теперь websockets приезжает
# транзитивно вместе с транспортом, и `auto` берёт настоящую реализацию.
# Именно `websockets_sansio_impl`, а не `websockets_impl`: в uvicorn 0.52
# первая ветка `auto` выбирает sansio, а старый impl остаётся для тех, кто
# просит его явно. Прописан по той же причине, что и `http.h11_impl` рядом:
# анализатор доходит до него сам (импорт в `auto` статический, просто внутри
# `try`), но держать выбор реализации явным дешевле, чем однажды выяснять из
# отчёта пользователя, что перестал.
hiddenimports = [
    "uvicorn.lifespan.off",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_sansio_impl",
    "uvicorn.logging",
    # pydantic v2 собирает модели через компилируемое ядро и обращается к
    # `pydantic.deprecated.*` из сгенерированного кода — эти ветки анализатор
    # тоже не проходит.
    "pydantic.deprecated.decorator",
    "pydantic_settings",
]

# Транспорты подключаются по имени из настройки `MAXUB_TRANSPORT`, и статически
# на них никто не ссылается. Список собирается по файлам, а не перечисляется
# руками: новый транспорт иначе молча не попал бы в exe.
TRANSPORT_DIR = SRC_DIR / "maxub" / "transport"
hiddenimports += [
    f"maxub.transport.{path.stem}"
    for path in sorted(TRANSPORT_DIR.glob("*.py"))
    if path.stem != "__init__"
]

# --- библиотека транспорта ---------------------------------------------------
# Сами модули адаптера выше — это ещё не транспорт: за ними стоит библиотека
# `maxapi-python` (импортируется как `pymax`), и без неё выбор
# `MAXUB_TRANSPORT=pymax` в exe упирается в совет «выполните pip install»,
# который внутри one-file выполнить нечем. В pyproject она объявлена
# необязательным extra — это верно для установки из PyPI, где заглушка остаётся
# рабочим режимом, но в собранный exe необязательных зависимостей не бывает:
# либо лежит сразу, либо не появится никогда.
#
# Берётся пакет целиком, а не по следам анализатора. Проверено на собранном
# exe: анализатор сам дотягивается до 146 модулей из 148 — не хватает
# `pymax.auth.email` (пустой файл) и `pymax.auth.service` (на неё в библиотеке
# никто не ссылается), то есть сегодня недостача безвредна. Но зависеть тут от
# трассировки нельзя: адаптер работает с библиотекой через `Any`, её внутренний
# API меняется без предупреждения, и достаточно одной ветки, до которой
# анализатор не дошёл, чтобы exe упал `ModuleNotFoundError` уже у пользователя —
# в единственном месте, куда нельзя доставить недостающее. Цена полноты — два
# лишних модуля.
PYMAX_PACKAGE = "pymax"
pymax_modules = collect_submodules(PYMAX_PACKAGE)
if not pymax_modules:
    # Молчаливая сборка «без транспорта» — ровно та поломка, ради которой этот
    # блок написан: exe получился бы внешне исправным и бесполезным при первом
    # же переключении транспорта. Пусть падает здесь, где ещё можно поставить
    # extra, а не у пользователя.
    #
    # Пустой список, а не `try/except`: проверено на PyInstaller 6.21 в самом
    # сборочном образе — `collect_submodules` для ненайденного пакета возвращает
    # `[]` и печатает предупреждение, а не бросает `ModuleNotFoundError`.
    raise SystemExit(
        f"maxub.spec: библиотека {PYMAX_PACKAGE!r} (пакет maxapi-python) не найдена "
        "или не импортируется в окружении сборки — поставьте её через extra: "
        "pip install '.[pymax]'"
    )
hiddenimports += pymax_modules

# Ненужное в exe: тянут за собой десятки мегабайт и в юзерботе не участвуют.
excludes = ["tkinter", "test", "unittest", "pydoc_data", "setuptools", "pip"]


def analyze(entry: str) -> Analysis:  # noqa: F821 — Analysis даёт PyInstaller
    return Analysis(  # noqa: F821
        [str(REPO_ROOT / "packaging" / "windows" / "entry" / entry)],
        pathex=[str(SRC_DIR)],
        binaries=[],
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
    )


# --- метаданные exe ----------------------------------------------------------
# Версия берётся из pyproject.toml: второй список правды разошёлся бы с первым
# при первом же релизе.
def version_resource(name: str, description: str) -> str:
    # Ресурс версии в Windows — ровно четыре числа, а в pyproject может стоять
    # и «0.2.0rc1»: берём числа, остальное отбрасываем.
    parts = [int(x) for x in re.findall(r"\d+", VERSION)] + [0, 0, 0, 0]
    quad = tuple(parts[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '041904b0',
        [StringStruct('FileDescription', {description!r}),
         StringStruct('FileVersion', {VERSION!r}),
         StringStruct('InternalName', {name!r}),
         StringStruct('OriginalFilename', {name + '.exe'!r}),
         StringStruct('ProductName', 'MAX Userbot'),
         StringStruct('ProductVersion', {VERSION!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [1049, 1200])])
  ]
)
"""
    path = Path(workpath) / f"version_{name}.txt"  # noqa: F821 — workpath даёт PyInstaller
    path.write_text(text, encoding="utf-8")
    return str(path)


def build_exe(analysis: Analysis, name: str, console: bool, description: str) -> None:  # noqa: F821
    """Собирает один exe.

    Onefile выражается тем, что бинарники и данные передаются прямо в `EXE` и
    нет шага `COLLECT`: пользователь скачивает ровно один файл.
    """
    pyz = PYZ(analysis.pure)  # noqa: F821
    EXE(  # noqa: F821
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name=name,
        console=console,
        # UPX выключен: выигрыш в размере не стоит ложных срабатываний
        # антивирусов, которыми упакованные exe известны.
        upx=False,
        strip=False,
        bootloader_ignore_signals=False,
        debug=False,
        disable_windowed_traceback=False,
        version=version_resource(name, description),
    )


build_exe(analyze("maxub_gui.py"), "maxub", console=False, description=DESCRIPTION)
build_exe(analyze("maxubctl_console.py"), "maxubctl", console=True, description="MAX Userbot CLI")
