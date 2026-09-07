from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from hera_v2.ollama_backend import OllamaBackend
from hera_v2.schema import (
    BackendRequest,
    ChatMessage,
    RequestType,
    emergency_json_schema,
)


def make_request() -> BackendRequest:
    return BackendRequest(
        request_id="request-1",
        request_type=RequestType.EMERGENCY,
        generation=1,
        attempt=1,
        model="fake-model",
        messages=(ChatMessage(role="user", content="recover"),),
        response_schema=emergency_json_schema(),
        temperature=0.0,
    )


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.first_sent = asyncio.Event()
        self.closed = False
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield b'{"message":{"content":"{"},"done":false}\n'
        self.first_sent.set()
        await self.release.wait()

    async def aclose(self) -> None:
        self.closed = True
        self.release.set()


class OllamaBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_real_schema_and_tags_stream(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads((await request.aread()).decode("utf-8")))
            body = (
                b'{"model":"fake","created_at":"now","message":'
                b'{"content":"ok"},"done":true}\n'
            )
            return httpx.Response(200, content=body)

        stages = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            backend = OllamaBackend(client=client)

            async def on_stage(stage, details):
                stages.append(stage)

            chunks = [
                chunk
                async for chunk in backend.stream(make_request(), on_stage=on_stage)
            ]

        self.assertIsInstance(captured["format"], dict)
        self.assertIn("properties", captured["format"])
        self.assertFalse(captured["think"])
        self.assertEqual("request-1", chunks[0].request_id)
        self.assertIs(RequestType.EMERGENCY, chunks[0].request_type)
        self.assertEqual(1, chunks[0].generation)
        self.assertIn("server_observed", stages)
        self.assertIn("first_chunk", stages)
        self.assertEqual("connection_closed", stages[-1])

    async def test_task_cancel_closes_stream(self):
        stream = _BlockingStream()

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        stages = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            backend = OllamaBackend(client=client)

            async def consume() -> None:
                async for _ in backend.stream(
                    make_request(),
                    on_stage=lambda stage, details: stages.append((stage, details)),
                ):
                    pass

            task = asyncio.create_task(consume())
            await asyncio.wait_for(stream.first_sent.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(stream.closed)
        self.assertEqual("connection_closed", stages[-1][0])
        self.assertTrue(stages[-1][1]["cancelled"])


if __name__ == "__main__":
    unittest.main()
