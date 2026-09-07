"""Async, streaming Ollama REST backend.

Cancellation of the consumer task propagates into ``httpx`` and the response is
explicitly closed in ``finally``.  The ``server_observed`` event means the
client observed Ollama's first valid NDJSON frame; it is not a claim about an
unobserved internal GPU start time.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

import httpx

from .schema import BackendRequest, RequestType


StageCallback = Callable[
    [str, Mapping[str, Any]], Awaitable[None] | None
]


async def _notify(
    callback: StageCallback | None,
    stage: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    if callback is None:
        return
    result = callback(stage, dict(details or {}))
    if inspect.isawaitable(result):
        await result


@dataclass(frozen=True, slots=True)
class BackendChunk:
    request_id: str
    request_type: RequestType
    generation: int
    text: str
    done: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


class StreamingBackend(Protocol):
    def stream(
        self,
        request: BackendRequest,
        on_stage: StageCallback | None = None,
    ) -> AsyncIterator[BackendChunk]:
        """Return an async stream; task cancellation must close its connection."""


class BackendProtocolError(RuntimeError):
    pass


class OllamaBackend:
    """Minimal ``/api/chat`` client with structured-output and stream support."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        connect_timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout_s,
                read=None,
                write=connect_timeout_s,
                pool=connect_timeout_s,
            )
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OllamaBackend":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def stream(
        self,
        request: BackendRequest,
        on_stage: StageCallback | None = None,
    ) -> AsyncIterator[BackendChunk]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.model_dump() for message in request.messages],
            "stream": True,
            "think": False,
            "options": {"temperature": request.temperature},
        }
        if request.seed is not None:
            payload["options"]["seed"] = request.seed
        if request.num_predict is not None:
            payload["options"]["num_predict"] = request.num_predict
        if request.response_schema is not None:
            # Ollama accepts an actual JSON Schema object here.  Do not replace
            # this with the weaker string value ``"json"``.
            payload["format"] = dict(request.response_schema)

        response: httpx.Response | None = None
        cancelled = False
        await _notify(on_stage, "connection_opening", {"url_path": "/api/chat"})
        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
                headers={"Accept": "application/x-ndjson"},
            ) as response:
                await _notify(
                    on_stage,
                    "response_headers",
                    {"http_status": response.status_code},
                )
                response.raise_for_status()
                observed = False
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise BackendProtocolError(
                            f"Ollama returned invalid NDJSON: {exc.msg}"
                        ) from exc
                    if not isinstance(frame, dict):
                        raise BackendProtocolError("Ollama stream frame is not an object")
                    if frame.get("error"):
                        raise BackendProtocolError(str(frame["error"]))
                    if not observed:
                        observed = True
                        server_fields = {
                            key: frame[key]
                            for key in ("model", "created_at", "done")
                            if key in frame
                        }
                        await _notify(on_stage, "server_observed", server_fields)
                        await _notify(on_stage, "first_chunk", server_fields)
                    message = frame.get("message") or {}
                    if not isinstance(message, dict):
                        raise BackendProtocolError("Ollama frame.message is not an object")
                    text = message.get("content", "")
                    if not isinstance(text, str):
                        raise BackendProtocolError("Ollama frame content is not a string")
                    metadata = {
                        key: frame[key]
                        for key in (
                            "model",
                            "created_at",
                            "done_reason",
                            "total_duration",
                            "load_duration",
                            "prompt_eval_count",
                            "prompt_eval_duration",
                            "eval_count",
                            "eval_duration",
                        )
                        if key in frame
                    }
                    yield BackendChunk(
                        request_id=request.request_id,
                        request_type=request.request_type,
                        generation=request.generation,
                        text=text,
                        done=bool(frame.get("done", False)),
                        metadata=metadata,
                    )
        except asyncio.CancelledError:
            cancelled = True
            if response is not None:
                await response.aclose()
            raise
        finally:
            if response is not None and not response.is_closed:
                await response.aclose()
            await _notify(on_stage, "connection_closed", {"cancelled": cancelled})
