"""Runtime checks: logs and run errors carry no visitor text or provider detail."""
import ast
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from checks_support import PLANTED_ADDRESS, ROOT, load, runtime_stubs, scripted_agent, stub_module


class LogPrivacyChecks(unittest.TestCase):
    """Runtime logs carry sizes, ids and error types, never prompts, alert text, titles or locations."""

    def logged(self, call):
        """Run a call and return every record it logged, tracebacks included."""
        with self.assertLogs(level="DEBUG") as captured:
            logging.getLogger("checked.logs").debug("capture")
            call()
        formatter = logging.Formatter()
        return "\n".join(formatter.format(record) for record in captured.records)

    def test_formatted_specialist_traceback_does_not_leak_exception_or_chained_values(self):
        canary = "SYNTHETIC-PROVIDER-LOCATION-AND-CREDENTIAL-CANARY"
        stubs = runtime_stubs(**{
            "agents.response_agent.tools.knowledge_base_tool": stub_module(
                "knowledge_base_tool", query_public_knowledge_base=MagicMock()),
        })
        with patch.dict(sys.modules, stubs):
            response = load("checked_response_exception_privacy",
                            "backend/runtime/agents/response_agent/response_agent.py")

        def failed_agent(**_kwargs):
            try:
                raise ValueError(canary)
            except ValueError as cause:
                raise RuntimeError(canary) from cause

        def call():
            with self.assertRaises(RuntimeError):
                response._run_response_agent("Fixture guidance query.")

        with patch.object(response, "Agent", side_effect=failed_agent):
            formatted = self.logged(call)
        self.assertNotIn(canary, formatted)
        self.assertIn("RuntimeError", formatted)

    def test_specialists_log_sizes_and_ids_not_queries_titles_or_answers(self):
        """Check specialist agents log lengths and ids, never user text, titles or answers."""
        stubs = runtime_stubs(**{
            "agents.response_agent.tools.knowledge_base_tool": stub_module(
                "knowledge_base_tool", query_public_knowledge_base=MagicMock()),
            "agents.communication_agent.tools.voice_message_tool": stub_module(
                "voice_message_tool", generate_voice_messages=MagicMock()),
        })
        with patch.dict(sys.modules, stubs):
            response = load("checked_response_logs", "backend/runtime/agents/response_agent/response_agent.py")
            communication = load("checked_communication_logs",
                                 "backend/runtime/agents/communication_agent/communication_agent.py")
        request = f"Evacuation guidance for {PLANTED_ADDRESS}"
        for module, output, call in (
            (response, {"situation": f"Routes near {PLANTED_ADDRESS}", "sections": []},
             lambda: response._run_response_agent(request)),
            (communication, {"text": f"Situation update for {PLANTED_ADDRESS}"},
             lambda: communication.build_communication_tool(owner=("actor", "session"), context={},
                                                            receipts=[])(request)),
        ):
            with self.subTest(module=module.__name__), patch.object(module, "Agent", scripted_agent(output)), \
                    patch.dict(os.environ, {"VOICE_MESSAGE_S3_BUCKET": "fixture-bucket"}):
                text = self.logged(call)
                self.assertNotIn(PLANTED_ADDRESS, text)
                self.assertIn(str(len(request)), text)

    def test_voice_messages_log_error_codes_not_alert_text(self):
        """Check translation and voice errors log AWS error codes, never the alert message text."""
        from botocore.exceptions import ClientError
        clients = MagicMock()
        clients.translate.return_value.translate_text.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": PLANTED_ADDRESS}}, "TranslateText")
        clients.polly.return_value.synthesize_speech.side_effect = ClientError(
            {"Error": {"Code": "TextLengthExceededException", "Message": PLANTED_ADDRESS}}, "SynthesizeSpeech")
        stubs = runtime_stubs(**{"utils.aws_clients": stub_module("utils.aws_clients", AWSClients=clients)})
        with patch.dict(sys.modules, stubs), patch.dict(os.environ, {"VOICE_MESSAGE_S3_BUCKET": "fixture-bucket"}):
            voice = load("checked_voice_logs", "backend/runtime/agents/communication_agent/tools/voice_message_tool.py")
            text = self.logged(lambda: self.assertFalse(voice.generate_voice_messages(
                english_text=f"Update about {PLANTED_ADDRESS}", languages=["en", "es"],
                owner=("actor", "session"), bucket_name="fixture-bucket")["success"]))
        self.assertNotIn(PLANTED_ADDRESS, text)
        self.assertIn("ClientError ValidationException", text)
        self.assertIn("ClientError TextLengthExceededException", text)

    def test_runtime_entrypoint_logs_and_client_errors_carry_no_request_or_exception_text(self):
        """Check entrypoint logs and client errors never include prompts, bodies or exceptions."""
        tree = ast.parse((ROOT / "backend/runtime/wfm_agui_entrypoint.py").read_text())
        request_text = {"prompt", "enriched", "context_envelope", "patched_messages",
                        "body_text", "body_json", "raw_body", "fwd"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                self.assertNotEqual(function, "str", f"str() can put exception text in a response: {ast.unparse(node)}")
                if function.startswith("logger."):
                    for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                        if isinstance(argument, ast.Call) and ast.unparse(argument.func) in {"len", "bool"}:
                            continue  # sizes and presence only
                        names = {n.id for n in ast.walk(argument) if isinstance(n, ast.Name)}
                        self.assertFalse(names & request_text, ast.unparse(node))
                if function == "RunErrorEvent":
                    message = next(k.value for k in node.keywords if k.arg == "message")
                    self.assertTrue(ast.unparse(message).startswith("RUN_ERROR_MESSAGES["), ast.unparse(node))
            if isinstance(node, ast.Dict):
                keys = [getattr(key, "value", None) for key in node.keys]
                self.assertNotIn("detail", keys, ast.unparse(node))
                if "message" in keys:
                    message = node.values[keys.index("message")]
                    self.assertTrue(ast.unparse(message).startswith("RUN_ERROR_MESSAGES["), ast.unparse(node))
