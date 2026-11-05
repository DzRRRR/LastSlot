"""Diffusion d'événements temps réel vers les navigateurs (Server-Sent Events).

Broker en mémoire : suffisant avec UN SEUL worker uvicorn. Avec plusieurs workers
ou plusieurs instances, il faudra passer par PostgreSQL LISTEN/NOTIFY ou Redis pub/sub,
sinon un événement émis dans le worker A n'atteint pas les clients connectés au worker B.
"""
import asyncio
import json
from typing import Any


class Broker:
    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def is_subscribed(self, queue: asyncio.Queue[str]) -> bool:
        return queue in self._subscribers

    def publish(self, event: str, data: dict[str, Any]) -> None:
        # Format SSE : lignes "event:" et "data:", message terminé par une ligne vide.
        message = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Client trop lent : on le désabonne plutôt que de faire grossir la mémoire.
                # Sa boucle de heartbeat détectera la désinscription et fermera le flux ;
                # le navigateur se reconnectera tout seul (EventSource) et rechargera l'état.
                self._subscribers.discard(queue)


broker = Broker()
