import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("nats")

from fastapi import FastAPI
import httpx

from agent_runtime import create_fastapi_event_callback_router
from agent_runtime.events import EventEnvelope
from agent_runtime.nats import (
    DEFAULT_NATS_STREAM_NAME,
    DropEvent,
    NATSConsumer,
    NATSConsumerConfig,
    RetryEvent,
    app_event_subject,
    ensure_nats_stream,
    nats_app_token,
    nats_token,
    render_nats_subject,
)

pytestmark = pytest.mark.optional


def event_payload(**overrides):
    payload = {
        "event_id": "event-1",
        "app_id": "app-a",
        "run_id": "run-1",
        "type": "run.completed",
        "data": {},
    }
    payload.update(overrides)
    return payload


def test_fastapi_event_callback_router_authenticates_and_scopes_app():
    received = []

    async def handler(event):
        received.append(event)
        return {"accepted": True, "event_id": event.event_id}

    app = FastAPI()
    app.include_router(
        create_fastapi_event_callback_router(handler, token="secret", app_id="app-a")
    )
    async def exercise_router():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = await client.post("/agent-runtime/events", json=event_payload())
            assert unauthorized.status_code == 401
            wrong_app = await client.post(
                "/agent-runtime/events",
                headers={"Authorization": "Bearer secret"},
                json=event_payload(app_id="other"),
            )
            assert wrong_app.status_code == 403
            accepted = await client.post(
                "/agent-runtime/events",
                headers={"Authorization": "Bearer secret"},
                json=event_payload(),
            )
            assert accepted.status_code == 200
            assert accepted.json() == {"accepted": True, "event_id": "event-1"}

    asyncio.run(exercise_router())
    assert len(received) == 1


class FakeMessage:
    def __init__(self, payload, deliveries=1):
        self.data = payload
        self.metadata = SimpleNamespace(num_delivered=deliveries)
        self.acked = 0
        self.nak_delays = []

    async def ack(self):
        self.acked += 1

    async def nak(self, delay=None):
        self.nak_delays.append(delay)


def test_nats_subject_helpers_and_defaults():
    event = EventEnvelope(
        app_id="helpin.stage",
        run_id="run/1",
        type="run.completed",
    )
    assert nats_token("run/1") == "run_1"
    assert nats_app_token("helpin.stage") == "helpin_stage"
    assert app_event_subject("helpin.stage") == "agent-runtime.events.helpin_stage.>"
    assert render_nats_subject("", event) == "agent-runtime.events.helpin_stage.run_1.run.completed"

    config = NATSConsumerConfig(app_id="helpin")
    assert config.stream == DEFAULT_NATS_STREAM_NAME
    assert config.subject == "agent-runtime.events.helpin.>"
    assert config.fetch_batch == 8
    assert config.max_deliver == 5


def test_nats_consumer_ack_retry_drop_and_max_delivery():
    consumer = NATSConsumer(
        NATSConsumerConfig(app_id="app-a", backoff=[1.0, 2.0], max_deliver=3)
    )
    payload = b'{"event_id":"event-1","app_id":"app-a","run_id":"run-1","type":"run.completed"}'

    success = FakeMessage(payload)
    asyncio.run(consumer.process_message(success, lambda event: None))
    assert success.acked == 1

    async def retry(_event):
        raise RetryEvent()

    retried = FakeMessage(payload, deliveries=2)
    asyncio.run(consumer.process_message(retried, retry))
    assert retried.acked == 0
    assert retried.nak_delays == [2.0]

    async def drop(_event):
        raise DropEvent()

    dropped = FakeMessage(payload)
    asyncio.run(consumer.process_message(dropped, drop))
    assert dropped.acked == 1

    exhausted = FakeMessage(payload, deliveries=3)
    asyncio.run(consumer.process_message(exhausted, retry))
    assert exhausted.acked == 1
    assert exhausted.nak_delays == []


def test_ensure_nats_stream_uses_runtime_defaults():
    class NotFoundError(Exception):
        pass

    class FakeJetStream:
        def __init__(self):
            self.added = None

        async def stream_info(self, _stream):
            raise NotFoundError()

        async def add_stream(self, **kwargs):
            self.added = kwargs

    jetstream = FakeJetStream()
    asyncio.run(ensure_nats_stream(jetstream))
    assert jetstream.added["name"] == DEFAULT_NATS_STREAM_NAME
    assert jetstream.added["subjects"] == ["agent-runtime.events.>"]
    assert jetstream.added["max_bytes"] == 512 * 1024 * 1024
