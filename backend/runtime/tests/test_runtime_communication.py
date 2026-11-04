"""Runtime checks: communication drafts for a selected incident."""
import hashlib
import io
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from checks_support import load, prompt_text, runtime_stubs, stub_module


class CommunicationChecks(unittest.TestCase):
    def test_audio_objects_are_unique_and_scoped_to_verified_owner(self):
        clients = MagicMock()
        clients.translate.return_value.translate_text.return_value = {"TranslatedText": "Translated draft"}
        clients.polly.return_value.synthesize_speech.side_effect = lambda **kwargs: {"AudioStream": io.BytesIO(b"mp3")}
        clients.s3.return_value.generate_presigned_url.return_value = "https://fixture.s3.us-west-2.amazonaws.com/draft"
        with patch.dict(sys.modules, runtime_stubs(**{
                "utils.aws_clients": stub_module("aws_clients", AWSClients=clients)})):
            voice = load("checked_owned_audio", "backend/runtime/agents/communication_agent/tools/voice_message_tool.py")
        for owner in (None, ("", "session"), ("actor",)):
            with self.assertRaises(ValueError):
                voice.generate_voice_messages("Draft", owner=owner, bucket_name="fixture")
        clients.s3.assert_not_called()
        first = voice.generate_voice_messages("Draft", owner=("actor", "session"), bucket_name="fixture")
        second = voice.generate_voice_messages("Draft", owner=("other", "session"), bucket_name="fixture")
        self.assertEqual(first["summary"], {"totalLanguages": 3, "successful": 3, "failed": 0})
        self.assertNotEqual(first["messageId"], second["messageId"])
        for code, item in first["languages"].items():
            digest = hashlib.sha256(b"actor\nsession").hexdigest()
            self.assertEqual(item["s3Key"], f"voice-messages/{digest}/{first['messageId']}/{code}.mp3")
            self.assertTrue(item["success"])
        for call in clients.s3.return_value.put_object.call_args_list:
            self.assertEqual(call.kwargs["IfNoneMatch"], "*")
            self.assertEqual(call.kwargs["ContentType"], "audio/mpeg")

    def test_communication_uses_one_selected_incident_and_reuses_its_receipt(self):
        audio = MagicMock(return_value={"summary": {"successful": 3, "totalLanguages": 3}})
        with patch.dict(sys.modules, runtime_stubs(**{
                "agents.communication_agent.tools.voice_message_tool":
                    stub_module("voice_message_tool", generate_voice_messages=audio)})):
            communication = load("checked_owned_draft", "backend/runtime/agents/communication_agent/communication_agent.py")
        incident = {"latitude": 38.5, "longitude": -121.5, "fire": {"name": "Selected fire"}}
        context = {"selected_location": incident, "previous_observations": {
            "mapData": {"locations": [incident, {"latitude": 34, "longitude": -118,
                "fire": {"name": "Other fire"}, "currentWeather": {"temperature": {"value": 66.6, "unit": "degF"}}}]},
            "location": {"latitude": 34, "longitude": -118},
            "forecastData": {"days": [{"summary": {"highTemp": {"value": 99.9, "unit": "degF"}}}]}}}
        draft_agent = MagicMock()
        draft_agent.return_value = types.SimpleNamespace(structured_output=types.SimpleNamespace(
            model_dump=lambda: {"text": "Review this sourced situation."}))
        with patch.object(communication, "Agent", return_value=draft_agent), \
                patch.dict(os.environ, {"VOICE_MESSAGE_S3_BUCKET": "fixture"}):
            receipts = []
            question = "Create a message for the Selected fire"
            tool = communication.build_communication_tool(
                owner=("actor", "session"), context=context, receipts=receipts, question=question)
            first = tool("Current weather near this fire is 66.6 degF and forecast 99.9 degF.")
            self.assertEqual(tool("Repeat"), first)
            model_input = prompt_text(draft_agent.call_args.args[0])
            self.assertIn(question, model_input)
            for unrelated in ("Other fire", "66.6", "99.9", "Current weather near this fire"):
                self.assertNotIn(unrelated, model_input)
            self.assertEqual(context["previous_observations"]["mapData"]["locations"][1]
                             ["currentWeather"]["temperature"]["value"], 66.6)
            audio.assert_called_once()
            self.assertEqual(audio.call_args.kwargs["owner"], ("actor", "session"))
            self.assertIn("not an official alert", audio.call_args.args[0])
            other = communication.build_communication_tool(owner=None, context=context, receipts=[])
            with self.assertRaises(ValueError):
                other("Create message")
        unselected = communication.build_communication_tool(
            owner=("actor", "session"), context={"previous_observations": context["previous_observations"]}, receipts=[])
        self.assertIsNone(unselected("Create message")["audioData"])
        self.assertIn("Which reported fire", unselected("Repeat")["response_text"])
        audio.assert_called_once()
