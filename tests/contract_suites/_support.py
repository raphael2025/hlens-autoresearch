"""contract suite 的共用断言工具。

不用裸 `assert`：`python -O` 会删除它，而契约检查不能因运行参数而消失。所有失败都是
`ContractSuiteFailure`，因此"suite 判定实现不合规"与"suite 自身崩溃"可以区分。
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from core.domain.base import Contract

__all__ = [
    "ContractSuiteFailure",
    "call_ok",
    "expect_error",
    "require",
    "revalidated",
]


class ContractSuiteFailure(AssertionError):
    """被测实现违反了 Adapter 契约。"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractSuiteFailure(message)


def call_ok[T](what: str, action: Callable[[], T]) -> T:
    """执行一个契约要求成功的调用；任何异常都记为契约违反。"""
    try:
        return action()
    except ContractSuiteFailure:
        raise
    except Exception as exc:
        raise ContractSuiteFailure(f"{what} 应当成功，却抛出 {type(exc).__name__}: {exc}") from exc


def expect_error[E: BaseException](error: type[E], what: str, action: Callable[[], object]) -> E:
    """执行一个契约要求以 `error` 失败的调用；成功或抛出其它异常都记为契约违反。"""
    try:
        action()
    except error as exc:
        return exc
    except Exception as exc:
        raise ContractSuiteFailure(
            f"{what} 应当抛出 {error.__name__}，实际抛出 {type(exc).__name__}: {exc}"
        ) from exc
    raise ContractSuiteFailure(f"{what} 应当抛出 {error.__name__}，实际成功")


def revalidated[M: Contract](model: type[M], value: object, what: str) -> M:
    """实现返回的 DTO 必须恰好是 `model`，且经 JSON 往返重新校验后不变。

    挡住以 `model_construct` 绕过校验伪造的返回值，以及子类冒充。
    """
    if type(value) is not model or not isinstance(value, model):
        raise ContractSuiteFailure(f"{what} 必须是 {model.__name__}，实际为 {type(value).__name__}")
    try:
        again = model.model_validate_json(value.model_dump_json())
    except (ValidationError, ValueError, TypeError) as exc:
        raise ContractSuiteFailure(f"{what} 无法通过契约校验：{exc}") from exc
    require(again == value, f"{what} 经重新校验后发生变化")
    return again
