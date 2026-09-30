"""Create isolated workspaces for anonymous demo visitors."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from switchboard.demo.portfolio import initialize_demo_portfolio
from switchboard.storage import WorkspaceStorage

WORKSPACE_LIFETIME_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class DemoWorkspace:
    id: UUID
    storage: WorkspaceStorage


@dataclass(frozen=True)
class OpenedDemoWorkspace:
    workspace: DemoWorkspace
    was_created: bool


def open_demo_workspace(
    *, workspace_id: UUID | None, base_storage: WorkspaceStorage
) -> OpenedDemoWorkspace:
    """Open a current workspace or create a new baseline workspace."""
    now = datetime.now(timezone.utc)
    if workspace_id is not None:
        storage = base_storage.for_workspace(workspace_id=str(workspace_id))
        existing = storage.get_workspace()
        if existing is not None:
            updated_at = datetime.fromisoformat(existing["updatedAt"])
            if (now - updated_at).total_seconds() <= WORKSPACE_LIFETIME_SECONDS:
                storage.touch_workspace(updated_at=now.isoformat())
                return OpenedDemoWorkspace(
                    workspace=DemoWorkspace(id=workspace_id, storage=storage),
                    was_created=False,
                )
            if not existing.get("import_hash"):
                storage.delete_workspace()

    new_id = uuid4()
    storage = base_storage.for_workspace(workspace_id=str(new_id))
    initialize_demo_portfolio(storage=storage)
    return OpenedDemoWorkspace(
        workspace=DemoWorkspace(id=new_id, storage=storage), was_created=True
    )
