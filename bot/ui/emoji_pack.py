"""Автосинхронизация 10-цветного custom_emoji-пака для inline-кнопок.

В Telegram Bot API поле ``InlineKeyboardButton.icon_custom_emoji_id`` позволяет
выводить настоящие 24-битные RGB-иконки слева от текста кнопки (в любых цветах
бренда: Mint, Amber, Tangerine, Cryo, Sapphire, Amethyst, Indigo, Jade, Ruby,
Slate), расширяя нативную палитру далеко за пределы трёх заливок ``style``.

Модуль умеет:

1. Принять готовые ``custom_emoji_id`` из ``.env`` (``INLINE_EMOJI_IDS``);
2. Или при старте бота (``INLINE_CUSTOM_ICONS=auto``) проверить / автоматически
   создать собственный набор ``custom_emoji`` ``nova_inline_v1_by_<bot_username>``
   из подготовленных ``100×100`` RGBA-иконок ``bot/assets/button_icons/*.png``;
3. Если у владельца бота нет подписки Telegram Premium (или бот ещё не общался
   с владельцем) — тихо оставить стандартные цветовые глифы без ошибок.
"""

from __future__ import annotations

import logging
import os
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputSticker

from bot.ui.palette import (
    TONE_ORDER,
    TONES,
    parse_custom_emoji_mapping,
    set_custom_emoji_ids,
)

logger = logging.getLogger(__name__)

PACK_PREFIX: str = "nova_inline_v1"


def _pack_short_name(bot_username: str) -> str:
    clean_user = re.sub(r"[^a-zA-Z0-9_]", "", bot_username).strip("_").lower()
    clean_user = re.sub(r"_+", "_", clean_user)
    return f"{PACK_PREFIX}_by_{clean_user}"[:64]


def _build_input_stickers() -> list[InputSticker]:
    stickers: list[InputSticker] = []
    for tone_key in TONE_ORDER:
        spec = TONES[tone_key]
        raw = spec.icon_path.read_bytes()
        stickers.append(
            InputSticker(
                sticker=BufferedInputFile(raw, filename=spec.icon_filename),
                format="static",
                emoji_list=[spec.sticker_emoji],
            )
        )
    return stickers


def _extract_mapping_from_set(sticker_set: object) -> dict[str, str]:
    stickers = getattr(sticker_set, "stickers", None) or []
    emoji_to_tone = {TONES[k].sticker_emoji: k for k in TONE_ORDER}
    mapping: dict[str, str] = {}

    for index, st in enumerate(stickers):
        eid = getattr(st, "custom_emoji_id", None)
        if not eid:
            continue
        st_emoji = getattr(st, "emoji", None)
        if st_emoji in emoji_to_tone and emoji_to_tone[st_emoji] not in mapping:
            mapping[emoji_to_tone[st_emoji]] = str(eid)
        elif index < len(TONE_ORDER):
            tone_key = TONE_ORDER[index]
            mapping.setdefault(tone_key, str(eid))
    return mapping


async def sync_inline_emoji_pack(
    bot: Bot,
    *,
    owner_id: int,
    club_name: str,
) -> dict[str, str]:
    """Подключить или автоматически создать 10-тоновый custom_emoji-пак кнопок."""
    mode = (os.getenv("INLINE_CUSTOM_ICONS") or "auto").strip().lower()
    if mode in ("0", "off", "false", "no", "none", "disabled"):
        logger.info("Кастомные RGB-иконки inline-кнопок отключены (INLINE_CUSTOM_ICONS=%s)", mode)
        return {}

    explicit_raw = (os.getenv("INLINE_EMOJI_IDS") or "").strip()
    if explicit_raw:
        explicit = parse_custom_emoji_mapping(explicit_raw)
        if explicit:
            set_custom_emoji_ids(explicit)
            logger.info(
                "Загружены custom_emoji_id для inline-палитры из .env (%s тонов)",
                len(explicit),
            )
            return explicit

    if mode != "auto":
        return {}

    try:
        me = await bot.get_me()
    except TelegramAPIError as exc:
        logger.debug("Не удалось запросить getMe для синхронизации палитры: %s", exc)
        return {}

    username = getattr(me, "username", None)
    if not username:
        return {}

    pack_name = _pack_short_name(username)
    sticker_set = None
    try:
        sticker_set = await bot.get_sticker_set(name=pack_name)
    except TelegramAPIError:
        sticker_set = None

    if sticker_set is None and owner_id > 0:
        try:
            created = await bot.create_new_sticker_set(
                user_id=owner_id,
                name=pack_name,
                title=f"{club_name[:40]} · Inline Palette",
                stickers=_build_input_stickers(),
                sticker_type="custom_emoji",
                needs_repainting=False,
            )
            if created:
                sticker_set = await bot.get_sticker_set(name=pack_name)
        except (TelegramAPIError, OSError) as exc:
            logger.debug(
                "Автосоздание custom_emoji-пака %s пропущено (используются стандартные глифы): %s",
                pack_name,
                exc,
            )
            return {}

    if sticker_set is None:
        return {}

    mapping = _extract_mapping_from_set(sticker_set)
    if mapping:
        set_custom_emoji_ids(mapping)
        logger.info(
            "Подключена 24-битная палитра иконок inline-кнопок %s (%s/%s тонов)",
            pack_name,
            len(mapping),
            len(TONE_ORDER),
        )
    return mapping


__all__ = ["PACK_PREFIX", "sync_inline_emoji_pack"]
