"""
Unit tests for signing in to LexChat and the reads made before a gather:
the build, and the models. LexChat is replaced by an
``httpx.MockTransport``, so nothing leaves the machine.
"""

import httpx
import pytest

from lex_eval.utils import get_llm, lexchat_client
from lex_eval.utils.lexchat_client import (
    SignInError,
    get_authenticated_client,
    is_spend_cap_refusal,
)

pytestmark = pytest.mark.unit

_TOKEN_REPLY = {"token": "tok", "user": {"username": "eval", "role": "eval"}}

# LexChat's own wording (services/spend_caps.py::_message and _UNCHECKABLE).
_CAP_MESSAGE = (
    "This request has been stopped because the evaluation budget for today for the "
    "AI service has been reached. Please try again later, or ask your administrator "
    "if you need more."
)
_UNCHECKABLE_MESSAGE = (
    "This request has been stopped because the AI service's spending limits cannot "
    "be checked right now. Please try again later, or ask your administrator."
)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("LEXCHAT_API", "http://nginx:8080")
    monkeypatch.setenv("USERNAME", "eval")
    monkeypatch.setenv("PASSWORD", "pw")


def _lexchat(routes: dict):
    """A transport answering each path from *routes*: (status, json body).

    A path not in *routes* gets a 404, as LexChat gives for an endpoint an
    older build does not have. The login is answered with a token unless
    *routes* says otherwise.
    """
    routes = {"/api/auth/login": (200, _TOKEN_REPLY), **routes}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = routes.get(request.url.path, (404, {"detail": "Not Found"}))
        return httpx.Response(status, json=body)

    transport = httpx.MockTransport(handler)
    transport.seen = seen
    return transport


def _use(monkeypatch, transport):
    """Make every helper in get_llm sign in through *transport*."""
    monkeypatch.setattr(
        get_llm,
        "get_authenticated_client",
        lambda: get_authenticated_client(transport=transport),
    )


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


class TestSignIn:
    def test_an_eval_account_gets_a_bearer_token(self):
        transport = _lexchat({})
        client = get_authenticated_client(transport=transport)
        assert client.headers["Authorization"] == "Bearer tok"
        assert transport.seen[0].url.path == "/api/auth/login"

    def test_a_wrong_password_names_the_settings_to_check(self):
        transport = _lexchat(
            {"/api/auth/login": (401, {"detail": "Invalid credentials"})}
        )
        with pytest.raises(SignInError, match="USERNAME and PASSWORD"):
            get_authenticated_client(transport=transport)

    def test_a_two_step_sign_in_says_to_use_an_eval_account(self):
        reply = {"mfa": "verify", "mfa_token": "x"}
        transport = _lexchat({"/api/auth/login": (200, reply)})
        with pytest.raises(SignInError, match="role 'eval'"):
            get_authenticated_client(transport=transport)

    def test_a_new_account_is_told_to_set_its_password(self):
        reply = {**_TOKEN_REPLY, "user": {"must_change_password": True}}
        transport = _lexchat({"/api/auth/login": (200, reply)})
        with pytest.raises(SignInError, match="must set its own password"):
            get_authenticated_client(transport=transport)

    def test_okta_refusing_a_password_sign_in_is_named(self):
        transport = _lexchat(
            {"/api/auth/login": (403, {"detail": "Sign in with Okta."})}
        )
        with pytest.raises(SignInError, match="Okta"):
            get_authenticated_client(transport=transport)

    def test_an_unreachable_lexchat_names_the_address(self):
        def refuse(request):
            raise httpx.ConnectError("Name or service not known", request=request)

        with pytest.raises(
            SignInError, match="Cannot reach LexChat at http://nginx:8080"
        ):
            get_authenticated_client(transport=httpx.MockTransport(refuse))

    def test_a_missing_setting_is_still_a_config_error(self, monkeypatch):
        monkeypatch.delenv("PASSWORD")
        with pytest.raises(lexchat_client.ConfigError):
            get_authenticated_client(transport=_lexchat({}))


# ---------------------------------------------------------------------------
# Spend caps
# ---------------------------------------------------------------------------


class TestSpendCapRefusal:
    def test_both_of_lexchats_refusals_are_recognised(self):
        assert is_spend_cap_refusal(_CAP_MESSAGE)
        assert is_spend_cap_refusal(_UNCHECKABLE_MESSAGE)

    def test_other_errors_are_not(self):
        assert not is_spend_cap_refusal("the AI provider returned HTTP 429")
        assert not is_spend_cap_refusal("")
        assert not is_spend_cap_refusal(None)


# ---------------------------------------------------------------------------
# The active model
# ---------------------------------------------------------------------------


# LexChat flags the first model listed when the configured one is not in its list.
_WRONG_FLAG = [
    {"name": "sonnet-4-6", "provider": "openrouter", "active": True},
    {"name": "opus", "provider": "openrouter", "active": False},
]


class TestActiveModel:
    def test_the_configured_model_wins_over_the_active_flag(self, monkeypatch):
        configured = {"active_provider": "openrouter", "model": "sonnet-5.5"}
        _use(
            monkeypatch,
            _lexchat(
                {
                    "/api/system/models": (200, configured),
                    "/api/models": (200, _WRONG_FLAG),
                }
            ),
        )
        assert get_llm.get_active_model() == ("sonnet-5.5", "openrouter")

    def test_an_older_lexchat_falls_back_to_the_active_flag(self, monkeypatch):
        _use(monkeypatch, _lexchat({"/api/models": (200, _WRONG_FLAG)}))
        assert get_llm.get_active_model() == ("sonnet-4-6", "openrouter")

    @pytest.mark.parametrize("status", [403, 500])
    def test_an_error_is_raised_rather_than_read_from_the_flag(
        self, monkeypatch, status
    ):
        _use(
            monkeypatch,
            _lexchat(
                {
                    "/api/system/models": (status, {"detail": "no"}),
                    "/api/models": (200, _WRONG_FLAG),
                }
            ),
        )
        with pytest.raises(httpx.HTTPStatusError):
            get_llm.get_active_model()

    def test_no_model_configured(self, monkeypatch):
        reply = {"active_provider": "openrouter", "model": ""}
        _use(monkeypatch, _lexchat({"/api/system/models": (200, reply)}))
        assert get_llm.get_active_model() == (None, None)


# ---------------------------------------------------------------------------
# The summarisation model
# ---------------------------------------------------------------------------


class TestSummarisationModel:
    def test_read_from_the_eval_endpoint(self, monkeypatch):
        models = {
            "active_provider": "bedrock",
            "model": "sonnet",
            "summarisation_model": "haiku",
            "summarisation_model_set": True,
        }
        _use(monkeypatch, _lexchat({"/api/system/models": (200, models)}))
        assert get_llm.get_summarisation_model() == ("haiku", "bedrock")

    def test_unknown_when_lexchat_predates_it_and_the_account_is_not_admin(
        self, monkeypatch
    ):
        transport = _lexchat(
            {
                "/api/developer/provider-config": (
                    403,
                    {"detail": "Admin access required"},
                )
            }
        )
        _use(monkeypatch, transport)
        assert get_llm.get_summarisation_model() == (None, None)

    def test_an_older_lexchat_with_an_admin_account_reads_the_provider_settings(
        self, monkeypatch
    ):
        config = {
            "active_provider": "openrouter",
            "providers": [
                {
                    "id": "openrouter",
                    "config": {"model": "gemini", "summarisation_model": ""},
                }
            ],
        }
        _use(monkeypatch, _lexchat({"/api/developer/provider-config": (200, config)}))
        # No separate summarisation model set: LexChat uses the main one.
        assert get_llm.get_summarisation_model() == ("gemini", "openrouter")


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


class TestBuild:
    def test_read_from_the_version_endpoint(self, monkeypatch):
        reply = {"version": "2026.11.9", "build": "v2026.11.9-1-gabc1234"}
        _use(monkeypatch, _lexchat({"/api/version": (200, reply)}))
        assert get_llm.get_lexchat_build() == "v2026.11.9-1-gabc1234"

    def test_an_older_lexchat_falls_back_to_bot_info(self, monkeypatch):
        reply = {"version": "2026.11.1", "build": "v2026.11.1-2-g6b5a15d"}
        _use(monkeypatch, _lexchat({"/api/bot-info": (200, reply)}))
        assert get_llm.get_lexchat_build() == "v2026.11.1-2-g6b5a15d"

    def test_a_server_with_no_git_history_gives_its_release(self, monkeypatch):
        _use(monkeypatch, _lexchat({"/api/version": (200, {"version": "2026.11.9"})}))
        assert get_llm.get_lexchat_build() == "2026.11.9"

    def test_unreachable_lexchat_gives_na_rather_than_raising(self, monkeypatch):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        _use(monkeypatch, httpx.MockTransport(refuse))
        assert get_llm.get_lexchat_build() == "n/a"
