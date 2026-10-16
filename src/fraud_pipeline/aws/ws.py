"""API Gateway WebSocket $connect / $disconnect handlers.

Connection ids live in DynamoDB with a TTL, so dashboards that vanish without
a clean disconnect are cleaned up automatically (and on the next failed push).
"""
from __future__ import annotations

import os
import time

_table = None


def _connections():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    return _table


def connect(event, context=None, table=None):
    table = table or _connections()
    cid = event["requestContext"]["connectionId"]
    table.put_item(Item={"connection_id": cid, "expires_at": int(time.time()) + 2 * 3600})
    return {"statusCode": 200}


def disconnect(event, context=None, table=None):
    table = table or _connections()
    table.delete_item(Key={"connection_id": event["requestContext"]["connectionId"]})
    return {"statusCode": 200}
