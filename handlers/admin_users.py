from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from sqlalchemy import select

from database.models import User
from database.repository import Repository
from states.admin_states import AdminUserFSM
from config import settings

router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


@router.callback_query(F.data == "admin_users")
async def admin_users_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.set_state(AdminUserFSM.search)

    await callback.message.edit_text(
        "👤 <b>Управление пользователями</b>\n\n"
        "Введите Telegram ID пользователя:"
    )

    await callback.answer()


@router.message(StateFilter(AdminUserFSM.search))
async def search_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()

    if not text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр.")
        return

    telegram_id = int(text)

    async with message.bot.sessionmaker() as session:
        result = await session.execute(
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
        f"💰 Активный баланс: <b>{user.balance_active} GMP</b>\n"
        f"🔒 Заблокировано: <b>{user.balance_locked} GMP</b>\n\n"
        "Введите сумму изменения баланса.\n"
        "Например:\n"
        "<code>100</code> — добавить 100 GMP\n"
        "<code>-100</code> — снять 100 GMP"
    )

    await state.set_state(AdminUserFSM.adjust_balance)


@router.message(StateFilter(AdminUserFSM.adjust_balance))
async def adjust_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip().replace(",", ".")

    try:
        amount = int(text)
    except ValueError:
        await message.answer(
            "❌ Введите целое число, например <code>100</code> или <code>-100</code>."
        )
        return

    if amount == 0:
        await message.answer("❌ Сумма не может быть равна 0.")
        return

    data = await state.get_data()
    user_id = data.get("user_id")

    if not user_id:
        await message.answer("❌ Пользователь не выбран.")
        await state.clear()
        return

    async with message.bot.sessionmaker() as session:
        repo = Repository(session)

        try:
            await repo.admin_adjust_balance(
                user_id=user_id,
                amount=amount,
                description=f"Изменение баланса администратором {message.from_user.id}",
            )
            await session.commit()

        except ValueError as e:
            await session.rollback()
            await message.answer(f"❌ {e}")
            return

        except Exception:
            await session.rollback()
            await message.answer(
                "❌ Произошла ошибка при изменении баланса."
            )
            return

    await state.clear()

    sign = "+" if amount > 0 else ""

    await message.answer(
        "✅ <b>Баланс изменён</b>\n\n"
        f"Изменение: <b>{sign}{amount} GMP</b>"
    )
