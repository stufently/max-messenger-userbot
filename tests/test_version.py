"""Номер версии в коде совпадает с версией установленного пакета.

Проверка появилась не из любви к порядку. Номер жил в трёх местах — pyproject,
`maxub.__version__` и заголовок FastAPI, — и при выпуске 0.2.0 два из них
остались на 0.1.0; нашлось это на ревью, то есть случайно. Теперь в коде номер
один, а этот тест связывает его с pyproject: версия установленного пакета берётся
из метаданных, которые hatchling собирает ровно из `[project].version`.
"""

from __future__ import annotations

from importlib import metadata

import maxub
from maxub.api.app import create_app
from maxub.config import Settings


def test_code_version_matches_package_metadata() -> None:
    assert maxub.__version__ == metadata.version("max-userbot")


def test_api_reports_the_same_version(settings: Settings) -> None:
    """Заголовок OpenAPI — то место, где расхождение увидит пользователь API."""
    assert create_app(settings).version == maxub.__version__
