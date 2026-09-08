from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from database.models import Withdrawal, User, WithdrawalStatus
from database.repository import Repository
from states.admin_states import RejectWithdrawalFSM

router = Router()


# =========================
# СПИСОК ЗАЯВОК НА ВЫВОД
# =========================

@router.callback_query(F.data == "admin_withdraws_menu")
async def admin_withdraws_menu(
    callback: CallbackQuery,
    repo: Repository,
    is_admin: bool
):
    if not is_admin:
        await callback.answer(
            "Нет доступа",
            show_alert=True
        )
        return

    result = await repo.session.execute(
        select(Withdrawal)
        .where(
            Withdrawal.status == WithdrawalStatus.PENDING
        )
        .order_by(Withdrawal.created_at.asc())
    )

    withdrawals = result.scalars().all()

    if not withdrawals:
        await callback.message.edit_text(
            "💸 <b>Заявок на вывод сейчас нет.</b>",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    withdrawal = withdrawals[0]

    user_result = await repo.session.execute(
        select(User).where(
            User.id == withdrawal.user_id
        )
    )

    user = user_result.scalar_one_or_none()

    username = (
        f"@{user.username}"
        if user and user.username
        else "нет"
    )

    text = (
        "💸 <b>Заявка на вывод</b>\n\n"
        f"🆔 Заявка: <code>#{withdrawal.id}</code>\n"
        f"👤 Username: {username}\n"
        f"🆔 Telegram ID: "
        f"<code>{user.telegram_id if user else 'не найден'}</code>\n\n"
        f"💰 Сумма: <b>{withdrawal.amount_gmp} GMP</b>\n"
        f"💵 К выплате: <b>{withdrawal.amount_money} грн</b>\n"
        f"💱 Курс: 1 GMP = {withdrawal.exchange_rate} грн\n\n"
        f"💳 Реквизиты:\n"
        f"<code>{withdrawal.requisites}</code>\n\n"
        f"📅 Создана: "
        f"{withdrawal.created_at.strftime('%d.%m.%Y %H:%M')}"
    )

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"adm_app_wth:{withdrawal.id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"adm_rej_wth:{withdrawal.id}"
                )
            ]
        ]
    )

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    await callback.answer()


# =========================
# ОДОБРЕНИЕ ВЫВОДА
# =========================

@router.callback_query(F.data.startswith("adm_app_wth:"))
async def approve_withdrawal(
    callback: CallbackQuery,
    repo: Repository,
    bot,
    is_admin: bool
):
    if not is_admin:
        await callback.answer(
            "Нет доступа",
            show_alert=True
        )
        return

    try:
        withdrawal_id = int(
            callback.data.split(":")[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный ID заявки",
            show_alert=True
        )
        return

    success, result, withdrawal = (
        await repo.approve_withdrawal(
            withdrawal_id
        )
    )

    if not success:
        await callback.answer(
            result,
            show_alert=True
        )
        return

    user = await repo.session.get(
        User,
        withdrawal.user_id
    )

    await callback.answer(
        "✅ Вывод одобрен"
    )

    if user:
        try:
            await bot.send_message(
                user.telegram_id,
                "✅ <b>Вывод одобрен!</b>\n\n"
                f"💰 Сумма: <b>{withdrawal.amount_gmp} GMP</b>\n"
                f"💵 Выплата: <b>{withdrawal.amount_money} грн</b>\n\n"
                "Средства успешно выведены.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await admin_withdraws_menu(
        callback,
        repo,
        is_admin
    )


# =========================
# НАЧАЛО ОТКЛОНЕНИЯ
# =========================

@router.callback_query(F.data.startswith("adm_rej_wth:"))
async def reject_withdrawal_start(
    callback: CallbackQuery,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        await callback.answer(
            "Нет доступа",
            show_alert=True
        )
        return

    try:
        withdrawal_id = int(
            callback.data.split(":")[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный ID заявки",
            show_alert=True
        )
        return

    await state.update_data(
        withdrawal_id=withdrawal_id
    )

    await state.set_state(
        RejectWithdrawalFSM.waiting_for_reason
    )

    await callback.message.answer(
        "❌ <b>Отклонение вывода</b>\n\n"
        "Введите причину отклонения:",
        parse_mode="HTML"
    )

    await callback.answer()


# =========================
# ЗАВЕРШЕНИЕ ОТКЛОНЕНИЯ
# =========================

@router.message(
    RejectWithdrawalFSM.waiting_for_reason
)
async def reject_withdrawal_finish(
    message: Message,
    state: FSMContext,
    repo: Repository,
    bot,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text or not message.text.strip():
        await message.answer(
            "❌ Причина не может быть пустой."
        )
        return

    data = await state.get_data()

    withdrawal_id = data.get(
        "withdrawal_id"
    )

    if not withdrawal_id:
        await state.clear()
        await message.answer(
            "❌ Заявка не найдена."
        )
        return

    reason = message.text.strip()

    success, result, withdrawal = (
        await repo.reject_withdrawal(
            withdrawal_id,
            reason
        )
    )

    await state.clear()

    if not success:
        await message.answer(
            f"❌ {result}"
        )
        return

    user = await repo.session.get(
        User,
        withdrawal.user_id
    )

    await message.answer(
        "❌ <b>Вывод отклонён.</b>\n\n"
        f"💰 {withdrawal.amount_gmp} GMP "
        "возвращены пользователю.",
        parse_mode="HTML"
    )

    if user:
        try:
            await bot.send_message(
                user.telegram_id,
                "❌ <b>Вывод отклонён</b>\n\n"
                f"💰 {withdrawal.amount_gmp} GMP "
                "возвращены на ваш баланс.\n\n"
                f"📝 Причина: {reason}",
                parse_mode="HTML"
            )
        except Exception:
            pass
