"""DynamoDB's named workspace operations and transactional business receipts."""

import hashlib
import json
import os
import random
import time
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

from botocore.exceptions import ClientError

from switchboard.aws_clients import aws_client
from switchboard.storage import StorageError

CHUNK_BYTES = 280 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def key(partition: str, sort: str) -> dict:
    return {"PK": {"S": partition}, "SK": {"S": sort}}


class DynamoStore:
    """A request's workspace generation; transactions fence every business write."""

    def __init__(self, table: str | None = None, client: Any = None):
        self.table = table or os.environ["DYNAMODB_TABLE"]
        self.client = client or aws_client("dynamodb")
        self.generations: dict[str, str] = {}
        self.staging: set[str] = set()
        self.lease: dict | None = None

    def get(self, partition: str, sort: str) -> Any:
        response = self.client.get_item(
            TableName=self.table, Key=key(partition, sort), ConsistentRead=True
        )
        item = response.get("Item")
        return json.loads(item["body"]["S"]) if item else None

    def query(self, partition: str, prefix: str) -> list:
        records = []
        arguments: dict[str, Any] = {
            "TableName": self.table,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": {"S": partition},
                ":prefix": {"S": prefix},
            },
            "ConsistentRead": True,
        }
        while True:
            response = self.client.query(**arguments)
            records.extend(json.loads(item["body"]["S"]) for item in response["Items"])
            if "LastEvaluatedKey" not in response:
                return records
            arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def put(self, partition: str, sort: str, body: Any, *, absent=False) -> dict:
        item = {**key(partition, sort), "body": {"S": encode(body)}}
        operation = {"TableName": self.table, "Item": item}
        if absent:
            operation["ConditionExpression"] = "attribute_not_exists(PK)"
        return {"Put": operation}

    def compare(self, partition: str, sort: str, previous: Any) -> dict:
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": key(partition, sort),
                "ConditionExpression": "#body = :previous",
                "ExpressionAttributeNames": {"#body": "body"},
                "ExpressionAttributeValues": {":previous": {"S": encode(previous)}},
            }
        }

    def replace(self, partition: str, sort: str, previous: Any, body: Any) -> dict:
        operation = self.put(partition, sort, body)
        operation["Put"].update(
            ConditionExpression="#body = :previous",
            ExpressionAttributeNames={"#body": "body"},
            ExpressionAttributeValues={":previous": {"S": encode(previous)}},
        )
        return operation

    def delete(self, partition: str, sort: str) -> dict:
        return {"Delete": {"TableName": self.table, "Key": key(partition, sort)}}

    def transaction(
        self, workspace: str, operations: list, *, completion=False, failure=False
    ):
        # 1. Fence a request against workspace reset and a worker against replacement.
        if workspace not in self.staging:
            metadata = self.metadata(workspace)
            generation = self.generation(workspace)
            if not metadata or metadata["generation"] != generation:
                raise StorageError("Workspace has changed", status=409)
            operations.append(
                {
                    "ConditionCheck": {
                        "TableName": self.table,
                        "Key": key(f"WS#{workspace}", "META"),
                        "ConditionExpression": "generation = :generation",
                        "ExpressionAttributeValues": {":generation": {"S": generation}},
                    }
                }
            )
        if self.lease is not None and not failure:
            from switchboard.investigation.deadline import require_time
            from switchboard.jobs import authorize
            from switchboard.storage import WorkspaceStorage

            require_time(10)
            if self.lease["lease_until"] <= time.time():
                raise StorageError("Worker lease expired", status=503)
            authorize(WorkspaceStorage(workspace, transport=self), self.lease)
            employee_key = f"EMPLOYEE#{self.lease['requester_employee_id']}"
            ticket_key = f"TICKET#{self.lease['ticket_id']}"
            employee = self.get(self.partition(workspace), employee_key)
            ticket = self.get(self.partition(workspace), ticket_key)
            if (
                not employee
                or not employee["active"]
                or employee["role"] != self.lease["requester_role"]
                or any(
                    customer not in employee["customer_ids"]
                    for customer in self.lease["customer_ids"]
                )
                or not ticket
                or ticket["assigned_employee_id"] != self.lease["requester_employee_id"]
            ):
                raise PermissionError("Investigation access has changed")
            operations.extend(
                [
                    self.compare(self.partition(workspace), employee_key, employee),
                    self.compare(self.partition(workspace), ticket_key, ticket),
                ]
            )
            if not completion:
                operations.append(
                    self.compare(
                        self.partition(workspace), f"JOB#{self.lease['id']}", self.lease
                    )
                )

        # 2. Commit with one token, retrying only temporary transaction conflicts.
        request_token = str(uuid4())
        for attempt in range(4):
            try:
                self.client.transact_write_items(
                    TransactItems=operations, ClientRequestToken=request_token
                )
                return
            except ClientError as error:
                code = error.response["Error"]["Code"]
                if code == "TransactionCanceledException":
                    reasons = error.response.get("CancellationReasons", [])
                    reason_codes = {
                        item.get("Code") for item in reasons if item.get("Code")
                    }
                    if "ConditionalCheckFailed" in reason_codes:
                        raise StorageError("Records have changed", status=409) from None

                    if (
                        reason_codes - {"None"} == {"TransactionConflict"}
                        and attempt < 3
                    ):
                        time.sleep(random.uniform(0.05, 0.1 * 2**attempt))
                        continue

                raise StorageError("Storage service unavailable", status=503) from error

    def metadata(self, workspace: str) -> dict | None:
        return self.get(f"WS#{workspace}", "META")

    def generation(self, workspace: str) -> str:
        if workspace not in self.generations:
            metadata = self.metadata(workspace)
            if not metadata:
                raise StorageError("Workspace unavailable", status=404)
            self.generations[workspace] = metadata["generation"]
        return self.generations[workspace]

    def partition(self, workspace: str) -> str:
        return f"WS#{workspace}#GEN#{self.generation(workspace)}"

    @contextmanager
    def initialize(self, workspace: str):
        """Keep seeded records private until all domain initialization succeeds."""
        previous = self.metadata(workspace)
        self.generations[workspace] = str(uuid4())
        self.staging.add(workspace)
        try:
            self.transaction(
                workspace,
                [
                    self.put(
                        self.partition(workspace),
                        "STAGING",
                        {"created_at": time.time()},
                    )
                ],
            )
            yield
            metadata = self.pending_metadata
            operation = self.put(
                f"WS#{workspace}", "META", metadata, absent=not previous
            )
            operation["Put"]["Item"]["generation"] = {"S": metadata["generation"]}
            if previous:
                operation["Put"].update(
                    ConditionExpression="generation = :previous",
                    ExpressionAttributeValues={
                        ":previous": {"S": previous["generation"]}
                    },
                )
            self.transaction(workspace, [operation])
        finally:
            self.staging.remove(workspace)

    def reset(self, workspace: str, payload: dict):
        if workspace not in self.staging:
            with self.initialize(workspace):
                self.reset(workspace, payload)
            return

        partition = self.partition(workspace)
        self.pending_metadata = {
            field: payload[field]
            for field in ("identityMode", "scenarioId", "inputs", "updatedAt")
        }
        self.pending_metadata["generation"] = self.generation(workspace)
        for collection, kind in (
            ("employees", "EMPLOYEE"),
            ("customers", "CUSTOMER"),
            ("integrations", "INTEGRATION"),
            ("tickets", "TICKET"),
            ("policies", "POLICY"),
        ):
            for record in payload["records"][collection]:
                self.transaction(
                    workspace, [self.put(partition, f"{kind}#{record['id']}", record)]
                )

    def save_unique(self, workspace: str, sort: str, body: dict, field: str) -> dict:
        partition = self.partition(workspace)
        existing = self.get(partition, sort)
        if existing:
            return {field: existing, "wasCreated": False}
        try:
            self.transaction(workspace, [self.put(partition, sort, body, absent=True)])
        except StorageError as error:
            existing = self.get(partition, sort)
            if error.status != 409 or not existing:
                raise
            return {field: existing, "wasCreated": False}
        return {field: body, "wasCreated": True}

    def save_proposal(self, workspace: str, proposal: dict) -> dict:
        # 1. Match the SQL business fingerprint, excluding receipt ID/time.
        fingerprint = {
            name: value
            for name, value in proposal.items()
            if name not in {"id", "created_at"}
        }
        digest = hashlib.sha256(encode(fingerprint).encode()).hexdigest()
        partition = self.partition(workspace)
        pointer_key = f"FINGERPRINT#{digest}"
        existing = self.get(partition, pointer_key)
        if existing:
            return {
                "proposal": self.get(partition, f"PROPOSAL#{existing}"),
                "wasCreated": False,
            }

        # 2. Publish the proposal and canonical pointer in the same transaction.
        try:
            self.transaction(
                workspace,
                [
                    self.put(
                        partition, f"PROPOSAL#{proposal['id']}", proposal, absent=True
                    ),
                    self.put(partition, pointer_key, proposal["id"], absent=True),
                ],
            )
        except StorageError as error:
            existing = self.get(partition, pointer_key)
            if error.status != 409 or not existing:
                raise
            return {
                "proposal": self.get(partition, f"PROPOSAL#{existing}"),
                "wasCreated": False,
            }
        return {"proposal": proposal, "wasCreated": True}

    def apply_execution(self, workspace: str, payload: dict) -> dict:
        # 1. Return an existing receipt before considering another version change.
        partition = self.partition(workspace)
        execution = payload["execution"]
        receipt_key = f"EXECUTION#{execution['proposal_id']}"
        existing = self.get(partition, receipt_key)
        if existing:
            return {"execution": existing, "wasCreated": False}
        integration_key = f"INTEGRATION#{payload['integrationId']}"
        integration = self.get(partition, integration_key)
        if not integration or (
            integration["customer_id"] != payload["customerId"]
            or integration["version"] != payload["expectedVersion"]
            or integration["endpoint"] != payload["currentEndpoint"]
        ):
            raise StorageError("Configuration changed since proposal", status=409)

        # 2. Compare the exact record and commit the change with its receipt.
        updated = {
            **integration,
            "version": integration["version"] + 1,
            "endpoint": payload["proposedEndpoint"],
        }
        try:
            self.transaction(
                workspace,
                [
                    self.replace(partition, integration_key, integration, updated),
                    self.put(partition, receipt_key, execution, absent=True),
                ],
            )
        except StorageError as error:
            existing = self.get(partition, receipt_key)
            if error.status != 409 or not existing:
                raise
            return {"execution": existing, "wasCreated": False}
        return {"execution": execution, "wasCreated": True}

    def verify(self, workspace: str, verification: dict) -> dict:
        # 1. Check the execution, current destination/version, and ticket.
        partition = self.partition(workspace)
        receipt_key = f"VERIFICATION#{verification['execution_id']}"
        existing = self.get(partition, receipt_key)
        if existing:
            return {"verification": existing, "wasCreated": False}
        proposal = self.get(partition, f"PROPOSAL#{verification['proposal_id']}")
        execution = self.get(partition, f"EXECUTION#{verification['proposal_id']}")
        if (
            not proposal
            or not execution
            or execution["id"] != verification["execution_id"]
        ):
            raise StorageError("Delivery verification requirements changed", status=409)
        integration_key = f"INTEGRATION#{proposal['integration_id']}"
        integration = self.get(partition, integration_key)
        ticket_key = f"TICKET#{proposal['ticket_id']}"
        ticket = self.get(partition, ticket_key)
        if (
            not ticket
            or not integration
            or (
                integration["endpoint"] != verification["destination"]
                or integration["version"]
                != execution["resulting_configuration_version"]
            )
        ):
            raise StorageError("Delivery verification requirements changed", status=409)
        if verification["outcome"] not in {"delivered", "failed", "inconclusive"}:
            raise StorageError("Invalid verification outcome", status=400)

        # 2. Publish evidence and ticket outcome without a partial receipt.
        updated = {
            **ticket,
            "status": "closed"
            if verification["outcome"] == "delivered"
            else "needs_attention",
        }
        try:
            self.transaction(
                workspace,
                [
                    self.compare(partition, integration_key, integration),
                    self.put(partition, receipt_key, verification, absent=True),
                    self.replace(partition, ticket_key, ticket, updated),
                ],
            )
        except StorageError as error:
            existing = self.get(partition, receipt_key)
            if error.status != 409 or not existing:
                raise
            return {"verification": existing, "wasCreated": False}
        return {"verification": verification, "wasCreated": True}

    def chunk(self, workspace: str, prefix: str, value: dict) -> dict:
        content = encode(value).encode()
        if len(content) > MAX_RESULT_BYTES:
            raise ValueError("Investigation result exceeds the buffered response limit")
        partition = self.partition(workspace)
        version = str(uuid4())
        chunks = [
            content[index : index + CHUNK_BYTES]
            for index in range(0, len(content), CHUNK_BYTES)
        ]
        for index, chunk in enumerate(chunks):
            item = {
                **key(partition, f"CHUNK#{prefix}#{version}#{index:04}"),
                "bytes": {"B": chunk},
            }
            self.transaction(
                workspace, [{"Put": {"TableName": self.table, "Item": item}}]
            )
        return {
            "prefix": prefix,
            "version": version,
            "count": len(chunks),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def hydrate(self, workspace: str, manifest: dict) -> dict:
        content = bytearray()
        for index in range(manifest["count"]):
            response = self.client.get_item(
                TableName=self.table,
                Key=key(
                    self.partition(workspace),
                    f"CHUNK#{manifest['prefix']}#{manifest['version']}#{index:04}",
                ),
                ConsistentRead=True,
            )
            if "Item" not in response:
                raise StorageError("Incomplete investigation result", status=503)
            content.extend(response["Item"]["bytes"]["B"])
        if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise StorageError("Invalid investigation result", status=503)
        return json.loads(content)

    def save_run(self, workspace: str, run: dict):
        # 1. Write immutable chunks before making a result visible.
        partition = self.partition(workspace)
        manifest = self.chunk(workspace, f"RUN#{run['id']}", run["result"])
        record = {name: value for name, value in run.items() if name != "result"}
        record["manifest"] = manifest
        operations = [
            self.put(partition, f"RUN#{run['id']}", record, absent=True),
            self.put(
                partition,
                f"HISTORY#{run['ticket_id']}#{run['created_at']}#{run['id']}",
                run["id"],
            ),
        ]
        proposal = run["result"].get("proposal")
        if proposal:
            operations.append(
                self.put(partition, f"PROPOSALRUN#{proposal['id']}", run["id"])
            )

        # 2. A worker publishes its result, terminal state and marker release together.
        if self.lease:
            from switchboard.jobs import release_marker

            completed = {**self.lease, "status": "completed", "dispatch": "sent"}
            operations.extend(
                [
                    self.replace(partition, f"JOB#{run['id']}", self.lease, completed),
                    release_marker(self, partition, self.lease),
                ]
            )
        self.transaction(workspace, operations, completion=True)

    def save_checkpoint(
        self,
        workspace: str,
        result: dict,
        *,
        stage: Literal["draft", "validated"] = "validated",
    ):
        if self.lease is None:
            return
        prefix = "DRAFTCHECKPOINT" if stage == "draft" else "CHECKPOINT"
        checkpoint_key = f"{prefix}#{self.lease['id']}"
        manifest = self.chunk(workspace, checkpoint_key, result)
        self.transaction(
            workspace,
            [
                self.put(
                    self.partition(workspace),
                    checkpoint_key,
                    manifest,
                )
            ],
        )

    def load_checkpoint(
        self,
        workspace: str,
        *,
        stage: Literal["draft", "validated"] = "validated",
    ) -> dict | None:
        if self.lease is None:
            return None
        prefix = "DRAFTCHECKPOINT" if stage == "draft" else "CHECKPOINT"
        checkpoint_key = f"{prefix}#{self.lease['id']}"
        manifest = self.get(self.partition(workspace), checkpoint_key)
        return self.hydrate(workspace, manifest) if manifest else None

    def load_run(self, workspace: str, run_id: str) -> dict | None:
        run = self.get(self.partition(workspace), f"RUN#{run_id}")
        if run:
            manifest = run.pop("manifest")
            run["result"] = self.hydrate(workspace, manifest)
        return run

    def __call__(self, operation: str, workspace: str, payload: dict) -> Any:
        try:
            return self.call(operation, workspace, payload)
        except ClientError as error:
            raise StorageError("Storage service unavailable", status=503) from error

    def call(self, operation: str, workspace: str, payload: dict) -> Any:
        # 1. Workspace metadata stays outside the replaceable generation.
        if operation == "workspace.get":
            if workspace in self.staging:
                return self.pending_metadata
            metadata = self.metadata(workspace)
            if metadata:
                self.generations.setdefault(workspace, metadata["generation"])
            return metadata
        if operation == "workspace.reset":
            return self.reset(workspace, payload)
        if operation in {"workspace.delete", "workspace.touch"}:
            metadata = self.metadata(workspace)
            if not metadata:
                return None
            if operation == "workspace.delete":
                action = self.delete(f"WS#{workspace}", "META")
                action["Delete"].update(
                    ConditionExpression="generation = :generation",
                    ExpressionAttributeValues={
                        ":generation": {"S": metadata["generation"]}
                    },
                )
            else:
                updated = {**metadata, "updatedAt": payload["updatedAt"]}
                action = self.replace(f"WS#{workspace}", "META", metadata, updated)
                action["Put"]["Item"]["generation"] = {"S": metadata["generation"]}
            # Metadata operations cannot also include a condition on the same item.
            try:
                self.client.transact_write_items(TransactItems=[action])
            except ClientError as error:
                if error.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
            return None

        # 2. Typed keys provide all browser reads without a table scan or index.
        partition = self.partition(workspace)
        kind, verb = operation.split(".")
        if operation == "employee.assignments":
            employee = self.get(partition, f"EMPLOYEE#{payload['employeeId']}")
            return employee["customer_ids"] if employee else []
        if operation == "employee.name":
            employee = self.get(partition, f"EMPLOYEE#{payload['id']}")
            return employee["name"] if employee else None
        if operation == "employee.list":
            return sorted(
                (
                    employee
                    for employee in self.query(partition, "EMPLOYEE#")
                    if employee["active"]
                ),
                key=lambda item: item["name"],
            )
        if verb == "get" and kind not in {"run"}:
            identifier = payload.get(
                "id", payload.get("proposalId", payload.get("executionId"))
            )
            return self.get(partition, f"{kind.upper()}#{identifier}")
        if operation in {"ticket.list", "policy.list"}:
            return self.query(partition, f"{kind.upper()}#")
        if operation == "proposal.list":
            proposals = self.query(partition, "PROPOSAL#")
            return sorted(
                (
                    proposal
                    for proposal in proposals
                    if not self.get(partition, f"APPROVAL#{proposal['id']}")
                    and not self.get(partition, f"EXECUTION#{proposal['id']}")
                ),
                key=lambda item: item["created_at"],
                reverse=True,
            )
        if operation == "proposal.save":
            return self.save_proposal(workspace, payload["proposal"])
        if operation == "approval.save":
            approval = payload["approval"]
            return self.save_unique(
                workspace, f"APPROVAL#{approval['proposal_id']}", approval, "approval"
            )["approval"]
        if operation == "execution.apply":
            return self.apply_execution(workspace, payload)
        if operation == "verification.record":
            return self.verify(workspace, payload["verification"])
        if operation == "run.save":
            return self.save_run(workspace, payload["run"])
        if operation == "run.get":
            return self.load_run(workspace, payload["id"])
        if operation == "run.list":
            identifiers = self.query(partition, f"HISTORY#{payload['ticketId']}#")
            return [
                self.load_run(workspace, identifier)
                for identifier in reversed(identifiers)
            ]
        if operation == "run.findProposal":
            return self.get(partition, f"PROPOSALRUN#{payload['proposalId']}")
        raise StorageError(f"Unsupported storage operation: {operation}", status=400)
