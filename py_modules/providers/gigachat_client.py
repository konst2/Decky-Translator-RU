# providers/gigachat_client.py
# GigaChat (Sber) API client: OAuth, model list, chat request, error mapping.
#
# Shared by the two GigaChat translation methods (direct and with context).
# ProviderManager keeps one client for both: one Authorization Key, one access
# token and one model cache, so switching methods costs no extra OAuth and a
# changed key reaches both at once.

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, List, NamedTuple, Optional, Tuple

import requests

from .base import NetworkError, ApiKeyError, RateLimitError, TranslationSupersededError

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
_API_BASE = "https://api.giga.chat/v1"
_CHAT_URL = f"{_API_BASE}/chat/completions"
_MODELS_URL = f"{_API_BASE}/models"

# Individual (physical person) plan. Corporate plans need another scope and
# are not what a Steam Deck user has.
_OAUTH_SCOPE = "GIGACHAT_API_PERS"

# The first model in /v1/models is used; the list changes over time (the
# former "GigaChat" model was removed in favour of GigaChat-2 and GigaChat-3),
# so it is not hardcoded. The fallback is only for the case when the list
# cannot be fetched at all.
_FALLBACK_MODEL = "GigaChat-2"

# The CA chain of the Ministry of Digital Development is not in any system
# store (macOS, SteamOS, browsers), so requests would reject the TLS
# certificate. The bundle ships with the plugin and is used only for the two
# GigaChat hosts; disabling verification is not an option: the long-lived
# Authorization Key travels over the same connection.
_CA_BUNDLE = os.path.join(os.path.dirname(__file__), "certs", "giga-ca.pem")

# notAfter of the Sub CA in the bundle (see the header of giga-ca.pem). The
# server certificates are signed by it, so this is the last moment TLS can be
# verified. Keep in sync with the dates in the file.
_CA_BUNDLE_NOT_AFTER = datetime(2029, 7, 19, 12, 50, 41, tzinfo=timezone.utc)

_OAUTH_TIMEOUT = 10.0
_CHAT_TIMEOUT = 40.0
_MODELS_TIMEOUT = 10.0

# Access tokens live 30 minutes; refresh a minute early so a token cannot
# expire between the check and the request.
_TOKEN_TTL = 30 * 60
_TOKEN_REFRESH_MARGIN = 60

# The model list is re-read every 10 minutes: models appear and disappear,
# and a stale name turns every translation into a 404.
_MODELS_TTL = 600

# After a failed fetch the list is retried sooner. Waiting the full TTL kept
# the probe red for 10 minutes after the network came back (e.g. the Deck
# woke up before Wi-Fi connected). Retrying on every call is not an option
# either: while /models is down each translation would wait for its timeout.
# 30 s is below the 60 s reachability TTL, so the next probe sees the fix.
_MODELS_RETRY_AFTER_FAILURE = 30

# GigaChat error text is shown in the overlay as is; a long one would
# overflow it, so it is cut to this length.
_MAX_ERROR_MESSAGE_LENGTH = 200

INVALID_RESPONSE_MESSAGE = "GigaChat вернул некорректный ответ"

# finish_reason values whose text must not be shown as a translation.
# "blacklist": GigaChat refused the topic and answered with a canned phrase
# (HTTP 200, so only this field tells it apart from a real translation; game
# text with violence or politics hits it). "length": the answer was cut off,
# a truncated last line could still pass the numbering check.
_UNUSABLE_FINISH_REASONS = {"blacklist", "length"}


# Picks a model from the API's list for one translation method; None means
# "no preference, use the client's default (the first model)".
ModelChooser = Callable[[List[str]], Optional[str]]


class TokenQuotaError(RateLimitError):
    """
    HTTP 402: the free token pool of the model is used up.

    A separate class so that a method on a non-default model can say which
    model ran out and suggest another method: each model has its own pool.
    """

    def __init__(self, message: str, model: str):
        super().__init__(message)
        self.model = model


class ChatAnswer(NamedTuple):
    content: str
    finish_reason: str

    @property
    def usable(self) -> bool:
        return self.finish_reason not in _UNUSABLE_FINISH_REASONS


def _shorten(message: str) -> str:
    message = message.strip()
    if len(message) > _MAX_ERROR_MESSAGE_LENGTH:
        message = message[:_MAX_ERROR_MESSAGE_LENGTH].rstrip() + "…"
    return message


def _check_bundle_fresh() -> None:
    """Fail with an actionable message once the shipped CA chain has expired."""
    if datetime.now(timezone.utc) >= _CA_BUNDLE_NOT_AFTER:
        logger.error(f"GigaChat CA bundle expired on {_CA_BUNDLE_NOT_AFTER.isoformat()}")
        raise NetworkError("Сертификат GigaChat устарел. Обновите плагин")


def _error_message(response) -> str:
    """Return the message of a GigaChat/OpenAI-style error response, or ""."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""

    message = payload.get("message")
    if not isinstance(message, str):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
    if not isinstance(message, str):
        return ""
    return _shorten(message)


class GigaChatClient:
    """Thread-safe GigaChat API client with token and model caches."""

    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        self._model = ""
        # Shared cache: the reachability probe and translation must not fetch
        # the list more often than once per _MODELS_TTL.
        self._models: Optional[List[str]] = None
        # When to fetch the list next: after a success and after a failure
        # the delays differ, so the deadline is stored, not the fetch time.
        self._models_refresh_at = 0.0
        # Models that are listed but answered 404: visible to the key, not
        # usable (e.g. by the plan). A method's own choice skips them until
        # the next fresh list, so a request does not hit the same 404 every
        # time. The default (first) model is never skipped: there is
        # nothing to fall back to.
        self._rejected_models: set = set()
        self._access_token = ""
        self._token_expires_at = 0.0
        # The probe and translations run in worker threads, so the token and
        # model caches need locks: two simultaneous calls must not fetch twice.
        self._token_lock = threading.Lock()
        self._model_lock = threading.Lock()
        # One chat request at a time (see chat). Taken outside the two locks
        # above and never while holding them, so the order is always the
        # same and they cannot deadlock.
        self._chat_lock = threading.Lock()
        # Number of the latest screen sent for translation (begin_screen).
        # Shared by both methods, as the lock above is.
        self._latest_screen = 0
        self._screen_lock = threading.Lock()

    @property
    def api_key(self) -> str:
        return self._api_key

    def set_api_key(self, api_key: str) -> None:
        """Update the Authorization Key and drop token and model caches."""
        if api_key != self._api_key:
            self._api_key = api_key
            self._invalidate_token()
            # A different key can see a different set of models, so the list
            # cached for the old key must not be reused.
            with self._model_lock:
                self._models = None
                self._models_refresh_at = 0.0
                self._model = ""
                self._rejected_models.clear()

    def check_ready(self) -> None:
        """Raise before any request if the key or the CA bundle is missing."""
        if not self._api_key:
            logger.error("GigaChat Authorization Key not configured")
            raise ApiKeyError("Не задан Authorization Key GigaChat")
        if not os.path.isfile(_CA_BUNDLE):
            logger.error(f"GigaChat CA bundle not found: {_CA_BUNDLE}")
            raise NetworkError(
                "Не найден сертификат Минцифры для GigaChat. Переустановите плагин"
            )
        _check_bundle_fresh()

    # -- chat --

    def begin_screen(self) -> int:
        """
        Register a new screen to translate and return its number.

        Every newer screen makes the older ones stale (see chat). The
        frontend translates one screen at a time, so a newer screen means
        the user closed the overlay of the older one and asked again.
        """
        with self._screen_lock:
            self._latest_screen += 1
            return self._latest_screen

    def chat(
        self,
        messages: List[dict],
        choose_model: Optional[ModelChooser] = None,
        screen: Optional[int] = None,
    ) -> ChatAnswer:
        """
        Send one chat request and return the assistant's text and finish_reason.

        choose_model lets a method pick its own model from the shared list
        (GigaChat+ prefers the strongest one); without it the first model is
        used, as the direct method always did.

        Requests go strictly one at a time, for every method sharing this
        client: a caller waits until the request before it has finished.
        Parallel requests or quick bursts can look like abuse to the service
        and get the request cut off or the key blocked. They did happen: the
        overlay can be closed while a translation is still on its way, and
        the next hotkey sent a second request beside the first. The GigaChat+
        glossary cleanup runs in the background and relies on this as well.

        screen — the number from begin_screen for a screen's translation,
        None for anything else (the glossary cleanup). A screen that became
        stale while waiting for its turn is not sent: TranslationSupersededError.
        Without that, five quick "close and translate again" would send all
        five requests one after another, and the only wanted one, the last,
        would wait for four thrown-away translations and pay their tokens.
        A request already sent is not interrupted: HTTP cannot be stopped
        halfway, it just finishes.
        """
        with self._chat_lock:
            return self._chat_one(messages, choose_model, screen)

    def _is_stale_screen(self, screen: int) -> bool:
        with self._screen_lock:
            return screen != self._latest_screen

    def _raise_if_stale(self, screen: Optional[int]) -> None:
        """Before every send, retries included: a retry of a stale screen is a wasted request too."""
        if screen is not None and self._is_stale_screen(screen):
            raise TranslationSupersededError(f"screen {screen} superseded by a newer one")

    def _chat_one(
        self, messages: List[dict], choose_model: Optional[ModelChooser], screen: Optional[int]
    ) -> ChatAnswer:
        """chat() without the lock: one request with its retries (401, 404). Caller holds _chat_lock."""
        self._raise_if_stale(screen)
        token = self._ensure_token()
        model = self._pick_model(token, choose_model)
        response = self._post_chat(token, self._payload(messages, model))

        # The token can expire or be revoked between the check and the
        # request; one refresh is cheap and avoids a failed translation.
        if response.status_code == 401:
            logger.info("GigaChat token rejected, refreshing it once")
            self._invalidate_token()
            token = self._ensure_token()
            self._raise_if_stale(screen)
            response = self._post_chat(token, self._payload(messages, model))

        # The model list changes: a name can be removed or replaced. Re-read
        # the list and retry once with the current first model.
        if response.status_code == 404:
            logger.warning(f"GigaChat model {model} not found, refreshing the list")
            new_model = self._pick_model(token, choose_model, force=True)
            if new_model == model and choose_model is not None:
                # Still in the fresh list, so the list will not help: the
                # model is listed but not usable for this key. Skip it and
                # take the method's next choice.
                logger.warning(f"GigaChat model {model} is listed but unavailable, skipping it")
                with self._model_lock:
                    self._rejected_models.add(model)
                new_model = self._pick_model(token, choose_model)
            if new_model != model:
                logger.info(f"GigaChat retry with model {new_model}")
                self._raise_if_stale(screen)
                response = self._post_chat(token, self._payload(messages, new_model))
                # Errors below must name the model that actually answered:
                # GigaChat+ tells a used-up pool of its own model apart by it.
                model = new_model

        if response.status_code != 200:
            self._handle_chat_error(response, model)

        try:
            body = response.json()
        except ValueError as e:
            logger.error("GigaChat returned invalid JSON")
            raise NetworkError(INVALID_RESPONSE_MESSAGE) from e

        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            logger.info(f"GigaChat tokens: {usage.get('total_tokens', '?')}")

        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.error(f"Unexpected GigaChat response structure: {str(body)[:500]}")
            raise NetworkError(INVALID_RESPONSE_MESSAGE)

        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = ""
        # Checked before the content: a refused or cut-off answer is handled
        # by the caller even when its text is empty, it is not a broken reply.
        if finish_reason in _UNUSABLE_FINISH_REASONS:
            return ChatAnswer(content if isinstance(content, str) else "", finish_reason)

        if not isinstance(content, str) or not content.strip():
            logger.error("GigaChat response has no translation text")
            raise NetworkError(INVALID_RESPONSE_MESSAGE)
        return ChatAnswer(content, finish_reason)

    @staticmethod
    def _payload(messages: List[dict], model: str) -> dict:
        return {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
        }

    def _post_chat(self, token: str, payload: dict):
        return self._post(
            _CHAT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=_CHAT_TIMEOUT,
        )

    def _post(self, url: str, headers: dict, timeout: float, data=None, json=None):
        try:
            return requests.post(
                url,
                headers=headers,
                data=data,
                json=json,
                timeout=timeout,
                verify=_CA_BUNDLE,
            )
        except requests.exceptions.Timeout as e:
            logger.error(f"GigaChat timeout: {e}")
            raise NetworkError("Превышено время ожидания GigaChat") from e
        except requests.exceptions.SSLError as e:
            logger.error(f"GigaChat TLS error: {e}")
            raise NetworkError(
                "Сертификат GigaChat не прошёл проверку. Обновите плагин"
            ) from e
        except requests.exceptions.ConnectionError as e:
            logger.error(f"GigaChat connection error: {e}")
            raise NetworkError("Нет подключения к интернету") from e
        except Exception as e:
            logger.error(f"GigaChat request error: {e}")
            raise NetworkError(f"Ошибка запроса к GigaChat: {type(e).__name__}") from e

    # -- model list --

    def _fetch_models(self, token: str) -> Optional[List[str]]:
        """
        Return chat model ids in the order the API lists them.

        None means the list could not be fetched; an empty list means the API
        answered but offered no chat models. Embeds are filtered out: they are
        listed next to chat models but cannot translate.
        """
        try:
            response = requests.get(
                _MODELS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=_MODELS_TIMEOUT,
                verify=_CA_BUNDLE,
            )
        except requests.exceptions.Timeout as e:
            logger.warning(f"GigaChat model list timeout: {e}")
            return None
        except requests.exceptions.SSLError as e:
            logger.warning(f"GigaChat model list TLS error: {e}")
            return None
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"GigaChat model list connection error: {e}")
            return None
        except Exception as e:
            logger.warning(f"GigaChat model list error: {e}")
            return None

        if response.status_code != 200:
            logger.warning(f"GigaChat model list: HTTP {response.status_code}")
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning("GigaChat model list returned invalid JSON")
            return None

        models: List[str] = []
        data = payload.get("data") if isinstance(payload, dict) else None
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if "embedding" in model_id.lower():
                continue
            models.append(model_id)

        logger.info(f"GigaChat model list: {len(models)} chat models available")
        return models

    def _get_models(self, token: str, force: bool = False) -> Optional[List[str]]:
        """
        Return the cached model list, refreshing it at most once per TTL.

        The probe and translation share this cache: a translation must not
        re-read the list on every request, and a model rename is still picked
        up without a plugin update.
        """
        with self._model_lock:
            now = time.time()
            if not force and now < self._models_refresh_at:
                return self._models

            models = self._fetch_models(token)
            if models is None:
                self._models_refresh_at = now + _MODELS_RETRY_AFTER_FAILURE
                logger.warning("GigaChat model list unavailable, keeping previous list")
                return self._models
            self._models_refresh_at = now + _MODELS_TTL
            self._models = models
            # A fresh list gives skipped models another chance (their plan
            # may have changed); one wasted request per TTL at most.
            self._rejected_models.clear()
            return models

    def _pick_model(
        self, token: str, choose_model: Optional[ModelChooser], force: bool = False
    ) -> str:
        """The method's own choice from the shared list, else the default model."""
        if choose_model is not None:
            models = self._get_models(token, force=force)
            with self._model_lock:
                usable = [name for name in models or [] if name not in self._rejected_models]
            if usable:
                chosen = choose_model(usable)
                if chosen:
                    return chosen
            # Nothing to choose from: the default path below keeps the
            # fallback model and its logging in one place.
            return self._ensure_model(token)
        return self._ensure_model(token, force=force)

    def _ensure_model(self, token: str, force: bool = False) -> str:
        """Return the first available chat model."""
        models = self._get_models(token, force=force)
        with self._model_lock:
            if models:
                if models[0] != self._model:
                    logger.info(f"GigaChat model selected: {models[0]}")
                self._model = models[0]
                return self._model

            # The list is unavailable: try the previous choice or the last
            # known model name rather than failing before the chat request.
            if not self._model:
                self._model = _FALLBACK_MODEL
            logger.warning(
                f"GigaChat model list unavailable, using model {self._model}"
            )
            return self._model

    # -- OAuth token --

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._access_token = ""
            self._token_expires_at = 0.0

    def _ensure_token(self) -> str:
        """Return a valid access token, requesting a new one when needed."""
        with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN:
                return self._access_token

            token, expires_at = self._fetch_token()
            self._access_token = token
            self._token_expires_at = expires_at
            logger.info("GigaChat access token refreshed")
            return token

    def _fetch_token(self) -> Tuple[str, float]:
        response = self._post(
            _OAUTH_URL,
            headers={
                # The Authorization Key is already Base64 of Client ID and
                # Client Secret; GigaChat expects it in the Basic header.
                "Authorization": f"Basic {self._api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                # Required header, a fresh UUID for every request.
                "RqUID": str(uuid.uuid4()),
            },
            data={"scope": _OAUTH_SCOPE},
            timeout=_OAUTH_TIMEOUT,
        )

        if response.status_code != 200:
            self._handle_oauth_error(response)

        try:
            payload = response.json()
        except ValueError as e:
            logger.error("GigaChat OAuth returned invalid JSON")
            raise NetworkError("GigaChat вернул некорректный ответ при авторизации") from e

        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            logger.error(f"GigaChat OAuth response has no access_token: {str(payload)[:300]}")
            raise NetworkError("GigaChat вернул некорректный ответ при авторизации")

        now = time.time()
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, (int, float)):
            # The API has returned both seconds and milliseconds over time.
            if expires_at > 1e12:
                expires_at /= 1000.0
            expires_at = min(float(expires_at), now + _TOKEN_TTL)
        else:
            expires_at = now + _TOKEN_TTL
        return token, expires_at

    def _handle_oauth_error(self, response) -> None:
        logger.error(f"GigaChat OAuth error: {response.status_code}")
        if response.status_code in (400, 401, 403):
            raise ApiKeyError("Неверный Authorization Key GigaChat")
        if response.status_code == 429:
            raise RateLimitError("Превышен лимит запросов GigaChat. Попробуйте позже")
        if response.status_code >= 500:
            raise NetworkError(
                f"Сервис GigaChat временно недоступен (код {response.status_code})"
            )
        raise NetworkError(f"Ошибка авторизации GigaChat (код {response.status_code})")

    # -- errors --

    def _handle_chat_error(self, response, model: str) -> None:
        body = (response.text or "")[:500]
        logger.error(f"GigaChat API error: {response.status_code}")
        if body:
            logger.error(f"Response: {body}")

        if response.status_code == 401:
            raise ApiKeyError("Неверный Authorization Key GigaChat")
        if response.status_code == 403:
            raise ApiKeyError("Доступ к GigaChat запрещён. Проверьте ключ и тариф")
        if response.status_code == 402:
            raise TokenQuotaError(
                "Закончился бесплатный лимит токенов GigaChat. Проверьте лимит в личном кабинете",
                model,
            )
        if response.status_code == 429:
            raise RateLimitError("Превышен лимит запросов GigaChat. Попробуйте позже")
        if response.status_code >= 500:
            raise NetworkError(
                f"Сервис GigaChat временно недоступен (код {response.status_code})"
            )
        if response.status_code == 404:
            raise NetworkError("Модель GigaChat не найдена. Повторите позже или обновите плагин")
        if response.status_code == 400:
            reason = _error_message(response)
            if reason:
                raise NetworkError(f"GigaChat отклонил запрос: {reason}")
            raise NetworkError("GigaChat отклонил запрос (код 400)")
        raise NetworkError(f"Ошибка GigaChat (код {response.status_code})")

    # -- reachability --

    def probe(self) -> Tuple[bool, str]:
        """
        Check the key and the model list (blocking; run it in a worker thread).

        Returns (ok, reason) in the format of TranslationProvider.test_network.
        """
        if not self._api_key:
            return False, "Требуется Authorization Key GigaChat"
        try:
            _check_bundle_fresh()
            token = self._ensure_token()
        except ApiKeyError:
            return False, "Неверный Authorization Key"
        except RateLimitError:
            return False, "Превышен лимит запросов GigaChat"
        except NetworkError as e:
            return False, str(e) or "Сеть недоступна"

        models = self._get_models(token)
        if models is None:
            return False, "Не удалось получить список моделей GigaChat"
        if not models:
            return False, "Нет доступных моделей GigaChat"

        # The probe and the first translation must use the same model.
        with self._model_lock:
            if models[0] != self._model:
                logger.info(f"GigaChat model selected: {models[0]}")
            self._model = models[0]
        return True, ""
