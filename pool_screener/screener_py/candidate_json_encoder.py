"""JSON encoder for screened pool candidates."""

from json import JSONEncoder
from typing import Any

from .candidate import Candidate


class CandidateJSONEncoder(JSONEncoder):
    """Serialize candidates without materializing a separate result list."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Candidate):
            return obj.to_dict()
        return super().default(obj)
