import json
from pathlib import Path

import pytest

from payoutpilot import pipeline
from payoutpilot.models import Campaign

EX = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def campaign() -> Campaign:
    return Campaign.model_validate(json.loads((EX / "campaign.json").read_text()))


@pytest.fixture
def subs():
    return pipeline.parse_csv((EX / "submissions.csv").read_text())
