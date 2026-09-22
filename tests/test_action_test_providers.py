import pytest

from mcp_studio.action_adapter import ACTION_ENVELOPE_SCHEMA
from mcp_studio.action_test_providers import (
    TEST_PROVIDER_SCHEMA,
    all_test_providers,
    get_test_provider,
    list_test_providers,
)


def test_test_provider_catalog_has_browser_world_and_finance():
    catalog = list_test_providers()
    assert catalog["schema"] == TEST_PROVIDER_SCHEMA
    assert catalog["executor_attached"] is False
    providers = catalog["providers"]
    assert [row["provider_id"] for row in providers] == [
        "browser",
        "world",
        "finance",
    ]
    assert all(row["executor_attached"] is False for row in providers)


def test_all_test_providers_use_one_action_envelope_schema():
    providers = all_test_providers()
    assert providers
    for provider in providers:
        envelope = provider.envelope
        assert envelope.schema == ACTION_ENVELOPE_SCHEMA
        assert envelope.domain == provider.domain
        assert envelope.metadata["executor_attached"] is False
        assert 1 <= len(envelope.actions) <= 32
        assert len({action.action_id for action in envelope.actions}) == len(envelope.actions)


def test_finance_fixture_exercises_consequence_metadata_without_orders():
    provider = get_test_provider("finance")
    assert provider.domain == "finance"
    assert provider.envelope.metadata["orders_forbidden"] is True

    prepare = next(
        action
        for action in provider.envelope.actions
        if action.action_id == "finance:prepare-rebalance"
    )
    assert prepare.risk.financial is True
    assert prepare.risk.consequential is True
    assert prepare.risk.requires_human_approval is True
    assert prepare.risk.external_side_effect is False
    assert prepare.constraints["orders_forbidden"] is True


def test_browser_and_world_fixtures_exercise_different_permission_shapes():
    browser = get_test_provider("browser")
    world = get_test_provider("world")

    assert {action.permission_class for action in browser.envelope.actions} == {
        "read",
        "write",
        "execute",
    }
    assert {action.permission_class for action in world.envelope.actions} == {
        "read",
        "execute",
    }


def test_unknown_test_provider_fails_closed():
    with pytest.raises(KeyError):
        get_test_provider("unknown-provider")
