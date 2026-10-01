"""Named DynamoDB operations for one isolated workspace."""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

StorageTransport = Callable[[str, str, dict], Any]


class StorageError(RuntimeError):
    def __init__(self, message: str, *, status: int = 500):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WorkspaceStorage:
    """Call explicit domain operations for one isolated workspace."""

    workspace_id: str
    transport: StorageTransport

    @classmethod
    def from_environment(cls, *, workspace_id: str) -> "WorkspaceStorage":
        from switchboard.dynamodb import DynamoStore

        return cls(workspace_id=workspace_id, transport=DynamoStore())

    def initialization(self):
        from switchboard.dynamodb import DynamoStore

        if isinstance(self.transport, DynamoStore):
            return self.transport.initialize(self.workspace_id)
        return nullcontext()

    def discard_failed_initialization(self) -> None:
        from switchboard.dynamodb import DynamoStore

        if not isinstance(self.transport, DynamoStore):
            self.delete_workspace()

    def for_workspace(self, *, workspace_id: str) -> "WorkspaceStorage":
        return WorkspaceStorage(
            workspace_id=workspace_id,
            transport=self.transport,
        )

    def get_workspace(self) -> dict | None:
        return self._call("workspace.get")

    def reset_workspace(self, *, payload: dict) -> None:
        from switchboard.demo.receivers import bind_receiver_destinations

        payload = bind_receiver_destinations(payload)
        self._call("workspace.reset", payload)

    def delete_workspace(self) -> None:
        self._call("workspace.delete")

    def touch_workspace(self, *, updated_at: str) -> None:
        self._call("workspace.touch", {"updatedAt": updated_at})

    def get_employee(self, *, employee_id: str) -> dict | None:
        return self._call("employee.get", {"id": employee_id})

    def get_employee_name(self, *, employee_id: str) -> str | None:
        return self._call("employee.name", {"id": employee_id})

    def list_active_employees(self) -> list[dict]:
        return self._call("employee.list")

    def get_employee_assignments(self, *, employee_id: str) -> list[str]:
        return self._call("employee.assignments", {"employeeId": employee_id})

    def get_customer(self, *, customer_id: str) -> dict | None:
        return self._call("customer.get", {"id": customer_id})

    def get_integration(self, *, integration_id: str) -> dict | None:
        return self._call("integration.get", {"id": integration_id})

    def get_ticket(self, *, ticket_id: str) -> dict | None:
        return self._call("ticket.get", {"id": ticket_id})

    def list_tickets(self) -> list[dict]:
        return self._call("ticket.list")

    def list_policies(self) -> list[dict]:
        return self._call("policy.list")

    def get_proposal(self, *, proposal_id: str) -> dict | None:
        return self._call("proposal.get", {"id": proposal_id})

    def list_pending_proposals(self) -> list[dict]:
        return self._call("proposal.list")

    def save_proposal(self, *, proposal: dict) -> dict:
        return self._call("proposal.save", {"proposal": proposal})

    def get_approval(self, *, proposal_id: str) -> dict | None:
        return self._call("approval.get", {"proposalId": proposal_id})

    def save_approval(self, *, approval: dict) -> dict:
        return self._call("approval.save", {"approval": approval})

    def get_execution(self, *, proposal_id: str) -> dict | None:
        return self._call("execution.get", {"proposalId": proposal_id})

    def get_delivery_verification(self, *, execution_id: str) -> dict | None:
        return self._call("verification.get", {"executionId": execution_id})

    def record_delivery_verification(self, *, verification: dict) -> dict:
        return self._call("verification.record", {"verification": verification})

    def apply_execution(
        self,
        *,
        execution: dict,
        integration_id: str,
        customer_id: str,
        expected_version: int,
        current_endpoint: str,
        proposed_endpoint: str,
    ) -> dict:
        return self._call(
            "execution.apply",
            {
                "execution": execution,
                "integrationId": integration_id,
                "customerId": customer_id,
                "expectedVersion": expected_version,
                "currentEndpoint": current_endpoint,
                "proposedEndpoint": proposed_endpoint,
            },
        )

    def get_run(self, *, run_id: str) -> dict | None:
        return self._call("run.get", {"id": run_id})

    def list_runs(self, *, ticket_id: str) -> list[dict]:
        return self._call("run.list", {"ticketId": ticket_id})

    def save_run(self, *, run: dict) -> None:
        self._call("run.save", {"run": run})

    def find_proposal_run(self, *, proposal_id: str) -> str | None:
        return self._call("run.findProposal", {"proposalId": proposal_id})

    def _call(self, operation: str, payload: dict | None = None):
        return self.transport(operation, self.workspace_id, payload or {})
