import os
import asyncio
from datetime import datetime, timezone, timedelta

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage

import admin
import pay
import referrals
import tariffs  # новый файл tariffs.py

load_dotenv()

DB_PATH = "bot.db"

# Рефералка
REFERRER_BONUS_DAYS = 14
INVITEE_TRIAL_DAYS = 3

# Отображение тарифа в кабинете
TARIFF_TITLES = {
    "outline": "OutLine",
    "v2ray": "v2raytun",
    "bundle": "OutLine/V2RayTun + AmneziaVPN",
}

# Уведомления по подписке
CHECK_INTERVAL_SECONDS = 15 * 60         # каждые 15 минут
WARN_BEFORE = timedelta(days=2)          # предупреждать за 2 дня
GRACE_AFTER_EXPIRE = timedelta(days=2)   # 2 дня после конца, потом удаляем ключи


# ---------------- Keyboards ----------------

def start_kb(is_admin_user: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Личный кабинет", callback_data="cabinet")]]
    if is_admin_user:
        rows.append([InlineKeyboardButton(text="Панель управления", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cabinet_actions_kb(outline_key: str | None, v2ray_key: str | None, amnezia_key: str | None) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="Оплатить", callback_data="pay")],
        [InlineKeyboardButton(text="Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton(text="Реферальная ссылка", callback_data="ref_link")],
        [InlineKeyboardButton(text="Рефералы", callback_data="refs")],
    ]

    # кнопки ключей показываем ТОЛЬКО если ключ существует
    if outline_key:
        kb.append([InlineKeyboardButton(text="🔑 Показать ключ OutLine", callback_data="show_outline_key")])
    if v2ray_key:
        kb.append([InlineKeyboardButton(text="🔑 Показать ключ v2raytun", callback_data="show_v2ray_key")])
    if amnezia_key:
        kb.append([InlineKeyboardButton(text="🔑 Показать ключ AmneziaVPN", callback_data="show_amnezia_key")])

    return InlineKeyboardMarkup(inline_keyboard=kb)


# ---------------- Helpers ----------------

def human_date(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_iso)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_iso


def parse_admin_ids(env_value: str | None) -> set[int]:
    if not env_value:
        return set()
    out = set()
    for part in env_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


def _parse_iso(dt_iso: str | None) -> datetime | None:
    if not dt_iso:
        return None
    try:
        return datetime.fromisoformat(dt_iso)
    except Exception:
        return None


# ---------------- DB ----------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                created_at TEXT,
                referrer_id INTEGER,
                ref_bonus_awarded INTEGER DEFAULT 0,
                first_paid INTEGER DEFAULT 0,
                first_paid_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                telegram_id INTEGER PRIMARY KEY,
                purchased_at TEXT,
                period_days INTEGER,
                expires_at TEXT,
                tariff TEXT,
                warn_2d_sent INTEGER DEFAULT 0,
                expired_sent INTEGER DEFAULT 0,
                keys_deleted INTEGER DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_keys (
                user_id INTEGER PRIMARY KEY,
                outline_key TEXT,
                v2ray_key TEXT,
                amnezia_key TEXT,
                updated_at TEXT,
                updated_by INTEGER
            )
        """)

        # --- migrations for older DBs ---
        migrations = [
            "ALTER TABLE users ADD COLUMN referrer_id INTEGER",
            "ALTER TABLE users ADD COLUMN ref_bonus_awarded INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN first_paid INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN first_paid_at TEXT",

            "ALTER TABLE subscriptions ADD COLUMN tariff TEXT",
            "ALTER TABLE subscriptions ADD COLUMN warn_2d_sent INTEGER DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN expired_sent INTEGER DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN keys_deleted INTEGER DEFAULT 0",

            "ALTER TABLE user_keys ADD COLUMN amnezia_key TEXT",
        ]
        for sql in migrations:
            try:
                await db.execute(sql)
            except Exception:
                pass

        await referrals.ensure_referrals_schema(db)
        await db.commit()


async def user_exists(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE telegram_id=? LIMIT 1", (user_id,))
        row = await cur.fetchone()
    return bool(row)


async def upsert_user(telegram_id: int, first_name: str, last_name: str, username: str | None):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(telegram_id, first_name, last_name, username, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username
        """, (telegram_id, first_name, last_name, username or "", now))
        await db.commit()


async def get_user_and_sub(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        user_cur = await db.execute(
            "SELECT telegram_id, first_name, last_name, username, created_at FROM users WHERE telegram_id=?",
            (telegram_id,)
        )
        user = await user_cur.fetchone()

        sub_cur = await db.execute(
            "SELECT purchased_at, period_days, expires_at, tariff FROM subscriptions WHERE telegram_id=?",
            (telegram_id,)
        )
        sub = await sub_cur.fetchone()

    return user, sub


async def get_keys(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT outline_key, v2ray_key, amnezia_key FROM user_keys WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


def cabinet_text(
    user_row,
    sub_row,
    refs_count: int,
    outline_key: str | None,
    v2ray_key: str | None,
    amnezia_key: str | None
) -> str:
    first_name = user_row[1] or ""
    last_name = user_row[2] or ""
    username = user_row[3] or ""
    created_at = user_row[4]

    if sub_row:
        purchased_at, period_days, expires_at, tariff_code = sub_row
    else:
        purchased_at, period_days, expires_at, tariff_code = None, None, None, None

    full_name = (first_name + " " + last_name).strip() or "—"
    tariff_title = TARIFF_TITLES.get(tariff_code, "—")

    if not outline_key and not v2ray_key and not amnezia_key:
        keys_block = "⏳ <b>Ожидайте когда администратор выдаст ключ</b>\n\n"
    else:
        lines = ["🔑 <b>Ключи</b>"]
        if outline_key:
            lines.append("• OutLine: <b>выдан</b>")
        if v2ray_key:
            lines.append("• v2raytun: <b>выдан</b>")
        if amnezia_key:
            lines.append("• AmneziaVPN: <b>выдан</b>")
        keys_block = "\n".join(lines) + "\n\n"

    return (
        "👤 <b>Личный кабинет</b>\n\n"
        f"• Имя и Фамилия: <b>{full_name}</b>\n"
        f"• Username: <b>@{username}</b>\n"
        f"• Регистрация: <b>{human_date(created_at)}</b>\n\n"
        "🔐 <b>VPN-подписка</b>\n"
        f"• Тариф: <b>{tariff_title}</b>\n"
        f"• Дата оформления: <b>{human_date(purchased_at)}</b>\n"
        f"• Период: <b>{(str(period_days) + ' дней') if period_days else '—'}</b>\n"
        f"• Действует до: <b>{human_date(expires_at)}</b>\n\n"
        f"{keys_block}"
        f"👥 <b>Рефералы:</b> <b>{refs_count}</b>\n"
        f"🎁 Реферал получает: <b>+{INVITEE_TRIAL_DAYS} дня</b>\n"
        f"🏆 Вы получаете: <b>+{REFERRER_BONUS_DAYS} дней</b> после <b>первой оплаты</b> реферала\n"
    )


async def notify_admins_new_user(bot: Bot, admin_ids: set[int], user_msg: Message):
    full_name = " ".join([p for p in [user_msg.from_user.first_name, user_msg.from_user.last_name] if p]).strip() or "Без имени"
    mention = f'<a href="tg://user?id={user_msg.from_user.id}">{full_name}</a>'
    text = (
        "🆕 <b>Новый пользователь</b>\n\n"
        f"{mention}\n"
        f"ID: <code>{user_msg.from_user.id}</code>\n"
        "Нужно выдать ключ(и) пользователю."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 Управление пользователем", callback_data=f"admin_user:{user_msg.from_user.id}")]
    ])
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ---------------- Subscription watcher ----------------

async def subscription_watcher(bot: Bot):
    while True:
        try:
            now = datetime.now(timezone.utc)

            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("""
                    SELECT telegram_id, expires_at, warn_2d_sent, expired_sent, keys_deleted
                    FROM subscriptions
                    WHERE expires_at IS NOT NULL AND expires_at != ''
                """)
                subs = await cur.fetchall()

                for telegram_id, expires_at, warn_2d_sent, expired_sent, keys_deleted in subs:
                    exp_dt = _parse_iso(expires_at)
                    if not exp_dt:
                        continue

                    warn_2d_sent = int(warn_2d_sent or 0)
                    expired_sent = int(expired_sent or 0)
                    keys_deleted = int(keys_deleted or 0)

                    if exp_dt > now:
                        remaining = exp_dt - now

                        if expired_sent == 1 or keys_deleted == 1:
                            await db.execute(
                                "UPDATE subscriptions SET expired_sent=0, keys_deleted=0 WHERE telegram_id=?",
                                (telegram_id,)
                            )

                        if remaining <= WARN_BEFORE and warn_2d_sent == 0:
                            try:
                                await bot.send_message(
                                    telegram_id,
                                    "⏳ До окончания подписки осталось меньше 2 дней.\n"
                                    "Пожалуйста, оплатите подписку, чтобы доступ не прервался.\n\n"
                                    "Нажмите «Оплатить» в личном кабинете."
                                )
                                await db.execute(
                                    "UPDATE subscriptions SET warn_2d_sent=1 WHERE telegram_id=?",
                                    (telegram_id,)
                                )
                            except Exception:
                                pass

                        if remaining > WARN_BEFORE and warn_2d_sent == 1:
                            await db.execute(
                                "UPDATE subscriptions SET warn_2d_sent=0 WHERE telegram_id=?",
                                (telegram_id,)
                            )

                    else:
                        if expired_sent == 0:
                            try:
                                await bot.send_message(
                                    telegram_id,
                                    "⚠️ Подписка окончена.\n"
                                    "Оплатите в течение 2 дней, иначе ключ будет удалён.\n\n"
                                    "Нажмите «Оплатить» в личном кабинете."
                                )
                                await db.execute(
                                    "UPDATE subscriptions SET expired_sent=1 WHERE telegram_id=?",
                                    (telegram_id,)
                                )
                            except Exception:
                                pass

                        if now >= exp_dt + GRACE_AFTER_EXPIRE and keys_deleted == 0:
                            try:
                                await db.execute(
                                    "UPDATE user_keys SET outline_key=NULL, v2ray_key=NULL, amnezia_key=NULL, updated_at=?, updated_by=? WHERE user_id=?",
                                    (now.isoformat(), 0, telegram_id)
                                )
                                await db.execute(
                                    "UPDATE subscriptions SET keys_deleted=1 WHERE telegram_id=?",
                                    (telegram_id,)
                                )
                                await db.commit()

                                try:
                                    await bot.send_message(
                                        telegram_id,
                                        "❌ Ключи удалены, так как подписка не была оплачена в течение 2 дней после окончания.\n"
                                        "Оплатите подписку, и администратор выдаст новый ключ."
                                    )
                                except Exception:
                                    pass
                            except Exception:
                                pass

                await db.commit()

        except Exception:
            pass

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ---------------- Cabinet sender ----------------

async def send_cabinet(bot: Bot, chat_id: int, user_obj):
    """
    Унифицированный показ личного кабинета (для callback и для /home).
    user_obj - Message.from_user или CallbackQuery.from_user
    """
    await upsert_user(
        telegram_id=user_obj.id,
        first_name=user_obj.first_name or "",
        last_name=user_obj.last_name or "",
        username=user_obj.username,
    )

    user, sub = await get_user_and_sub(user_obj.id)
    if not user:
        await bot.send_message(chat_id, "Нажмите /start ещё раз")
        return

    outline_key, v2ray_key, amnezia_key = await get_keys(user_obj.id)
    refs_count = await referrals.get_referrals_count(user_obj.id)

    await bot.send_message(
        chat_id,
        cabinet_text(user, sub, refs_count, outline_key, v2ray_key, amnezia_key),
        reply_markup=cabinet_actions_kb(outline_key, v2ray_key, amnezia_key),
        parse_mode=ParseMode.HTML
    )


# ---------------- Main ----------------

async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Нет BOT_TOKEN в .env")

    admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS"))

    await init_db()

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher(storage=MemoryStorage())

    me = await bot.get_me()
    bot_username = me.username

    admin.setup_admin(DB_PATH, admin_ids)
    dp.include_router(admin.router)

    referrals.setup_referrals(DB_PATH, bot_username)
    dp.include_router(referrals.router)

    pay.setup_pay(DB_PATH, admin_ids)
    await pay.init_pay_db()
    dp.include_router(pay.router)

    dp.include_router(tariffs.router)

    asyncio.create_task(subscription_watcher(bot))

    @dp.message(CommandStart())
    async def start(message: Message):
        is_new = not await user_exists(message.from_user.id)

        await upsert_user(
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name or "",
            last_name=message.from_user.last_name or "",
            username=message.from_user.username,
        )

        # рефералом может стать только новый пользователь
        if is_new:
            referrer_id = await referrals.apply_referral_on_start(message.from_user.id, message.text)
            if referrer_id:
                full_name = " ".join([p for p in [message.from_user.first_name, message.from_user.last_name] if p]).strip() or "Без имени"
                mention = f'<a href="tg://user?id={message.from_user.id}">{full_name}</a>'
                try:
                    await bot.send_message(referrer_id, f"✅ Ваш реферал {mention} зарегистрировался в боте.", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

        text = (
            "👋 Добро пожаловать!\n\n"
            "⏳ Пожалуйста, дождитесь уведомления, что администратор выдал вам ключ(и).\n"
            "После выдачи ключ появится в «Личном кабинете».\n\n"
            "Нажмите кнопку ниже."
        )
        await message.answer(text, reply_markup=start_kb(message.from_user.id in admin_ids))

        if is_new and admin_ids:
            await notify_admins_new_user(bot, admin_ids, message)

    @dp.message(Command("home"))
    async def home(message: Message):
        # /home открывает личный кабинет
        await send_cabinet(bot, message.chat.id, message.from_user)

    @dp.callback_query(F.data == "cabinet")
    async def cabinet(callback: CallbackQuery):
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass

        await send_cabinet(bot, callback.message.chat.id, callback.from_user)
        await callback.answer()

    @dp.callback_query(F.data == "show_outline_key")
    async def show_outline_key(callback: CallbackQuery):
        outline_key, _, _ = await get_keys(callback.from_user.id)
        if outline_key:
            await callback.message.answer(
                "🔑 <b>Ваш ключ OutLine</b>\n\n"
                f"<code>{outline_key}</code>",
                parse_mode=ParseMode.HTML
            )
        await callback.answer()

    @dp.callback_query(F.data == "show_v2ray_key")
    async def show_v2ray_key(callback: CallbackQuery):
        _, v2ray_key, _ = await get_keys(callback.from_user.id)
        if v2ray_key:
            await callback.message.answer(
                "🔑 <b>Ваш ключ v2raytun</b>\n\n"
                f"<code>{v2ray_key}</code>",
                parse_mode=ParseMode.HTML
            )
        await callback.answer()

    @dp.callback_query(F.data == "show_amnezia_key")
    async def show_amnezia_key(callback: CallbackQuery):
        _, _, amnezia_key = await get_keys(callback.from_user.id)
        if amnezia_key:
            await callback.message.answer(
                "🔑 <b>Ваш ключ AmneziaVPN</b>\n\n"
                f"<code>{amnezia_key}</code>",
                parse_mode=ParseMode.HTML
            )
        await callback.answer()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
