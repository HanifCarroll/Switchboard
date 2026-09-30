"""Failure and concurrency checks for persisted jobs and native event entry points."""

import json
import time
from uuid import UUID, uuid4

import boto3
import pytest
from botocore.exceptions import ClientError

from switchboard.demo.portfolio import initialize_demo_portfolio
from switchboard.dynamodb import DynamoStore
from switchboard.investigation.report_validation import ReportValidationError
from switchboard.jobs import process_message, read, submit
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


def test_rejected_report_logs_the_error_type_without_model_content(
    queued, monkeypatch, caplog
):
    storage, sqs, _, _ = queued
    job = submit_job(storage, sqs)

    def reject_report(**arguments):
        raise ReportValidationError("Private model content must not reach logs")

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
    assert "Private model content" not in caplog.text
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
