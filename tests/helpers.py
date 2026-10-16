"""Shared test helpers: transaction factory and in-memory AWS fakes."""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

TAMPA = (27.95, -82.46)
LONDON = (51.51, -0.13)


def txn(user="u1", ts=1_000_000.0, amount=25.0, lat=TAMPA[0], lon=TAMPA[1], country="US",
        device="d1", category="grocery", txn_id=None, label=0, fraud_type=None):
    return {"txn_id": txn_id or uuid.uuid4().hex, "user_id": user, "ts": ts, "amount": amount,
            "currency": "USD", "merchant_category": category, "merchant_id": "m1",
            "city": "Tampa" if country == "US" else "London", "country": country,
            "lat": lat, "lon": lon, "device_id": device, "label": label,
            "fraud_type": fraud_type}


class ClientError(Exception):
    """Mimics botocore.exceptions.ClientError's .response shape."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    """Just enough of a boto3 DynamoDB Table to exercise conditional writes."""

    def __init__(self, key):
        self.key, self.items, self.fail_next_put = key, {}, None

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get(Key[self.key])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if self.fail_next_put:
            err, self.fail_next_put = self.fail_next_put, None
            raise err
        k = Item[self.key]
        existing = self.items.get(k)
        if ConditionExpression and ConditionExpression.startswith("attribute_not_exists"):
            if existing is not None:
                raise ClientError("ConditionalCheckFailedException")
        elif ConditionExpression == "version = :v":
            if existing is None or existing["version"] != ExpressionAttributeValues[":v"]:
                raise ClientError("ConditionalCheckFailedException")
        self.items[k] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key[self.key], None)

    def scan(self, ProjectionExpression=None):
        return {"Items": [dict(v) for v in self.items.values()]}


class FakeSns:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)


class FakeFirehose:
    def __init__(self):
        self.records = []

    def put_record_batch(self, DeliveryStreamName, Records):
        self.records.extend(Records)
        return {"FailedPutCount": 0, "RequestResponses": [{} for _ in Records]}


class FakeApiGw:
    def __init__(self, gone=()):
        self.sent, self.gone = [], set(gone)

    def post_to_connection(self, ConnectionId, Data):
        if ConnectionId in self.gone:
            raise ClientError("GoneException")
        self.sent.append((ConnectionId, Data))
