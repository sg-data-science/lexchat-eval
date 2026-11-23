import httpx
import os
import logging
import re
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Configure logging
logger = logging.getLogger(__name__)

# Load environment variables from parent directory's .env file
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""

    pass


def _get_required_env(key: str) -> str:
    """Get a required environment variable or raise ConfigError."""
    value = os.getenv(key)
    if not value:
        raise ConfigError(f"Missing required environment variable: {key}")
    return value


def _validate_config() -> dict[str, str]:
    """Validate and return configuration from environment variables."""
    try:
        base_url = _get_required_env("LEXCHAT_API")
        username = _get_required_env("USERNAME")
        password = _get_required_env("PASSWORD")

        return {"base_url": base_url, "username": username, "password": password}
    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        logger.error(f"Please ensure .env file exists at: {env_path}")
        raise


class SignInError(RuntimeError):
    """LexChat would not give this account a session; the message says why."""

    pass


# LexChat ends a request with this wording when a spending cap is reached
# (services/spend_caps.py::_message and _UNCHECKABLE).
_SPEND_CAP_RE = re.compile(
    r"has been stopped because .*(has been reached|cannot be checked)"
)
SPEND_CAP_ERROR_PREFIX = "LexChat spend cap reached: "


def is_spend_cap_refusal(message: Optional[str]) -> bool:
    """True when *message* is LexChat refusing a request over a spending cap."""
    return bool(message) and bool(_SPEND_CAP_RE.search(message))


def error_detail(resp: httpx.Response) -> str:
    """The ``detail`` LexChat puts in an error response, or the raw body."""
    try:
        return str(resp.json().get("detail") or "")
    except Exception:
        return resp.text[:200]


def _sign_in(client: httpx.Client, username: str, password: str) -> str:
    """Sign in and return the session token, or raise SignInError saying why not."""
    base = client.base_url
    try:
        resp = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
    except httpx.TransportError as e:
        raise SignInError(
            f"Cannot reach LexChat at {base} ({type(e).__name__}: {e}). Check "
            "LEXCHAT_API, and that LexChat is running and reachable from here."
        ) from e

    if resp.status_code == 401:
        raise SignInError(
            f"LexChat refused the user name or password for {username!r} (HTTP 401). "
            "Check USERNAME and PASSWORD."
        )
    if resp.status_code == 403 and "okta" in error_detail(resp).lower():
        raise SignInError(
            "LexChat signs in through Okta, which refuses a password sign-in for "
            f"every account except admins, so {username!r} cannot sign in (HTTP 403)."
        )
    if resp.status_code >= 400:
        raise SignInError(
            f"LexChat sign-in failed with HTTP {resp.status_code}: {error_detail(resp)}"
        )

    body = resp.json()
    if body.get("mfa"):
        raise SignInError(
            f"LexChat asked {username!r} for a two-step sign-in code. Use an account "
            "with role 'eval' (Admin Portal, Users), which is never asked for one."
        )
    if (body.get("user") or {}).get("must_change_password"):
        raise SignInError(
            f"{username!r} must set its own password before it can be used. Sign in "
            "once as that account in a browser, choose a new password, and put it "
            "in PASSWORD."
        )
    token = body.get("token")
    if not token:
        raise SignInError("LexChat's sign-in response had no token.")
    return token


def get_authenticated_client(
    transport: Optional[httpx.BaseTransport] = None,
) -> httpx.Client:
    """
    Creates and returns an authenticated HTTP client for the LexChat API.

    The client is configured with the base URL from environment variables
    and authenticated using credentials from the .env file.

    Args:
        transport: Replaces the network, for tests only.

    Returns:
        httpx.Client: An authenticated HTTP client with bearer token set.

    Raises:
        ConfigError: If required environment variables are missing.
        SignInError: If LexChat cannot be reached or will not sign the
            account in. The message names the cause and what to check.

    Note:
        Caller is responsible for closing the client when done.
        Use in a context manager or call client.close() explicitly.
    """
    config = _validate_config()

    # Timeout for streaming LLM responses, in seconds. The read timeout is the
    # binding one: it is how long the stream may go silent before we give up,
    # not how long the whole response may take. Deep Research runs can stream
    # for well over the 300s default and still be working, so raise this with
    # LEXCHAT_TIMEOUT (or gather_responses.py --timeout) rather than losing them.
    timeout_s = float(os.getenv("LEXCHAT_TIMEOUT", "300"))
    timeout = httpx.Timeout(timeout_s, read=timeout_s)
    client = httpx.Client(
        base_url=config["base_url"], timeout=timeout, transport=transport
    )

    logger.info("Authenticating with LexChat API...")
    try:
        token = _sign_in(client, config["username"], config["password"])
    except Exception as e:
        client.close()
        logger.error("Sign-in failed: %s", e)
        raise
    client.headers.update({"Authorization": f"Bearer {token}"})
    logger.info("Successfully authenticated with LexChat API")
    return client
