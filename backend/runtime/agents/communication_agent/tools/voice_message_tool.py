"""Bounded parallel translation and speech for owner-scoped, private draft recordings."""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from utils.aws_clients import AWSClients
from utils.log_safety import error_summary

logger = logging.getLogger(__name__)
LANGUAGE_CONFIG = {
    "en": {"voice": "Joanna", "name": "English", "engine": "neural"},
    "es": {"voice": "Lupe", "name": "Spanish", "engine": "neural"},
    "hi": {"voice": "Aditi", "name": "Hindi", "engine": "standard"},
}


def translate_text(text, target_language):
    if target_language == "en":
        return text
    return AWSClients.translate().translate_text(
        Text=text, SourceLanguageCode="en", TargetLanguageCode=target_language)["TranslatedText"]


def generate_voice_messages(english_text, *, owner, bucket_name, languages=None):
    """Infrastructure identifiers are code-bound, never model tool arguments. No public writes."""
    languages = list(dict.fromkeys(languages or LANGUAGE_CONFIG))
    if (not owner or len(owner) != 2 or not all(owner) or not bucket_name
            or not english_text.strip() or len(english_text) > 3000
            or not languages or not set(languages) <= LANGUAGE_CONFIG.keys()):
        raise ValueError("Invalid owned communication request")
    owner_id = hashlib.sha256((owner[0]+"\n"+owner[1]).encode()).hexdigest()
    message_id = uuid4().hex
    now = datetime.now(UTC)
    expires = int((now+timedelta(hours=1)).timestamp())
    # Construct clients before threads; avoid simultaneous lazy singleton construction.
    s3, polly = AWSClients.s3(), AWSClients.polly()
    AWSClients.translate()

    def process(code):
        config = LANGUAGE_CONFIG[code]
        result = {"success": False, "languageName": config["name"], "voice": config["voice"],
                  "isExpired": False}
        try:
            text = translate_text(english_text, code)
            result["translatedText"] = text
            response = polly.synthesize_speech(Text=text, OutputFormat="mp3", TextType="text",
                                              VoiceId=config["voice"], Engine=config["engine"])
            with response["AudioStream"] as stream:
                audio = stream.read()
            if not audio:
                raise ValueError("Empty synthesized audio")
            key = f"voice-messages/{owner_id}/{message_id}/{code}.mp3"
            s3.put_object(Bucket=bucket_name, Key=key, Body=audio, ContentType="audio/mpeg",
                          IfNoneMatch="*", ServerSideEncryption="AES256")
            url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket_name, "Key": key},
                                           ExpiresIn=max(1, expires-int(datetime.now(UTC).timestamp())))
            result.update(success=True, audioUrl=url, s3Key=key, expires=expires)
        except Exception as exc:  # noqa: BLE001 -- each language retains its own failure receipt
            logger.warning("Communication audio failed language=%s error=%s", code, error_summary(exc))
        return code, result

    with ThreadPoolExecutor(max_workers=len(languages)) as executor:
        results = dict(executor.map(process, languages))
    successful = sum(item["success"] for item in results.values())
    return {"success": bool(successful), "messageId": message_id, "generatedAt": now.isoformat(),
            "expiresAt": datetime.fromtimestamp(expires, UTC).isoformat(), "languages": results,
            "summary": {"totalLanguages": len(languages), "successful": successful,
                        "failed": len(languages)-successful}}
