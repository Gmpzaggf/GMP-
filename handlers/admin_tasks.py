from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from database.models import Task, TaskSubmission, User
from database.repository import Repository
from states.admin_states import CreateTaskFSM, RejectSubmissionFSM

router = Router()


# =========================
# МЕНЮ ЗАДАНИЙ
# =========================

@router.callback_query(F.data == "admin_tasks_menu")
async def admin_tasks_menu(
    callback: CallbackQuery,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.clear()

    await callback.message.edit_text(
        "📝 <b>Управление заданиями</b>\n\n"
        "Используйте команды:\n\n"
        "➕ Создать задание — через /create_task\n"
        "📋 Проверить выполнения — через кнопку проверки.",
        parse_mode="HTML"
    )

    await callback.answer()


# =========================
# СОЗДАНИЕ ЗАДАНИЯ
# =========================

@router.message(F.text == "/create_task")
async def create_task_start(
    message: Message,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        return

    await state.clear()
    await state.set_state(CreateTaskFSM.title)

    await message.answer(
        "➕ <b>Создание задания</b>\n\n"
        "Введите название задания:",
        parse_mode="HTML"
    )


@router.message(CreateTaskFSM.title)
async def create_task_title(
    message: Message,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text or not message.text.strip():
        await message.answer("❌ Название не может быть пустым.")
        return

    await state.update_data(title=message.text.strip())
    await state.set_state(CreateTaskFSM.description)

    await message.answer(
        "Введите описание задания:"
    )


@router.message(CreateTaskFSM.description)
async def create_task_description(
    message: Message,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text or not message.text.strip():
        await message.answer("❌ Описание не может быть пустым.")
        return

    await state.update_data(description=message.text.strip())
    await state.set_state(CreateTaskFSM.instructions)

    await message.answer(
        "Введите инструкцию для выполнения:"
    )


@router.message(CreateTaskFSM.instructions)
async def create_task_instructions(
    message: Message,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text or not message.text.strip():
        await message.answer("❌ Инструкция не может быть пустой.")
        return

    await state.update_data(instructions=message.text.strip())
    await state.set_state(CreateTaskFSM.link)

    await message.answer(
        "Введите ссылку на задание.\n\n"
        "Если ссылка не нужна — напишите <code>-</code>.",
        parse_mode="HTML"
    )


@router.message(CreateTaskFSM.link)
async def create_task_link(
    message: Message,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text:
        await message.answer("❌ Введите ссылку или -.") 
        return

    link = message.text.strip()

    if link == "-":
        link = None

    await state.update_data(link=link)
    await state.set_state(CreateTaskFSM.reward)

    await message.answer(
        "💰 Введите награду в GMP.\n\n"
        "Например: <code>100</code>",
        parse_mode="HTML"
    )


@router.message(CreateTaskFSM.reward)
async def create_task_reward(
    message: Message,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text:
        await message.answer("❌ Введите число.")
        return

    try:
        reward = int(message.text.strip())
    except ValueError:
        await message.answer(
            "❌ Награда должна быть целым числом."
        )
        return

    if reward <= 0:
        await message.answer(
            "❌ Награда должна быть больше 0."
        )
        return

    await state.update_data(reward=reward)
    await state.set_state(CreateTaskFSM.proof_req)

    await message.answer(
        "📸 Что пользователь должен предоставить как доказательство?\n\n"
        "Например:\n"
        "Скриншот выполнения задания\n\n"
        "Если доказательство не требуется — напишите <code>-</code>.",
        parse_mode="HTML"
    )


@router.message(CreateTaskFSM.proof_req)
async def create_task_finish(
    message: Message,
    state: FSMContext,
    repo: Repository,
    is_admin: bool
):
    if not is_admin:
        return

    if not message.text or not message.text.strip():
        await message.answer(
            "❌ Укажите требование к доказательству или -."
        )
        return

    proof_req = message.text.strip()

    if proof_req == "-":
        proof_req = None

    data = await state.get_data()

    task = Task(
        title=data["title"],
        description=data["description"],
        instructions=data["instructions"],
        link=data.get("link"),
        reward_gmp=data["reward"],
        proof_requirement=proof_req,
        is_active=True
    )

    repo.session.add(task)
    await repo.session.commit()

    await state.clear()

    await message.answer(
        "✅ <b>Задание создано!</b>\n\n"
        f"🆔 ID: <code>{task.id}</code>\n"
        f"📝 Название: <b>{task.title}</b>\n"
        f"💰 Награда: <b>{task.reward_gmp} GMP</b>\n"
        f"🟢 Статус: активно",
        parse_mode="HTML"
    )


# =========================
# ПРОВЕРКА ВЫПОЛНЕНИЙ
# =========================

@router.callback_query(F.data == "admin_check_tasks")
async def admin_check_tasks(
    callback: CallbackQuery,
    state: FSMContext,
    repo: Repository,
    is_admin: bool
):
    if not is_admin:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.clear()

    result = await repo.session.execute(
        select(TaskSubmission)
        .where(TaskSubmission.status == "PENDING")
        .order_by(TaskSubmission.created_at.asc())
    )

    submissions = result.scalars().all()

    if not submissions:
        await callback.message.edit_text(
            "📭 <b>Новых выполнений нет.</b>",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    submission = submissions[0]

    user_result = await repo.session.execute(
        select(User).where(User.id == submission.user_id)
    )
    user = user_result.scalar_one_or_none()

    task_result = await repo.session.execute(
        select(Task).where(Task.id == submission.task_id)
    )
    task = task_result.scalar_one_or_none()

    if not task:
        await callback.message.edit_text(
            "❌ Задание больше не существует."
        )
        await callback.answer()
        return

    username = f"@{user.username}" if user and user.username else "нет"

    text = (
        "🔍 <b>Проверка выполнения</b>\n\n"
        f"📌 Задание: <b>{task.title}</b>\n"
        f"🆔 Submission ID: <code>{submission.id}</code>\n"
        f"👤 Telegram ID: <code>{user.telegram_id if user else 'не найден'}</code>\n"
        f"👤 Username: {username}\n"
        f"💰 Награда: <b>{submission.reward_gmp_snapshot} GMP</b>\n\n"
        f"📎 Доказательство:\n"
        f"<code>{submission.proof_data or 'не указано'}</code>"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML"
    )

    await callback.message.answer(
        "Для проверки используй callback-кнопки из админской клавиатуры."
    )

    await callback.answer()


# =========================
# ОДОБРЕНИЕ ВЫПОЛНЕНИЯ
# =========================

@router.callback_query(F.data.startswith("adm_app_sub:"))
async def approve_submission(
    callback: CallbackQuery,
    repo: Repository,
    is_admin: bool
):
    if not is_admin:
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        submission_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный ID",
            show_alert=True
        )
        return

    success, result = await repo.approve_submission(
        submission_id
    )

    if not success:
        await callback.answer(
            str(result),
            show_alert=True
        )
        return

    await callback.answer(
        "✅ Выполнение одобрено"
    )

    await callback.message.answer(
        f"✅ Выполнение #{submission_id} одобрено.\n"
        f"Пользователю начислено <b>{result} GMP</b>.",
        parse_mode="HTML"
    )


# =========================
# ОТКЛОНЕНИЕ ВЫПОЛНЕНИЯ
# =========================

@router.callback_query(F.data.startswith("adm_rej_sub:"))
async def reject_submission_start(
    callback: CallbackQuery,
    state: FSMContext,
    is_admin: bool
):
    if not is_admin:
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        submission_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный ID",
            show_alert=True
        )
        return

    await state.update_data(
        submission_id=submission_id
    )

    await state.set_state(
        RejectSubmissionFSM.waiting_for_reason
    )

    await callback.message.answer(
        "❌ Введите причину отклонения:"
    )

    await callback.answer()


@router.message(RejectSubmissionFSM.waiting_for_reason)
async def reject_submission_finish(
    message: Message,
    state: FSMContext,
    repo: Repository,
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
    submission_id = data.get("submission_id")

    if not submission_id:
        await state.clear()
        await message.answer(
            "❌ Выполнение не выбрано."
        )
        return

    success, result = await repo.reject_submission(
        submission_id,
        message.text.strip()
    )

    await state.clear()

    if not success:
        await message.answer(
            f"❌ {result}"
        )
        return

    await message.answer(
        f"❌ Выполнение #{submission_id} отклонено.\n\n"
        f"Причина: {message.text.strip()}"
    )
