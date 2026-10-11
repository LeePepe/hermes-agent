from unittest.mock import patch

from hermes_cli import models
from hermes_cli.model_switch import list_authenticated_providers


def test_copilot_acp_provider_uses_authenticated_acp_session_catalog():
    live = ["auto", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4"]

    with patch(
        "agent.copilot_acp_client.discover_copilot_acp_models",
        return_value=live,
    ) as discover, patch.object(models, "_fetch_github_models") as github:
        result = models.provider_model_ids("copilot-acp", force_refresh=True)

    assert result == live
    discover.assert_called_once()
    github.assert_not_called()


def test_authenticated_picker_includes_copilot_acp_external_process():
    live = ["auto", "gpt-5.6-sol", "gpt-5.4"]

    with patch("agent.models_dev.fetch_models_dev", return_value={}), patch(
        "hermes_cli.auth.get_external_process_provider_status",
        return_value={"configured": True, "logged_in": True},
    ), patch(
        "agent.copilot_acp_client.discover_copilot_acp_models",
        return_value=live,
    ):
        rows = list_authenticated_providers(
            current_provider="copilot-acp",
            current_model="gpt-5.6-sol",
            max_models=None,
            for_picker=True,
        )

    row = next(item for item in rows if item["slug"] == "copilot-acp")
    assert row["is_current"] is True
    assert row["models"] == live
