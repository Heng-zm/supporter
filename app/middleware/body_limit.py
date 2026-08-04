from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.problems import problem_response


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value.strip()
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if content_lengths:
            if (
                len(set(content_lengths)) != 1
                or not content_lengths[0]
                or not content_lengths[0].isdigit()
            ):
                await self._reject_invalid_length(scope, receive, send)
                return
            canonical_length = content_lengths[0].lstrip(b"0") or b"0"
            maximum = str(self.max_bytes).encode("ascii")
            if len(canonical_length) > len(maximum) or (
                len(canonical_length) == len(maximum)
                and canonical_length > maximum
            ):
                await self._reject_too_large(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                # A streaming endpoint has already started its response, so a
                # second response cannot be emitted safely. Close the request.
                raise
            await self._reject_too_large(scope, receive, send)

    @staticmethod
    async def _reject_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = problem_response(
            scope,
            status_code=413,
            detail="Request body is too large.",
            error_code="payload_too_large",
        )
        await response(scope, receive, send)

    @staticmethod
    async def _reject_invalid_length(
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        response = problem_response(
            scope,
            status_code=400,
            detail="The Content-Length header is invalid.",
            error_code="invalid_content_length",
        )
        await response(scope, receive, send)
