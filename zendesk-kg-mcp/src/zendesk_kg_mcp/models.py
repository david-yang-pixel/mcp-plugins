from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Channel = Literal["issue_vector", "solution_vector", "fulltext", "keyword_graph"]
Include = Literal["parser", "fusion", "graph", "embedding2d", "trace"]
OrderBy = Literal["rrfScore", "createdAt", "priority"]
Nonempty = Annotated[str, Field(min_length=1, max_length=512, strict=True)]
TopK = Annotated[int, Field(ge=1, le=20, strict=True)]
CandidateK = Annotated[int, Field(ge=1, le=50, strict=True)]
RrfK = Annotated[int, Field(ge=1, strict=True)]
LinkK = Annotated[int, Field(ge=1, le=10, strict=True)]
Weight = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
Query = Annotated[str, Field(min_length=1, max_length=8192, strict=True)]


def iso_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Dates must use ISO 8601 format") from None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class RetrievalFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    versions: list[Nonempty] | None = Field(default=None, max_length=50)
    priority: Literal["urgent", "high", "normal", "low"] | None = None
    status: Nonempty | None = None
    handledBy: Nonempty | None = None
    keywords: list[Nonempty] | None = Field(default=None, max_length=50)
    createdAfter: Nonempty | None = None
    createdBefore: Nonempty | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        after = iso_instant(self.createdAfter) if self.createdAfter else None
        before = iso_instant(self.createdBefore) if self.createdBefore else None
        if after and before and after > before:
            raise ValueError("createdAfter must not be later than createdBefore")
        return self


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: Query
    topK: TopK = 3
    vectorK: CandidateK = 15
    fulltextK: CandidateK = 15
    rrfK: RrfK = 60
    channels: list[Channel] | None = Field(default=None, min_length=1, max_length=4)
    channelWeights: dict[Channel, Weight] | None = None
    filter: RetrievalFilter | None = None
    include: list[Include] | None = Field(default=None, max_length=5)
    orderBy: OrderBy = "rrfScore"
    allowVersionFamilyFallback: bool = False
    recommendedLinkK: LinkK = 3

    @model_validator(mode="after")
    def validate_query(self):
        if not self.query.strip():
            raise ValueError("query must contain non-whitespace text")
        return self
