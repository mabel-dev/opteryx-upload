from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from enum import Enum
from typing import Any
from typing import Dict
from typing import List
from typing import Optional


class ConflictResolution(str, Enum):
    FAIL = "fail"
    OVERWRITE = "overwrite"
    APPEND = "append"


@dataclass(frozen=True)
class Target:
    workspace: str
    collection: str
    dataset: str

    def as_dict(self) -> Dict[str, str]:
        return {"workspace": self.workspace, "collection": self.collection, "dataset": self.dataset}


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    url: str
    expires_at: datetime
    parts: bool = True

    @classmethod
    def from_response(cls, payload: Dict[str, Any]) -> "SessionInfo":
        return cls(
            session_id=payload["session_id"],
            url=payload["url"],
            expires_at=datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")),
            parts=payload.get("parts", True),
        )


@dataclass(frozen=True)
class PartInfo:
    id: int
    rows: Optional[int] = None
    bytes: Optional[int] = None
    kind: Optional[str] = None
    blob: Optional[str] = None
    uploaded_at: Optional[datetime] = None

    @classmethod
    def from_response(cls, payload: Dict[str, Any]) -> "PartInfo":
        uploaded_at = payload.get("uploaded_at")
        return cls(
            id=payload["id"],
            rows=payload.get("rows"),
            bytes=payload.get("bytes"),
            kind=payload.get("kind"),
            blob=payload.get("blob"),
            uploaded_at=datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
            if uploaded_at
            else None,
        )


@dataclass(frozen=True)
class Issue:
    issue: str
    part: Optional[int] = None


@dataclass(frozen=True)
class InspectResult:
    rows_estimate: Optional[int]
    schema: Dict[str, Any]
    parts: List[PartInfo] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)

    @classmethod
    def from_response(cls, payload: Dict[str, Any]) -> "InspectResult":
        return cls(
            rows_estimate=payload.get("rows_estimate"),
            schema=payload.get("schema", {}),
            parts=[PartInfo.from_response(p) for p in payload.get("parts", [])],
            issues=[Issue(**i) for i in payload.get("issues", [])],
        )

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


@dataclass(frozen=True)
class CommitResult:
    table: str
    commit_id: str
    rows_written: Optional[int]
    files_created: int

    @classmethod
    def from_response(cls, payload: Dict[str, Any]) -> "CommitResult":
        return cls(
            table=payload["table"],
            commit_id=payload["commit_id"],
            rows_written=payload.get("rows_written"),
            files_created=payload["files_created"],
        )
