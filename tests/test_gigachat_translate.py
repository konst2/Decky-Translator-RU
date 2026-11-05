"""
GigaChat provider: OAuth, token cache, batch translation and error mapping.

HTTP (in gigachat_client) is replaced with fakes: no real keys, no network. The CA bundle and TLS
are checked separately against the live hosts; here the point is the logic
that the user's translation depends on.
"""

import asyncio
import json
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from providers import gigachat_client as gcc
from providers import gigachat_translate as gc
from providers.base import ApiKeyError, NetworkError, RateLimitError

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://api.giga.chat/v1/chat/completions"
MODELS_URL = "https://api.giga.chat/v1/models"

API_KEY = "test-authorization-key"

# The first item is what the provider must use; embeddings are listed next to
# chat models but cannot translate.
DEFAULT_MODELS = ["GigaChat-2", "GigaChat-3-Lightning", "Embeddings", "EmbeddingsGigaR"]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        if text is not None:
            self.text = text
        else:
            self.text = json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("body is not JSON")
        return self._payload


def token_response(token="token-1", expires_in=1800):
    return FakeResponse(200, {
        "access_token": token,
        "expires_at": int(time.time()) + expires_in,
    })


def chat_response(content, finish_reason="stop"):
    return FakeResponse(200, {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {"total_tokens": 42},
    })


# GigaChat's canned answer to a topic it refuses: HTTP 200, only finish_reason
# tells it from a translation.
REFUSAL = "Что-то в вашем вопросе меня смущает. Может, поговорим на другую тему?"


def models_response(model_ids=None):
    ids = DEFAULT_MODELS if model_ids is None else model_ids
    return FakeResponse(200, {"data": [{"id": model_id} for model_id in ids]})


class FakeApi:
    """Routes requests to OAuth, the model list and chat completions."""

    def __init__(self, chat_contents=None, oauth_responses=None, chat_responses=None,
                 model_ids=None, models_responses=None):
        # Either a list of chat answers (served in order) or ready responses.
        self.chat_contents = list(chat_contents or [])
        self.chat_responses = list(chat_responses or [])
        self.oauth_responses = list(oauth_responses or [])
        self.model_ids = list(DEFAULT_MODELS if model_ids is None else model_ids)
        self.models_responses = list(models_responses or [])
        self.oauth_calls = []
        self.chat_calls = []
        self.models_calls = []
        self.verify_paths = []

    def post(self, url, headers=None, data=None, json=None, timeout=None, verify=None):
        self.verify_paths.append(verify)
        if url == OAUTH_URL:
            self.oauth_calls.append({"headers": headers, "data": data})
            if self.oauth_responses:
                return self.oauth_responses.pop(0)
            return token_response(f"token-{len(self.oauth_calls)}")
        if url == CHAT_URL:
            self.chat_calls.append({"headers": headers, "json": json})
            if self.chat_responses:
                return self.chat_responses.pop(0)
            if self.chat_contents:
                return chat_response(self.chat_contents.pop(0))
            raise AssertionError("unexpected chat request")
        raise AssertionError(f"unexpected URL: {url}")

    def get(self, url, headers=None, timeout=None, verify=None):
        self.verify_paths.append(verify)
        if url == MODELS_URL:
            self.models_calls.append({"headers": headers})
            if self.models_responses:
                return self.models_responses.pop(0)
            return models_response(self.model_ids)
        raise AssertionError(f"unexpected URL: {url}")


@contextmanager
def fake_http(api):
    """Patch both HTTP methods used by the provider."""
    with patch.object(gcc.requests, "post", side_effect=api.post), \
            patch.object(gcc.requests, "get", side_effect=api.get):
        yield


def run(coro):
    return asyncio.run(coro)


def provider(api_key=API_KEY):
    return gc.GigaChatTranslateProvider(api_key=api_key)


class GigaChatTranslationTest(unittest.TestCase):

    def test_missing_key_raises(self):
        p = provider(api_key="")
        with self.assertRaises(ApiKeyError):
            run(p.translate_batch(["Hello"], "en", "ru"))
        self.assertFalse(p.is_available("en", "ru"))

    def test_language_pair(self):
        p = provider()
        self.assertTrue(p.is_available("auto", "ru"))
        self.assertTrue(p.is_available("en", "zh-CN"))
        self.assertFalse(p.is_available("en", "auto"))

    def test_batch_translation_uses_one_request(self):
        api = FakeApi(chat_contents=["1. Привет\n2. Мир"])
        with fake_http(api):
            result = run(provider().translate_batch(["Hello", "World"], "en", "ru"))

        self.assertEqual(result, ["Привет", "Мир"])
        self.assertEqual(len(api.oauth_calls), 1)
        self.assertEqual(len(api.chat_calls), 1)

        oauth = api.oauth_calls[0]
        self.assertEqual(oauth["headers"]["Authorization"], f"Basic {API_KEY}")
        self.assertEqual(oauth["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertTrue(oauth["headers"]["RqUID"])
        self.assertEqual(oauth["data"], {"scope": "GIGACHAT_API_PERS"})

        chat = api.chat_calls[0]
        self.assertEqual(chat["headers"]["Authorization"], "Bearer token-1")
        self.assertEqual(chat["json"]["model"], DEFAULT_MODELS[0])
        self.assertEqual(chat["json"]["temperature"], 0.1)
        user_prompt = chat["json"]["messages"][1]["content"]
        self.assertIn("1. Hello", user_prompt)
        self.assertIn("2. World", user_prompt)
        self.assertIn("русский", user_prompt)
        self.assertEqual(len(api.models_calls), 1)

        # The Ministry of Digital Development bundle is used for both hosts.
        self.assertTrue(all(path == gcc._CA_BUNDLE for path in api.verify_paths))

    def test_first_model_from_list_is_used(self):
        api = FakeApi(
            chat_contents=["1. Привет"],
            model_ids=["GigaChat-3-Ultra", "GigaChat-2", "Embeddings"],
        )
        with fake_http(api):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual(api.chat_calls[0]["json"]["model"], "GigaChat-3-Ultra")

    def test_embeddings_are_filtered(self):
        api = FakeApi(
            chat_contents=["1. Привет"],
            model_ids=["Embeddings", "EmbeddingsGigaR", "GigaChat-2"],
        )
        with fake_http(api):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual(api.chat_calls[0]["json"]["model"], "GigaChat-2")

    def test_model_404_refreshes_list_and_retries(self):
        api = FakeApi(
            chat_responses=[
                FakeResponse(404, {"message": "No such model"}),
                chat_response("1. Привет"),
            ],
            models_responses=[
                models_response(["GigaChat-2"]),
                models_response(["GigaChat-3-Lightning"]),
            ],
        )
        with fake_http(api):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual(len(api.chat_calls), 2)
        self.assertEqual(api.chat_calls[0]["json"]["model"], "GigaChat-2")
        self.assertEqual(api.chat_calls[1]["json"]["model"], "GigaChat-3-Lightning")
        self.assertEqual(len(api.models_calls), 2)

    def test_model_list_failure_uses_fallback(self):
        api = FakeApi(chat_contents=["1. Привет"])

        def fail_get(*args, **kwargs):
            raise gcc.requests.exceptions.ConnectionError("no network")

        with patch.object(gcc.requests, "post", side_effect=api.post), \
                patch.object(gcc.requests, "get", side_effect=fail_get):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual(api.chat_calls[0]["json"]["model"], gcc._FALLBACK_MODEL)

    def test_model_list_failure_is_not_retried_every_request(self):
        api = FakeApi(chat_contents=["1. Привет", "1. Пока"])
        attempts = 0

        def fail_get(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise gcc.requests.exceptions.ConnectionError("no network")

        p = provider()
        with patch.object(gcc.requests, "post", side_effect=api.post), \
                patch.object(gcc.requests, "get", side_effect=fail_get):
            run(p.translate_batch(["Hello"], "en", "ru"))
            run(p.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual(attempts, 1)
        self.assertEqual(len(api.chat_calls), 2)

    def test_model_list_is_cached_between_translations(self):
        api = FakeApi(chat_contents=["1. Привет", "1. Пока"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Hello"], "en", "ru"))
            run(p.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual(len(api.models_calls), 1)
        self.assertEqual(len(api.chat_calls), 2)

    def test_model_list_is_refreshed_after_ttl(self):
        api = FakeApi(
            chat_contents=["1. Привет", "1. Пока"],
            models_responses=[
                models_response(["GigaChat-2"]),
                models_response(["GigaChat-3-Lightning"]),
            ],
        )
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Hello"], "en", "ru"))
            p._client._models_refresh_at = 0  # simulate the 10-minute cache expiry
            run(p.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual(len(api.models_calls), 2)
        self.assertEqual(api.chat_calls[0]["json"]["model"], "GigaChat-2")
        self.assertEqual(api.chat_calls[1]["json"]["model"], "GigaChat-3-Lightning")

    def test_new_key_resets_token_and_model_cache(self):
        api = FakeApi(
            chat_contents=["1. Привет", "1. Пока"],
            models_responses=[
                models_response(["GigaChat-2"]),
                models_response(["GigaChat-3-Lightning"]),
            ],
        )
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Hello"], "en", "ru"))
            p.set_api_key("another-key")
            run(p.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual(len(api.oauth_calls), 2)
        self.assertEqual(len(api.models_calls), 2)
        self.assertEqual(api.chat_calls[1]["json"]["model"], "GigaChat-3-Lightning")

    def test_token_is_reused_between_calls(self):
        api = FakeApi(chat_contents=["1. Привет", "1. Пока"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Hello"], "en", "ru"))
            run(p.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual(len(api.oauth_calls), 1)
        self.assertEqual(len(api.chat_calls), 2)

    def test_wrong_numbering_falls_back_to_single_requests(self):
        api = FakeApi(chat_contents=["Извините, не могу", "Привет", "Мир"])
        with fake_http(api):
            result = run(provider().translate_batch(["Hello", "World"], "en", "ru"))

        self.assertEqual(result, ["Привет", "Мир"])
        self.assertEqual(len(api.chat_calls), 3)

    def test_missing_line_falls_back(self):
        api = FakeApi(chat_contents=["1. Привет", "Привет", "Мир"])
        with fake_http(api):
            result = run(provider().translate_batch(["Hello", "World"], "en", "ru"))

        self.assertEqual(result, ["Привет", "Мир"])
        self.assertEqual(len(api.chat_calls), 3)

    def test_multiline_answer_and_quotes_are_cleaned(self):
        api = FakeApi(chat_contents=["1. «Привет,\nмир»\n2. Пока"])
        with fake_http(api):
            result = run(provider().translate_batch(["Hello", "Bye"], "en", "ru"))

        self.assertEqual(result, ["Привет, мир", "Пока"])

    def test_quotes_from_source_are_kept(self):
        # The source line is quoted, so the quotes in the answer are the
        # game's, not the model's; the style may change ("..." -> «...»).
        api = FakeApi(chat_contents=["1. «Привет»\n2. Пока"])
        with fake_http(api):
            result = run(provider().translate_batch(['"Hello"', "Bye"], "en", "ru"))

        self.assertEqual(result, ["«Привет»", "Пока"])

    def test_quotes_kept_in_single_line_mode(self):
        api = FakeApi(chat_contents=["не текст с номерами", "«Привет»"])
        with fake_http(api):
            result = run(provider().translate_batch(['"Hello"'], "en", "ru"))

        self.assertEqual(result, ["«Привет»"])

    def test_added_quotes_removed_in_single_line_mode(self):
        api = FakeApi(chat_contents=["не текст с номерами", "«Привет»"])
        with fake_http(api):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])

    def test_blank_texts_are_not_sent(self):
        api = FakeApi(chat_contents=["1. Привет"])
        with fake_http(api):
            result = run(provider().translate_batch(["", "Hello", "   "], "en", "ru"))

        self.assertEqual(result, ["", "Привет", "   "])
        self.assertEqual(len(api.chat_calls), 1)
        self.assertEqual(len(api.chat_calls[0]["json"]["messages"]), 2)

    def test_invalid_key_is_reported(self):
        api = FakeApi(oauth_responses=[FakeResponse(401, {"message": "Unauthorized"})])
        with fake_http(api):
            with self.assertRaises(ApiKeyError):
                run(provider().translate_batch(["Hello"], "en", "ru"))

    def test_rate_limit_is_reported(self):
        api = FakeApi(oauth_responses=[FakeResponse(429, {"message": "Too many"})])
        with fake_http(api):
            with self.assertRaises(RateLimitError):
                run(provider().translate_batch(["Hello"], "en", "ru"))

    def test_expired_token_is_refreshed_once(self):
        api = FakeApi(
            oauth_responses=[token_response("old"), token_response("new")],
            chat_responses=[FakeResponse(401, {"message": "token expired"}), chat_response("1. Привет")],
        )
        with fake_http(api):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual(len(api.oauth_calls), 2)
        self.assertEqual(api.chat_calls[0]["headers"]["Authorization"], "Bearer old")
        self.assertEqual(api.chat_calls[1]["headers"]["Authorization"], "Bearer new")

    def test_refused_batch_loses_only_the_refused_line(self):
        api = FakeApi(chat_responses=[
            chat_response(REFUSAL, "blacklist"),  # the whole batch is refused
            chat_response(REFUSAL, "blacklist"),  # line 1 alone: refused again
            chat_response("Привет"),              # line 2 alone: translated
        ])
        with fake_http(api):
            result = run(provider().translate_batch(["Kill them all", "Hello"], "en", "ru"))

        # None: main.py shows the original and keeps it out of the cache.
        self.assertEqual(result, [None, "Привет"])
        self.assertEqual(len(api.chat_calls), 3)

    def test_refusal_with_empty_text_is_not_an_error(self):
        api = FakeApi(chat_responses=[
            chat_response("", "blacklist"),
            chat_response("", "blacklist"),
        ])
        with fake_http(api):
            result = run(provider().translate_batch(["Kill them all"], "en", "ru"))

        self.assertEqual(result, [None])

    def test_cut_off_batch_is_not_trusted(self):
        # Both numbers are present, so without finish_reason the truncated
        # second line would pass as a translation.
        api = FakeApi(chat_responses=[
            chat_response("1. Привет\n2. Длинная фра", "length"),
            chat_response("Привет"),
            chat_response("Длинная фраза"),
        ])
        with fake_http(api):
            result = run(provider().translate_batch(["Hello", "A long phrase"], "en", "ru"))

        self.assertEqual(result, ["Привет", "Длинная фраза"])
        self.assertEqual(len(api.chat_calls), 3)

    def test_cut_off_single_line_is_none(self):
        api = FakeApi(chat_responses=[
            chat_response("1. Длинная фра", "length"),
            chat_response("Длинная фра", "length"),
        ])
        with fake_http(api):
            result = run(provider().translate_batch(["A long phrase"], "en", "ru"))

        self.assertEqual(result, [None])

    def test_single_translate_returns_original_on_refusal(self):
        api = FakeApi(chat_responses=[
            chat_response(REFUSAL, "blacklist"),
            chat_response(REFUSAL, "blacklist"),
        ])
        with fake_http(api):
            result = run(provider().translate("Kill them all", "en", "ru"))

        self.assertEqual(result, "Kill them all")

    def test_empty_single_translation_raises(self):
        api = FakeApi(chat_contents=["не текст с номерами", "   "])
        with fake_http(api):
            with self.assertRaises(NetworkError):
                run(provider().translate_batch(["Hello"], "en", "ru"))

    def test_missing_ca_bundle_raises(self):
        with patch.object(gcc, "_CA_BUNDLE", "/nonexistent/giga-ca.pem"):
            with self.assertRaises(NetworkError):
                run(provider().translate_batch(["Hello"], "en", "ru"))

    def test_expired_bundle_asks_to_update_plugin(self):
        expired = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with patch.object(gcc, "_CA_BUNDLE_NOT_AFTER", expired):
            with self.assertRaises(NetworkError) as ctx:
                run(provider().translate_batch(["Hello"], "en", "ru"))
        self.assertIn("Сертификат GigaChat устарел", str(ctx.exception))
        self.assertIn("Обновите плагин", str(ctx.exception))

    def test_tls_error_asks_to_update_plugin(self):
        api = FakeApi()

        def fail_post(*args, **kwargs):
            raise gcc.requests.exceptions.SSLError("certificate verify failed")

        with patch.object(gcc.requests, "post", side_effect=fail_post), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            with self.assertRaises(NetworkError) as ctx:
                run(provider().translate_batch(["Hello"], "en", "ru"))
        self.assertIn("Обновите плагин", str(ctx.exception))

    def test_chat_error_is_reported(self):
        api = FakeApi(
            chat_responses=[FakeResponse(400, {"message": "model is not available"})],
        )
        with fake_http(api):
            with self.assertRaises(NetworkError) as ctx:
                run(provider().translate_batch(["Hello"], "en", "ru"))
        self.assertIn("model is not available", str(ctx.exception))


class GigaChatNetworkTest(unittest.TestCase):

    def test_network_probe_ok(self):
        api = FakeApi()
        with fake_http(api):
            ok, reason = run(provider().test_network())
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(len(api.oauth_calls), 1)
        self.assertEqual(len(api.models_calls), 1)

    def test_probe_feeds_model_cache(self):
        api = FakeApi(
            chat_contents=["1. Привет"],
            model_ids=["GigaChat-3-Lightning", "GigaChat-2"],
        )
        p = provider()
        with fake_http(api):
            ok, _ = run(p.test_network())
            result = run(p.translate_batch(["Hello"], "en", "ru"))

        self.assertTrue(ok)
        self.assertEqual(result, ["Привет"])
        self.assertEqual(api.chat_calls[0]["json"]["model"], "GigaChat-3-Lightning")
        self.assertEqual(len(api.models_calls), 1)

    def test_probe_does_not_refetch_models_each_time(self):
        api = FakeApi()
        p = provider()
        with fake_http(api):
            run(p.test_network())
            run(p.test_network())

        self.assertEqual(len(api.models_calls), 1)

    def test_network_probe_reports_no_models(self):
        api = FakeApi(model_ids=["Embeddings"])
        with fake_http(api):
            ok, reason = run(provider().test_network())
        self.assertFalse(ok)
        self.assertIn("Нет доступных моделей", reason)

    def test_network_probe_reports_model_list_failure(self):
        api = FakeApi()

        def fail_get(*args, **kwargs):
            raise gcc.requests.exceptions.ConnectionError("no network")

        with patch.object(gcc.requests, "post", side_effect=api.post), \
                patch.object(gcc.requests, "get", side_effect=fail_get):
            ok, reason = run(provider().test_network())
        self.assertFalse(ok)
        self.assertIn("список моделей", reason)

    def test_probe_recovers_soon_after_model_list_failure(self):
        # A failed fetch must not keep the probe red for the whole 10-minute
        # TTL: once the retry delay passes and the network is back, it is green.
        api = FakeApi()
        network_up = False

        def flaky_get(*args, **kwargs):
            if not network_up:
                raise gcc.requests.exceptions.ConnectionError("no network")
            return api.get(*args, **kwargs)

        p = provider()
        start = time.time()
        with patch.object(gcc.requests, "post", side_effect=api.post), \
                patch.object(gcc.requests, "get", side_effect=flaky_get):
            ok_down, _ = run(p.test_network())
            network_up = True
            # Before the retry delay the failure is still cached.
            ok_cached, _ = run(p.test_network())
            with patch.object(gcc.time, "time",
                              return_value=start + gcc._MODELS_RETRY_AFTER_FAILURE + 1):
                ok_up, reason = run(p.test_network())

        self.assertFalse(ok_down)
        self.assertFalse(ok_cached)
        self.assertTrue(ok_up, reason)
        self.assertEqual(len(api.models_calls), 1)

    def test_network_probe_reports_bad_key(self):
        api = FakeApi(oauth_responses=[FakeResponse(401, {"message": "Unauthorized"})])
        with fake_http(api):
            ok, reason = run(provider().test_network())
        self.assertFalse(ok)
        self.assertIn("Authorization Key", reason)

    def test_network_probe_without_key(self):
        ok, reason = run(provider(api_key="").test_network())
        self.assertFalse(ok)
        self.assertIn("Authorization Key", reason)

    def test_network_probe_reports_expired_bundle(self):
        expired = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with patch.object(gcc, "_CA_BUNDLE_NOT_AFTER", expired):
            ok, reason = run(provider().test_network())
        self.assertFalse(ok)
        self.assertIn("Обновите плагин", reason)


if __name__ == "__main__":
    unittest.main()
