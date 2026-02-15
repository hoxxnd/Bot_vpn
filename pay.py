import aiosqlite
from datetime import datetime, timezone
from html import escape

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

router = Router()

_DB_PATH: str | None = None
_ADMIN_IDS: set[int] = set()

# --- ТАРИФЫ ---
TARIFFS = {
    "outline": {"title": "OutLine", "price": 70},
    "v2ray": {"title": "v2raytun", "price": 70},
    "bundle": {"title": "OutLine/V2RayTun + AmneziaVPN", "price": 140},
}

# сюда потом вставишь свои реквизиты
PAY_REQUISITES_TEMPLATE = (
    "💳 <b>Оплата</b>\n\n"
    "Вы выбрали: <b>{tariff_title}</b>\n"
    "Стоимость: <b>{price} ₽ / месяц</b>\n\n"
    "Реквизиты для оплаты:\n"
    "<b>[ТУТ БУДУТ РЕКВИЗИТЫ]</b>\n\n"
    "После оплаты отправьте сюда <b>скриншот</b> (фото) оплаты.\n"
    "Я передам его администратору на проверку."
)


class PayStates(StatesGroup):
    choosing_tariff = State()
    waiting_screenshot = State()
    waiting_comment = State()


def setup_pay(db_path: str, admin_ids: set[int]) -> None:
    global _DB_PATH, _ADMIN_IDS
    _DB_PATH = db_path
    _ADMIN_IDS = admin_ids


async def init_pay_db():
    if _DB_PATH is None:
        raise RuntimeError("pay not setup: call setup_pay() first")

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                screenshot_file_id TEXT NOT NULL,
                tariff TEXT NOT NULL,
                comment TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        # миграция (если таблица была раньше без tariff)
        try:
            await db.execute("ALTER TABLE payments ADD COLUMN tariff TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE payments ADD COLUMN comment TEXT")
        except Exception:
            pass

        await db.commit()


def tariff_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="OutLine — 70 ₽/мес", callback_data="pay_tariff:outline")],
        [InlineKeyboardButton(text="v2raytun — 70 ₽/мес", callback_data="pay_tariff:v2ray")],
        [InlineKeyboardButton(text="OutLine/v2raytun + AmneziaVPN — 140 ₽/мес", callback_data="pay_tariff:bundle")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="pay_cancel")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="pay_cancel")]
    ])


def requisites_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Написать комментарий", callback_data="pay_comment")],
        [InlineKeyboardButton(text="Отмена", callback_data="pay_cancel")],
    ])

def comment_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="pay_comment_back")],
        [InlineKeyboardButton(text="Отмена", callback_data="pay_cancel")],
    ])

def admin_manage_user_kb(user_id: int) -> InlineKeyboardMarkup:
    # должно совпасть с твоим admin.py (callback_data="admin_user:<id>")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 Управление подпиской", callback_data=f"admin_user:{user_id}")]
    ])


def _user_label(u) -> str:
    full_name = " ".join([p for p in [u.first_name, u.last_name] if p]).strip() or "Пользователь"
    username = f"@{u.username}" if u.username else "—"
    user_id = u.id
    mention = f'<a href="tg://user?id={user_id}">{full_name}</a>'
    return f"{mention}\nUsername: <b>{username}</b>\nID: <code>{user_id}</code>"


def _extract_image_file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    if message.document and message.document.mime_type:
        if message.document.mime_type.startswith("image/"):
            return message.document.file_id
    return None


@router.callback_query(F.data == "pay")
async def pay_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PayStates.choosing_tariff)
    await callback.message.answer(
        "✅ Выберите тариф для оплаты:",
        reply_markup=tariff_kb()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_tariff:"))
async def pay_choose_tariff(callback: CallbackQuery, state: FSMContext):
    tariff_code = callback.data.split(":", 1)[1].strip()
    if tariff_code not in TARIFFS:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    await state.update_data(tariff=tariff_code, comment=None)
    await state.set_state(PayStates.waiting_screenshot)

    t = TARIFFS[tariff_code]
    text = PAY_REQUISITES_TEMPLATE.format(tariff_title=t["title"], price=t["price"])

    await callback.message.answer(text, reply_markup=requisites_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data == "pay_comment")
async def pay_comment_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    existing = (data.get("comment") or "").strip()

    await state.set_state(PayStates.waiting_comment)

    if existing:
        text = (
            "Отправьте новый комментарий к оплате одним сообщением.\n\n"
            f"Текущий комментарий:\n<code>{escape(existing)}</code>"
        )
    else:
        text = "Напишите комментарий к оплате одним сообщением."

    await callback.message.answer(text, reply_markup=comment_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data == "pay_comment_back")
async def pay_comment_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PayStates.waiting_screenshot)
    data = await state.get_data()
    comment = (data.get("comment") or "").strip()

    if comment:
        text = "Комментарий сохранён. Отправьте скриншот оплаты."
    else:
        text = "Ок, без комментария. Отправьте скриншот оплаты."

    await callback.message.answer(text, reply_markup=requisites_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data == "pay_cancel")
async def pay_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Ок, оплату отменил. Если нужно — нажмите «Оплатить» снова.")
    await callback.answer()


@router.message(PayStates.waiting_comment)
async def pay_waiting_comment(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("Пожалуйста, отправьте комментарий текстом.", reply_markup=comment_kb())
        return

    await state.update_data(comment=comment)
    await state.set_state(PayStates.waiting_screenshot)

    await message.answer("Комментарий сохранён. Отправьте скриншот оплаты.", reply_markup=requisites_kb())


@router.message(PayStates.waiting_screenshot)
async def pay_waiting_screenshot(message: Message, state: FSMContext):
    if _DB_PATH is None:
        await message.answer("Ошибка конфигурации оплаты (DB_PATH).")
        await state.clear()
        return

    data = await state.get_data()
    tariff_code = data.get("tariff")
    comment = (data.get("comment") or "").strip() or None
    if tariff_code not in TARIFFS:
        await message.answer("Ошибка: тариф не выбран. Нажмите «Оплатить» заново.")
        await state.clear()
        return

    file_id = _extract_image_file_id(message)
    if not file_id:
        await message.answer(
            "Нужно отправить <b>фото/картинку</b> (скриншот).\n"
            "Если нужно, нажмите \"Написать комментарий\".",
            reply_markup=requisites_kb(),
            parse_mode=ParseMode.HTML
        )
        return

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments(user_id, created_at, screenshot_file_id, tariff, comment, status) VALUES (?, ?, ?, ?, ?, 'pending')",
            (message.from_user.id, datetime.now(timezone.utc).isoformat(), file_id, tariff_code, comment)
        )
        await db.commit()

    await message.answer("✅ Скриншот получен. Передал администратору на проверку.")
    await state.clear()

    # уведомляем админов + показываем тариф
    t = TARIFFS[tariff_code]
    comment_block = ""
    if comment:
        comment_block = f"Комментарий: <b>{escape(comment)}</b>\n\n"
    admin_text = (
        "📩 <b>Новый скриншот оплаты</b>\n\n"
        f"{_user_label(message.from_user)}\n\n"
        f"Тариф: <b>{t['title']}</b>\n"
        f"Сумма: <b>{t['price']} ₽ / месяц</b>\n\n"
        f"{comment_block}"
        "Ниже кнопка для управления подпиской этого пользователя."
    )

    for admin_id in _ADMIN_IDS:
        try:
            await message.bot.send_message(
                admin_id,
                admin_text,
                reply_markup=admin_manage_user_kb(message.from_user.id),
                parse_mode=ParseMode.HTML
            )
            await message.bot.send_photo(admin_id, photo=file_id, caption="🧾 Скриншот оплаты")
        except Exception:
            pass
