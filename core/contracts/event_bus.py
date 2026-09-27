"""`EventBusAdapter`：进程间事件总线的 Protocol 与 DTO（ADR-0044；Phase 11 地基）。

对应 05-plugin.md（基础设施 Adapter，"在首次消费时交付"）与 10-migration.md §2
（"事件总线：至少一次投递 + 幂等消费测试"）。Phase 1～6 不引入 NATS（ADR-0021）；
首个实现是进程内内存总线，NATS 等外部实现以后按同一 Protocol 与 contract suite 接入。

- **至少一次**：消息在被 `ack` 之前可能重复投递；消费者必须按 `message_id` 幂等处理。
- **身份**：`message_id` = 主题、键与规范化载荷的内容哈希——同一内容重复发布得到同一 ID。
- **顺序**：同一主题内按发布顺序投递；跨主题不保证顺序。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field, model_validator

from core.domain.base import ContentHash, Contract, FrozenMapping, content_hash

__all__ = ["BusMessage", "EventBusAdapter", "message_id_for"]


def message_id_for(topic: str, key: str, payload: Any) -> str:
    return content_hash({"topic": topic, "key": key, "payload": payload})


class BusMessage(Contract):
    topic: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    key: str = Field(min_length=1)
    payload: FrozenMapping[str, Any]
    message_id: ContentHash

    @model_validator(mode="after")
    def _identity(self) -> BusMessage:
        payload = self.model_dump(mode="json")["payload"]
        if self.message_id != message_id_for(self.topic, self.key, payload):
            raise ValueError("message_id 必须是主题、键与载荷的内容哈希")
        return self

    @classmethod
    def build(cls, topic: str, key: str, payload: dict[str, Any]) -> BusMessage:
        return cls.model_validate(
            {
                "topic": topic,
                "key": key,
                "payload": payload,
                "message_id": message_id_for(topic, key, payload),
            }
        )


class EventBusAdapter(Protocol):
    """至少一次投递的事件总线（ADR-0044）。"""

    def publish(self, message: BusMessage) -> None: ...

    def poll(self, consumer: str, topic: str, limit: int) -> tuple[BusMessage, ...]:
        """该消费者在该主题上尚未确认的消息（按发布顺序，最多 `limit` 条）。"""
        ...

    def ack(self, consumer: str, topic: str, message_id: str) -> None: ...
