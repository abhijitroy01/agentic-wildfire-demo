"""
Input validation utilities for AgentCore runtime.

AWS-recommended approach: Validate input FORMAT only (not semantic content).
Guardrails handle semantic/content validation.
"""

import logging

logger = logging.getLogger(__name__)


def validate_input_format(user_message: str) -> tuple[bool, str]:
    """
    Validate user input FORMAT only (not semantic content).

    AWS recommendation: Validate structure, length, and encoding.
    Let Bedrock Guardrails handle semantic validation (jailbreaks, topics, etc.)

    Args:
        user_message: The user's input message

    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if format is valid, False otherwise
        - error_message: Empty string if valid, error description if invalid
    """

    # Check for null/empty input
    if not user_message or not user_message.strip():
        logger.warning("⚠️ Empty input detected")
        return False, "Please provide a valid query."

    # Check length (prevent token exhaustion attacks)
    # Max 10,000 characters is reasonable for user queries
    if len(user_message) > 10000:
        logger.warning(f"⚠️ Input too long: {len(user_message)} characters")
        return False, "Input too long. Please limit your query to 10,000 characters."

    # Check encoding (prevent encoding attacks)
    try:
        user_message.encode('utf-8')
    except UnicodeEncodeError as e:
        logger.warning(f"⚠️ Invalid encoding detected: {e}")
        return False, "Invalid characters detected in your input."

    # Check for excessive whitespace (potential obfuscation attempt)
    whitespace_ratio = sum(c.isspace()
                           for c in user_message) / len(user_message)
    if whitespace_ratio > 0.5:
        logger.warning(
            f"⚠️ Excessive whitespace detected: {whitespace_ratio:.2%}")
        return False, "Invalid input format detected."

    # All format checks passed
    logger.debug(
        f"✅ Input format validation passed ({len(user_message)} chars)")
    return True, ""


def sanitize_input(user_message: str) -> str:
    """
    Sanitize user input by normalizing whitespace.

    Note: This is minimal sanitization. Guardrails handle content filtering.

    Args:
        user_message: The user's input message

    Returns:
        Sanitized message with normalized whitespace
    """
    # Normalize whitespace (collapse multiple spaces, remove leading/trailing)
    sanitized = ' '.join(user_message.split())

    if sanitized != user_message:
        logger.debug("🧹 Normalized whitespace in user input")

    return sanitized
