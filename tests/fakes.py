"""
Поддельный провайдер для тестов readiness.py.

Отвечает ровно на то, что читает readiness: local_model(),
is_model_downloaded(), is_available(). Настоящие провайдеры для этого
не годятся — им нужны файлы моделей, Python 3.13 и сеть.
"""

from typing import List, Optional, Tuple

from providers.base import LocalModel


class FakeProvider:
    def __init__(
        self,
        model: Optional[LocalModel] = None,
        downloaded: bool = True,
        available: bool = True,
    ):
        # model=None — облачный метод (как у настоящих облачных провайдеров).
        self.model = model
        self.downloaded = downloaded
        self.available = available
        # С какими языками readiness спрашивал is_available — чтобы проверить,
        # что предпроверка задаёт тот же вопрос, что и настоящий запуск.
        self.availability_questions: List[Tuple[str, ...]] = []

    def local_model(self) -> Optional[LocalModel]:
        return self.model

    def is_model_downloaded(self) -> bool:
        return self.downloaded

    def is_available(self, *languages: str) -> bool:
        self.availability_questions.append(languages)
        return self.available


def cloud() -> FakeProvider:
    return FakeProvider()


def local(label: str, size_mb: int, downloaded: bool = True, available: bool = True) -> FakeProvider:
    return FakeProvider(LocalModel(label, size_mb), downloaded=downloaded, available=available)
