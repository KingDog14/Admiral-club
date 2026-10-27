"""Хроматическая дизайн-система inline-интерфейса NOVA ARENA (10 тонов + 4 поверхности).

Telegram Bot API жёстко ограничивает заливку фона ``InlineKeyboardButton.style``
тремя именованными цветами (``success``, ``primary``, ``danger``) плюс нейтральной
полупрозрачной поверхностью (``style=None``). Если красить почти каждую кнопку
в сплошной зелёный, синий и красный, палитра кажется одновременно скудной и
кричащей («гирлянда»).

Чтобы сделать палитру шире, но сохранить строгую визуальную иерархию, модуль
делит цвет на два согласованных уровня:

1. **Правило 60–30–10 для поверхностей кнопок (:class:`Surface`):**

   * **60–75 % — матовое стекло (:attr:`Surface.GLASS`, ``style=None``):** сетки
     разделов, выбор зала и пакета времени, список FAQ, неактивные вкладки,
     пагинация и спокойный подвал «◀ Назад / 🏠 В меню». На тёмном стекле цветные
     акценты внутри кнопок читаются чисто и не спорят с заливкой фона.
   * **10–20 % — сапфировый акцент (:attr:`Surface.ACCENT`, ``style="primary"``):**
     активная вкладка в переключателе разделов, симметричная пара ключевых
     переходов или кнопка копирования реквизитов.
   * **≤ 10 % — главный CTA (:attr:`Surface.HERO`, ``style="success"``):** строго
     не более одной главной целевой кнопки на экран («🎮 Забронировать место»,
     «⚡️ Сейчас», «📸 Оплатил — отправить чек», «✅ Я согласен»).
   * **≤ 5 % — стоп-действие (:attr:`Surface.DANGER`, ``style="danger"``):**
     исключительно отмена, отклонение, удаление и отзыв согласия 152-ФЗ — без
     ложной красноты на залах («S VIP») или справочных разделах («152-ФЗ»).

2. **10-тоновая семантическая палитра (:class:`Tone` и :data:`TONES`):**

   Единые 10 оттенков (Mint, Amber, Tangerine, Cryo, Sapphire, Amethyst, Indigo,
   Jade, Ruby, Slate) используются синхронно:

   * в цветовых маркерах кнопок на матовом стекле;
   * в векторных ``100×100`` RGBA-иконках ``bot/assets/button_icons/*.png`` для
     поля ``InlineKeyboardButton.icon_custom_emoji_id`` (24-битный RGB прямо
     внутри inline-кнопки при поддержке со стороны аккаунта владельца бота, с
     прозрачным откатом на обычные глифы);
   * в акцентной подсветке графических HD-карточек ``1280×720`` (:mod:`bot.ui.cards`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

BUTTON_ICONS_DIR: Final[Path] = (
    Path(__file__).resolve().parent / "assets" / "button_icons"
)


# --------------------------------------------------------------------------- #
# 1. Четыре нативные поверхности кнопок Bot API (правило 60–30–10)
# --------------------------------------------------------------------------- #
class Surface:
    """Роль поверхности inline-кнопки (маппинг на ``InlineKeyboardButton.style``)."""

    #: Спокойное полупрозрачное стекло (основная поверхность сеток и списков).
    GLASS: Final[None] = None
    #: Главный целевой CTA экрана (не более 1 на обычный экран).
    HERO: Final[str] = "success"
    #: Выделенный фокус: активная вкладка, ключевая пара или копирование.
    ACCENT: Final[str] = "primary"
    #: Строго деструктивное / отменяющее действие.
    DANGER: Final[str] = "danger"


VALID_STYLES: Final[frozenset[str]] = frozenset(
    {Surface.HERO, Surface.ACCENT, Surface.DANGER}
)


# --------------------------------------------------------------------------- #
# 2. Десять семантических хроматических тонов бренда NOVA ARENA
# --------------------------------------------------------------------------- #
class Tone:
    """Идентификаторы 10 хроматических тонов интерфейса."""

    MINT: Final[str] = "mint"  # #24F0A8 — главный CTA, Standart, «Сейчас»
    AMBER: Final[str] = "amber"  # #FFB224 — VIP, дневной пакет, цены и предоплата
    TANGERINE: Final[str] = "tangerine"  # #FF6224 — S VIP, утренний пакет, FACEIT
    CRYO: Final[str] = "cryo"  # #30D2EE — Double, ПК и железо, контакты
    SAPPHIRE: Final[str] = (
        "sapphire"  # #3B82F6 — активная вкладка, реквизиты, мои брони
    )
    AMETHYST: Final[str] = "amethyst"  # #A855F7 — ночной пакет, ночной FAQ, команда
    INDIGO: Final[str] = "indigo"  # #6366F1 — правила клуба, защита ПДн 152-ФЗ
    JADE: Final[str] = "jade"  # #14B8A6 — оплата на месте, гибкое время
    RUBY: Final[str] = "ruby"  # #FF485C — отмена, отказ, антифрод-памятка
    SLATE: Final[str] = "slate"  # #94A3B8 — спокойная навигация («Назад», «В меню»)


@dataclass(frozen=True, slots=True)
class ToneSpec:
    """Полное описание одного тона дизайн-системы."""

    key: str
    title: str
    hex_color: str
    rgba: tuple[int, int, int, int]
    glyph: str
    sticker_emoji: str
    icon_filename: str
    role: str

    @property
    def icon_path(self) -> Path:
        return BUTTON_ICONS_DIR / self.icon_filename


TONES: Final[dict[str, ToneSpec]] = {
    Tone.MINT: ToneSpec(
        key=Tone.MINT,
        title="Supernova Mint",
        hex_color="#24F0A8",
        rgba=(36, 240, 168, 255),
        glyph="🟢",
        sticker_emoji="🟢",
        icon_filename="mint.png",
        role="Главный CTA, зал Standart, мгновенный старт",
    ),
    Tone.AMBER: ToneSpec(
        key=Tone.AMBER,
        title="Cyber Amber",
        hex_color="#FFB224",
        rgba=(255, 178, 36, 255),
        glyph="🟡",
        sticker_emoji="🟡",
        icon_filename="amber.png",
        role="Зал VIP, дневной пакет, тарифы и предоплата",
    ),
    Tone.TANGERINE: ToneSpec(
        key=Tone.TANGERINE,
        title="Solar Tangerine",
        hex_color="#FF6224",
        rgba=(255, 98, 36, 255),
        glyph="🟠",
        sticker_emoji="🟠",
        icon_filename="tangerine.png",
        role="Буткемп S VIP, утренний пакет, FACEIT",
    ),
    Tone.CRYO: ToneSpec(
        key=Tone.CRYO,
        title="Cryo Cyan",
        hex_color="#30D2EE",
        rgba=(48, 210, 238, 255),
        glyph="💎",
        sticker_emoji="💎",
        icon_filename="cryo.png",
        role="Приватная парная Double, железо и контакты",
    ),
    Tone.SAPPHIRE: ToneSpec(
        key=Tone.SAPPHIRE,
        title="Electric Sapphire",
        hex_color="#3B82F6",
        rgba=(59, 130, 246, 255),
        glyph="🔷",
        sticker_emoji="🔷",
        icon_filename="sapphire.png",
        role="Активная вкладка, копирование реквизитов, мои брони",
    ),
    Tone.AMETHYST: ToneSpec(
        key=Tone.AMETHYST,
        title="Deep Amethyst",
        hex_color="#A855F7",
        rgba=(168, 85, 247, 255),
        glyph="🌙",
        sticker_emoji="🟣",
        icon_filename="amethyst.png",
        role="Ночной пакет 22:00–08:00, ночной FAQ, команда",
    ),
    Tone.INDIGO: ToneSpec(
        key=Tone.INDIGO,
        title="Astral Indigo",
        hex_color="#6366F1",
        rgba=(99, 102, 241, 255),
        glyph="🛡",
        sticker_emoji="🛡",
        icon_filename="indigo.png",
        role="Правила клуба и защита персональных данных 152-ФЗ",
    ),
    Tone.JADE: ToneSpec(
        key=Tone.JADE,
        title="Warm Jade",
        hex_color="#14B8A6",
        rgba=(20, 184, 166, 255),
        glyph="⏱",
        sticker_emoji="💠",
        icon_filename="jade.png",
        role="Оплата на месте, гибкое время и ожидание",
    ),
    Tone.RUBY: ToneSpec(
        key=Tone.RUBY,
        title="Crimson Ruby",
        hex_color="#FF485C",
        rgba=(255, 72, 92, 255),
        glyph="❌",
        sticker_emoji="❗️",
        icon_filename="ruby.png",
        role="Отмена, отклонение, отзыв согласия и защита от мошенников",
    ),
    Tone.SLATE: ToneSpec(
        key=Tone.SLATE,
        title="Frost Slate",
        hex_color="#94A3B8",
        rgba=(148, 163, 184, 255),
        glyph="⬅️",
        sticker_emoji="▫️",
        icon_filename="slate.png",
        role="Спокойная навигация: Назад, В меню, Обновить, пагинация",
    ),
}

TONE_ORDER: Final[tuple[str, ...]] = (
    Tone.MINT,
    Tone.AMBER,
    Tone.TANGERINE,
    Tone.CRYO,
    Tone.SAPPHIRE,
    Tone.AMETHYST,
    Tone.INDIGO,
    Tone.JADE,
    Tone.RUBY,
    Tone.SLATE,
)

# Семантическая привязка предметных сущностей к 10-тоновой палитре
TARIFF_TONES: Final[dict[str, str]] = {
    "standart": Tone.MINT,
    "vip": Tone.AMBER,
    "svip": Tone.TANGERINE,
    "double": Tone.CRYO,
}

TIME_TONES: Final[dict[str, str]] = {
    "now": Tone.MINT,
    "morning": Tone.TANGERINE,
    "day": Tone.AMBER,
    "night": Tone.AMETHYST,
    "other": Tone.SAPPHIRE,
}

FAQ_TONES: Final[dict[str, str]] = {
    "night": Tone.AMETHYST,
    "packages": Tone.AMBER,
    "prepay": Tone.AMBER,
    "payment": Tone.SAPPHIRE,
    "pcs": Tone.CRYO,
    "faceit": Tone.TANGERINE,
    "late": Tone.JADE,
    "scam": Tone.RUBY,
}

RULES_TAB_TONES: Final[dict[str, str]] = {
    "main": Tone.INDIGO,
    "pay": Tone.AMBER,
    "club": Tone.CRYO,
}


def tone_rgba(tone_key: str) -> tuple[int, int, int, int]:
    """Вернуть RGBA-цвет тона (с безопасным откатом на Mint)."""
    spec = TONES.get(tone_key) or TONES[Tone.MINT]
    return spec.rgba


# --------------------------------------------------------------------------- #
# 3. Реестр 24-битных custom_emoji иконок для inline-кнопок + мягкий откат
# --------------------------------------------------------------------------- #
_CUSTOM_EMOJI_IDS: dict[str, str] = {}
_DISABLED_BOT_KEYS: set[int] = set()
_FALLBACK_TEXT_BY_EMOJI_ID: dict[str, str] = {}

_HAS_EMOJI_RE = re.compile(
    r"[\U0001f000-\U0001faff\u2190-\u21ff\u2300-\u23ff\u25a0-\u27bf\u2b00-\u2bff]"
)


def button_has_emoji(text: str) -> bool:
    """Проверить, что в подписи кнопки есть видимый эмодзи-значок."""
    return bool(_HAS_EMOJI_RE.search(text or ""))


def parse_custom_emoji_mapping(raw: str) -> dict[str, str]:
    """Разобрать строку вида ``mint=5368...,amber=5369...`` из ``.env``."""
    result: dict[str, str] = {}
    for chunk in re.split(r"[,;\s]+", (raw or "").strip()):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        tone = key.strip().lower()
        eid = value.strip()
        if tone in TONES and eid.isdigit():
            result[tone] = eid
    return result


def set_custom_emoji_ids(mapping: dict[str, str]) -> None:
    """Зарегистрировать ``custom_emoji_id`` для тонов палитры."""
    _CUSTOM_EMOJI_IDS.clear()
    for tone, eid in mapping.items():
        if tone in TONES and eid:
            _CUSTOM_EMOJI_IDS[tone] = str(eid)


def get_custom_emoji_ids() -> dict[str, str]:
    """Копия текущей таблицы ``tone -> custom_emoji_id``."""
    return dict(_CUSTOM_EMOJI_IDS)


def get_custom_emoji_id(tone: str | None) -> str | None:
    """Вернуть ``custom_emoji_id`` для тона, если пак активен."""
    if not tone:
        return None
    return _CUSTOM_EMOJI_IDS.get(tone)


def is_custom_emoji_enabled_for_bot(bot_key: int | None = None) -> bool:
    """Проверить, активны ли custom-emoji иконки кнопок для данного бота."""
    if not _CUSTOM_EMOJI_IDS:
        return False
    return not (bot_key is not None and bot_key in _DISABLED_BOT_KEYS)


def disable_custom_emoji_for_bot(bot_key: int) -> None:
    """Отключить ``icon_custom_emoji_id`` для бота без поддержки Premium/Fragment."""
    _DISABLED_BOT_KEYS.add(bot_key)


def reset_custom_emoji_state() -> None:
    """Сбросить состояние реестра (используется в тестах)."""
    _CUSTOM_EMOJI_IDS.clear()
    _DISABLED_BOT_KEYS.clear()
    _FALLBACK_TEXT_BY_EMOJI_ID.clear()


def apply_tone_icon(text: str, tone: str | None) -> tuple[str, str | None]:
    """Подготовить ``(text, icon_custom_emoji_id)`` для кнопки, ВСЕГДА сохраняя эмодзи в тексте.

    Стандартные эмодзи в ``text`` никогда не вырезаются: на клиентах и у ботов без
    Telegram Premium Telegram может молча проигнорировать ``icon_custom_emoji_id``
    без ошибки API, поэтому понятный эмодзи в самой строке кнопки обязан быть всегда.
    Если в ``text`` вдруг нет эмодзи, а тон задан — автоматически подставляется глиф тона.
    """
    label = (text or "").strip()
    if label and not button_has_emoji(label) and tone in TONES:
        label = f"{TONES[tone].glyph} {label}"
    eid = get_custom_emoji_id(tone)
    if not eid:
        return label, None
    _FALLBACK_TEXT_BY_EMOJI_ID[f"{eid}:{label}"] = label
    return label, eid


def strip_custom_emoji_markup(
    markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    """Убрать ``icon_custom_emoji_id`` из клавиатуры с восстановлением глифов."""
    if markup is None:
        return None
    changed = False
    new_rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            eid = getattr(button, "icon_custom_emoji_id", None)
            if eid:
                changed = True
                restored_text = _FALLBACK_TEXT_BY_EMOJI_ID.get(
                    f"{eid}:{button.text}", button.text
                )
                data = button.model_dump(exclude_none=True)
                data.pop("icon_custom_emoji_id", None)
                data["text"] = restored_text
                new_row.append(InlineKeyboardButton(**data))
            else:
                new_row.append(button)
        new_rows.append(new_row)
    if not changed:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


def prepare_markup_for_bot(
    markup: InlineKeyboardMarkup | None, bot_key: int
) -> InlineKeyboardMarkup | None:
    """Если для ``bot_key`` ранее выявлено отсутствие Premium — сразу очистить иконки."""
    if markup is None or bot_key not in _DISABLED_BOT_KEYS:
        return markup
    return strip_custom_emoji_markup(markup)


# --------------------------------------------------------------------------- #
# 4. Автоматический аудит гармонии («анти-гирлянда»)
# --------------------------------------------------------------------------- #
_DESTRUCTIVE_TOKENS: Final[tuple[str, ...]] = (
    "cancel",
    "revoke",
    ":no:",
    ":del:",
    ":rej:",
    ":can:",
    ":noshow:",
    "cli:cancel:",
    "adm_del",
    "admin_bk_rej",
    "admin_bk_can",
)


@dataclass(frozen=True, slots=True)
class HarmonyReport:
    """Результат проверки inline-клавиатуры на визуальную чистоту."""

    ok: bool
    total: int
    glass_count: int
    hero_count: int
    accent_count: int
    danger_count: int
    issues: tuple[str, ...]

    @property
    def glass_ratio(self) -> float:
        return self.glass_count / self.total if self.total else 1.0


def audit_keyboard_harmony(
    markup: InlineKeyboardMarkup,
    *,
    allow_batch_decisions: bool = False,
) -> HarmonyReport:
    """Проверить клавиатуру на соблюдение правил 60–30–10 и отсутствие «гирлянды»."""
    issues: list[str] = []
    total = 0
    glass = 0
    hero = 0
    accent = 0
    danger = 0

    for row_idx, row in enumerate(markup.inline_keyboard):
        row_styles: set[str] = set()
        for button in row:
            total += 1
            if not button_has_emoji(button.text):
                issues.append(f"Кнопка без эмодзи: {button.text!r}")
            style = getattr(button, "style", None)
            cb = getattr(button, "callback_data", None) or ""
            if not style:
                glass += 1
                continue
            row_styles.add(style)
            if style == Surface.HERO:
                hero += 1
            elif style == Surface.ACCENT:
                accent += 1
            elif style == Surface.DANGER:
                danger += 1
                if cb and not any(tok in cb for tok in _DESTRUCTIVE_TOKENS):
                    issues.append(
                        f"Ложный danger на недеструктивной кнопке {button.text!r} ({cb})"
                    )
            else:
                issues.append(f"Неизвестный style={style!r} на {button.text!r}")

        # Запрещаем смешивать разные яркие заливки в одной строке, кроме бинарных решений
        if (
            len(row_styles) > 1
            and row_styles != {Surface.HERO, Surface.DANGER}
            and row_styles != {Surface.DANGER, Surface.ACCENT}
        ):
            issues.append(
                f"Строка {row_idx} смешивает конкурирующие заливки: {sorted(row_styles)}"
            )

    if not allow_batch_decisions and hero > 1:
        issues.append(
            f"На экране {hero} кнопок success (допустим максимум 1 главный CTA)"
        )

    if not allow_batch_decisions and total >= 5 and glass / total < 0.45:
        issues.append(
            f"Перегруз цветными заливками («гирлянда»): стекло {glass}/{total} ({glass / total:.0%} < 45%)"
        )

    return HarmonyReport(
        ok=not issues,
        total=total,
        glass_count=glass,
        hero_count=hero,
        accent_count=accent,
        danger_count=danger,
        issues=tuple(issues),
    )


def format_palette_summary() -> str:
    """Текстовая сводка 10-тоновой палитры и правил поверхностей для CLI."""
    lines = [
        "=== INLINE PALETTE · NOVA ARENA (10 тонов + 4 поверхности Bot API) ===",
        "Поверхности (правило 60–30–10):",
        "  • GLASS   (style=None)      — 60–75 %: спокойное матовое стекло (сетки, списки, подвал)",
        "  • ACCENT  (style='primary') — 10–20 %: активная вкладка, ключевая пара, реквизиты",
        "  • HERO    (style='success') — ≤ 10 % : ровно 1 главный CTA на экран",
        "  • DANGER  (style='danger')  — ≤  5 % : строго отмена / отказ / удаление / отзыв 152-ФЗ",
        "",
        "10 семантических тонов (глифы + 100×100 RGBA custom_emoji + акценты HD-карточек):",
    ]
    for key in TONE_ORDER:
        spec = TONES[key]
        lines.append(
            f"  • {spec.glyph} {spec.key:<10} {spec.hex_color}  {spec.title:<18} — {spec.role}"
        )
    return "\n".join(lines)


__all__ = [
    "BUTTON_ICONS_DIR",
    "FAQ_TONES",
    "RULES_TAB_TONES",
    "TARIFF_TONES",
    "TIME_TONES",
    "TONES",
    "TONE_ORDER",
    "VALID_STYLES",
    "HarmonyReport",
    "Surface",
    "Tone",
    "ToneSpec",
    "apply_tone_icon",
    "audit_keyboard_harmony",
    "button_has_emoji",
    "disable_custom_emoji_for_bot",
    "format_palette_summary",
    "get_custom_emoji_id",
    "get_custom_emoji_ids",
    "is_custom_emoji_enabled_for_bot",
    "parse_custom_emoji_mapping",
    "prepare_markup_for_bot",
    "reset_custom_emoji_state",
    "set_custom_emoji_ids",
    "strip_custom_emoji_markup",
    "tone_rgba",
]
