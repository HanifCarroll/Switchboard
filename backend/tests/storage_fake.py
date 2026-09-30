"""In-memory implementation of the named storage operations used in tests."""

from copy import deepcopy
from threading import RLock

from switchboard.storage import StorageError, WorkspaceStorage


class MemoryWorkspaceStore:
    """Exercise application behavior through the same domain-operation boundary."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._workspaces: dict[str, dict] = {}

    def storage(self, *, workspace_id: str) -> WorkspaceStorage:
        return WorkspaceStorage(
            workspace_id=workspace_id,
            transport=self.handle,
        )

    def handle(self, operation: str, workspace_id: str, payload: dict):
        with self._lock:
            return deepcopy(self._dispatch(operation, workspace_id, payload))

    def _dispatch(self, operation: str, workspace_id: str, payload: dict):
        if operation == "workspace.get":
            workspace = self._workspaces.get(workspace_id)
            return self._workspace_metadata(workspace) if workspace else None
        if operation == "workspace.reset":
            self._workspaces[workspace_id] = self._new_workspace(payload)
            return None
        if operation == "workspace.delete":
            self._workspaces.pop(workspace_id, None)
            return None

        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            if operation.endswith((".get", ".name")) or operation in {
                "run.findProposal",
            }:
                return None
            if operation.endswith((".list", ".assignments")):
                return []
            raise StorageError("Workspace unavailable", status=404)

        if operation == "workspace.touch":
            workspace["updatedAt"] = payload["updatedAt"]
            return None
        if operation == "employee.get":
            return self._by_id(workspace["employees"], payload["id"])
        if operation == "employee.name":
            employee = self._by_id(workspace["employees"], payload["id"])
            return employee["name"] if employee else None
        if operation == "employee.list":
            return [
                {"id": item["id"], "name": item["name"], "role": item["role"]}
                for item in workspace["employees"]
                if item["active"]
            ]
        if operation == "employee.assignments":
            employee = self._by_id(workspace["employees"], payload["employeeId"])
            return employee["customer_ids"] if employee else []
        if operation == "customer.get":
            return self._by_id(workspace["customers"], payload["id"])
        if operation == "integration.get":
            return self._by_id(workspace["integrations"], payload["id"])
        if operation == "ticket.get":
            return self._by_id(workspace["tickets"], payload["id"])
        if operation == "ticket.list":
            return workspace["tickets"]
        if operation == "policy.list":
            return workspace["policies"]
        if operation == "proposal.get":
            return self._by_id(workspace["proposals"], payload["id"])
        if operation == "proposal.list":
            completed = {
                item["proposal_id"]
                for table in ("approvals", "executions")
                for item in workspace[table]
            }
            return [
                item for item in workspace["proposals"] if item["id"] not in completed
            ]
        if operation == "proposal.save":
            return self._save_proposal(workspace, payload["proposal"])
        if operation == "approval.get":
            return self._by_proposal(workspace["approvals"], payload["proposalId"])
        if operation == "approval.save":
            return self._save_approval(workspace, payload["approval"])
        if operation == "execution.get":
            return self._by_proposal(workspace["executions"], payload["proposalId"])
        if operation == "execution.apply":
            return self._apply_execution(workspace, payload)
        if operation == "verification.get":
            return next(
                (
                    item
                    for item in workspace["verifications"]
                    if item["execution_id"] == payload["executionId"]
                ),
                None,
            )
        if operation == "verification.record":
            return self._record_delivery_verification(
                workspace, payload["verification"]
            )
        if operation == "run.get":
            return self._by_id(workspace["runs"], payload["id"])
        if operation == "run.list":
            return sorted(
                (
                    item
                    for item in workspace["runs"]
                    if item["ticket_id"] == payload["ticketId"]
                ),
                key=lambda item: item["created_at"],
                reverse=True,
            )
        if operation == "run.save":
            if self._by_id(workspace["runs"], payload["run"]["id"]):
                raise StorageError("Investigation already exists", status=409)
            workspace["runs"].append(payload["run"])
            return None
        if operation == "run.findProposal":
            return self._find_proposal_run(workspace, payload["proposalId"])
        raise StorageError("Unknown storage operation", status=404)

    @staticmethod
    def _new_workspace(payload: dict) -> dict:
        records = deepcopy(payload["records"])
        return {
            "identityMode": payload["identityMode"],
            "scenarioId": payload["scenarioId"],
            "inputs": deepcopy(payload["inputs"]),
            "updatedAt": payload["updatedAt"],
            **records,
            "proposals": [],
            "approvals": [],
            "executions": [],
            "verifications": [],
            "runs": [],
        }

    @staticmethod
    def _workspace_metadata(workspace: dict) -> dict:
        return {
            key: deepcopy(workspace[key])
            for key in ("identityMode", "scenarioId", "inputs", "updatedAt")
        }

    @staticmethod
    def _by_id(records: list[dict], record_id: str) -> dict | None:
        return next((item for item in records if item["id"] == record_id), None)

    @staticmethod
    def _by_proposal(records: list[dict], proposal_id: str) -> dict | None:
        return next(
            (item for item in records if item["proposal_id"] == proposal_id), None
        )

    @staticmethod
    def _proposal_key(proposal: dict) -> tuple:
        return tuple(
            proposal[field]
            for field in (
                "proposed_by_employee_id",
                "ticket_id",
                "requester_contact_id",
                "customer_id",
                "integration_id",
                "environment",
                "current_endpoint",
                "proposed_endpoint",
                "expected_configuration_version",
                "recovery_plan",
                "status",
            )
        )

    def _save_proposal(self, workspace: dict, proposal: dict) -> dict:
        key = self._proposal_key(proposal)
        existing = next(
            (
                item
                for item in workspace["proposals"]
                if self._proposal_key(item) == key
            ),
            None,
        )
        if existing:
            return {"proposal": existing, "wasCreated": False}
        if self._by_id(workspace["proposals"], proposal["id"]):
            raise StorageError("Proposal could not be saved", status=409)
        workspace["proposals"].append(proposal)
        return {"proposal": proposal, "wasCreated": True}

    def _save_approval(self, workspace: dict, approval: dict) -> dict:
        existing = self._by_proposal(workspace["approvals"], approval["proposal_id"])
        if existing:
            return existing
        workspace["approvals"].append(approval)
        return approval

    def _apply_execution(self, workspace: dict, payload: dict) -> dict:
        execution = payload["execution"]
        existing = self._by_proposal(workspace["executions"], execution["proposal_id"])
        if existing:
            return {"execution": existing, "wasCreated": False}

        integration = self._by_id(workspace["integrations"], payload["integrationId"])
        if (
            integration is None
            or integration["customer_id"] != payload["customerId"]
            or integration["version"] != payload["expectedVersion"]
            or integration["endpoint"] != payload["currentEndpoint"]
        ):
            raise StorageError(
                "Configuration changed since the proposal was prepared", status=409
            )

        integration["endpoint"] = payload["proposedEndpoint"]
        integration["version"] = execution["resulting_configuration_version"]
        workspace["executions"].append(execution)
        return {"execution": execution, "wasCreated": True}

    def _record_delivery_verification(
        self, workspace: dict, verification: dict
    ) -> dict:
        existing = next(
            (
                item
                for item in workspace["verifications"]
                if item["execution_id"] == verification["execution_id"]
            ),
            None,
        )
        if existing:
            return {"verification": existing, "wasCreated": False}

        execution = self._by_id(workspace["executions"], verification["execution_id"])
        proposal = self._by_id(workspace["proposals"], verification["proposal_id"])
        if execution is None or proposal is None:
            raise StorageError("Execution unavailable", status=409)

        ticket = self._by_id(workspace["tickets"], proposal["ticket_id"])
        if ticket is None:
            raise StorageError("Ticket unavailable", status=409)

        workspace["verifications"].append(verification)
        ticket["status"] = (
            "closed" if verification["outcome"] == "delivered" else "needs_attention"
        )
        return {"verification": verification, "wasCreated": True}

    @staticmethod
    def _find_proposal_run(workspace: dict, proposal_id: str) -> str | None:
        for run in sorted(
            workspace["runs"], key=lambda item: item["created_at"], reverse=True
        ):
            proposal = run["result"].get("proposal")
            if proposal and proposal["id"] == proposal_id:
                return run["id"]
        return None
