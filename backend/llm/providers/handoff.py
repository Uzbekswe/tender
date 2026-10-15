"""Development provider: hands LLM requests to an outside reader through files.

Used while no API model is available. On a call it looks for an answer in
<data_dir>/llm_handoff/inbox/<id>.json; if there is none it writes the request to
outbox/<id>/ (request.json + page images) and raises HandoffPending. Someone (in
practice a Claude agent in the coding session) answers the outbox, and the next
pipeline run picks the answers up. The service layer then validates and caches
them like any other provider response, so downstream code is exercised for real.

The id is a hash of the request content, so re-running never duplicates work.
Not for production: there is no latency guarantee and no data-processing contract.
"""

import hashlib
import json
from pathlib import Path

from llm.providers import ProviderRequest, ProviderResponse

ANSWERED_BY = "handoff"


class HandoffPending(Exception):
    """No answer yet; the request is waiting in the outbox."""


class HandoffProvider:
    def __init__(self, root: Path, answered_by: str = ANSWERED_BY) -> None:
        self.outbox = root / "outbox"
        self.inbox = root / "inbox"
        self.answered_by = answered_by

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        request_id = handoff_id(request)
        answer = self.inbox / f"{request_id}.json"
        if answer.exists():
            # The inbox file is the raw answer, exactly as a model would return it.
            return ProviderResponse(text=answer.read_text(encoding="utf-8"), model_answered=request.model)
        self._write_request(request_id, request)
        raise HandoffPending(f"waiting for handoff answer {request_id} ({request.schema_name})")

    def _write_request(self, request_id: str, request: ProviderRequest) -> None:
        folder = self.outbox / request_id
        if (folder / "request.json").exists():
            return
        folder.mkdir(parents=True, exist_ok=True)
        messages = []
        for message in request.messages:
            images = []
            for n, image in enumerate(message.images, start=1):
                name = f"image_{len(messages)}_{n}.png"
                (folder / name).write_bytes(image.png_bytes)
                images.append({"file": name, "label": image.label})
            messages.append({"role": message.role, "text": message.text, "images": images})
        (folder / "request.json").write_text(
            json.dumps(
                {
                    "id": request_id,
                    "model": request.model,
                    "schema_name": request.schema_name,
                    "schema": request.schema.model_json_schema(),
                    "messages": messages,
                    "answer_to": str(self.inbox / f"{request_id}.json"),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )


def handoff_id(request: ProviderRequest) -> str:
    h = hashlib.sha256()
    h.update(request.model.encode())
    h.update(request.schema_name.encode())
    for message in request.messages:
        h.update(message.role.encode())
        h.update(message.text.encode())
        for image in message.images:
            h.update(hashlib.sha256(image.png_bytes).digest())
    return h.hexdigest()[:24]
