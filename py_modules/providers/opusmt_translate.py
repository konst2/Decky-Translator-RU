# providers/opusmt_translate.py
# CTranslate2 + Opus-MT en→ru (Marian) lightweight translation provider
#
# A narrow English→Russian model (~82 MB) as an alternative to the NLLB
# all-pairs model (1.4 GB). Reuses the shared CTranslate2 text pipeline; the
# worker picks Marian tokenization (source.spm/target.spm) from the model
# directory and translates without NLLB language tags.

import logging

from .base import ProviderType
from .ct2_translate import CT2TranslateProvider
from .opusmt_downloader import OpusMtDownloader

logger = logging.getLogger(__name__)


class OpusMtTranslateProvider(CT2TranslateProvider):
    """
    Offline English→Russian translation using CTranslate2 + Opus-MT (Marian).
    One narrow model instead of the NLLB all-pairs model: much smaller
    download and faster startup, but only the en→ru pair.
    """

    DISPLAY_NAME = "Offline (Opus-MT en→ru)"
    PROVIDER_TYPE = ProviderType.OPUSMT
    MODEL_LABEL = "Opus-MT en→ru"
    SINGLE_PAIR = ("en", "ru")
    # A 77M Marian model may drop the first sentence of a multi-sentence
    # input ("Your health is low. Find a medic...") - exactly how visual
    # novel text boxes arrive, so split on every sentence boundary.
    SENTENCE_SPLIT_CHAR_LIMIT = 0

    def __init__(self, model_manager: OpusMtDownloader, plugin_dir: str = ""):
        super().__init__(model_manager, plugin_dir)
