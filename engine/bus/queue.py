"""A persisted message queue on ops.EventQueue (Redis streams are an optional later swap)."""

from datetime import datetime

from pydantic import TypeAdapter
from sqlalchemy import Engine, insert, select, update

from engine.bus.messages import Envelope, Message
from engine.data.schema import EVENT_QUEUE

_MESSAGE = TypeAdapter(Message)
Q = EVENT_QUEUE.c


class MessageBus:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def publish(self, correlation_id: str, producer: str, body: Message, at: datetime) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(EVENT_QUEUE),
                {
                    "CorrelationID": correlation_id,
                    "MessageType": body.type,
                    "Producer": producer,
                    "Payload": body.model_dump_json(),
                    "CreatedAt": at,
                },
            )
            key = result.inserted_primary_key
            assert key is not None
            return int(key[0])

    @staticmethod
    def _envelope(row: object) -> Envelope:
        r = dict(row._mapping)  # type: ignore[attr-defined]
        return Envelope(
            message_id=r["MessageID"],
            correlation_id=r["CorrelationID"],
            producer=r["Producer"],
            created_at=r["CreatedAt"],
            body=_MESSAGE.validate_json(r["Payload"]),
        )

    def consume(
        self, consumer: str, message_type: str, correlation_id: str, at: datetime
    ) -> list[Envelope]:
        """Unconsumed messages of one type for one correlation id, oldest first; marks them read."""
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(EVENT_QUEUE)
                .where(Q.MessageType == message_type)
                .where(Q.CorrelationID == correlation_id)
                .where(Q.ConsumedAt.is_(None))
                .order_by(Q.MessageID)
            ).all()
            if rows:
                conn.execute(
                    update(EVENT_QUEUE)
                    .where(Q.MessageID.in_([r.MessageID for r in rows]))
                    .values(ConsumedBy=consumer, ConsumedAt=at)
                )
        return [self._envelope(r) for r in rows]

    def history(self, correlation_id: str | None = None) -> list[Envelope]:
        query = select(EVENT_QUEUE).order_by(Q.MessageID)
        if correlation_id is not None:
            query = query.where(Q.CorrelationID == correlation_id)
        with self.engine.connect() as conn:
            return [self._envelope(r) for r in conn.execute(query).all()]
