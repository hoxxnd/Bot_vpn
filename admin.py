import aiosqlite
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.enums import ParseMode
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

router = Router()

_DB_PATH: str | None = None
_ADMIN_IDS: set[int] = set()

PAGE_SIZE = 10
BONUS_DAYS_FOR_REFERRER = 14

TARIFFS = {
    "outline": {"title": "OutLine", "price": 70},
    "v2ray": {"title": "v2raytun", "price": 70},
    "bundle": {"title": "OutLine/V2RayTun + AmneziaVPN", "price": 140},
}


def setup_admin(db_path: str, admin_ids: set[int]) -> None:
    global _DB_PATH, _ADMIN_IDS
    _DB_PATH = db_path
    _ADMIN_IDS = admin_ids


def is_admin(user_id: int) -> bool:
    return user_id in _ADMIN_IDS


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Список пользователей", callback_data="admin_users:0")],
    ])


def users_list_kb(offset: int, total: int) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_users:{offset + PAGE_SIZE}"))
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="🏠 Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def user_manage_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+1 мес", callback_data=f"admin_add:{user_id}:1"),
            InlineKeyboardButton(text="+2 мес", callback_data=f"admin_add:{user_id}:2"),
            InlineKeyboardButton(text="+3 мес", callback_data=f"admin_add:{user_id}:3"),
        ],
        [
            InlineKeyboardButton(text="+6 мес", callback_data=f"admin_add:{user_id}:6"),
            InlineKeyboardButton(text="+12 мес", callback_data=f"admin_add:{user_id}:12"),
        ],
        [
            InlineKeyboardButton(text="🧾 Изменить тариф", callback_data=f"admin_tariff:{user_id}"),
        ],
        [
            InlineKeyboardButton(text="➕ Ключ OutLine", callback_data=f"admin_key:{user_id}:outline"),
            InlineKeyboardButton(text="➕ Ключ v2raytun", callback_data=f"admin_key:{user_id}:v2ray"),
        ],
        [
            InlineKeyboardButton(text="➕ Ключ AmneziaVPN", callback_data=f"admin_key:{user_id}:amnezia"),
        ],
        [
            InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_users:0"),
            InlineKeyboardButton(text="🏠 Панель", callback_data="admin_panel"),
        ]
    ])


def tariff_select_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="OutLine — 70 ₽/мес", callback_data=f"admin_set_tariff:{user_id}:outline")],
        [InlineKeyboardButton(text="v2raytun — 70 ₽/мес", callback_data=f"admin_set_tariff:{user_id}:v2ray")],
        [InlineKeyboardButton(text="OutLine/v2raytun + AmneziaVPN — 140 ₽/мес", callback_data=f"admin_set_tariff:{user_id}:bundle")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_user:{user_id}")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_key_cancel")]
    ])


def _parse_iso(dt_iso: str | None):
    if not dt_iso:
        return None
    try:
        return datetime.fromisoformat(dt_iso)
    except Exception:
        return None


def _fmt(dt_iso: str | None) -> str:
    dt = _parse_iso(dt_iso)
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")


def _tariff_title(code: str | None) -> str:
    if not code:
        return "—"
    return TARIFFS.get(code, {}).get("title", "—")


class AdminKeyStates(StatesGroup):
    waiting_key = State()


async def _stats():
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    now = datetime.now(timezone.utc)

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = int((await cur.fetchone())[0])

        cur = await db.execute("SELECT expires_at FROM subscriptions")
        rows = await cur.fetchall()
        active = 0
        for (expires_at,) in rows:
            dt = _parse_iso(expires_at)
            if dt and dt > now:
                active += 1

    return total, active


async def _list_users(offset: int, limit: int):
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = int((await cur.fetchone())[0])

        cur = await db.execute("""
            SELECT u.telegram_id, u.first_name, u.last_name, u.username, s.expires_at
            FROM users u
            LEFT JOIN subscriptions s ON s.telegram_id = u.telegram_id
            ORDER BY u.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        users = await cur.fetchall()

    return total, users


async def _get_user(user_id: int):
    """
    Возвращает:
    uid, first, last, username, created_at, first_paid, first_paid_at,
    purchased_at, period_days, expires_at, tariff
    """
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("""
            SELECT u.telegram_id, u.first_name, u.last_name, u.username,
                   u.created_at, u.first_paid, u.first_paid_at,
                   s.purchased_at, s.period_days, s.expires_at, s.tariff
            FROM users u
            LEFT JOIN subscriptions s ON s.telegram_id = u.telegram_id
            WHERE u.telegram_id=?
        """, (user_id,))
        row = await cur.fetchone()
    return row


async def _get_keys(user_id: int):
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("""
            SELECT outline_key, v2ray_key, amnezia_key FROM user_keys WHERE user_id=?
        """, (user_id,))
        row = await cur.fetchone()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


def _key_title(key_type: str) -> str:
    return {
        "outline": "OutLine",
        "v2ray": "v2raytun",
        "amnezia": "AmneziaVPN",
    }[key_type]


async def _set_key(user_id: int, key_type: str, key_value: str, admin_id: int):
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    now = datetime.now(timezone.utc).isoformat()

    col = {
        "outline": "outline_key",
        "v2ray": "v2ray_key",
        "amnezia": "amnezia_key",
    }[key_type]

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("""
            INSERT INTO user_keys(user_id, outline_key, v2ray_key, amnezia_key, updated_at, updated_by)
            VALUES (?, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(user_id) DO NOTHING
        """, (user_id, now, admin_id))

        await db.execute(f"""
            UPDATE user_keys
            SET {col} = ?, updated_at = ?, updated_by = ?
            WHERE user_id = ?
        """, (key_value, now, admin_id, user_id))

        await db.commit()


async def _set_subscription_tariff(user_id: int, tariff_code: str):
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    async with aiosqlite.connect(_DB_PATH) as db:
        # гарантируем строку subscriptions
        await db.execute("""
            INSERT INTO subscriptions(telegram_id, purchased_at, period_days, expires_at, tariff, warn_2d_sent, expired_sent, keys_deleted)
            VALUES (?, NULL, NULL, NULL, ?, 0, 0, 0)
            ON CONFLICT(telegram_id) DO NOTHING
        """, (user_id, tariff_code))

        await db.execute("UPDATE subscriptions SET tariff=? WHERE telegram_id=?", (tariff_code, user_id))
        await db.commit()


async def _add_days(user_id: int, days: int):
    """
    Добавляет дни к expires_at:
    - если подписки нет -> создаёт
    - если активна -> продлевает от expires_at
    - если истекла -> продлевает от now
    Также сбрасывает warn/expired/keys_deleted, если подписка стала активной.
    """
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    now = datetime.now(timezone.utc)

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT period_days, expires_at, tariff FROM subscriptions WHERE telegram_id=?", (user_id,))
        row = await cur.fetchone()

        if not row:
            purchased_at = now
            period_days = days
            expires_at = now + timedelta(days=days)
            await db.execute("""
                INSERT INTO subscriptions(telegram_id, purchased_at, period_days, expires_at, tariff, warn_2d_sent, expired_sent, keys_deleted)
                VALUES (?, ?, ?, ?, NULL, 0, 0, 0)
            """, (user_id, purchased_at.isoformat(), period_days, expires_at.isoformat()))
            await db.commit()
            return

        period_days, expires_at, tariff = row
        period_days = int(period_days or 0)

        exp_dt = _parse_iso(expires_at)
        base = exp_dt if (exp_dt and exp_dt > now) else now

        new_expires = base + timedelta(days=days)
        new_period = period_days + days

        await db.execute("""
            UPDATE subscriptions
            SET period_days=?,
                expires_at=?,
                warn_2d_sent=0,
                expired_sent=0,
                keys_deleted=0
            WHERE telegram_id=?
        """, (new_period, new_expires.isoformat(), user_id))

        # если purchased_at пустой — заполним
        cur = await db.execute("SELECT purchased_at FROM subscriptions WHERE telegram_id=?", (user_id,))
        p = await cur.fetchone()
        if p and not p[0]:
            await db.execute("UPDATE subscriptions SET purchased_at=? WHERE telegram_id=?", (now.isoformat(), user_id))

        await db.commit()


async def _add_months(user_id: int, months: int) -> int:
    days = months * 30
    await _add_days(user_id, days)
    return days


async def _apply_latest_pending_payment_tariff(user_id: int) -> str | None:
    """
    Берём последнюю pending-оплату пользователя (если таблица payments есть),
    ставим её tariff в subscriptions, помечаем approved.
    """
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    try:
        async with aiosqlite.connect(_DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, tariff FROM payments WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None

            payment_id, tariff_code = row
            if tariff_code:
                await _set_subscription_tariff(user_id, tariff_code)

            await db.execute("UPDATE payments SET status='approved' WHERE id=?", (payment_id,))
            await db.commit()

            return tariff_code
    except Exception:
        # если таблицы payments нет или другая ошибка — просто пропускаем
        return None


async def _award_referrer_bonus_if_first_paid(user_id: int) -> int | None:
    if _DB_PATH is None:
        raise RuntimeError("admin not setup")

    now_iso = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            "SELECT referrer_id, ref_bonus_awarded, first_paid FROM users WHERE telegram_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None

        referrer_id, ref_bonus_awarded, first_paid = row
        ref_bonus_awarded = int(ref_bonus_awarded or 0)
        first_paid = int(first_paid or 0)

        if first_paid == 1:
            return None

        await db.execute(
            "UPDATE users SET first_paid=1, first_paid_at=? WHERE telegram_id=?",
            (now_iso, user_id)
        )
        await db.commit()

        if referrer_id is None or ref_bonus_awarded == 1:
            return None

    await _add_days(int(referrer_id), BONUS_DAYS_FOR_REFERRER)

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("UPDATE users SET ref_bonus_awarded=1 WHERE telegram_id=?", (user_id,))
        await db.commit()

    return int(referrer_id)


# ---------------- Handlers ----------------

@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    total, active = await _stats()
    text = (
        "🛠 <b>Панель управления</b>\n\n"
        f"👥 Пользователей всего: <b>{total}</b>\n"
        f"✅ Активных (подписка действует): <b>{active}</b>\n"
    )
    await callback.message.answer(text, reply_markup=admin_panel_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_users:"))
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        offset = int(callback.data.split(":", 1)[1])
    except Exception:
        offset = 0

    total, users = await _list_users(offset, PAGE_SIZE)

    lines = [f"👥 <b>Пользователи</b> (показано {len(users)} из {total})\n"]
    kb_rows = []

    for uid, first, last, username, expires_at in users:
        full_name = (f"{first or ''} {last or ''}").strip() or "Без имени"
        uname = f"@{username}" if username else "—"
        exp = _fmt(expires_at)

        lines.append(f"• <b>{full_name}</b> ({uname}) — до: <b>{exp}</b>")
        kb_rows.append([InlineKeyboardButton(text=f"Управлять: {full_name}", callback_data=f"admin_user:{uid}")])

    nav = users_list_kb(offset, total)
    keyboard = InlineKeyboardMarkup(inline_keyboard=kb_rows + nav.inline_keyboard)

    await callback.message.answer("\n".join(lines), reply_markup=keyboard, parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user:"))
async def admin_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        user_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    row = await _get_user(user_id)
    if not row:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    uid, first, last, username, created_at, first_paid, first_paid_at, purchased_at, period_days, expires_at, tariff = row
    full_name = (f"{first or ''} {last or ''}").strip() or "Без имени"
    uname = f"@{username}" if username else "—"

    outline_key, v2ray_key, amnezia_key = await _get_keys(uid)

    text = (
        "👤 <b>Управление пользователем</b>\n\n"
        f"• Имя: <b>{full_name}</b>\n"
        f"• Username: <b>{uname}</b>\n"
        f"• ID: <code>{uid}</code>\n"
        f"• Регистрация: <b>{_fmt(created_at)}</b>\n"
        f"• Первая оплата: <b>{_fmt(first_paid_at) if int(first_paid or 0) == 1 else '—'}</b>\n\n"
        "🔐 <b>Подписка</b>\n"
        f"• Тариф: <b>{_tariff_title(tariff)}</b>\n"
        f"• Оформление: <b>{_fmt(purchased_at)}</b>\n"
        f"• Период (дней): <b>{period_days if period_days else '—'}</b>\n"
        f"• Действует до: <b>{_fmt(expires_at)}</b>\n\n"
        "🔑 <b>Ключи</b>\n"
        f"• OutLine: <b>{'✅ выдан' if outline_key else '—'}</b>\n"
        f"• v2raytun: <b>{'✅ выдан' if v2ray_key else '—'}</b>\n"
        f"• AmneziaVPN: <b>{'✅ выдан' if amnezia_key else '—'}</b>\n"
    )

    await callback.message.answer(text, reply_markup=user_manage_kb(uid), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_tariff:"))
async def admin_tariff(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        user_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    row = await _get_user(user_id)
    if not row:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    uid, first, last, *_rest = row
    full_name = (f"{first or ''} {last or ''}").strip() or "Без имени"

    await callback.message.answer(
        f"🧾 Выберите тариф для пользователя <b>{full_name}</b> (ID: <code>{uid}</code>):",
        reply_markup=tariff_select_kb(uid),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_set_tariff:"))
async def admin_set_tariff(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        _, user_id_str, tariff_code = callback.data.split(":")
        user_id = int(user_id_str)
        if tariff_code not in TARIFFS:
            raise ValueError()
    except Exception:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    await _set_subscription_tariff(user_id, tariff_code)

    # уведомим пользователя (полезно)
    try:
        await callback.bot.send_message(
            user_id,
            f"🧾 Администратор установил вам тариф: <b>{TARIFFS[tariff_code]['title']}</b>.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    await callback.message.answer(
        f"✅ Тариф обновлён: <b>{TARIFFS[tariff_code]['title']}</b>",
        reply_markup=user_manage_kb(user_id),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_add:"))
async def admin_add(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        _, user_id_str, months_str = callback.data.split(":")
        user_id = int(user_id_str)
        months = int(months_str)
        if months not in (1, 2, 3, 6, 12):
            raise ValueError()
    except Exception:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    # начисляем период
    added_days = await _add_months(user_id, months)

    # если пользователь оплатил через pay.py — подтянем выбранный тариф из pending оплаты
    await _apply_latest_pending_payment_tariff(user_id)

    # реферальный бонус (только при первой оплате)
    referrer_id = await _award_referrer_bonus_if_first_paid(user_id)
    if referrer_id:
        row = await _get_user(user_id)
        if row:
            uid, first, last, *_ = row
            full_name = (f"{first or ''} {last or ''}").strip() or "Без имени"
            mention = f'<a href="tg://user?id={uid}">{full_name}</a>'
            try:
                await callback.bot.send_message(
                    referrer_id,
                    f"💳 Ваш реферал {mention} оплатил первый месяц.\n"
                    f"🎁 Вам начислено <b>{BONUS_DAYS_FOR_REFERRER} дней</b> бесплатной подписки.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    # уведомление пользователю: оплата успешна + начислено дней
    row = await _get_user(user_id)
    if row:
        uid, first, last, username, created_at, first_paid, first_paid_at, purchased_at, period_days, expires_at, tariff = row
        try:
            await callback.bot.send_message(
                user_id,
                "✅ <b>Оплата успешно подтверждена</b>\n\n"
                f"Вам начислено: <b>+{added_days} дней</b>\n"
                f"Тариф: <b>{_tariff_title(tariff)}</b>\n"
                f"Подписка действует до: <b>{_fmt(expires_at)}</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    await callback.message.answer("✅ Подписка обновлена.", reply_markup=user_manage_kb(user_id), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_key:"))
async def admin_key_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        _, user_id_str, key_type = callback.data.split(":")
        user_id = int(user_id_str)
        if key_type not in ("outline", "v2ray", "amnezia"):
            raise ValueError()
    except Exception:
        await callback.answer("Некорректная команда", show_alert=True)
        return

    row = await _get_user(user_id)
    if not row:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    uid, first, last, *_ = row
    full_name = (f"{first or ''} {last or ''}").strip() or "Без имени"
    key_name = _key_title(key_type)

    await state.set_state(AdminKeyStates.waiting_key)
    await state.update_data(target_user_id=user_id, key_type=key_type)

    await callback.message.answer(
        f"✍️ Введите ключ <b>{key_name}</b> для пользователя <b>{full_name}</b> (ID: <code>{uid}</code>)\n\n"
        "Отправьте ключ одним сообщением.",
        reply_markup=cancel_kb(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data == "admin_key_cancel")
async def admin_key_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Ок, отменено.")
    await callback.answer()


@router.message(AdminKeyStates.waiting_key)
async def admin_key_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        await state.clear()
        return

    data = await state.get_data()
    user_id = int(data.get("target_user_id", 0))
    key_type = data.get("key_type")

    key_value = (message.text or "").strip()
    if not key_value or len(key_value) < 5:
        await message.answer("Ключ выглядит пустым/слишком коротким. Отправьте ключ текстом одним сообщением.")
        return

    await _set_key(user_id, key_type, key_value, message.from_user.id)
    await state.clear()

    key_name = _key_title(key_type)

    # уведомляем пользователя
    try:
        await message.bot.send_message(
            user_id,
            f"✅ Администратор добавил вам ключ <b>{key_name}</b>.\n"
            f"Откройте «Личный кабинет» — ключ появится там.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    await message.answer(f"✅ Ключ <b>{key_name}</b> сохранён и пользователь уведомлён.", parse_mode=ParseMode.HTML)
