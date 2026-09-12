"""Mock settlement must be impossible outside explicit local/test environments."""
import pytest

from utils import configs, payment


@pytest.mark.parametrize('environment', ['production', '', 'unknown'])
def test_mock_provider_cannot_settle_in_production(monkeypatch, environment):
    monkeypatch.setattr(configs, 'app_env', environment, raising=False)
    monkeypatch.setattr(configs, 'payment_provider', 'mock')
    assert payment.get_provider() is None
    with pytest.raises(payment.PaymentError):
        payment.MockProvider().begin({'order_id': 'synthetic-order'})


@pytest.mark.parametrize('environment', ['development', 'test'])
def test_explicit_local_environment_can_use_mock(monkeypatch, environment):
    monkeypatch.setattr(configs, 'app_env', environment, raising=False)
    monkeypatch.setattr(configs, 'payment_provider', 'mock')
    provider = payment.require_provider()
    assert provider.begin({'order_id': 'synthetic-order'})['auto_settle'] is True
