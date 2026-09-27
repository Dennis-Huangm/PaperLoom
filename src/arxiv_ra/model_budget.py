"""Task-local request limits, including compatibility retries and vision calls."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable


class ModelBudgetExceeded(RuntimeError):
    pass


@dataclass
class ModelBudget:
    limit: int
    used: int = 0
    blocked: int = 0
    on_request: Callable[[int], None] | None = None

    def reserve(self):
        if self.used >= self.limit:
            self.blocked += 1
            raise ModelBudgetExceeded(f"本轮模型请求已达到 {self.limit} 次上限，可手动继续未完成步骤")
        self.used += 1
        if self.on_request:
            self.on_request(self.used)


_BUDGET: ContextVar[ModelBudget | None] = ContextVar("model_request_budget", default=None)


def current_model_budget():
    return _BUDGET.get()


@contextmanager
def model_request_budget(limit: int | None, on_request=None):
    budget = ModelBudget(limit, on_request=on_request) if limit is not None else None
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)
