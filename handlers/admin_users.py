from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from database.models import User
from database.repository import Repository
from states.admin_states import AdminUserFSM
from config import settings

router = Router()


def check_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids_list


@router.callback_query(F.data == "admin_users_menu")
async def admin_users_menu(
    callback: CallbackQuery,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.set_state(AdminUserFSM.waiting_for_user_query)

    await callback.message.edit_text(
        "👥 <b>Управление пользователями</b>\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="HTML"
    )

    await callback.answer()


@router.message(AdminUserFSM.waiting_for_user_query)
async def search_user(
    message: Message,
    state: FSMContext,
    repo: Repository,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text or not message.text.strip().isdigit():
        await message.answer(
            "❌ Введите корректный Telegram ID."
        )
        return

    telegram_id = int(message.text.strip())

    result = await repo.session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )

    user = result.scalar_one_or_none()

    if not user:
        await message.answer(
            "❌ Пользователь с таким Telegram ID не найден."
        )
        return

    await state.update_data(user_id=user.id)

    await message.answer(
        "👤 <b>Пользователь найден</b>\n\n"
        f"🆔 Telegram ID: <code>{user.telegram_id}</code>\n"
        f"👤 Username: @{user.username or 'нет'}\n"
        f"💰 Баланс: <b>{user.balance_active} GMP</b>\n"
        f"🔒 В выводе: <b>{user.balance_locked} GMP</b>\n\n"
        "Введите сумму изменения баланса.\n\n"
        "Например:\n"
        "<code>100</code> — добавить 100 GMP\n"
        "<code>-100</code> — снять 100 GMP",
        parse_mode="HTML"
    )

    await state.set_state(AdminUserFSM.waiting_for_adjust_amount)


@router.message(AdminUserFSM.waiting_for_adjust_amount)
async def adjust_balance(
    message: Message,
    state: FSMContext,
    repo: Repository,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text:
        await message.answer("❌ Введите число.")
        return

    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ Введите целое число.\n"
            "Например: <code>100</code> или <code>-100</code>.",
            parse_mode="HTML"
        )
        return

    if amount == 0:
        await message.answer(
            "❌ Сумма не может быть равна 0."
        )
        return

    data = await state.get_data()
    user_id = data.get("user_id")

    if not user_id:
        await state.clear()
        await message.answer(
            "❌ Пользователь не выбран."
        )
        return

    reason = "Изменение баланса администратором"

    success, result = await repo.admin_adjust_balance(
        user_id=user_id,
        amount=amount,
        reason=reason
    )

    await state.clear()

    if not success:
        await message.answer(
            f"❌ {result}"
        )
        return

    await message.answer(
        f"✅ <b>Баланс изменён</b>\n\n"
        f"Изменение: <b>{amount:+d} GMP</b>\n"
        f"{result}",
        parse_mode="HTML"
    )
