"""Where a schema comes from, said out loud.

There are three answers and no default. Omitting it is a `TypeError` at the call
site rather than a quiet inference, because a schema that got chosen because
nobody said otherwise is how a column of dotted quads is catalogued as VARCHAR
forever - and once it is, no amount of reading the data back can tell you it was
a mistake.

The same three modes the studio's upload drawer shows as radio buttons. A script
and a person are making the same decision, so they say it the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Union


@dataclass(frozen=True)
class Schema:
    """A schema source. Build one with `declared`, `inferred` or `of_dataset`."""

    mode: str
    columns: Optional[List[Dict[str, str]]] = None
    write: Optional[str] = None

    @classmethod
    def declared(cls, columns: Union[Mapping[str, str], Sequence[Dict[str, str]]]) -> "Schema":
        """You name every column and its type.

        A column in your files that you have not declared is refused, rather than
        included on your behalf. Pass `ignore=` to `negotiate` for one you know
        about and do not want.

            Schema.declared({"source_ip": "IPV4", "published": "TIMESTAMP[us]"})
        """
        if isinstance(columns, Mapping):
            listed = [{"name": name, "type": type_} for name, type_ in columns.items()]
        else:
            listed = [dict(column) for column in columns]
        if not listed:
            raise ValueError("declared() needs at least one column")
        return cls(mode="declared", columns=listed)

    @classmethod
    def inferred(cls) -> "Schema":
        """Work the types out from a sample, and show them to me first.

        The contract comes back `proposed` and refuses writes until it is
        accepted, so a script that never looks at what was inferred fails loudly
        instead of catalogueing a guess.
        """
        return cls(mode="infer")

    @classmethod
    def of_dataset(cls, write: str = "append") -> "Schema":
        """Use the types the dataset already declares.

        `write="append"` adds rows; `write="overwrite"` replaces the rows the
        dataset resolves to and leaves its definition exactly as the catalog
        holds it - a dataset defined as IPV4 is still IPV4 afterwards.
        """
        if write not in ("append", "overwrite"):
            raise ValueError("write must be 'append' or 'overwrite'")
        return cls(mode="dataset", write=write)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"mode": self.mode}
        if self.columns is not None:
            payload["columns"] = self.columns
        if self.write is not None:
            payload["write"] = self.write
        return payload


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class PlanEntry:
    """What will happen to one column, before it happens."""

    column: str
    from_: str
    to: str
    action: str

    @property
    def changes_values(self) -> bool:
        """True when the data itself is rewritten, not just relabelled."""
        return self.action == "cast"

    @classmethod
    def from_json(cls, payload: Dict[str, str]) -> "PlanEntry":
        return cls(
            column=payload["column"],
            from_=payload["from"],
            to=payload["to"],
            action=payload["action"],
        )

    def __str__(self) -> str:
        if self.action in ("keep", "undeclared"):
            return f"{self.column}: {self.to}"
        return f"{self.column}: {self.from_} -> {self.to} ({self.action})"


@dataclass(frozen=True)
class Issue:
    """Something the two schemas disagree about, and whether it stops the upload.

    `severity` is the service's decision, carried here rather than re-derived. A
    client that decides for itself what blocks is one that will eventually
    disagree with the service about it.
    """

    code: str
    column: Optional[str] = None
    detail: str = ""
    severity: str = "blocking"

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "Issue":
        return cls(
            code=payload.get("code", "issue"),
            column=payload.get("column"),
            detail=payload.get("detail", ""),
            severity=payload.get("severity", "blocking"),
        )

    def __str__(self) -> str:
        where = f" [{self.column}]" if self.column else ""
        return f"{self.code}{where}: {self.detail}"
