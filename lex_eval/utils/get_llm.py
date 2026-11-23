import logging
from typing import Optional, Tuple

from .lexchat_client import get_authenticated_client

logger = logging.getLogger(__name__)


def get_active_model() -> Tuple[Optional[str], Optional[str]]:
    """
    Query LexChat's /api/models endpoint and find the active model.

    The active model is the one configured in LexChat's admin portal. This is
    the model that will actually process requests. The eval does not select or
    override the model; it reads it from the API.

    Returns:
        Tuple of (model_name, provider) where both are None if no active model
        was found. Provider is the provider name ("ollama" or "openrouter") for
        informational purposes only.
    """
    client = get_authenticated_client()
    try:
        models_response = client.get("/api/models")
        models_response.raise_for_status()
        models = models_response.json()
        for m in models:
            if m.get("active") is True:
                return m.get("name"), m.get("provider", "").lower()
        return None, None
    finally:
        client.close()


def get_summarisation_model() -> Tuple[Optional[str], Optional[str]]:
    """
    Return the summarisation model LexChat actually uses, and its provider.

    Read from /api/system/models, which an eval account may call. On a LexChat
    too old to have it, the admin-only /api/developer/provider-config is tried
    instead. When neither answers, both values are None: the model is unknown.
    It is never assumed to be the main model.
    """
    client = get_authenticated_client()
    try:
        resp = client.get("/api/system/models")
        if resp.status_code == 200:
            data = resp.json()
            return data.get("summarisation_model") or None, data.get("active_provider")
        logger.info(
            "/api/system/models returned HTTP %d; trying the admin provider settings",
            resp.status_code,
        )
        return _summarisation_from_provider_config(client)
    finally:
        client.close()


def _summarisation_from_provider_config(client) -> Tuple[Optional[str], Optional[str]]:
    """The summarisation model from the admin-only provider settings (older LexChat)."""
    resp = client.get("/api/developer/provider-config")
    if resp.status_code != 200:
        logger.warning(
            "Summarisation model unknown: /api/developer/provider-config returned "
            "HTTP %d (admin only).",
            resp.status_code,
        )
        return None, None
    data = resp.json()
    provider = data.get("active_provider")
    cfgs = {p["id"]: p.get("config", {}) for p in data.get("providers", [])}
    cfg = cfgs.get(provider, {})
    # LexChat summarises with the main model when no separate one is set.
    return cfg.get("summarisation_model") or cfg.get("model") or None, provider


def get_lexchat_build() -> str:
    """
    Return the LexChat build the server reports, e.g. "v2026.09.3-2-gabc1234".

    Read from /api/version. On a LexChat older than v2026.11.2, which has no
    /api/version, /api/bot-info is read instead, as it then carried the build.
    Either gives the release alone ("2026.09.3") when the server has no git
    history. Returns "n/a" when neither reports one, as builds before
    v2026.09.3 do not, or when LexChat cannot be reached.
    """
    try:
        client = get_authenticated_client()
    except Exception as exc:
        logger.warning("Could not read the LexChat build (%s)", exc)
        return "n/a"
    try:
        info = {}
        for path in ("/api/version", "/api/bot-info"):
            resp = client.get(path)
            if resp.status_code == 200:
                info = resp.json()
                if info.get("build"):
                    break
    except Exception as exc:
        logger.warning("Could not read the LexChat build (%s)", exc)
        return "n/a"
    finally:
        client.close()
    return info.get("build") or info.get("version") or "n/a"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    model, provider = get_active_model()
    if model:
        print(f"\nActive model (manager/worker): {model}  (provider: {provider})")
    else:
        print("\nNo active model found. Set one in LexChat's admin portal.")

    print(f"LexChat build:                 {get_lexchat_build()}")

    summ_model, summ_provider = get_summarisation_model()
    if summ_model:
        if summ_model == model:
            print(
                f"Summarisation model:           {summ_model}"
                "  (same as active model, no separate summarisation model configured)"
            )
        else:
            print(
                f"Summarisation model:           {summ_model}  (provider: {summ_provider})"
            )
    else:
        print(
            "Summarisation model:           unknown (LexChat gave no summarisation "
            "model to this account)"
        )
