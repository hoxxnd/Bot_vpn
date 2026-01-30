from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

import aiosqlite
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.enums import ParseMode

router = Router()

_DB_PATH: str | None = None
_BOT_USERNAME: str | None = None

# Настройки рефералки
BONUS_DAYS_FOR_REFERRER = 14      # рефереру после первой оплаты реферала
TRIAL_DAYS_FOR_INVITEE = 3        # приглашённому сразу при заходе по ссылке


# ---------- setup / schema ----------

def setup_referrals(db_path: str, bot_username: str) -> None:
    global _DB_PATH, _BOT_USERNAME
    _DB_PATH = db_path
    _BOT_USERNAME = bot_username


async def ensure_referrals_schema(db: aiosqlite.Connection) -> None:
    """
    Миграции для таблицы users.
    Вызывать из init_db() в main.py.
    """
    # кто пригласил
    try:
        await db.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
    except Exception:
        pass

    # за этого пользователя бонус рефереру уже выдавали (1/0)
    try:
        await db.execute("ALTER TABLE users ADD COLUMN ref_bonus_awarded INTEGER DEFAULT 0")
    except Exception:
        pass

    # был ли у пользователя первый платный период (1/0)
    try:
        await db.execute("ALTER TABLE users ADD COLUMN first_paid INTEGER DEFAULT 0")
    except Exception:
        pass

    # дата первой оплаты (когда админ впервые начислил месяцы)
    try:
        await db.execute("ALTER TABLE users ADD COLUMN first_paid_at TEXT")
    except Exception:
        pass


# ---------- helpers ----------

def parse_referrer_id_from_start(text: str | None, current_user_id: int) -> Optional[int]:
    """
    /start ref_12345 -> 12345
    """
    if not text:
        return None

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None

    payload = parts[1].strip()
    if not payload.startswith("ref_"):
        return None

    try:
        referrer_id = int(payload.replace("ref_", "").strip())
    except ValueError:
        return None

    # защита от самореферала
    if referrer_id == current_user_id:
        return None

    return referrer_id


def build_referral_link(user_id: int) -> str:
    if not _BOT_USERNAME:
        raise RuntimeError("Referrals not setup: bot username missing")
    return f"https://t.me/{_BOT_USERNAME}?start=ref_{user_id}"


def _parse_iso(dt_iso: str | None) -> datetime | None:
    if not dt_iso:
        return None
    try:
        return datetime.fromisoformat(dt_iso)
    except Exception:
        return None


def _fmt(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    dt = _parse_iso(dt_iso)
    if not dt:
        return dt_iso
    return dt.strftime("%d.%m.%Y %H:%M")


async def add_days_to_subscription(user_id: int, days: int) -> None:
    """
    Универсально добавляет days к подписке:
    - если подписки нет -> создаёт
    - если активна -> продлевает от expires_at
    - если просрочена -> продлевает от now
    """
    if _DB_PATH is None:
        raise RuntimeError("Referrals not setup: call setup_referrals() first")

    now = datetime.now(timezone.utc)

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT purchased_at, period_days, expires_at FROM subscriptions WHERE telegram_id=?",
            (user_id,),
        )
        row = await cur.fetchone()

        if not row:
            purchased_at = now
            period_days = days
            expires_at = now + timedelta(days=days)
            await db.execute(
                """
                INSERT INTO subscriptions(telegram_id, purchased_at, period_days, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, purchased_at.isoformat(), period_days, expires_at.isoformat()),
            )
            await db.commit()
            return

        purchased_at, period_days, expires_at = row
        period_days = int(period_days or 0)

        base = now
        exp_dt = _parse_iso(expires_at)
        if exp_dt and exp_dt > now:
            base = exp_dt

        new_expires = base + timedelta(days=days)
        new_period = period_days + days

        await db.execute(
            """
            UPDATE subscriptions
            SET period_days=?, expires_at=?
            WHERE telegram_id=?
            """,
            (new_period, new_expires.isoformat(), user_id),
        )
        await db.commit()


# ---------- core logic ----------

async def apply_referral_on_start(new_user_id: int, start_text: str | None) -> int | None:
    """
    Вызывать в /start (после upsert_user).

    Если пользователь впервые зашёл по ref-ссылке:
      - сохраняем referrer_id (только если ещё не был установлен)
      - выдаём приглашённому +3 дня trial
      - возвращаем referrer_id (чтобы main.py мог отправить уведомление рефереру)
    Иначе возвращаем None.

    ВАЖНО: +14 дней рефереру здесь НЕ выдаём (это делается в admin.py при первой оплате).
    """
    if _DB_PATH is None:
        raise RuntimeError("Referrals not setup: call setup_referrals() first")

    referrer_id = parse_referrer_id_from_start(start_text, new_user_id)
    if referrer_id is None:
        return None

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT referrer_id FROM users WHERE telegram_id=?", (new_user_id,))
        row = await cur.fetchone()
        if not row:
            return None

        current_referrer = row[0]
        if current_referrer is not None:
            return None

        await db.execute(
            "UPDATE users SET referrer_id=? WHERE telegram_id=? AND referrer_id IS NULL",
            (referrer_id, new_user_id),
        )
        await db.commit()

    # выдаём trial приглашённому
    await add_days_to_subscription(new_user_id, TRIAL_DAYS_FOR_INVITEE)

    return referrer_id


async def get_referrals_count(referrer_id: int) -> int:
    if _DB_PATH is None:
        raise RuntimeError("Referrals not setup: call setup_referrals() first")

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (referrer_id,))
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def get_referrals_list(referrer_id: int) -> List[Tuple[str | None, str | None, str | None, int]]:
    """
    Возвращает список:
      (first_name, last_name, first_paid_at, ref_bonus_awarded)
    """
    if _DB_PATH is None:
        raise RuntimeError("Referrals not setup: call setup_referrals() first")

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT first_name, last_name, first_paid_at, ref_bonus_awarded
            FROM users
            WHERE referrer_id=?
            ORDER BY created_at DESC
            """,
            (referrer_id,),
        )
        rows = await cur.fetchall()

    # приведение ref_bonus_awarded к int
    out: List[Tuple[str | None, str | None, str | None, int]] = []
    for first_name, last_name, first_paid_at, ref_bonus_awarded in rows:
        out.append((first_name, last_name, first_paid_at, int(ref_bonus_awarded or 0)))
    return out


# ---------- handlers ----------

@router.callback_query(F.data == "ref_link")
async def ref_link(callback: CallbackQuery):
    link = build_referral_link(callback.from_user.id)
    await callback.message.answer(
        "🔗 <b>Ваша реферальная ссылка</b>\n\n"
        f"🎁 Приглашённый получит: <b>+{TRIAL_DAYS_FOR_INVITEE} дня</b>\n"
        f"🏆 Вы получите: <b>+{BONUS_DAYS_FOR_REFERRER} дней</b>\n"
        "Важно! Бонус начисляется только после оплаты подписки вашего реферала.\n\n"
        f"{link}",
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data == "refs")
async def refs(callback: CallbackQuery):
    rows = await get_referrals_list(callback.from_user.id)

    if not rows:
        await callback.message.answer("👥 У вас пока нет рефералов.")
        await callback.answer()
        return

    paid_refs = sum(1 for r in rows if r[3] == 1)  # ref_bonus_awarded == 1
    total_bonus_days = paid_refs * BONUS_DAYS_FOR_REFERRER

    lines = [
        "👥 <b>Ваши рефералы</b>\n",
        f"🏆 <b>Суммарный бонус:</b> <b>{total_bonus_days}</b> дней",
        f"✅ <b>Оплатили первый раз:</b> <b>{paid_refs}</b>\n",
        "<b>Список:</b>"
    ]

    for first_name, last_name, first_paid_at, ref_bonus_awarded in rows:
        full_name = (f"{first_name or ''} {last_name or ''}").strip() or "Без имени"
        mark = "✅" if ref_bonus_awarded == 1 else "⏳"
        lines.append(f"• {mark} <b>{full_name}</b> — первая оплата: <b>{_fmt(first_paid_at)}</b>")

    await callback.message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    await callback.answer()
