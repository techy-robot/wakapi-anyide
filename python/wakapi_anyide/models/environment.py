from dataclasses import dataclass

from wakapi_anyide.models.config import WakatimeConfig
from wakapi_anyide.models.project import Project


@dataclass
class Environment:
    is_test_only: bool
    config: WakatimeConfig
    project: Project
