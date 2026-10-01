"""Hourly recovery and bounded cleanup; progress survives Lambda invocations."""

import json
import os
import time
from datetime import datetime
from typing import Any

from switchboard.dynamodb import DynamoStore, key
from switchboard.jobs import TERMINAL, authorize, dispatch, fail, queue_client
from switchboard.storage import StorageError, WorkspaceStorage
from switchboard.workflow import advance, complete_investigation


def maintain(context=None):
    store = DynamoStore()
    sqs = queue_client()
    deadline = time.monotonic() + (
        context.get_remaining_time_in_millis() / 1000 - 10 if context else 100
    )
    counts = {"dispatched": 0, "failed": 0, "deleted": 0}

    # 1. Persist DLQ failures before removing messages, never replacing a completion.
    while time.monotonic() < deadline:
        response = sqs.receive_message(
            QueueUrl=os.environ["INVESTIGATION_DLQ_URL"],
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
        )
        messages = response.get("Messages", [])
        if not messages:
            break
        for message in messages:
            body = json.loads(message["Body"])
            metadata = store.metadata(body["workspace_id"])
            if metadata and metadata["generation"] == body["generation"]:
                store.generations[body["workspace_id"]] = body["generation"]
                storage = WorkspaceStorage(body["workspace_id"], transport=store)
                job = store.get(
                    store.partition(body["workspace_id"]), f"JOB#{body['run_id']}"
                )
                if job and job["status"] not in TERMINAL:
                    if job["lease_until"] > time.time():
                        continue
                    try:
                        fail(
                            storage,
                            job,
                            "Investigation exhausted automatic retries. Start a new investigation to try again.",
                        )
                    except StorageError as error:
                        if error.status == 409:
                            continue
                        raise
                    counts["failed"] += 1
            complete_investigation(body)
            sqs.delete_message(
                QueueUrl=os.environ["INVESTIGATION_DLQ_URL"],
                ReceiptHandle=message["ReceiptHandle"],
            )

    # 2. Scan a bounded portion; resume on the next hourly invocation.
    cursor = store.get("MAINTENANCE", "CURSOR")
    arguments: dict[str, Any] = {
        "TableName": store.table,
        "Limit": 100,
        "ConsistentRead": True,
    }
    if cursor:
        arguments["ExclusiveStartKey"] = cursor
    while time.monotonic() < deadline:
        response = store.client.scan(**arguments)
        for item in response["Items"]:
            partition = item["PK"]["S"]
            sort = item["SK"]["S"]
            if not partition.startswith("WS#"):
                continue
            workspace, separator, generation = partition[3:].partition("#GEN#")
            metadata = store.metadata(workspace)
            if (
                not separator
                and sort == "META"
                and metadata
                and metadata["identityMode"] == "demo"
                and not metadata.get("import_hash")
            ):
                if (
                    time.time()
                    - datetime.fromisoformat(metadata["updatedAt"]).timestamp()
                    > 86400
                ):
                    try:
                        operation = store.delete(partition, sort)
                        operation["Delete"].update(
                            ConditionExpression="#body = :previous",
                            ExpressionAttributeNames={"#body": "body"},
                            ExpressionAttributeValues={
                                ":previous": {"S": item["body"]["S"]}
                            },
                        )
                        store.client.transact_write_items(TransactItems=[operation])
                        counts["deleted"] += 1
                    except store.client.exceptions.TransactionCanceledException:
                        pass
                continue
            if not separator:
                continue
            if not metadata or metadata["generation"] != generation:
                staging = store.get(partition, "STAGING")
                if staging and time.time() - staging["created_at"] < 3600:
                    continue
                store.client.delete_item(
                    TableName=store.table, Key=key(partition, sort)
                )
                counts["deleted"] += 1
                continue
            if sort.startswith("JOB#"):
                job = json.loads(item["body"]["S"])
                if job["dispatch"] == "pending" and job["status"] == "queued":
                    store.generations[workspace] = generation
                    storage = WorkspaceStorage(workspace, transport=store)
                    try:
                        authorize(storage, job)
                    except PermissionError:
                        continue
                    try:
                        dispatch(storage=storage, job=job, sqs=sqs)
                        counts["dispatched"] += 1
                    except StorageError as error:
                        if error.status != 409:
                            raise
            if sort.startswith("FLOW#"):
                waiting = json.loads(item["body"]["S"])
                store.generations[workspace] = generation
                advance(
                    WorkspaceStorage(workspace, transport=store),
                    waiting["run_id"],
                    waiting["stage"],
                )
        cursor = response.get("LastEvaluatedKey")
        store.client.put_item(
            TableName=store.table,
            Item=store.put("MAINTENANCE", "CURSOR", cursor)["Put"]["Item"],
        )
        if not cursor:
            break
        arguments["ExclusiveStartKey"] = cursor
    return counts
