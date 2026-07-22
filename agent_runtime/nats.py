from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable, List, Optional, Union

from .events import EventEnvelope, parse_event_envelope

DEFAULT_NATS_STREAM_NAME = "AGENT_RUNTIME_EVENTS"
DEFAULT_NATS_STREAM_SUBJECT = "agent-runtime.events.>"
DEFAULT_NATS_SUBJECT_TEMPLATE = "agent-runtime.events.{app_id}.{run_id}.{event_type}"


class RetryEvent(Exception):
    """Ask the consumer to negatively acknowledge and redeliver an event."""


class DropEvent(Exception):
    """Acknowledge an event without running it again."""


NATSEventHandler = Callable[
    [EventEnvelope],
    Union[None, Awaitable[None]],
]


@dataclass
class NATSConsumerConfig:
    app_id: str
    url: Optional[str] = None
    client_name: str = "agent-runtime-sdk-consumer"
    jetstream: Any = None
    connection: Any = None
    stream: str = DEFAULT_NATS_STREAM_NAME
    stream_subjects: List[str] = field(default_factory=lambda: [DEFAULT_NATS_STREAM_SUBJECT])
    durable: str = "agent-runtime-projection"
    subject: Optional[str] = None
    fetch_batch: int = 8
    max_wait: float = 5.0
    ack_wait: float = 45.0
    max_deliver: int = 5
    backoff: List[float] = field(default_factory=lambda: [5.0, 15.0, 45.0, 120.0, 300.0])
    max_ack_pending: int = 64
    ensure_stream: Optional[bool] = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("agent_runtime.nats"))

    def __post_init__(self) -> None:
        self.app_id = self.app_id.strip()
        self.url = (self.url or "").strip() or None
        self.client_name = self.client_name.strip() or "agent-runtime-sdk-consumer"
        self.stream = self.stream.strip() or DEFAULT_NATS_STREAM_NAME
        self.stream_subjects = self.stream_subjects or [DEFAULT_NATS_STREAM_SUBJECT]
        self.durable = self.durable.strip() or "agent-runtime-projection"
        self.subject = (self.subject or "").strip() or app_event_subject(self.app_id)
        self.fetch_batch = self.fetch_batch if self.fetch_batch > 0 else 8
        self.max_wait = self.max_wait if self.max_wait > 0 else 5.0
        self.ack_wait = self.ack_wait if self.ack_wait > 0 else 45.0
        self.max_deliver = self.max_deliver if self.max_deliver > 0 else 5
        self.backoff = self.backoff or [5.0, 15.0, 45.0, 120.0, 300.0]
        self.max_ack_pending = self.max_ack_pending if self.max_ack_pending > 0 else 64
        if self.ensure_stream is None:
            self.ensure_stream = self.jetstream is None and self.connection is None and self.url is not None


class NATSConsumer:
    def __init__(self, config: NATSConsumerConfig) -> None:
        self.config = config

    async def run(self, handler: NATSEventHandler) -> None:
        if handler is None:
            raise ValueError("event handler is required")
        jetstream, connection, owns_connection = await self._jetstream()
        try:
            if self.config.ensure_stream:
                await ensure_nats_stream(jetstream, self.config.stream, self.config.stream_subjects)
            subscription = await self._subscribe(jetstream)
            while True:
                try:
                    messages = await subscription.fetch(
                        batch=self.config.fetch_batch,
                        timeout=self.config.max_wait,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if _is_timeout_error(exc):
                        continue
                    self.config.logger.exception(
                        "agent runtime NATS consumer fetch failed",
                        extra={"stream": self.config.stream, "durable": self.config.durable},
                    )
                    await asyncio.sleep(min(self.config.max_wait, 5.0))
                    continue
                for message in messages:
                    await self.process_message(message, handler)
        finally:
            if owns_connection and connection is not None:
                await connection.drain()

    async def process_message(self, message: Any, handler: NATSEventHandler) -> None:
        try:
            event = parse_event_envelope(message.data)
        except Exception as exc:
            self.config.logger.warning("dropping invalid agent runtime event: %s", exc)
            await _resolve(message.ack())
            return

        deliveries = _deliveries(message)
        try:
            await _resolve(handler(event))
        except DropEvent:
            await _resolve(message.ack())
        except Exception as exc:
            if deliveries >= self.config.max_deliver:
                self.config.logger.error(
                    "dropping agent runtime event after max deliveries",
                    extra={
                        "runtime_run_id": event.run_id,
                        "host_run_id": event.host_run_id,
                        "event_type": event.type,
                        "error": str(exc),
                    },
                )
                await _resolve(message.ack())
                return
            await _resolve(message.nak(delay=self.retry_delay(deliveries)))
        else:
            await _resolve(message.ack())

    def retry_delay(self, deliveries: int) -> float:
        index = max(deliveries, 1) - 1
        return self.config.backoff[min(index, len(self.config.backoff) - 1)]

    async def _jetstream(self) -> tuple[Any, Any, bool]:
        if self.config.jetstream is not None:
            return self.config.jetstream, self.config.connection, False
        if self.config.connection is not None:
            return self.config.connection.jetstream(), self.config.connection, False
        if not self.config.url:
            raise ValueError("NATS URL, connection, or JetStream context is required")
        try:
            import nats
        except ImportError as exc:
            raise RuntimeError("Install agent-runtime[nats] to use NATS consumer helpers") from exc
        connection = await nats.connect(
            servers=[self.config.url],
            name=self.config.client_name,
            reconnect_time_wait=2,
            max_reconnect_attempts=-1,
        )
        return connection.jetstream(), connection, True

    async def _subscribe(self, jetstream: Any) -> Any:
        try:
            from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
        except ImportError as exc:
            raise RuntimeError("Install agent-runtime[nats] to use NATS consumer helpers") from exc
        config = ConsumerConfig(
            durable_name=self.config.durable,
            filter_subject=self.config.subject,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.config.ack_wait,
            max_deliver=self.config.max_deliver,
            backoff=self.config.backoff,
            deliver_policy=DeliverPolicy.ALL,
            max_ack_pending=self.config.max_ack_pending,
        )
        return await jetstream.pull_subscribe(
            self.config.subject,
            durable=self.config.durable,
            stream=self.config.stream,
            config=config,
        )


async def ensure_nats_stream(
    jetstream: Any,
    stream: str = DEFAULT_NATS_STREAM_NAME,
    subjects: Optional[List[str]] = None,
) -> None:
    stream = stream.strip() or DEFAULT_NATS_STREAM_NAME
    subjects = subjects or [DEFAULT_NATS_STREAM_SUBJECT]
    try:
        await jetstream.stream_info(stream)
        return
    except Exception as exc:
        if not _is_stream_not_found(exc):
            raise
    try:
        from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType
    except ImportError as exc:
        raise RuntimeError("Install agent-runtime[nats] to use NATS consumer helpers") from exc
    await jetstream.add_stream(
        name=stream,
        subjects=subjects,
        storage=StorageType.FILE,
        retention=RetentionPolicy.LIMITS,
        discard=DiscardPolicy.OLD,
        duplicate_window=timedelta(minutes=2).total_seconds(),
        max_age=timedelta(days=7).total_seconds(),
        max_bytes=512 * 1024 * 1024,
    )


def app_event_subject(app_id: str) -> str:
    return f"agent-runtime.events.{nats_app_token(app_id)}.>"


def render_nats_subject(template: str, event: EventEnvelope) -> str:
    value = template.strip() or DEFAULT_NATS_SUBJECT_TEMPLATE
    return (
        value.replace("{app_id}", nats_app_token(event.app_id))
        .replace("{run_id}", nats_token(event.run_id))
        .replace("{event_type}", nats_token(event.type))
        .replace("{type}", nats_token(event.type))
    )


def nats_token(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return "_"
    for source in (" ", "\t", "\n", "\r", "/", "\\", "*", ">"):
        value = value.replace(source, "_")
    return value


def nats_app_token(value: str) -> str:
    return nats_token(value).replace(".", "_")


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _deliveries(message: Any) -> int:
    metadata = getattr(message, "metadata", None)
    if callable(metadata):
        metadata = metadata()
    return int(getattr(metadata, "num_delivered", 1) or 1)


def _is_timeout_error(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or exc.__class__.__name__ == "TimeoutError"


def _is_stream_not_found(exc: Exception) -> bool:
    return exc.__class__.__name__ in {"NotFoundError", "StreamNotFoundError"}
