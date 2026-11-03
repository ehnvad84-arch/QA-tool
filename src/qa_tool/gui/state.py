"""Состояние окна: текущий файл стенда и сценария."""

from dataclasses import dataclass, field
from pathlib import Path

from qa_tool.core.models import Place, Scenario, Stand


def default_stand_path() -> Path:
    return Path.home() / "qa-tool" / "stand.yaml"


@dataclass
class AppState:
    stand_path: Path = field(default_factory=default_stand_path)
    stand: Stand | None = None
    scenario: Scenario = field(default_factory=Scenario)
    running: bool = False

    def __post_init__(self) -> None:
        if self.stand is None:
            self.stand = Stand(place=Place.COLOR_BOX, runs_dir=self.stand_path.parent / "runs")
