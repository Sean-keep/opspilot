"""Route classes.

One behavioural rule lives here: **a 2xx means the write is durable.**

FastAPI unwinds ``yield`` dependencies on the request exit stack, and
``fastapi.routing.request_response`` only closes that stack *after* it has
already sent the response::

    async with AsyncExitStack() as request_stack:   # get_db tears down here
        async with AsyncExitStack() as function_stack:
            response = await f(request)
        await response(scope, receive, send)        # ...but the 201 left already

So a commit in ``get_db``'s teardown is a commit *after* the client has been
told ``201 Created``. An immediate follow-up read — which is exactly what the
console does after every create — loses the race and gets ``404``.

``CommitRoute`` closes that gap at one place instead of asking every write
handler to remember. The handler returns, the session is committed, and only
then does the response leave the process. ``get_db`` still rolls back on error
and still closes the session; its teardown commit is now a no-op safety net.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute


def commit_request_session(request: Request) -> None:
    """Commit the session ``get_db`` attached to this request, if any.

    A handler that already committed leaves nothing open, and a read-only
    handler leaves an empty transaction — both are no-ops. An exception raised
    by the handler never reaches here, so the rollback path is unchanged.
    """
    session = getattr(request.state, "db", None)
    if session is not None and session.in_transaction():
        session.commit()


class CommitRoute(APIRoute):
    """An :class:`APIRoute` that commits before it replies."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response = await original(request)
            commit_request_session(request)
            return response

        return handler


class AppRouter(APIRouter):
    """Every router in the app uses :class:`CommitRoute`.

    Subclassed rather than passed at each call site so a new feature router
    cannot quietly opt out of the durability rule.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("route_class", CommitRoute)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
