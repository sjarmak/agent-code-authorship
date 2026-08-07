from types import SimpleNamespace

import pytest

from authorship.era_matching import control_roles, select_matched_controls


class Observation:
    def __init__(self, change_size, path_type_counts, code_age_days):
        self.change_size = change_size
        self.path_type_counts = path_type_counts
        self.code_age_days = code_age_days

    @property
    def test_share(self):
        total = sum(self.path_type_counts.values())
        return self.path_type_counts.get("test", 0) / total


class Repository:
    def __init__(self, repository_id, observations):
        self.repository_id = repository_id
        self.role = "never_adopter_control"
        self.observations = observations

    def observation(self, period):
        return self.observations.get(period)


def _repository(repository_id, *, change_size, test_count, source_count, age):
    observations = {
        period: Observation(
            change_size,
            {"test": test_count, "source": source_count},
            age,
        )
        for period in (1, 3)
    }
    return Repository(repository_id, observations)


def test_matching_is_deterministic_and_uses_repository_id_tiebreaker():
    adopter = _repository(
        "treated", change_size=100, test_count=1, source_count=9, age=10
    )
    controls = [
        _repository(
            repository_id,
            change_size=100 + index,
            test_count=1,
            source_count=9,
            age=10,
        )
        for index, repository_id in enumerate(("c", "a", "b", "d", "e", "f"))
    ]

    selected, distances = select_matched_controls(adopter, controls, 1, 3)

    assert len(selected) == 5
    assert selected[0].repository_id == "c"
    assert set(distances) == {row.repository_id for row in selected}


def test_matching_requires_complete_period_covariates():
    adopter = _repository(
        "treated", change_size=100, test_count=1, source_count=9, age=10
    )
    adopter.observations.pop(3)

    with pytest.raises(ValueError, match="complete"):
        select_matched_controls(adopter, [], 1, 3)


def test_control_roles_preserve_frozen_role_separation():
    controls = [
        SimpleNamespace(repository_id="ban", role="h2_ai_ban_control"),
        SimpleNamespace(repository_id="never", role="never_adopter_control"),
        SimpleNamespace(repository_id="future", role="adopter"),
    ]

    assert control_roles(controls) == {
        "h2_ai_ban_control": ["ban"],
        "never_adopter_control": ["never"],
        "not_yet_adopter": ["future"],
    }
