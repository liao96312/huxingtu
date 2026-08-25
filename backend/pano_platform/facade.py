from __future__ import annotations

from uuid import uuid4

from .contracts import AcceptedTask, CommandEnvelope, CommandHandler, CommandName


class CommandNotImplementedError(LookupError):
    pass


class CommandFacade:
    def __init__(self) -> None:
        self._handlers: dict[CommandName, CommandHandler] = {}

    def register(self, command: CommandName, handler: CommandHandler) -> None:
        self._handlers[command] = handler

    async def dispatch(self, envelope: CommandEnvelope) -> AcceptedTask:
        handler = self._handlers.get(envelope.command)
        if handler is None:
            raise CommandNotImplementedError(envelope.command.value)
        return await handler(envelope)


async def accept_for_contract_test(_: CommandEnvelope) -> AcceptedTask:
    """Minimal handler used by smoke checks until the first real stage worker lands."""
    return AcceptedTask(taskId=uuid4())
