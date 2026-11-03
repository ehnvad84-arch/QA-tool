"""Состояние окна: текущий файл стенда и сценария; любое изменение сразу пишется на диск."""

from dataclasses import dataclass, field
from pathlib import Path

from qa_tool.core.config import load_scenario, load_stand, save_scenario, save_stand
from qa_tool.core.models import Place, Scenario, Stand


def default_stand_path() -> Path:
    return Path.home() / "qa-tool" / "stand.yaml"


@dataclass
class AppState:
    stand_path: Path = field(default_factory=default_stand_path)
    stand: Stand | None = None
    scenario: Scenario | None = None
    running: bool = False

    def __post_init__(self) -> None:
        if self.stand is None:
            if self.stand_path.exists():
                self.stand = load_stand(self.stand_path)
            else:
                self.stand = Stand(place=Place.COLOR_BOX, runs_dir=self.stand_path.parent / "runs")
        if self.scenario is None:
            self.scenario = load_scenario(self.scenario_path)

    @property
    def scenario_path(self) -> Path:
        return self.stand_path.parent / "scenario.yaml"

    def save(self) -> None:
        save_stand(self.stand, self.stand_path)
        save_scenario(self.scenario, self.scenario_path)
