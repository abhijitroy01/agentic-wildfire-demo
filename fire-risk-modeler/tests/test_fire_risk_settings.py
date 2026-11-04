"""Fire modeler central configuration validation and safe service startup."""
import pytest
from fire_risk_modeler import settings


def test_local_defaults_come_from_declared_settings(private_key):
    settings.validate()
    assert settings.default_parameters().distance_bands_km == [
        settings.limit("FIRE_RISK_INNER_KM"), settings.limit("FIRE_RISK_MIDDLE_KM"),
        settings.limit("FIRE_RISK_OUTER_KM"),
    ]


def test_ecs_requires_each_environment_binding(monkeypatch, private_key):
    monkeypatch.setenv("FIRE_RISK_ENVIRONMENT", "ecs")
    monkeypatch.delenv("FIRE_RISK_INNER_KM", raising=False)
    with pytest.raises(RuntimeError, match="requires FIRE_RISK_INNER_KM"):
        settings.validate()


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "1000"])
def test_nonfinite_and_out_of_range_settings_refused(value, monkeypatch):
    monkeypatch.setenv("FIRE_RISK_WIND_SHAPE_SCALE", value)
    with pytest.raises(RuntimeError, match="FIRE_RISK_WIND_SHAPE_SCALE"):
        settings.limit("FIRE_RISK_WIND_SHAPE_SCALE")


def test_short_secret_refused(monkeypatch):
    monkeypatch.setenv("FIRE_RISK_SERVICE_KEY", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        settings.service_key()
