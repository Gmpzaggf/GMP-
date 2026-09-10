import asyncio
import enum
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
)

from sqlalchemy import (
    BigInteger, String, Text, Integer, Float, Boolean, DateTime, Enum as SQLEnum,
    ForeignKey, select, and_, desc, func, delete
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase

# =====================================================================
# 🛠 1. КОНФИГУРАЦИЯ (Считываем из Environment Variables)
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_БОТА")

admin_id_raw = os.getenv("ADMIN_IDS", "123456789")
ADMIN_IDS = [int(x.strip()) for x in admin_id_raw.split(",") if x.strip().isdigit()]

DB_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./gmp_bot.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# =====================================================================
# 📦 2. МОДЕЛИ БАЗЫ ДАННЫХ
# =====================================================================
class Base(DeclarativeBase):
    pass

class SubmissionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class WithdrawalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class TransactionType(str, enum.Enum):
    TASK_REWARD = "task_reward"
    WITHDRAWAL_LOCK = "withdrawal_lock"
    WITHDRAWAL_FINAL = "withdrawal_final"
    WITHDRAWAL_REFUND = "withdrawal_refund"

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    balance_active: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_locked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    submissions: Mapped[list["TaskSubmission"]] = relationship(back_populates="user")
    withdrawals: Mapped[list["Withdrawal"]] = relationship(back_populates="user")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    reward_gmp: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    submissions: Mapped[list["TaskSubmission"]] = relationship(back_populates="task")

class TaskSubmission(Base):
    __tablename__ = "task_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    reward_gmp_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(SQLEnum(SubmissionStatus), default=SubmissionStatus.PENDING, nullable=False, index=True)
    proof_type: Mapped[str] = mapped_column(String(32), nullable=False)
    proof_data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="user")
    task: Mapped["Task"] = relationship(back_populates="submissions")

class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    amount_gmp: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_money: Mapped[float] = mapped_column(Float, nullable=False)
    requisites: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WithdrawalStatus] = mapped_column(SQLEnum(WithdrawalStatus), default=WithdrawalStatus.PENDING, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="withdrawals")

class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    type: Mapped[TransactionType] = mapped_column(SQLEnum(TransactionType), nullable=False)
    amount_gmp: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="transactions")

class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)

# =====================================================================
# ⚙️ 3. РЕПОЗИТОРИЙ И БД
# =====================================================================
engine = create_async_engine(DB_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            if not await session.get(Setting, "gmp_rate"):
                session.add(Setting(key="gmp_rate", value="0.10"))
            if not await session.get(Setting, "min_withdraw"):
                session.add(Setting(key="min_withdraw", value="500"))

class Repository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create_user(self, telegram_id: int, username: Optional[str]) -> User:
        stmt = select(User).where(User.telegram_id == telegram_id)
        res = await self.session.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id, username=username)
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)
        elif user.username != username:
            user.username = username
            await self.session.commit()
        return user

    async def get_setting(self, key: str, default: str) -> str:
        res = await self.session.execute(select(Setting.value).where(Setting.key == key))
        val = res.scalar_one_or_none()
        return val if val is not None else default

    async def set_setting(self, key: str, value: str):
        async with self.session.begin():
            setting = await self.session.get(Setting, key)
            if setting:
                setting.value = value
            else:
                self.session.add(Setting(key=key, value=value))

    async def get_stats(self) -> dict:
        total_users = (await self.session.execute(select(func.count(User.id)))).scalar_one()
        total_tasks = (await self.session.execute(select(func.count(Task.id)))).scalar_one()
        pending_subs = (await self.session.execute(select(func.count(TaskSubmission.id)).where(TaskSubmission.status == SubmissionStatus.PENDING))).scalar_one()
        pending_wths = (await self.session.execute(select(func.count(Withdrawal.id)).where(Withdrawal.status == WithdrawalStatus.PENDING))).scalar_one()
        return {
            "total_users": total_users,
            "total_tasks": total_tasks,
            "pending_subs": pending_subs,
            "pending_wths": pending_wths
        }

    async def get_available_tasks_for_user(self, user_id: int) -> Sequence[Task]:
        sub_query = select(TaskSubmission.task_id).where(
            and_(
                TaskSubmission.user_id == user_id,
                TaskSubmission.status.in_([SubmissionStatus.PENDING, SubmissionStatus.APPROVED])
            )
        )
        stmt = select(Task).where(and_(Task.is_active == True, Task.id.not_in(sub_query)))
        return (await self.session.execute(stmt)).scalars().all()

    async def create_submission(self, user_id: int, task_id: int, proof_type: str, proof_data: str) -> Optional[TaskSubmission]:
        async with self.session.begin():
            task = await self.session.get(Task, task_id)
            if not task or not task.is_active: return None
            sub = TaskSubmission(
                user_id=user_id, task_id=task_id, reward_gmp_snapshot=task.reward_gmp,
                status=SubmissionStatus.PENDING, proof_type=proof_type, proof_data=proof_data
            )
            self.session.add(sub)
            return sub

    async def approve_submission(self, submission_id: int) -> Tuple[bool, str, Optional[TaskSubmission]]:
        async with self.session.begin():
            sub = (await self.session.execute(select(TaskSubmission).where(TaskSubmission.id == submission_id).with_for_update())).scalar_one_or_none()
            if not sub or sub.status != SubmissionStatus.PENDING:
                return False, "Заявка не найдена или уже обработана.", None

            user = await self.session.get(User, sub.user_id, with_for_update=True)
            user.balance_active += sub.reward_gmp_snapshot
            sub.status = SubmissionStatus.APPROVED
            return True, "Одобрено.", sub

    async def create_withdrawal(self, user_id: int, amount_gmp: int, requisites: str) -> Tuple[bool, str, Optional[Withdrawal]]:
        async with self.session.begin():
            user = await self.session.get(User, user_id, with_for_update=True)
            min_w = int(await self.get_setting("min_withdraw", "500"))
            rate = float(await self.get_setting("gmp_rate", "0.10"))

            if amount_gmp < min_w or user.balance_active < amount_gmp:
                return False, "Недостаточно средств или меньше минимума.", None

            user.balance_active -= amount_gmp
            user.balance_locked += amount_gmp

            wth = Withdrawal(
                user_id=user.id, amount_gmp=amount_gmp, amount_money=round(amount_gmp * rate, 2),
                requisites=requisites, status=WithdrawalStatus.PENDING
            )
            self.session.add(wth)
            return True, "Заявка на вывод создана.", wth

    async def approve_withdrawal(self, withdrawal_id: int) -> Tuple[bool, str, Optional[Withdrawal]]:
        async with self.session.begin():
            wth = (await self.session.execute(select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update())).scalar_one_or_none()
            if not wth or wth.status != WithdrawalStatus.PENDING:
                return False, "Заявка не найдена.", None

            user = await self.session.get(User, wth.user_id, with_for_update=True)
            user.balance_locked -= wth.amount_gmp
            wth.status = WithdrawalStatus.APPROVED
            return True, "Вывод одобрен.", wth

    async def cleanup_old_data(self):
        """ Очистка старых записей старше 30 дней для экономии места """
        async with self.session.begin():
            cutoff = datetime.utcnow() - timedelta(days=30)
            await self.session.execute(
                delete(TaskSubmission).where(and_(TaskSubmission.created_at < cutoff, TaskSubmission.status != SubmissionStatus.PENDING))
            )
            await self.session.execute(
                delete(Withdrawal).where(and_(Withdrawal.created_at < cutoff, Withdrawal.status != WithdrawalStatus.PENDING))
            )

# =====================================================================
# 🔄 4. ФОНОВЫЕ ЗАДАЧИ (Авто-ринг + Авто-чистка)
# =====================================================================
async def auto_ring_loop(bot: Bot):
    TARGET_URL = "https://httpbin.org/get"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(TARGET_URL, timeout=15) as response:
                    if response.status == 200:
                        logging.info("✅ Авто-ринг успешен!")
        except Exception as e:
            logging.error(f"❌ Ошибка авто-ринга: {e}")
        await asyncio.sleep(300)

async def periodic_cleanup_loop():
    while True:
        try:
            async with AsyncSessionLocal() as session:
                repo = Repository(session)
                await repo.cleanup_old_data()
                logging.info("🧹 Кэш и старые данные автоматически очищены.")
        except Exception as e:
            logging.error(f"❌ Ошибка при очистке БД: {e}")
        await asyncio.sleep(86400) # Очистка раз в сутки

# =====================================================================
# 🔄 5. MIDDLEWARES & STATES
# =====================================================================
class DbSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        async with AsyncSessionLocal() as session:
            data["repo"] = Repository(session)
            data["is_admin"] = data.get("event_from_user").id in ADMIN_IDS if data.get("event_from_user") else False
            return await handler(event, data)

class TaskSubmissionFSM(StatesGroup):
    waiting_for_proof = State()

class WithdrawFSM(StatesGroup):
    waiting_for_amount = State()
    waiting_for_requisites = State()

class CreateTaskFSM(StatesGroup):
    title = State()
    description = State()
    reward = State()

class AdminSettingsFSM(StatesGroup):
    waiting_for_rate = State()
    waiting_for_min_withdraw = State()

# =====================================================================
# ⌨️ 6. КЛАВИАТУРЫ
# =====================================================================
def main_user_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Задания"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📜 История")],
            [KeyboardButton(text="💸 Вывод GMP"), KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="⚙️ Изменить курс/лимиты", callback_data="admin_settings")],
        [InlineKeyboardButton(text="➕ Создать задание", callback_data="admin_add_task")],
        [InlineKeyboardButton(text="📝 Проверка заданий", callback_data="admin_check_tasks")],
        [InlineKeyboardButton(text="💸 Заявки на вывод", callback_data="admin_withdraws_menu")]
    ])

# =====================================================================
# 🚀 7. ХЭНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ
# =====================================================================
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(msg: Message, repo: Repository):
    await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    await msg.answer("👋 Добро пожаловать в GMP!", reply_markup=main_user_kb())

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(msg: Message):
    await msg.answer("ℹ️ Выполняйте задания, получай GMP и выводите GMP!")

@dp.message(F.text == "💰 Баланс")
async def cmd_balance(msg: Message, repo: Repository):
    user = await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    rate = float(await repo.get_setting("gmp_rate", "0.10"))
    await msg.answer(
        f"💰 **Ваш баланс:**\n\n"
        f"💳 Доступно: {user.balance_active} GMP (≈ {round(user.balance_active * rate, 2)} грн)\n"
        f"🔒 В процессе вывода: {user.balance_locked} GMP",
        parse_mode="Markdown"
    )

@dp.message(F.text == "👤 Профиль")
async def cmd_profile(msg: Message, repo: Repository, is_admin: bool):
    user = await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    role_str = "👑 Администратор" if is_admin else "👤 Пользователь"
    
    text = (
        f"👤 **Ваш Профиль:**\n"
        f"Статус: {role_str}\n"
        f"ID: `{user.telegram_id}`\n"
        f"Логин: @{user.username}\n"
        f"Баланс: {user.balance_active} GMP"
    )
    if is_admin:
        text += "\n\n💡 _Для входа в панель админа используйте /admin_"
        
    await msg.answer(text, parse_mode="Markdown")

# --- ЗАДАНИЯ ---
@dp.message(F.text == "📋 Задания")
async def list_tasks(msg: Message, repo: Repository):
    user = await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    tasks = await repo.get_available_tasks_for_user(user.id)
    if not tasks:
        await msg.answer("🎉 Вы выполнили все доступные задания на сегодня!")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔹 {t.title} — {t.reward_gmp} GMP", callback_data=f"view_task:{t.id}")] for t in tasks
    ])
    await msg.answer("📋 **Доступные задания:**", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("view_task:"))
async def view_task(call: CallbackQuery, repo: Repository):
    task = await repo.session.get(Task, int(call.data.split(":")[1]))
    text = f"📋 **{task.title}**\n\n{task.description}\n\nНаграда: {task.reward_gmp} GMP"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Выполнить", callback_data=f"start_task:{task.id}")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("start_task:"))
async def start_task(call: CallbackQuery, state: FSMContext):
    await state.update_data(current_task_id=int(call.data.split(":")[1]))
    await state.set_state(TaskSubmissionFSM.waiting_for_proof)
    await call.message.edit_text("📤 Отправьте **фото** или **текст** в качестве отчета:")

@dp.message(TaskSubmissionFSM.waiting_for_proof)
async def process_proof(msg: Message, state: FSMContext, repo: Repository):
    data = await state.get_data()
    user = await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    proof_type = "photo" if msg.photo else "text"
    proof_data = msg.photo[-1].file_id if msg.photo else msg.text

    sub = await repo.create_submission(user.id, data["current_task_id"], proof_type, proof_data)
    await state.clear()
    if sub:
        await msg.answer("⏳ **Отчет отправлен на проверку!**", parse_mode="Markdown")
    else:
        await msg.answer("❌ Ошибка отправки отчета.")

# --- ВЫВОД ---
@dp.message(F.text == "💸 Вывод GMP")
async def withdraw_start(msg: Message, repo: Repository, state: FSMContext):
    user = await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    min_w = int(await repo.get_setting("min_withdraw", "500"))
    if user.balance_active < min_w:
        await msg.answer(f"❌ Минимальный вывод: {min_w} GMP. На вашем балансе: {user.balance_active} GMP.")
        return
    await state.set_state(WithdrawFSM.waiting_for_amount)
    await msg.answer("💰 Введите сумму GMP для вывода:")

@dp.message(WithdrawFSM.waiting_for_amount)
async def withdraw_amount(msg: Message, state: FSMContext):
    if not msg.text or not msg.text.isdigit():
        await msg.answer("❌ Введите корректное число.")
        return
    await state.update_data(withdraw_amount=int(msg.text))
    await state.set_state(WithdrawFSM.waiting_for_requisites)
    await msg.answer("💳 Введите номер вашей карты или кошелька:")

@dp.message(WithdrawFSM.waiting_for_requisites)
async def withdraw_reqs(msg: Message, state: FSMContext, repo: Repository):
    data = await state.get_data()
    user = await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    ok, err, wth = await repo.create_withdrawal(user.id, data["withdraw_amount"], msg.text.strip())
    await state.clear()
    if ok:
        await msg.answer(f"✅ Заявка на вывод #{wth.id} принята!")
    else:
        await msg.answer(f"❌ Ошибка: {err}")

@dp.message(F.text == "📜 История")
async def history(msg: Message, repo: Repository):
    user = await repo.get_or_create_user(msg.from_user.id, msg.from_user.username)
    subs = (await repo.session.execute(select(TaskSubmission).where(TaskSubmission.user_id == user.id).order_by(desc(TaskSubmission.created_at)).limit(5))).scalars().all()
    text = "📜 **Последние действия:**\n"
    for s in subs:
        text += f"• Задание #{s.task_id}: {s.status.value} (+{s.reward_gmp_snapshot} GMP)\n"
    await msg.answer(text, parse_mode="Markdown")

# =====================================================================
# 👑 8. РАСШИРЕННАЯ АДМИН-ПАНЕЛЬ
# =====================================================================
@dp.message(Command("admin"))
async def cmd_admin(msg: Message, is_admin: bool):
    if is_admin:
        await msg.answer("👑 **Панель Управления**", reply_markup=admin_main_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_stats")
async def adm_stats(call: CallbackQuery, repo: Repository, is_admin: bool):
    if not is_admin: return
    stats = await repo.get_stats()
    text = (
        f"📊 **Статистика Бота:**\n\n"
        f"👥 Всего пользователей: `{stats['total_users']}`\n"
        f"📋 Активных заданий: `{stats['total_tasks']}`\n"
        f"⏳ Заданий на проверке: `{stats['pending_subs']}`\n"
        f"💸 Заявок на вывод: `{stats['pending_wths']}`"
    )
    await call.message.edit_text(text, reply_markup=admin_main_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_settings")
async def adm_settings_menu(call: CallbackQuery, repo: Repository, is_admin: bool):
    if not is_admin: return
    rate = await repo.get_setting("gmp_rate", "0.10")
    min_w = await repo.get_setting("min_withdraw", "500")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить курс GMP", callback_data="set_rate")],
        [InlineKeyboardButton(text="✏️ Изменить мин. вывод", callback_data="set_min_w")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    await call.message.edit_text(
        f"⚙️ **Текущие настройки:**\n\n"
        f"📈 Курс: 1 GMP = {rate} валюты\n"
        f"🔻 Мин. вывод: {min_w} GMP",
        reply_markup=kb, parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_back")
async def adm_back(call: CallbackQuery, is_admin: bool):
    if is_admin:
        await call.message.edit_text("👑 **Панель Управления**", reply_markup=admin_main_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "set_rate")
async def set_rate_start(call: CallbackQuery, state: FSMContext, is_admin: bool):
    if not is_admin: return
    await state.set_state(AdminSettingsFSM.waiting_for_rate)
    await call.message.answer("Введите новый курс (например, `0.15`):")

@dp.message(AdminSettingsFSM.waiting_for_rate)
async def set_rate_finish(msg: Message, state: FSMContext, repo: Repository):
    await repo.set_setting("gmp_rate", msg.text.strip().replace(',', '.'))
    await state.clear()
    await msg.answer("✅ Курс успешно обновлен!")

@dp.callback_query(F.data == "set_min_w")
async def set_min_w_start(call: CallbackQuery, state: FSMContext, is_admin: bool):
    if not is_admin: return
    await state.set_state(AdminSettingsFSM.waiting_for_min_withdraw)
    await call.message.answer("Введите минимальную сумму вывода в GMP:")

@dp.message(AdminSettingsFSM.waiting_for_min_withdraw)
async def set_min_w_finish(msg: Message, state: FSMContext, repo: Repository):
    await repo.set_setting("min_withdraw", msg.text.strip())
    await state.clear()
    await msg.answer("✅ Минимальная сумма вывода обновлена!")

@dp.callback_query(F.data == "admin_add_task")
async def adm_add_task(call: CallbackQuery, state: FSMContext, is_admin: bool):
    if not is_admin: return
    await state.set_state(CreateTaskFSM.title)
    await call.message.answer("Введите название задания:")

@dp.message(CreateTaskFSM.title)
async def adm_t1(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text)
    await state.set_state(CreateTaskFSM.description)
    await msg.answer("Введите описание задания:")

@dp.message(CreateTaskFSM.description)
async def adm_t2(msg: Message, state: FSMContext):
    await state.update_data(desc=msg.text)
    await state.set_state(CreateTaskFSM.reward)
    await msg.answer("Введите сумму награды в GMP:")

@dp.message(CreateTaskFSM.reward)
async def adm_t3(msg: Message, state: FSMContext, repo: Repository):
    data = await state.get_data()
    task = Task(title=data["title"], description=data["desc"], instructions="Инструкция", reward_gmp=int(msg.text))
    repo.session.add(task)
    await repo.session.commit()
    await state.clear()
    await msg.answer("✅ Задание создано!")

@dp.callback_query(F.data == "admin_check_tasks")
async def adm_check_t(call: CallbackQuery, repo: Repository, is_admin: bool):
    if not is_admin: return
    sub = (await repo.session.execute(select(TaskSubmission).where(TaskSubmission.status == SubmissionStatus.PENDING).limit(1))).scalar_one_or_none()
    if not sub:
        await call.message.edit_text("🎉 Все отчеты проверены!")
        return

    text = f"📝 **Заявка #{sub.id}**\nНаграда: {sub.reward_gmp_snapshot} GMP"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"app_s:{sub.id}")
    ]])
    if sub.proof_type == "photo":
        await call.message.delete()
        await call.message.answer_photo(sub.proof_data, caption=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await call.message.edit_text(f"{text}\nОтчет: {sub.proof_data}", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("app_s:"))
async def adm_app_sub(call: CallbackQuery, repo: Repository, bot: Bot):
    ok, msg, sub = await repo.approve_submission(int(call.data.split(":")[1]))
    if ok:
        user = await repo.session.get(User, sub.user_id)
        await bot.send_message(user.telegram_id, f"✅ Ваш отчет одобрен! Начислено {sub.reward_gmp_snapshot} GMP.")
    await call.answer(msg)

@dp.callback_query(F.data == "admin_withdraws_menu")
async def adm_wth_menu(call: CallbackQuery, repo: Repository, is_admin: bool):
    if not is_admin: return
    wth = (await repo.session.execute(select(Withdrawal).where(Withdrawal.status == WithdrawalStatus.PENDING).limit(1))).scalar_one_or_none()
    if not wth:
        await call.message.edit_text("🎉 Все заявки на вывод обработаны!")
        return
    text = f"💸 **Вывод #{wth.id}**\nСумма: {wth.amount_gmp} GMP ({wth.amount_money})\nРеквизиты: `{wth.requisites}`"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить выплату", callback_data=f"app_w:{wth.id}")
    ]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("app_w:"))
async def adm_app_wth(call: CallbackQuery, repo: Repository, bot: Bot):
    ok, msg, wth = await repo.approve_withdrawal(int(call.data.split(":")[1]))
    if ok:
        user = await repo.session.get(User, wth.user_id)
        await bot.send_message(user.telegram_id, f"✅ Вывод #{wth.id} на сумму {wth.amount_gmp} GMP успешно выполнен!")
    await call.answer(msg)

# =====================================================================
# 🏁 9. ЗАПУСК
# =====================================================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp.update.middleware(DbSessionMiddleware())

    asyncio.create_task(auto_ring_loop(bot))
    asyncio.create_task(periodic_cleanup_loop())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
