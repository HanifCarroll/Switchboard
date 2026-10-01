"""Verify delivery after an endpoint-change execution."""

import json
from datetime import datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from switchboard.change_management import get_proposal
from switchboard.integrations import delivery_service
from switchboard.integrations.configuration_service import get_integration
from switchboard.integrations.employee_directory import CONFIG_ROLES, EmployeeSession
from switchboard.models import (
    DeliveryVerification,
    Execution,
    VerifyDeliveryResult,
)
from switchboard.storage import StorageError


def _from_storage(model, record: dict):
    return model.model_validate_json(json.dumps(record))


def verify_execution_delivery(
    *, proposal_id: str, session: EmployeeSession, verified_at: datetime
) -> VerifyDeliveryResult:
    """Test delivery once after confirming the executed configuration is still active."""
    if verified_at.utcoffset() is None:
        raise ValueError("Verification time must be timezone-aware")

    # 1. Recheck employee access and require a recorded execution.
    proposal = get_proposal(session=session, proposal_id=proposal_id)
    session.require_customer_access(
        customer_id=proposal.customer_id,
        allowed_roles=CONFIG_ROLES,
    )
    execution_record = session.storage.get_execution(proposal_id=proposal.id)
    if execution_record is None:
        raise ValueError("Delivery cannot be verified before execution")
    execution = _from_storage(Execution, execution_record)
    if verified_at < execution.executed_at:
        raise ValueError("Verification time precedes execution")

    # 2. Return durable evidence before attempting another test event.
    existing = session.storage.get_delivery_verification(execution_id=execution.id)
    if existing is not None:
        return VerifyDeliveryResult(
            verification=_from_storage(DeliveryVerification, existing),
            was_created=False,
        )

    # 3. Confirm the executed endpoint and version are still active.
    integration = get_integration(
        session=session,
        integration_id=proposal.integration_id,
    )
    if (
        integration.endpoint != proposal.proposed_endpoint
        or integration.version != execution.resulting_configuration_version
    ):
        raise ValueError("Configuration changed after execution")

    # 4. Send an event with a stable identity and persist its observed outcome.
    test_event_id = str(uuid5(NAMESPACE_URL, f"switchboard:{execution.id}:delivery"))
    test_result = delivery_service.send_synthetic_test_event(
        destination=str(integration.endpoint),
        test_event_id=test_event_id,
        storage=session.storage,
    )
    verification = DeliveryVerification(
        id=str(uuid4()),
        execution_id=execution.id,
        proposal_id=proposal.id,
        verified_by_employee_id=session.employee_id,
        outcome=test_result.outcome,
        test_event_id=test_result.test_event_id,
        destination=integration.endpoint,
        evidence=test_result.evidence,
        verified_at=verified_at,
    )
    try:
        saved = session.storage.record_delivery_verification(
            verification=verification.model_dump(mode="json")
        )
    except StorageError as error:
        if error.status == 409:
            raise ValueError(str(error)) from None
        raise
    return VerifyDeliveryResult(
        verification=_from_storage(DeliveryVerification, saved["verification"]),
        was_created=saved["wasCreated"],
    )
