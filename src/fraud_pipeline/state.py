"""Per-user state stores.

InMemoryStateStore  -- local demo, tests, training
DynamoDBStateStore  -- production. State is stored as one JSON string per user
                       (no float->Decimal conversion headaches) with a version
                       number for optimistic locking.
"""
from __future__ import annotations

import json

from .features import UserState


class StaleStateError(Exception):
    """Another writer updated this user's state first; the batch will be retried."""


class InMemoryStateStore:
    def __init__(self):
        self._data: dict[str, dict] = {}

    def get(self, user_id: str) -> UserState:
        d = self._data.get(user_id)
        return UserState.from_dict(json.loads(json.dumps(d))) if d else UserState(user_id=user_id)

    def put(self, state: UserState) -> None:
        state.version += 1
        self._data[state.user_id] = state.to_dict()

    def __len__(self):
        return len(self._data)


class DynamoDBStateStore:
    """`table` is a boto3 DynamoDB Table resource (or a test double)."""

    def __init__(self, table):
        self.table = table

    def get(self, user_id: str) -> UserState:
        item = self.table.get_item(Key={"user_id": user_id}, ConsistentRead=True).get("Item")
        if not item:
            return UserState(user_id=user_id)
        return UserState.from_dict(json.loads(item["state"]))

    def put(self, state: UserState) -> None:
        expected = state.version
        state.version += 1
        kwargs = {"Item": {"user_id": state.user_id, "version": state.version,
                           "state": json.dumps(state.to_dict(), separators=(",", ":"))}}
        if expected == 0:
            kwargs["ConditionExpression"] = "attribute_not_exists(user_id)"
        else:
            kwargs["ConditionExpression"] = "version = :v"
            kwargs["ExpressionAttributeValues"] = {":v": expected}
        try:
            self.table.put_item(**kwargs)
        except Exception as e:  # botocore ClientError
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                state.version = expected
                raise StaleStateError(state.user_id) from e
            raise
