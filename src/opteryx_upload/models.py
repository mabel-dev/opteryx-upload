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
    target: Optional[Target] = None
    # The target's declared columns as {name: logical type}, or None when the
    # dataset does not exist yet and its types will be inferred from the data.
    declared_schema: Optional[Dict[str, str]] = None

    @classmethod
    def from_response(cls, payload: Dict[str, Any]) -> "SessionInfo":
        target = payload.get("target")
        return cls(
            session_id=payload["session_id"],
            url=payload["url"],
            expires_at=datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")),
            parts=payload.get("parts", True),
            target=Target(**target) if target else None,
            declared_schema=payload.get("declared_schema"),
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
    column: Optional[str] = None


@dataclass(frozen=True)
class PartAccepted:
    """What the service made of a part, answered as the part is accepted.

    `schema` is the LOGICAL schema - the types the data now HAS, after being cast
    to the target's declared types. Each column carries the type it arrived as and
    what was done to it (`keep`, `widen`, `retag`, `cast`, `undeclared`,
    `unsupported`), so a widening or a reinterpretation is visible rather than
    guessed at from a type that changed on its own.
    """

    status: str
    part: int
    rows: Optional[int] = None
    schema: Dict[str, Any] = field(default_factory=dict)
    issues: List[Issue] = field(default_factory=list)

    @classmethod
    def from_response(cls, payload: Optional[Dict[str, Any]], part: int) -> "PartAccepted":
        # A service that predates the acquire-time report answers with a bare
        # `{"status": "stored"}`, and older ones with no body at all.
        payload = payload or {}
        return cls(
            status=payload.get("status", "stored"),
            part=payload.get("part", part),
            rows=payload.get("rows"),
            schema=payload.get("schema", {}),
            issues=[Issue(**i) for i in payload.get("issues", [])],
        )

    @property
    def columns(self) -> List[Dict[str, Any]]:
        return list(self.schema.get("columns", []))

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


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
