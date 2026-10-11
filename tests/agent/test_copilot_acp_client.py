"""Focused regressions for the Copilot ACP shim safety layer."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.copilot_acp_client import (
    CopilotACPClient,
    _copilot_args_for_hermes_mcp,
    _copilot_mcp_cli_config,
    _extract_acp_model_ids,
    _hermes_tools_mcp_bridge,
)


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()


class CopilotACPClientSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = CopilotACPClient(acp_cwd="/tmp")

    def test_stream_true_preserves_tool_call_deltas(self) -> None:
        tool_response = (
            "<tool_call>"
            '{"id":"call_read","type":"function",'
            '"function":{"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}'
            "</tool_call>"
        )

        with patch.object(self.client, "_run_prompt", return_value=(tool_response, "")):
            stream = self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "read README.md"}],
                stream=True,
            )

        chunks = list(stream)
        delta = chunks[0].choices[0].delta
        self.assertIsNone(delta.content)
        self.assertEqual(chunks[0].choices[0].finish_reason, "tool_calls")
        self.assertEqual(len(delta.tool_calls), 1)
        tool_delta = delta.tool_calls[0]
        self.assertEqual(tool_delta.index, 0)
        self.assertEqual(tool_delta.id, "call_read")
        self.assertEqual(tool_delta.function.name, "read_file")
        self.assertEqual(
            json.loads(tool_delta.function.arguments),
            {"path": "README.md"},
        )
        self.assertEqual(chunks[1].choices, [])

    def test_close_kills_entire_acp_process_tree(self) -> None:
        proc = MagicMock()
        proc.pid = 4242
        self.client._active_process = proc

        with patch("agent.deadline.kill_process_tree") as kill_tree:
            self.client.close()

        kill_tree.assert_called_once_with(4242)

    def test_extract_acp_model_ids_uses_session_new_catalog(self) -> None:
        session = {
            "models": {
                "availableModels": [
                    {"modelId": "auto"},
                    {"modelId": "gpt-5.6-sol"},
                    {"modelId": "gpt-5.6-terra"},
                    {"modelId": "gpt-5.6-sol"},
                    {"name": "missing id"},
                ]
            }
        }

        self.assertEqual(
            _extract_acp_model_ids(session),
            ["auto", "gpt-5.6-sol", "gpt-5.6-terra"],
        )

    def test_requested_model_is_selected_through_acp_session(self) -> None:
        request = MagicMock(return_value={})
        session = {
            "models": {
                "currentModelId": "gpt-5.6-sol",
                "availableModels": [
                    {"modelId": "gpt-5.6-sol"},
                    {"modelId": "gpt-5.4"},
                ],
            }
        }

        self.client._select_session_model(
            request,
            session=session,
            session_id="session-1",
            requested_model="gpt-5.4",
        )

        request.assert_called_once_with(
            "session/set_config_option",
            {"sessionId": "session-1", "configId": "model", "value": "gpt-5.4"},
        )

    def test_requested_model_is_reapplied_when_session_metadata_already_matches(self) -> None:
        request = MagicMock(return_value={})
        session = {
            "models": {
                "currentModelId": "gpt-5.4",
                "availableModels": [{"modelId": "gpt-5.4"}],
            }
        }

        self.client._select_session_model(
            request,
            session=session,
            session_id="session-1",
            requested_model="gpt-5.4",
        )

        request.assert_called_once_with(
            "session/set_config_option",
            {"sessionId": "session-1", "configId": "model", "value": "gpt-5.4"},
        )

    def test_unavailable_requested_model_fails_instead_of_silent_fallback(self) -> None:
        request = MagicMock()
        session = {
            "models": {
                "currentModelId": "grok-4.5",
                "availableModels": [{"modelId": "grok-4.5"}],
            }
        }

        with self.assertRaisesRegex(RuntimeError, "not available"):
            self.client._select_session_model(
                request,
                session=session,
                session_id="session-1",
                requested_model="gpt-5.6-sol",
            )

        request.assert_not_called()

    def test_acp_prompt_usage_is_exposed_as_openai_usage(self) -> None:
        with patch.object(
            self.client,
            "_run_prompt",
            return_value=(
                "done",
                "",
                {
                    "inputTokens": 42_188,
                    "outputTokens": 26,
                    "totalTokens": 42_214,
                    "cachedReadTokens": 12_000,
                },
            ),
        ):
            completion = self.client._create_chat_completion(
                model="gpt-5.4",
                messages=[{"role": "user", "content": "hello"}],
            )

        assert completion.usage.prompt_tokens == 42_188
        assert completion.usage.completion_tokens == 26
        assert completion.usage.total_tokens == 42_214
        assert completion.usage.prompt_tokens_details.cached_tokens == 12_000

    def test_hermes_tools_mcp_bridge_exposes_only_requested_safe_tools(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {"name": name, "description": "", "parameters": {}},
            }
            for name in ("skills_list", "web_search", "terminal", "unknown_tool")
        ]

        servers, native_tool_names = _hermes_tools_mcp_bridge(tools)

        self.assertEqual(
            native_tool_names,
            {"skills_list", "terminal", "web_search"},
        )
        self.assertEqual(len(servers), 1)
        server = servers[0]
        self.assertEqual(server["name"], "hermes-tools")
        self.assertEqual(
            server["args"],
            ["-m", "agent.transports.hermes_tools_mcp_server"],
        )
        env = {item["name"]: item["value"] for item in server["env"]}
        self.assertEqual(
            json.loads(env["HERMES_TOOLS_MCP_ALLOWED"]),
            ["skills_list", "terminal", "web_search"],
        )
        schemas = json.loads(env["HERMES_TOOLS_MCP_SCHEMAS"])
        self.assertEqual(set(schemas), {"skills_list", "terminal", "web_search"})

    def test_hermes_mcp_disables_copilot_bash_by_default(self) -> None:
        servers, _ = _hermes_tools_mcp_bridge(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "skills_list",
                        "description": "List skills",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )

        effective = _copilot_args_for_hermes_mcp(
            ["--acp", "--stdio"],
            mcp_servers=servers,
        )

        self.assertEqual(effective[:3], ["--acp", "--stdio", "--excluded-tools"])
        self.assertIn("bash", effective)
        self.assertNotIn("apply_patch", effective)
        config_index = effective.index("--additional-mcp-config") + 1
        config = json.loads(effective[config_index])
        self.assertEqual(
            config["mcpServers"]["hermes-tools"]["tools"],
            ["*"],
        )
        self.assertIn(
            "HERMES_TOOLS_MCP_ALLOWED",
            config["mcpServers"]["hermes-tools"]["env"],
        )

    def test_explicit_copilot_tool_filter_is_preserved(self) -> None:
        args = ["--acp", "--stdio", "--excluded-tools", "shell"]

        servers, _ = _hermes_tools_mcp_bridge(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "skills_list",
                        "description": "List skills",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )

        effective = _copilot_args_for_hermes_mcp(args, mcp_servers=servers)

        self.assertEqual(effective[: len(args)], args)
        self.assertEqual(effective.count("--excluded-tools"), 1)
        self.assertIn("--additional-mcp-config", effective)

    def test_native_mcp_tool_turn_returns_content_instead_of_permission_cancellation(self) -> None:
        """Replay ACP's MCP approval boundary through the real completion path."""
        spawned_args = []

        def spawn(argv, **kwargs):
            spawned_args.extend(argv)
            # Copilot's documented server(tool) grants skip its redundant MCP
            # prompt. Without one it asks ACP, where unknown requests fail closed.
            granted = "--allow-tool=hermes-tools(skill_view)" in argv
            events = [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"sessionId": "test-session"}},
                {"id": 3, "result": {}},
            ]
            if granted:
                events.append({
                    "method": "session/update",
                    "params": {"update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Skill loaded successfully"},
                    }},
                })
            else:
                # Observed Copilot wire shape: MCP is kind=other, even when
                # the preceding tool_call update reports kind=read.
                events.append({
                    "id": 0, "method": "session/request_permission",
                    "params": {
                        "toolCall": {"kind": "other", "title": "skill_view",
                                     "rawInput": {"name": "example"}},
                        "options": [{"optionId": "once", "kind": "allow_once"}],
                    },
                })
            events.append({"id": 4, "result": {"stopReason": "end_turn"}})
            proc = MagicMock()
            proc.pid = 4242
            proc.poll.return_value = None
            proc.stdin = io.StringIO()
            proc.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
            proc.stderr = io.StringIO()
            return proc

        with patch("agent.copilot_acp_client._acp_supported", return_value=True), \
             patch("agent.copilot_acp_client.subprocess.Popen", side_effect=spawn), \
             patch("agent.deadline.kill_process_tree"):
            result = self.client.chat.completions.create(
                model="test-model",
                messages=[{"role": "user", "content": "Load the example skill"}],
                tools=[{"type": "function", "function": {
                    "name": "skill_view", "description": "Load a skill",
                    "parameters": {"type": "object", "properties": {
                        "name": {"type": "string"}}, "required": ["name"]},
                }}],
                timeout=2,
            )

        self.assertEqual(result.choices[0].message.content, "Skill loaded successfully")
        self.assertEqual(
            [arg for arg in spawned_args if arg.startswith("--allow")],
            ["--allow-tool=hermes-tools(skill_view)"],
        )

    def test_mcp_grants_are_turn_scoped_and_preserve_explicit_denials(self) -> None:
        servers, names = _hermes_tools_mcp_bridge([
            {"type": "function", "function": {"name": name, "parameters": {}}}
            for name in ("read_file", "skill_view", "unknown_tool")
        ])
        args = ["--acp", "--stdio", "--deny-tool=hermes-tools(read_file)"]
        effective = _copilot_args_for_hermes_mcp(
            args, mcp_servers=servers, native_tool_names=names,
        )
        self.assertEqual(effective[:len(args)], args)
        self.assertEqual(
            {arg for arg in effective if arg.startswith("--allow")},
            {"--allow-tool=hermes-tools(read_file)", "--allow-tool=hermes-tools(skill_view)"},
        )
        # Another request with no bridge must not inherit grants, and a
        # third-party MCP server must not receive Hermes permissions.
        for unrelated in (None, [{"name": "other", "command": "example", "args": []}]):
            effective = _copilot_args_for_hermes_mcp(
                args, mcp_servers=unrelated, native_tool_names=names,
            )
            self.assertFalse(any(arg.startswith("--allow") for arg in effective))

    def test_unknown_mcp_permission_still_fails_closed(self) -> None:
        response = self._dispatch({
            "id": 10, "method": "session/request_permission",
            "params": {
                "toolCall": {"kind": "other", "title": "read_file", "rawInput": {}},
                "options": [{"optionId": "once", "kind": "allow_once"}],
            },
        }, cwd="/tmp")
        self.assertEqual(response["result"]["outcome"], {"outcome": "cancelled"})

    def test_invalid_mcp_entries_do_not_change_copilot_args(self) -> None:
        args = ["--acp", "--stdio"]

        self.assertIsNone(_copilot_mcp_cli_config([{"name": "broken"}]))
        self.assertEqual(
            _copilot_args_for_hermes_mcp(
                args,
                mcp_servers=[{"name": "broken"}],
            ),
            args,
        )

    def test_requested_model_is_forwarded_to_copilot_process(self) -> None:
        effective = _copilot_args_for_hermes_mcp(
            ["--acp", "--stdio"],
            mcp_servers=None,
            requested_model="gpt-5.6-sol",
        )

        self.assertEqual(
            effective,
            ["--acp", "--stdio", "--model", "gpt-5.6-sol"],
        )

    def test_explicit_copilot_model_arg_is_preserved(self) -> None:
        effective = _copilot_args_for_hermes_mcp(
            ["--acp", "--stdio", "--model", "grok-4.5"],
            mcp_servers=None,
            requested_model="gpt-5.6-sol",
        )

        self.assertEqual(
            effective,
            ["--acp", "--stdio", "--model", "grok-4.5"],
        )

    def test_copilot_tool_filter_notice_is_not_returned_to_user(self) -> None:
        notice = "Info: Disabled tools: bash, glob, rg, view"
        with patch.object(
            self.client,
            "_run_prompt",
            return_value=(notice + "Hermes has 260 skills.", ""),
        ):
            completion = self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "count skills"}],
            )

        self.assertEqual(
            completion.choices[0].message.content,
            "Hermes has 260 skills.",
        )

    def test_hermes_tools_mcp_bridge_is_absent_without_matching_tools(self) -> None:
        servers, native_tool_names = _hermes_tools_mcp_bridge(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "description": "",
                        "parameters": {},
                    },
                }
            ]
        )

        self.assertEqual(servers, [])
        self.assertEqual(native_tool_names, set())

    def test_native_mcp_tools_are_not_duplicated_as_xml_tool_specs(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "skills_list",
                    "description": "List Hermes skills",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        with patch.object(
            self.client,
            "_run_prompt",
            return_value=("260 skills", ""),
        ) as run_prompt:
            self.client._create_chat_completion(
                model="copilot-acp",
                messages=[{"role": "user", "content": "count skills"}],
                tools=tools,
            )

        prompt_text = run_prompt.call_args.args[0]
        self.assertIn("skills_list", prompt_text)
        self.assertIn("MUST use it", prompt_text)
        self.assertIn("authoritative", prompt_text)
        self.assertNotIn('"name": "skills_list"', prompt_text)
        self.assertEqual(
            run_prompt.call_args.kwargs["mcp_servers"][0]["name"],
            "hermes-tools",
        )

    def _dispatch(self, message: dict, *, cwd: str) -> dict:
        process = _FakeProcess()
        handled = self.client._handle_server_message(
            message,
            process=process,
            cwd=cwd,
            text_parts=[],
            reasoning_parts=[],
        )
        self.assertTrue(handled)
        payload = process.stdin.getvalue().strip()
        self.assertTrue(payload)
        return json.loads(payload)

    def test_execute_permission_uses_hermes_guard_and_selects_allow_once(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "kind": "execute",
                    "title": "Read project metadata",
                    "rawInput": {"command": "head -n 1 pyproject.toml"},
                },
                "options": [
                    {"optionId": "allow_once", "kind": "allow_once"},
                    {"optionId": "allow_always", "kind": "allow_always"},
                    {"optionId": "reject_once", "kind": "reject_once"},
                ],
            },
        }

        with patch(
            "tools.terminal_tool._check_all_guards",
            return_value={"approved": True, "message": None},
        ) as guard:
            response = self._dispatch(request, cwd="/tmp")

        guard.assert_called_once_with("head -n 1 pyproject.toml", "local")
        self.assertEqual(
            response["result"]["outcome"],
            {"outcome": "selected", "optionId": "allow_once"},
        )

    def test_execute_permission_denies_when_hermes_guard_blocks(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "kind": "execute",
                    "rawInput": {"command": "rm -rf /"},
                },
                "options": [
                    {"optionId": "allow_once", "kind": "allow_once"},
                    {"optionId": "reject_once", "kind": "reject_once"},
                ],
            },
        }

        with patch(
            "tools.terminal_tool._check_all_guards",
            return_value={"approved": False, "message": "hardline block"},
        ):
            response = self._dispatch(request, cwd="/tmp")

        self.assertEqual(
            response["result"]["outcome"],
            {"outcome": "cancelled"},
        )

    def test_execute_permission_never_upgrades_to_allow_always(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "kind": "execute",
                    "rawInput": {"command": "head -n 1 pyproject.toml"},
                },
                "options": [
                    {"optionId": "allow_always", "kind": "allow_always"},
                    {"optionId": "reject_once", "kind": "reject_once"},
                ],
            },
        }

        with patch(
            "tools.terminal_tool._check_all_guards",
            return_value={"approved": True, "message": None},
        ):
            response = self._dispatch(request, cwd="/tmp")

        self.assertEqual(
            response["result"]["outcome"],
            {"outcome": "cancelled"},
        )

    def test_permission_without_execute_command_fails_closed(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "session/request_permission",
            "params": {
                "toolCall": {"kind": "edit", "rawInput": {"path": "note.md"}},
                "options": [
                    {"optionId": "allow_once", "kind": "allow_once"},
                ],
            },
        }

        with patch("tools.terminal_tool._check_all_guards") as guard:
            response = self._dispatch(request, cwd="/tmp")

        guard.assert_not_called()
        self.assertEqual(
            response["result"]["outcome"],
            {"outcome": "cancelled"},
        )

    def test_permission_guard_exception_fails_closed(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "kind": "execute",
                    "rawInput": {"command": "head -n 1 pyproject.toml"},
                },
                "options": [
                    {"optionId": "allow_once", "kind": "allow_once"},
                ],
            },
        }

        with patch(
            "tools.terminal_tool._check_all_guards",
            side_effect=RuntimeError("guard unavailable"),
        ):
            response = self._dispatch(request, cwd="/tmp")

        self.assertEqual(
            response["result"]["outcome"],
            {"outcome": "cancelled"},
        )

    def test_permission_with_malformed_params_fails_closed(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "session/request_permission",
            "params": ["not", "an", "object"],
        }

        response = self._dispatch(request, cwd="/tmp")

        self.assertEqual(
            response["result"]["outcome"],
            {"outcome": "cancelled"},
        )

    def test_read_text_file_redacts_sensitive_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secret_file = root / "config.env"
            secret_file.write_text("OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012")

            # agent.redact snapshots HERMES_REDACT_SECRETS at import time into
            # _REDACT_ENABLED, so patching os.environ is a no-op. Flip the
            # module-level constant directly for the duration of the call.
            with patch("agent.redact._REDACT_ENABLED", True):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "fs/read_text_file",
                        "params": {"path": str(secret_file)},
                    },
                    cwd=str(root),
                )

        content = ((response.get("result") or {}).get("content") or "")
        self.assertNotIn("abc123def456", content)
        self.assertIn("OPENAI_API_KEY=", content)

    def test_fs_read_text_file_decodes_as_utf8_under_non_utf8_locale(self) -> None:
        """Regression for #18637 (bug 2): fs/read_text_file used
        ``path.read_text()`` with no explicit encoding, so on Windows
        GBK/CP932/CP949 locales the Copilot read_file tool crashed on any
        source file with non-ASCII content (e.g. a CJK comment, an em dash,
        or UTF-8 BOM)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "note.md"
            target.write_text("# 中文标题\nem dash — here\n", encoding="utf-8")

            original_read_text = Path.read_text

            def strict_read_text(self, encoding=None, errors=None, **kwargs):
                if self == target and encoding != "utf-8":
                    raise UnicodeDecodeError(
                        "gbk", b"\x94", 0, 1, "illegal multibyte sequence"
                    )
                return original_read_text(
                    self, encoding=encoding, errors=errors, **kwargs
                )

            with patch.object(Path, "read_text", strict_read_text):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "fs/read_text_file",
                        "params": {"path": str(target)},
                    },
                    cwd=str(root),
                )

        self.assertNotIn("error", response)
        content = ((response.get("result") or {}).get("content") or "")
        self.assertIn("中文标题", content)
        self.assertIn("em dash —", content)



    def test_write_text_file_respects_safe_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            safe_root = root / "workspace"
            safe_root.mkdir()
            outside = root / "outside.txt"

            with patch.dict(os.environ, {"HERMES_WRITE_SAFE_ROOT": str(safe_root)}, clear=False):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "fs/write_text_file",
                        "params": {
                            "path": str(outside),
                            "content": "should-not-write",
                        },
                    },
                    cwd=str(root),
                )

        self.assertIn("error", response)
        self.assertIn("HERMES_WRITE_SAFE_ROOT", str(response["error"]))
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()


# ── HOME env propagation tests (from PR #11285) ─────────────────────

from unittest.mock import patch as _patch
import pytest


def _make_home_client(tmp_path):
    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="copilot",
        acp_args=["--acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )


def _fake_popen_capture(captured):
    def _fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        raise FileNotFoundError("copilot not found")
    return _fake


def test_run_prompt_preserves_real_home_when_profile_home_available(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    (hermes_home / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Hermeticity: an ambient HERMES_REAL_HOME (exported by Hermes' own
    # terminal contract on dev boxes) outranks HOME in the candidate ladder,
    # and an ambient TERMINAL_HOME_MODE would change the policy under test.
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)
    # Hermeticity: get_subprocess_home()'s auto mode prefers the profile home
    # when is_container() is True — on a containerized CI runner that real
    # probe flips the resolution this test asserts. The host/VM branch is the
    # contract under test; pin containment off.
    monkeypatch.setattr("hermes_constants.is_container", lambda: False)

    captured = {}
    client = _make_home_client(tmp_path)

    # Hermeticity: the --acp support probe (PR #87308) calls subprocess.run
    # before Popen; stub it inconclusive so no real CLI on the host box can
    # flip the resolution this test asserts.
    with _patch("agent.copilot_acp_client.subprocess.run", side_effect=FileNotFoundError):
        with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert captured["kwargs"]["env"]["HOME"] == str(real_home)
    assert captured["kwargs"]["env"]["HERMES_REAL_HOME"] == str(real_home)
    if os.name != "nt":
        assert captured["kwargs"]["process_group"] == 0


def test_run_prompt_passes_home_when_parent_env_is_clean(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    captured = {}
    client = _make_home_client(tmp_path)

    # Hermeticity: the --acp support probe (PR #87308) calls subprocess.run
    # before Popen; stub it inconclusive so no real CLI on the host box can
    # flip the resolution this test asserts.
    with _patch("agent.copilot_acp_client.subprocess.run", side_effect=FileNotFoundError):
        with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)

    assert "env" in captured["kwargs"]
    assert captured["kwargs"]["env"]["HOME"]


# ── --acp support probe tests (PR #87308 / issue #87309) ────────────

import subprocess as _subprocess

from agent.copilot_acp_client import _ACP_PROBE_CACHE, _acp_supported


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    _ACP_PROBE_CACHE.clear()
    yield
    _ACP_PROBE_CACHE.clear()


def _completed(returncode=0, stdout=""):
    return _subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_probe_true_when_help_advertises_acp():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp] [--stdio]"),
    ):
        assert _acp_supported("copilot", ["--acp", "--stdio"]) is True


def test_probe_false_when_help_lacks_acp_and_run_prompt_fast_fails(tmp_path):
    client = _make_home_client(tmp_path)
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: claude [--print] [--model]"),
    ):
        with pytest.raises(RuntimeError, match="ACP transport not supported"):
            client._run_prompt("hello", timeout_seconds=1)


def test_probe_inconclusive_falls_through_to_spawn_error(tmp_path):
    """Missing binary: probe must NOT mask the established spawn error."""
    client = _make_home_client(tmp_path)
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        side_effect=FileNotFoundError("copilot not found"),
    ):
        with _patch(
            "agent.copilot_acp_client.subprocess.Popen",
            side_effect=FileNotFoundError("copilot not found"),
        ):
            with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
                client._run_prompt("hello", timeout_seconds=1)


def test_probe_result_cached_per_binary_path():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        return_value=_completed(stdout="Usage: copilot [--acp]"),
    ) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is True
        assert _acp_supported("copilot", ["--acp"]) is True
    assert run_mock.call_count == 1


def test_probe_inconclusive_not_cached():
    with _patch(
        "agent.copilot_acp_client.subprocess.run",
        side_effect=FileNotFoundError,
    ) as run_mock:
        assert _acp_supported("copilot", ["--acp"]) is None
        assert _acp_supported("copilot", ["--acp"]) is None
    assert run_mock.call_count == 2  # inconclusive verdicts retry


def test_probe_skipped_for_custom_args_without_acp():
    with _patch("agent.copilot_acp_client.subprocess.run") as run_mock:
        assert _acp_supported("mycli", ["--custom-transport"]) is True
    run_mock.assert_not_called()
