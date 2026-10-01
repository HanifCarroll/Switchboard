"""Failure and concurrency checks for persisted jobs and native event entry points."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import boto3
import pytest
from botocore.exceptions import ClientError

from switchboard.demo.portfolio import initialize_demo_portfolio
from switchboard.dynamodb import DynamoStore
from switchboard.investigation.report_validation import (
    EmptyModelResponseError,
    ReportValidationError,
)
from switchboard.jobs import fail, process_message, read, submit
from switchboard.maintenance import maintain
from switchboard.storage import StorageError, WorkspaceStorage
from tests import test_dynamodb

dynamo = test_dynamodb.dynamo


@pytest.fixture
def queued(dynamo, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("DYNAMODB_TABLE", dynamo.table)
    monkeypatch.setenv("SWITCHBOARD_INVESTIGATION_MODE", "fixture")
    monkeypatch.setenv("SWITCHBOARD_AUTH_MODE", "demo")
    monkeypatch.setenv("SWITCHBOARD_RUNTIME", "local")
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="investigations")["QueueUrl"]
    dlq = sqs.create_queue(QueueName="failed")["QueueUrl"]
    monkeypatch.setenv("INVESTIGATION_QUEUE_URL", queue)
    monkeypatch.setenv("INVESTIGATION_DLQ_URL", dlq)
    storage = WorkspaceStorage(str(uuid4()), transport=dynamo)
    initialize_demo_portfolio(storage=storage)
    return storage, sqs, queue, dlq


def submit_job(storage, sqs, request_key=None, ticket="CHG-1042"):
    return submit(
        storage=storage,
        employee_id="emp-alex",
        ticket_id=ticket,
        request_key=request_key or uuid4(),
        sqs=sqs,
    )


def message(storage, identifier):
    return {
        "workspace_id": storage.workspace_id,
        "generation": storage.get_workspace()["generation"],
        "run_id": str(identifier),
    }


def test_submission_delivery_terminal_duplicate_and_access(queued):
    storage, sqs, queue, _ = queued
    request_key = uuid4()
    job = submit_job(storage, sqs, request_key)
    assert submit_job(storage, sqs, request_key).run_id == job.run_id
    assert submit_job(storage, sqs).run_id == job.run_id
    with pytest.raises(StorageError, match="different inputs"):
        submit_job(storage, sqs, request_key, ticket="CHG-1045")
    process_message(message(storage, job.run_id))
    result = read(storage=storage, employee_id="emp-alex", run_id=job.run_id)
    assert result.status == "completed"
    process_message(message(storage, job.run_id))
    assert len(storage.list_runs(ticket_id="CHG-1042")) == 1
    with pytest.raises(PermissionError):
        read(storage=storage, employee_id="emp-priya", run_id=job.run_id)
    assert submit_job(storage, sqs).run_id != job.run_id


def test_daily_allowance_preserves_retries_and_cannot_be_reset_by_a_visitor(
    queued, monkeypatch
):
    storage, sqs, queue, _ = queued
    monkeypatch.setenv("INVESTIGATION_DAILY_LIMIT", "100")
    monkeypatch.setenv("INVESTIGATION_VISITOR_DAILY_LIMIT", "1")

    request_key = uuid4()
    job = submit_job(storage, sqs, request_key)
    assert submit_job(storage, sqs, request_key).run_id == job.run_id
    assert submit_job(storage, sqs).run_id == job.run_id
    process_message(message(storage, job.run_id))
    assert submit_job(storage, sqs, request_key).run_id == job.run_id

    with pytest.raises(StorageError) as denied:
        submit_job(storage, sqs, ticket="CHG-1045")
    assert denied.value.status == 429
    assert not storage.transport.query(
        storage.transport.partition(storage.workspace_id), "JOBHISTORY#CHG-1045"
    )
    assert len(sqs.receive_message(QueueUrl=queue)["Messages"]) == 1

    initialize_demo_portfolio(storage=storage)
    with pytest.raises(StorageError) as reset_denied:
        submit_job(storage, sqs)
    assert reset_denied.value.status == 429


def test_global_allowance_is_atomic_across_visitors_and_rolls_over_at_utc_midnight(
    queued, monkeypatch
):
    first, sqs, _, _ = queued
    monkeypatch.setenv("INVESTIGATION_DAILY_LIMIT", "1")
    monkeypatch.setenv("INVESTIGATION_VISITOR_DAILY_LIMIT", "10")
    second = WorkspaceStorage(
        str(uuid4()),
        transport=DynamoStore(first.transport.table, first.transport.client),
    )
    initialize_demo_portfolio(storage=second)

    def try_submit(storage):
        try:
            return submit_job(storage, sqs)
        except StorageError as error:
            assert error.status == 429
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(try_submit, [first, second]))
    assert sum(result is not None for result in results) == 1
    day = datetime.now(timezone.utc).date().isoformat()
    counter = first.transport.client.get_item(
        TableName=first.transport.table,
        Key={"PK": {"S": "USAGE"}, "SK": {"S": day}},
        ConsistentRead=True,
    )["Item"]
    assert counter["count"]["N"] == "1"
    assert int(counter["expires_at"]["N"]) > time.time()

    winner = first if results[0] is not None else second
    winner_job = results[0] or results[1]
    assert winner_job is not None
    saved = winner.transport.get(
        winner.transport.partition(winner.workspace_id), f"JOB#{winner_job.run_id}"
    )
    fail(winner, saved, "Test completed")
    future = datetime.now(timezone.utc) + timedelta(days=1)

    class Tomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr("switchboard.jobs.datetime", Tomorrow)
    assert submit_job(winner, sqs).run_id != winner_job.run_id


def test_submission_window_limits_new_jobs_without_blocking_idempotent_retries(
    queued, monkeypatch
):
    storage, sqs, _, _ = queued
    monkeypatch.setenv("INVESTIGATION_WINDOW_LIMIT", "1")
    key = uuid4()
    job = submit_job(storage, sqs, key)
    assert submit_job(storage, sqs, key).run_id == job.run_id
    with pytest.raises(StorageError, match="few minutes") as denied:
        submit_job(storage, sqs, ticket="CHG-1045")
    assert denied.value.status == 429
    future = datetime.now(timezone.utc) + timedelta(minutes=5)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    monkeypatch.setattr("switchboard.jobs.datetime", Later)
    assert submit_job(storage, sqs, ticket="CHG-1045").run_id != job.run_id


def test_maintenance_removes_only_expired_submission_counters(queued):
    storage, _, _, _ = queued
    store = storage.transport
    for period, expires in [
        ("expired", time.time() - 60),
        ("current", time.time() + 86400),
    ]:
        store.client.put_item(
            TableName=store.table,
            Item={
                "PK": {"S": "USAGE"},
                "SK": {"S": period},
                "count": {"N": "1"},
                "expires_at": {"N": str(int(expires))},
            },
        )
    assert maintain()["deleted"] == 1
    assert "Item" not in store.client.get_item(
        TableName=store.table, Key={"PK": {"S": "USAGE"}, "SK": {"S": "expired"}}
    )
    assert (
        store.client.get_item(
            TableName=store.table, Key={"PK": {"S": "USAGE"}, "SK": {"S": "current"}}
        )["Item"]["count"]["N"]
        == "1"
    )


def test_save_before_send_failure_and_hourly_recovery(queued):
    storage, sqs, queue, _ = queued

    class Unavailable:
        def send_message(self, **arguments):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "temporary"}},
                "SendMessage",
            )

    request_key = uuid4()
    with pytest.raises(StorageError, match="Submission saved"):
        submit_job(storage, Unavailable(), request_key)
    store = storage.transport
    saved = store.query(store.partition(storage.workspace_id), "JOB#")[0]
    assert saved["dispatch"] == "pending"
    assert maintain()["dispatched"] == 1
    assert submit_job(storage, sqs, request_key).run_id == UUID(saved["id"])
    assert sqs.receive_message(QueueUrl=queue)["Messages"]


def test_worker_before_send_ack_does_not_rewind_completion(queued):
    storage, sqs, queue, _ = queued

    class Immediate:
        def send_message(self, **arguments):
            response = sqs.send_message(**arguments)
            process_message(json.loads(arguments["MessageBody"]))
            return response

    job = submit_job(storage, Immediate())
    assert job.status == "completed"
    assert (
        read(storage=storage, employee_id="emp-alex", run_id=job.run_id).status
        == "completed"
    )


def test_failed_publication_duplicate_lease_and_recovery(queued, monkeypatch):
    storage, sqs, _, _ = queued
    job = submit_job(storage, sqs)
    original = DynamoStore.save_run

    def unavailable(self, workspace, run):
        raise StorageError("Temporary publication failure", status=503)

    monkeypatch.setattr(DynamoStore, "save_run", unavailable)
    with pytest.raises(StorageError, match="publication"):
        process_message(message(storage, job.run_id))
    with pytest.raises(StorageError, match="already running"):
        process_message(message(storage, job.run_id))
    store = storage.transport
    partition = store.partition(storage.workspace_id)
    claimed = store.get(partition, f"JOB#{job.run_id}")
    store.transaction(
        storage.workspace_id,
        [
            store.replace(
                partition, f"JOB#{job.run_id}", claimed, {**claimed, "lease_until": 0}
            )
        ],
    )
    monkeypatch.setattr(DynamoStore, "save_run", original)
    process_message(message(storage, job.run_id))
    assert (
        read(storage=storage, employee_id="emp-alex", run_id=job.run_id).status
        == "completed"
    )
    assert len(storage.list_runs(ticket_id="CHG-1042")) == 1


def test_dlq_terminal_failure_and_access_revocation(queued):
    storage, sqs, _, dlq = queued
    job = submit_job(storage, sqs)
    sqs.send_message(QueueUrl=dlq, MessageBody=json.dumps(message(storage, job.run_id)))
    assert maintain()["failed"] == 1
    assert (
        read(storage=storage, employee_id="emp-alex", run_id=job.run_id).status
        == "failed"
    )
    process_message(message(storage, job.run_id))
    assert not storage.list_runs(ticket_id="CHG-1042")
    next_job = submit_job(storage, sqs)
    store = storage.transport
    partition = store.partition(storage.workspace_id)
    employee = store.get(partition, "EMPLOYEE#emp-alex")
    store.transaction(
        storage.workspace_id,
        [
            store.replace(
                partition, "EMPLOYEE#emp-alex", employee, {**employee, "active": False}
            )
        ],
    )
    process_message(message(storage, next_job.run_id))
    assert store.get(partition, f"JOB#{next_job.run_id}")["status"] == "failed"


def test_rejected_report_logs_diagnostics_without_exposing_them_in_public_results(
    queued, monkeypatch, caplog
):
    storage, sqs, _, _ = queued
    job = submit_job(storage, sqs)

    def reject_report(**arguments):
        raise ReportValidationError(
            "Policy review returned invalid output",
            diagnostics={
                "stage": "policy_review",
                "rejected_output": "Invalid reviewer JSON",
                "rejected_report": {"outcome": "blocked"},
            },
        )

    monkeypatch.setattr(
        "switchboard.investigation.fixtures.investigate_ticket_fixture", reject_report
    )
    with caplog.at_level("INFO", logger="switchboard.jobs"):
        process_message(message(storage, job.run_id))

    assert (
        read(storage=storage, employee_id="emp-alex", run_id=job.run_id).status
        == "failed"
    )
    rejected = next(r for r in caplog.records if r.message == "Investigation rejected")
    assert rejected.error_type == "ReportValidationError"
    assert rejected.run_id == str(job.run_id)
    assert rejected.error_reason == "Policy review returned invalid output"
    assert rejected.report_validation["rejected_output"] == "Invalid reviewer JSON"
    public_result = read(storage=storage, employee_id="emp-alex", run_id=job.run_id)
    assert "Invalid reviewer JSON" not in public_result.model_dump_json()
    assert "report_validation" not in public_result.model_dump()
    assert not storage.list_runs(ticket_id="CHG-1042")


def test_replaced_owner_cannot_publish(queued):
    storage, sqs, _, _ = queued
    job = submit_job(storage, sqs)
    store = storage.transport
    partition = store.partition(storage.workspace_id)
    saved = store.get(partition, f"JOB#{job.run_id}")
    old = {
        **saved,
        "status": "running",
        "owner": "old",
        "lease_until": time.time() + 330,
    }
    store.transaction(
        storage.workspace_id,
        [store.replace(partition, f"JOB#{job.run_id}", saved, old)],
    )
    replacement = {**old, "owner": "replacement"}
    store.transaction(
        storage.workspace_id,
        [store.replace(partition, f"JOB#{job.run_id}", old, replacement)],
    )
    store.lease = old
    with pytest.raises(StorageError, match="Records have changed"):
        store.save_proposal(storage.workspace_id, {"id": "stale", "created_at": "now"})
    assert store.get(partition, "PROPOSAL#stale") is None


def test_empty_model_response_is_retried_without_publishing(
    queued, monkeypatch, caplog
):
    storage, sqs, _, _ = queued
    job = submit_job(storage, sqs)
    previous_proposals = storage.list_pending_proposals()
    target = "switchboard.investigation.fixtures.investigate_ticket_fixture"

    def empty_response(**arguments):
        raise EmptyModelResponseError(
            diagnostics={"stage": "policy_review", "rejected_output": ""}
        )

    with monkeypatch.context() as patch:
        patch.setattr(target, empty_response)
        with pytest.raises(EmptyModelResponseError):
            process_message(message(storage, job.run_id))

    assert not storage.list_runs(ticket_id="CHG-1042")
    assert storage.list_pending_proposals() == previous_proposals
    retry = next(
        r for r in caplog.records if r.message == "Investigation awaits SQS retry"
    )
    assert retry.error_type == "EmptyModelResponseError"
    assert retry.report_validation["rejected_output"] == ""
    public = read(storage=storage, employee_id="emp-alex", run_id=job.run_id)
    assert public.status == "running"
    assert "report_validation" not in public.model_dump()

    # SQS redelivery after the owner lease expires must recover the same job.
    store = storage.transport
    partition = store.partition(storage.workspace_id)
    claimed = store.get(partition, f"JOB#{job.run_id}")
    store.transaction(
        storage.workspace_id,
        [
            store.replace(
                partition, f"JOB#{job.run_id}", claimed, {**claimed, "lease_until": 0}
            )
        ],
    )
    process_message(message(storage, job.run_id))
    assert (
        read(storage=storage, employee_id="emp-alex", run_id=job.run_id).status
        == "completed"
    )
    assert len(storage.list_runs(ticket_id="CHG-1042")) == 1
    assert store.get(partition, f"JOB#{job.run_id}")["attempt"] == 2


def test_maintenance_does_not_dispatch_revoked_pending_work(queued):
    storage, sqs, _, _ = queued
    job = submit_job(storage, sqs)
    store = storage.transport
    partition = store.partition(storage.workspace_id)
    current = store.get(partition, f"JOB#{job.run_id}")
    employee = store.get(partition, "EMPLOYEE#emp-alex")
    store.transaction(
        storage.workspace_id,
        [
            store.replace(
                partition,
                f"JOB#{job.run_id}",
                current,
                {**current, "dispatch": "pending"},
            ),
            store.replace(
                partition, "EMPLOYEE#emp-alex", employee, {**employee, "active": False}
            ),
        ],
    )
    assert maintain()["dispatched"] == 0
    assert store.get(partition, f"JOB#{job.run_id}")["dispatch"] == "pending"
