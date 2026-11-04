"""One door to Gemini for every module.

Modules keep their own prompts and schemas and call `json` or `text` with a
`task` name. The task name lets tests answer each kind of call with a script,
and it labels the call in the logs. When no key or project is set, Musha runs
without a model: `enabled` is False and both calls return None, so every
caller keeps a plain fallback.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings

log = logging.getLogger("musha.ai")


@dataclass(frozen=True)
class Blob:
    """Bytes for the model: an image, a short clip or a voice note."""
    data: bytes
    mime: str


Part = str | Blob

# Light tasks run with low thinking: a person waits on each, and each reads short text. The
# proof engine's question and its weighing of an answer run inside the manager's message. The goods
# check runs beside the receipt read inside a 15 s cap; at low thinking it judged the right goods,
# the wrong goods and a short count the same as at the default, in a third of the time.
QUICK = {"localize", "transcribe", "read_message", "camera.read", "owner_answer",
         "proof_question", "proof_answer", "goods_check"}
# A call that runs past this is dropped and the caller falls back, so a request never hangs on the model.
TIMEOUT = {"quick": 40.0, "other": 90.0}
_LATE = object()


async def _within(aw, seconds: float):
    """The awaitable's result, or _LATE once `seconds` pass. A late call is cancelled and left
    behind, so a call that ignores cancellation never holds its caller."""
    task = asyncio.ensure_future(aw)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, seconds))
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    _left.add(task)  # the loop keeps tasks weakly; this keeps a late one until it ends
    task.add_done_callback(_forget)
    return _LATE


_left: set[asyncio.Future] = set()


def _forget(task: asyncio.Future) -> None:
    _left.discard(task)
    if not task.cancelled():
        task.exception()  # read, so a late failure stays quiet


async def bounded(aw, seconds: float, default=None):
    """The awaitable's result, or `default` once `seconds` pass. An error inside it raises."""
    out = await _within(aw, seconds)
    return default if out is _LATE else out


class Gemini:
    enabled = True

    def __init__(self, settings: Settings):
        from google import genai

        if settings.gemini_api_key:
            self.client = genai.Client(api_key=settings.gemini_api_key)
        else:
            self.client = genai.Client(vertexai=True, project=settings.gemini_project,
                                       location=settings.gemini_location)
        self.model = settings.gemini_model

    def _contents(self, parts: list[Part]):
        from google.genai import types

        return [types.Part.from_bytes(data=p.data, mime_type=p.mime) if isinstance(p, Blob) else p
                for p in parts]

    async def json(self, task: str, parts: list[Part], schema: dict, *, system: str = "") -> Any:
        from google.genai import types

        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_json_schema=schema,
            system_instruction=system or None, thinking_config=self._thinking(task))
        text = await self._generate(task, parts, config)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            log.warning("ai %s returned text that is not JSON", task)
            return None

    async def text(self, task: str, parts: list[Part], *, system: str = "") -> str | None:
        from google.genai import types

        config = types.GenerateContentConfig(system_instruction=system or None,
                                             thinking_config=self._thinking(task))
        text = await self._generate(task, parts, config)
        return text.strip() if text else None

    def _thinking(self, task: str):
        from google.genai import types

        return types.ThinkingConfig(thinking_level="low") if task in QUICK else None

    async def _generate(self, task: str, parts: list[Part], config) -> str | None:
        started = time.monotonic()
        limit = TIMEOUT["quick" if task in QUICK else "other"]
        try:
            r = await _within(self.client.aio.models.generate_content(
                model=self.model, contents=self._contents(parts), config=config), limit)
        except Exception as e:  # the model is a witness, never a single point of failure
            log.warning("ai %s failed: %s", task, e)
            return None
        if r is _LATE:
            log.warning("ai %s failed: no answer in %.0fs", task, limit)
            return None
        log.info("ai %s %.1fs", task, time.monotonic() - started)
        return r.text


class NoModel:
    """Musha with the model switched off. Callers fall back to plain rules."""
    enabled = False
    model = ""

    async def json(self, task: str, parts: list[Part], schema: dict, *, system: str = "") -> Any:
        return None

    async def text(self, task: str, parts: list[Part], *, system: str = "") -> str | None:
        return None


class Scripted:
    """A stand-in model for tests: each task answers from a value or a function of the parts."""
    enabled = True
    model = "scripted"

    def __init__(self, answers: dict[str, Any | Callable[[list[Part]], Any]] | None = None):
        self.answers = dict(answers or {})
        self.calls: list[tuple[str, list[Part]]] = []

    def _answer(self, task: str, parts: list[Part]):
        self.calls.append((task, parts))
        a = self.answers.get(task)
        return a(parts) if callable(a) else a

    async def json(self, task: str, parts: list[Part], schema: dict, *, system: str = "") -> Any:
        return self._answer(task, parts)

    async def text(self, task: str, parts: list[Part], *, system: str = "") -> str | None:
        return self._answer(task, parts)


def make_model(settings: Settings):
    if settings.gemini_api_key or settings.gemini_project:
        return Gemini(settings)
    return NoModel()
