import asyncio
import enum
import logging
import os
import base64
import json
from urllib.parse import quote
from contextlib import suppress
from datetime import datetime
from typing import Optional, Sequence, Tuple

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SQLEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    and_,
    desc,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ============================================================
# 1. CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

GITHUB_OWNER = os.getenv("GITHUB_OWNER", "Gmpzaggf")
GITHUB_REPO = os.getenv("GITHUB_REPO", "GMP-")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_DATA_PATH = os.getenv("GITHUB_DATA_PATH", "data/gmp_data.json").strip()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

if not DATABASE_URL:
    DATABASE_URL = "sqlite+aiosqlite:///./gmp_bot.db"
    logging.warning(
        "DATABASE_URL is not set. SQLite is being used. "
        "On Render this database will NOT survive a redeploy/restart."
    )

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in Environment Variables.")

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is not set or contains no valid Telegram IDs.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gmp_bot")


# ============================================================
# 2. DATABASE MODELS
# ============================================================

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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    balance_active: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_locked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    submissions: Mapped[list["TaskSubmission"]] = relationship(
        back_populates="user", lazy="select"
    )
    withdrawals: Mapped[list["Withdrawal"]] = relationship(
        back_populates="user", lazy="select"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reward_gmp: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    submissions: Mapped[list["TaskSubmission"]] = relationship(
        back_populates="task", lazy="select"
    )


class TaskSubmission(Base):
    __tablename__ = "task_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id"), nullable=False, index=True
    )
    reward_gmp_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(
        SQLEnum(SubmissionStatus),
        default=SubmissionStatus.PENDING,
        nullable=False,
        index=True,
    )
    proof_type: Mapped[str] = mapped_column(String(32), nullable=False)
    proof_data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="submissions")
    task: Mapped["Task"] = relationship(back_populates="task")


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    amount_gmp: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_money: Mapped[float] = mapped_column(Float, nullable=False)
    requisites: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WithdrawalStatus] = mapped_column(
        SQLEnum(WithdrawalStatus),
        default=WithdrawalStatus.PENDING,
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="withdrawals")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


# ============================================================
# 3. DATABASE ENGINE & GITHUB BACKUP
# ============================================================

engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}

if DATABASE_URL.startswith("postgresql+asyncpg://"):
    engine_kwargs.update(
        {
            "pool_size": 5,
            "max_overflow": 10,
            "pool_recycle": 1800,
        }
    )

engine = create_async_engine(DATABASE_URL, **engine_kwargs)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class GitHubPersistence:
    def __init__(self) -> None:
        self.owner = GITHUB_OWNER
        self.repo = GITHUB_REPO
        self.branch = GITHUB_BRANCH
        self.token = GITHUB_TOKEN
        self.path = GITHUB_DATA_PATH
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.owner and self.repo and self.path)

    @property
    def url(self) -> str:
        return (
            f"https://api.github.com/repos/{quote(self.owner)}/"
            f"{quote(self.repo)}/contents/{quote(self.path, safe='/')}"
        )

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GMP-Telegram-Bot",
            "Content-Type": "application/json",
        }

    async def _get_file(self, http: "aiohttp.ClientSession"):
        async with http.get(
            self.url,
            headers=self._headers(),
            params={"ref": self.branch},
        ) as response:
            if response.status == 404:
                return None, None
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"GitHub GET failed: HTTP {response.status}: {body[:300]}"
                )
            payload = await response.json()
            content = base64.b64decode(payload["content"].replace("\n", "")).decode(
                "utf-8"
            )
            return json.loads(content), payload.get("sha")

    async def load_snapshot(self) -> Optional[dict]:
        if not self.enabled:
            return None

        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                data, _ = await self._get_file(http)
                return data
        except Exception as exc:
            logger.exception("Could not load data from GitHub: %s", exc)
            return None

    async def save_snapshot(self, session: AsyncSession) -> bool:
        if not self.enabled:
            return False

        import aiohttp

        async with self._lock:
            users = (await session.execute(select(User))).scalars().all()
            tasks = (await session.execute(select(Task))).scalars().all()
            submissions = (
                await session.execute(select(TaskSubmission))
            ).scalars().all()
            withdrawals = (
                await session.execute(select(Withdrawal))
            ).scalars().all()
            settings = (await session.execute(select(Setting))).scalars().all()

            snapshot = {
                "version": 2,
                "saved_at": datetime.utcnow().isoformat(),
                "users": [
                    {
                        "id": u.id,
                        "telegram_id": u.telegram_id,
                        "username": u.username,
                        "balance_active": u.balance_active,
                        "balance_locked": u.balance_locked,
                        "created_at": u.created_at.isoformat(),
                    }
                    for u in users
                ],
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description,
                        "reward_gmp": t.reward_gmp,
                        "is_active": t.is_active,
                        "created_at": t.created_at.isoformat(),
                    }
                    for t in tasks
                ],
                "submissions": [
                    {
                        "id": x.id,
                        "user_id": x.user_id,
                        "task_id": x.task_id,
                        "reward_gmp_snapshot": x.reward_gmp_snapshot,
                        "status": x.status.value,
                        "proof_type": x.proof_type,
                        "proof_data": x.proof_data,
                        "created_at": x.created_at.isoformat(),
                    }
                    for x in submissions
                ],
                "withdrawals": [
                    {
                        "id": w.id,
                        "user_id": w.user_id,
                        "amount_gmp": w.amount_gmp,
                        "amount_money": w.amount_money,
                        "requisites": w.requisites,
                        "status": w.status.value,
                        "created_at": w.created_at.isoformat(),
                    }
                    for w in withdrawals
                ],
                "settings": [
                    {"key": x.key, "value": x.value}
                    for x in settings
                ],
            }

            raw = json.dumps(
                snapshot, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
            encoded = base64.b64encode(raw).decode("ascii")

            timeout = aiohttp.ClientTimeout(total=60)
            last_error = None

            for attempt in range(1, 4):
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as http:
                        _, sha = await self._get_file(http)

                        body = {
                            "message": "chore: save GMP bot data",
                            "content": encoded,
                            "branch": self.branch,
                        }
                        if sha:
                            body["sha"] = sha

                        async with http.put(
                            self.url,
                            headers=self._headers(),
                            json=body,
                        ) as response:
                            if response.status in (200, 201):
                                return True
                            response_body = await response.text()
                            last_error = RuntimeError(
                                f"GitHub PUT failed: HTTP {response.status}: {response_body[:300]}"
                            )
                except Exception as exc:
                    last_error = exc

                await asyncio.sleep(attempt)

            logger.error("GitHub save failed after 3 attempts: %s", last_error)
            return False


github_persistence = GitHubPersistence()


async def restore_from_github(session: AsyncSession) -> bool:
    counts = {
        "users": (await session.execute(select(func.count(User.id)))).scalar_one(),
        "tasks": (await session.execute(select(func.count(Task.id)))).scalar_one(),
        "submissions": (
            await session.execute(select(func.count(TaskSubmission.id)))
        ).scalar_one(),
        "withdrawals": (
            await session.execute(select(func.count(Withdrawal.id)))
        ).scalar_one(),
    }

    if any(counts.values()):
        return False

    snapshot = await github_persistence.load_snapshot()
    if not snapshot:
        return False

    try:
        for item in snapshot.get("users", []):
            session.add(
                User(
                    id=int(item["id"]),
                    telegram_id=int(item["telegram_id"]),
                    username=item.get("username"),
                    balance_active=int(item.get("balance_active", 0)),
                    balance_locked=int(item.get("balance_locked", 0)),
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
            )

        for item in snapshot.get("tasks", []):
            session.add(
                Task(
                    id=int(item["id"]),
                    title=item["title"],
                    description=item["description"],
                    reward_gmp=int(item["reward_gmp"]),
                    is_active=bool(item.get("is_active", True)),
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
            )

        for item in snapshot.get("submissions", []):
            session.add(
                TaskSubmission(
                    id=int(item["id"]),
                    user_id=int(item["user_id"]),
                    task_id=int(item["task_id"]),
                    reward_gmp_snapshot=int(item["reward_gmp_snapshot"]),
                    status=SubmissionStatus(item["status"]),
                    proof_type=item["proof_type"],
                    proof_data=item["proof_data"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
            )

        for item in snapshot.get("withdrawals", []):
            session.add(
                Withdrawal(
                    id=int(item["id"]),
                    user_id=int(item["user_id"]),
                    amount_gmp=int(item["amount_gmp"]),
                    amount_money=float(item["amount_money"]),
                    requisites=item["requisites"],
                    status=WithdrawalStatus(item["status"]),
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
            )

        for item in snapshot.get("settings", []):
            session.add(Setting(key=item["key"], value=item["value"]))

        await session.commit()
        logger.info("Database restored from GitHub snapshot.")
        return True
    except Exception:
        await session.rollback()
        logger.exception("GitHub restore failed.")
        return False


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        try:
            restored = await restore_from_github(session)

            if await session.get(Setting, "gmp_rate") is None:
                session.add(Setting(key="gmp_rate", value="1.00"))

            if await session.get(Setting, "min_withdraw") is None:
                session.add(Setting(key="min_withdraw", value="500"))

            await session.commit()

            if not restored:
                await github_persistence.save_snapshot(session)
        except Exception:
            await session.rollback()
            raise


# ============================================================
# 4. REPOSITORY
# ============================================================

class Repository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def commit(self) -> None:
        await self.session.commit()
        await github_persistence.save_snapshot(self.session)

    async def get_or_create_user(
        self, telegram_id: int, username: Optional[str]
    ) -> User:
        result = await self.session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                balance_active=0,
                balance_locked=0,
            )
            self.session.add(user)
            try:
                await self.commit()
                await self.session.refresh(user)
            except IntegrityError:
                await self.session.rollback()
                result = await self.session.execute(
                    select(User).where(User.telegram_id == telegram_id)
                )
                user = result.scalar_one()
        elif user.username != username:
            user.username = username
            await self.commit()

        return user

    async def get_setting(self, key: str, default: str) -> str:
        result = await self.session.execute(
            select(Setting.value).where(Setting.key == key)
        )
        value = result.scalar_one_or_none()
        return value if value is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        try:
            setting = await self.session.get(Setting, key)
            if setting is None:
                self.session.add(Setting(key=key, value=value))
            else:
                setting.value = value
            await self.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def get_stats(self) -> dict:
        total_users = (
            await self.session.execute(select(func.count(User.id)))
        ).scalar_one()

        total_tasks = (
            await self.session.execute(select(func.count(Task.id)))
        ).scalar_one()

        pending_subs = (
            await self.session.execute(
                select(func.count(TaskSubmission.id)).where(
                    TaskSubmission.status == SubmissionStatus.PENDING
                )
            )
        ).scalar_one()

        pending_wths = (
            await self.session.execute(
                select(func.count(Withdrawal.id)).where(
                    Withdrawal.status == WithdrawalStatus.PENDING
                )
            )
        ).scalar_one()

        total_gmp = (
            await self.session.execute(
                select(func.coalesce(func.sum(User.balance_active), 0))
            )
        ).scalar_one()

        return {
            "total_users": total_users,
            "total_tasks": total_tasks,
            "pending_subs": pending_subs,
            "pending_wths": pending_wths,
            "total_gmp": total_gmp,
        }

    async def get_available_tasks_for_user(
        self, user_id: int
    ) -> Sequence[Task]:
        sub_query = select(TaskSubmission.task_id).where(
            and_(
                TaskSubmission.user_id == user_id,
                TaskSubmission.status.in_(
                    [SubmissionStatus.PENDING, SubmissionStatus.APPROVED]
                ),
            )
        )

        result = await self.session.execute(
            select(Task)
            .where(
                and_(
                    Task.is_active.is_(True),
                    Task.id.not_in(sub_query),
                )
            )
            .order_by(Task.id.desc())
        )
        return result.scalars().all()

    async def get_task(self, task_id: int) -> Optional[Task]:
        return await self.session.get(Task, task_id)

    async def create_submission(
        self,
        user_id: int,
        task_id: int,
        proof_type: str,
        proof_data: str,
    ) -> Optional[TaskSubmission]:
        try:
            task = await self.session.get(Task, task_id)

            if task is None or not task.is_active:
                await self.session.rollback()
                return None

            existing = await self.session.execute(
                select(TaskSubmission.id).where(
                    and_(
                        TaskSubmission.user_id == user_id,
                        TaskSubmission.task_id == task_id,
                        TaskSubmission.status.in_(
                            [SubmissionStatus.PENDING, SubmissionStatus.APPROVED]
                        ),
                    )
                )
            )
            if existing.scalar_one_or_none() is not None:
                await self.session.rollback()
                return None

            submission = TaskSubmission(
                user_id=user_id,
                task_id=task_id,
                reward_gmp_snapshot=task.reward_gmp,
                status=SubmissionStatus.PENDING,
                proof_type=proof_type,
                proof_data=proof_data,
            )

            self.session.add(submission)
            await self.commit()
            await self.session.refresh(submission)
            return submission

        except Exception:
            await self.session.rollback()
            raise

    async def get_pending_submission(
        self, submission_id: int
    ) -> Optional[TaskSubmission]:
        result = await self.session.execute(
            select(TaskSubmission).where(TaskSubmission.id == submission_id)
        )
        return result.scalar_one_or_none()

    async def approve_submission(
        self, submission_id: int
    ) -> Tuple[bool, str, Optional[TaskSubmission]]:
        try:
            result = await self.session.execute(
                select(TaskSubmission)
                .where(TaskSubmission.id == submission_id)
                .with_for_update()
            )
            submission = result.scalar_one_or_none()

            if (
                submission is None
                or submission.status != SubmissionStatus.PENDING
            ):
                await self.session.rollback()
                return False, "Заявка не найдена или уже обработана.", None

            user = await self.session.get(
                User, submission.user_id, with_for_update=True
            )
            if user is None:
                await self.session.rollback()
                return False, "Пользователь не найден.", None

            user.balance_active += submission.reward_gmp_snapshot
            submission.status = SubmissionStatus.APPROVED

            await self.commit()
            await self.session.refresh(submission)

            return True, "Одобрено.", submission

        except Exception:
            await self.session.rollback()
            raise

    async def reject_submission(
        self, submission_id: int
    ) -> Tuple[bool, str, Optional[TaskSubmission]]:
        try:
            result = await self.session.execute(
                select(TaskSubmission)
                .where(TaskSubmission.id == submission_id)
                .with_for_update()
            )
            submission = result.scalar_one_or_none()

            if (
                submission is None
                or submission.status != SubmissionStatus.PENDING
            ):
                await self.session.rollback()
                return False, "Заявка не найдена или уже обработана.", None

            submission.status = SubmissionStatus.REJECTED
            await self.commit()
            await self.session.refresh(submission)

            return True, "Отклонено.", submission

        except Exception:
            await self.session.rollback()
            raise

    async def create_withdrawal(
        self,
        user_id: int,
        amount_gmp: int,
        requisites: str,
    ) -> Tuple[bool, str, Optional[Withdrawal]]:
        if amount_gmp <= 0:
            return False, "Сумма должна быть больше нуля.", None

        if not requisites.strip():
            return False, "Реквизиты не могут быть пустыми.", None

        try:
            user = await self.session.get(
                User, user_id, with_for_update=True
            )
            if user is None:
                await self.session.rollback()
                return False, "Пользователь не найден.", None

            min_withdraw = int(
                await self.get_setting("min_withdraw", "500")
            )

            try:
                rate = float(await self.get_setting("gmp_rate", "1.00"))
            except ValueError:
                rate = 1.0

            if amount_gmp < min_withdraw:
                await self.session.rollback()
                return (
                    False,
                    f"Минимальный вывод: {min_withdraw} GMP.",
                    None,
                )

            if user.balance_active < amount_gmp:
                await self.session.rollback()
                return (
                    False,
                    f"Недостаточно GMP. Баланс: {user.balance_active}.",
                    None,
                )

            user.balance_active -= amount_gmp
            user.balance_locked += amount_gmp

            withdrawal = Withdrawal(
                user_id=user.id,
                amount_gmp=amount_gmp,
                amount_money=round(amount_gmp * rate, 2),
                requisites=requisites.strip(),
                status=WithdrawalStatus.PENDING,
            )

            self.session.add(withdrawal)
            await self.commit()
            await self.session.refresh(withdrawal)

            return True, "Заявка создана.", withdrawal

        except Exception:
            await self.session.rollback()
            raise

    async def approve_withdrawal(
        self, withdrawal_id: int
    ) -> Tuple[bool, str, Optional[Withdrawal]]:
        try:
            result = await self.session.execute(
                select(Withdrawal)
                .where(Withdrawal.id == withdrawal_id)
                .with_for_update()
            )
            withdrawal = result.scalar_one_or_none()

            if (
                withdrawal is None
                or withdrawal.status != WithdrawalStatus.PENDING
            ):
                await self.session.rollback()
                return False, "Заявка не найдена или уже обработана.", None

            user = await self.session.get(
                User, withdrawal.user_id, with_for_update=True
            )
            if user is None:
                await self.session.rollback()
                return False, "Пользователь не найден.", None

            user.balance_locked = max(
                0, user.balance_locked - withdrawal.amount_gmp
            )
            withdrawal.status = WithdrawalStatus.APPROVED

            await self.commit()
            await self.session.refresh(withdrawal)

            return True, "Вывод одобрен.", withdrawal

        except Exception:
            await self.session.rollback()
            raise

    async def reject_withdrawal(
        self, withdrawal_id: int
    ) -> Tuple[bool, str, Optional[Withdrawal]]:
        try:
            result = await self.session.execute(
                select(Withdrawal)
                .where(Withdrawal.id == withdrawal_id)
                .with_for_update()
            )
            withdrawal = result.scalar_one_or_none()

            if (
                withdrawal is None
                or withdrawal.status != WithdrawalStatus.PENDING
            ):
                await self.session.rollback()
                return False, "Заявка не найдена или уже обработана.", None

            user = await self.session.get(
                User, withdrawal.user_id, with_for_update=True
            )
            if user is None:
                await self.session.rollback()
                return False, "Пользователь не найден.", None

            user.balance_locked = max(
                0, user.balance_locked - withdrawal.amount_gmp
            )
            user.balance_active += withdrawal.amount_gmp
            withdrawal.status = WithdrawalStatus.REJECTED

            await self.commit()
            await self.session.refresh(withdrawal)

            return True, "Вывод отклонён, GMP возвращены.", withdrawal

        except Exception:
            await self.session.rollback()
            raise

    async def get_last_history(
        self, user_id: int, limit: int = 10
    ) -> tuple[list[TaskSubmission], list[Withdrawal]]:
        submissions = (
            await self.session.execute(
                select(TaskSubmission)
                .where(TaskSubmission.user_id == user_id)
                .order_by(desc(TaskSubmission.created_at))
                .limit(limit)
            )
        ).scalars().all()

        withdrawals = (
            await self.session.execute(
                select(Withdrawal)
                .where(Withdrawal.user_id == user_id)
                .order_by(desc(Withdrawal.created_at))
                .limit(limit)
            )
        ).scalars().all()

        return list(submissions), list(withdrawals)


# ============================================================
# 5. MIDDLEWARE
# ============================================================

class DbSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        async with SessionLocal() as session:
            data["repo"] = Repository(session)

            event_user = data.get("event_from_user")
            data["is_admin"] = bool(
                event_user and event_user.id in ADMIN_IDS
            )

            try:
                return await handler(event, data)
            except Exception:
                await session.rollback()
                raise


# ============================================================
# 6. FSM
# ============================================================

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


# ============================================================
# 7. KEYBOARDS
# ============================================================

def main_user_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📋 Задания"),
                KeyboardButton(text="👤 Профиль"),
            ],
            [
                KeyboardButton(text="💰 Баланс"),
                KeyboardButton(text="📜 История"),
            ],
            [
                KeyboardButton(text="💸 Вывод GMP"),
                KeyboardButton(text="ℹ️ Помощь"),
            ],
        ],
        resize_keyboard=True,
    )


def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="admin_stats",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    callback_data="admin_settings",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Создать задание",
                    callback_data="admin_add_task",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Управление заданиями",
                    callback_data="admin_manage_tasks",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📝 Проверка заданий",
                    callback_data="admin_check_tasks",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💸 Заявки на вывод",
                    callback_data="admin_withdraws_menu",
                )
            ],
        ]
    )


# ============================================================
# 8. BOT / DISPATCHER & ADMIN NOTIFICATION HELPER
# ============================================================

dp = Dispatcher(storage=MemoryStorage())
bot_instance: Optional[Bot] = None


def safe_username(message: Message) -> str:
    username = message.from_user.username
    return f"@{username}" if username else "не указан"


async def notify_admins(text: str, photo_id: Optional[str] = None, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """Отправляет уведомления всем администраторам."""
    if not bot_instance:
        return
    for admin_id in ADMIN_IDS:
        with suppress(Exception):
            if photo_id:
                await bot_instance.send_photo(
                    admin_id,
                    photo=photo_id,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
            else:
                await bot_instance.send_message(
                    admin_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )


# -------------------- START / HELP ---------------------------

@dp.message(CommandStart())
async def cmd_start(msg: Message, repo: Repository):
    await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    await msg.answer(
        "👋 <b>Добро пожаловать в GMP!</b>\n\n"
        "Здесь можно выполнять задания, получать GMP и оформлять вывод.",
        reply_markup=main_user_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(msg: Message):
    await msg.answer(
        "<b>ℹ️ Помощь</b>\n\n"
        "📋 <b>Задания</b> — доступные задания.\n"
        "💰 <b>Баланс</b> — доступные и заблокированные GMP.\n"
        "📜 <b>История</b> — последние операции.\n"
        "💸 <b>Вывод GMP</b> — создание заявки на вывод.\n"
        "👤 <b>Профиль</b> — информация об аккаунте.\n\n"
        "Если вы отправили отчёт, дождитесь проверки администратора.",
        parse_mode="HTML",
    )


# -------------------- BALANCE / PROFILE ----------------------

@dp.message(Command("balance"))
@dp.message(F.text == "💰 Баланс")
async def cmd_balance(msg: Message, repo: Repository):
    user = await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    await msg.answer(
        f"💰 <b>Ваш баланс</b>\n\n"
        f"💳 Доступно: <b>{user.balance_active}</b> GMP\n"
        f"🔒 На выводе: <b>{user.balance_locked}</b> GMP",
        parse_mode="HTML",
    )


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профиль")
async def cmd_profile(
    msg: Message,
    repo: Repository,
    is_admin: bool,
):
    user = await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    role = "👑 Администратор" if is_admin else "👤 Пользователь"

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"Статус: {role}\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Логин: {safe_username(msg)}\n"
        f"Баланс: <b>{user.balance_active}</b> GMP\n\n"
        f"🔗 GitHub: <code>{GITHUB_OWNER}/{GITHUB_REPO}</code>\n"
        f"🌿 Ветка: <code>{GITHUB_BRANCH}</code>"
    )

    if is_admin:
        text += "\n\n/admin — панель администратора"

    await msg.answer(text, parse_mode="HTML")


# -------------------- TASKS ----------------------------------

@dp.message(Command("tasks"))
@dp.message(F.text == "📋 Задания")
async def list_tasks(msg: Message, repo: Repository):
    user = await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    tasks = await repo.get_available_tasks_for_user(user.id)

    if not tasks:
        await msg.answer("🎉 Сейчас нет доступных заданий.")
        return

    rows = [
        [
            InlineKeyboardButton(
                text=f"🔹 {task.title} — {task.reward_gmp} GMP",
                callback_data=f"view_task:{task.id}",
            )
        ]
        for task in tasks
    ]

    await msg.answer(
        "📋 <b>Доступные задания:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("view_task:"))
async def view_task(call: CallbackQuery, repo: Repository):
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    task = await repo.get_task(task_id)

    if task is None or not task.is_active:
        await call.answer("Задание недоступно.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Выполнить",
                    callback_data=f"start_task:{task.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ К заданиям",
                    callback_data="back_tasks",
                )
            ],
        ]
    )

    await call.message.edit_text(
        f"📋 <b>{task.title}</b>\n\n"
        f"{task.description}\n\n"
        f"💰 Награда: <b>{task.reward_gmp} GMP</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "back_tasks")
async def back_tasks(call: CallbackQuery, repo: Repository):
    user = await repo.get_or_create_user(
        call.from_user.id,
        call.from_user.username,
    )
    tasks = await repo.get_available_tasks_for_user(user.id)

    if not tasks:
        await call.message.edit_text("🎉 Сейчас нет доступных заданий.")
        await call.answer()
        return

    rows = [
        [
            InlineKeyboardButton(
                text=f"🔹 {task.title} — {task.reward_gmp} GMP",
                callback_data=f"view_task:{task.id}",
            )
        ]
        for task in tasks
    ]

    await call.message.edit_text(
        "📋 <b>Доступные задания:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("start_task:"))
async def start_task(
    call: CallbackQuery,
    state: FSMContext,
    repo: Repository,
):
    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    task = await repo.get_task(task_id)

    if task is None or not task.is_active:
        await call.answer("Задание недоступно.", show_alert=True)
        return

    user = await repo.get_or_create_user(
        call.from_user.id,
        call.from_user.username,
    )

    existing = await repo.session.execute(
        select(TaskSubmission.id).where(
            and_(
                TaskSubmission.user_id == user.id,
                TaskSubmission.task_id == task_id,
                TaskSubmission.status.in_(
                    [SubmissionStatus.PENDING, SubmissionStatus.APPROVED]
                ),
            )
        )
    )

    if existing.scalar_one_or_none() is not None:
        await call.answer(
            "Вы уже отправляли это задание.",
            show_alert=True,
        )
        return

    await state.update_data(current_task_id=task_id)
    await state.set_state(TaskSubmissionFSM.waiting_for_proof)

    await call.message.edit_text(
        "📤 <b>Отправьте отчёт</b>\n\n"
        "Можно отправить фото или текст.",
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(TaskSubmissionFSM.waiting_for_proof)
async def process_proof(
    msg: Message,
    state: FSMContext,
    repo: Repository,
):
    data = await state.get_data()
    task_id = data.get("current_task_id")

    if not task_id:
        await state.clear()
        await msg.answer("❌ Сессия задания потеряна. Откройте задание заново.")
        return

    user = await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    if msg.photo:
        proof_type = "photo"
        proof_data = msg.photo[-1].file_id
    elif msg.text:
        proof_type = "text"
        proof_data = msg.text.strip()
    else:
        await msg.answer("❌ Отправьте фото или текст.")
        return

    submission = await repo.create_submission(
        user.id,
        int(task_id),
        proof_type,
        proof_data,
    )

    await state.clear()

    if submission is None:
        await msg.answer(
            "❌ Не удалось отправить отчёт. Возможно, вы уже отправляли это задание."
        )
        return

    await msg.answer(
        f"⏳ <b>Отчёт #{submission.id} отправлен на проверку.</b>\n"
        "После одобрения GMP автоматически начислятся на баланс.",
        parse_mode="HTML",
    )

    # Отправка уведомления администраторам о новом отчёте по заданию
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"app_s:{submission.id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"rej_s:{submission.id}",
                ),
            ]
        ]
    )

    caption = (
        f"📥 <b>Новый отчёт #{submission.id} на проверку!</b>\n"
        f"👤 Пользователь: <code>{user.telegram_id}</code> (@{user.username or 'нет'})\n"
        f"📋 Задание: <code>#{submission.task_id}</code>\n"
        f"💰 Награда: <b>{submission.reward_gmp_snapshot} GMP</b>\n"
        f"📎 Тип отчёта: <b>{proof_type}</b>"
    )

    if proof_type == "photo":
        await notify_admins(caption, photo_id=proof_data, reply_markup=admin_kb)
    else:
        full_text = f"{caption}\n\n📄 <b>Текст отчёта:</b>\n{proof_data}"
        await notify_admins(full_text, reply_markup=admin_kb)


# -------------------- WITHDRAW -------------------------------

@dp.message(Command("withdraw"))
@dp.message(F.text == "💸 Вывод GMP")
async def withdraw_start(
    msg: Message,
    repo: Repository,
    state: FSMContext,
):
    user = await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    min_withdraw = int(
        await repo.get_setting("min_withdraw", "500")
    )

    if user.balance_active < min_withdraw:
        await msg.answer(
            f"❌ Минимальный вывод: <b>{min_withdraw} GMP</b>\n"
            f"Ваш баланс: <b>{user.balance_active} GMP</b>",
            parse_mode="HTML",
        )
        return

    await state.set_state(WithdrawFSM.waiting_for_amount)

    await msg.answer(
        f"💸 Введите сумму GMP для вывода.\n"
        f"Минимум: <b>{min_withdraw}</b> GMP",
        parse_mode="HTML",
    )


@dp.message(WithdrawFSM.waiting_for_amount)
async def withdraw_amount(
    msg: Message,
    state: FSMContext,
):
    if not msg.text or not msg.text.strip().isdigit():
        await msg.answer("❌ Введите целое положительное число.")
        return

    amount = int(msg.text.strip())

    if amount <= 0:
        await msg.answer("❌ Сумма должна быть больше нуля.")
        return

    await state.update_data(withdraw_amount=amount)
    await state.set_state(WithdrawFSM.waiting_for_requisites)

    await msg.answer(
        "💳 Теперь отправьте реквизиты для выплаты.\n"
        "Не отправляйте сюда пароль, код подтверждения или данные аккаунта Telegram."
    )


@dp.message(WithdrawFSM.waiting_for_requisites)
async def withdraw_reqs(
    msg: Message,
    state: FSMContext,
    repo: Repository,
):
    if not msg.text or not msg.text.strip():
        await msg.answer("❌ Реквизиты не могут быть пустыми.")
        return

    data = await state.get_data()
    amount = int(data.get("withdraw_amount", 0))

    user = await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    ok, error, withdrawal = await repo.create_withdrawal(
        user.id,
        amount,
        msg.text,
    )

    await state.clear()

    if not ok or withdrawal is None:
        await msg.answer(f"❌ {error}")
        return

    await msg.answer(
        f"✅ <b>Заявка #{withdrawal.id} создана.</b>\n"
        f"Сумма: <b>{withdrawal.amount_gmp} GMP</b>\n"
        "Ожидайте проверки администратора.",
        parse_mode="HTML",
    )

    # Уведомление администраторов о заявке на вывод
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить выплату",
                    callback_data=f"app_w:{withdrawal.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"rej_w:{withdrawal.id}",
                )
            ]
        ]
    )

    text_admin = (
        f"💸 <b>Новая заявка на вывод #{withdrawal.id}</b>\n\n"
        f"👤 Пользователь: <code>{user.telegram_id}</code> (@{user.username or 'нет'})\n"
        f"💰 Сумма: <b>{withdrawal.amount_gmp} GMP</b> (К выплате: {withdrawal.amount_money})\n"
        f"💳 Реквизиты: <code>{withdrawal.requisites}</code>"
    )

    await notify_admins(text_admin, reply_markup=admin_kb)


# -------------------- HISTORY --------------------------------

@dp.message(Command("history"))
@dp.message(F.text == "📜 История")
async def history(msg: Message, repo: Repository):
    user = await repo.get_or_create_user(
        msg.from_user.id,
        msg.from_user.username,
    )

    submissions, withdrawals = await repo.get_last_history(user.id)

    lines = ["📜 <b>Последняя история</b>", ""]

    if submissions:
        lines.append("<b>Задания:</b>")
        for sub in submissions[:10]:
            status_map = {
                SubmissionStatus.PENDING: "⏳ на проверке",
                SubmissionStatus.APPROVED: "✅ одобрено",
                SubmissionStatus.REJECTED: "❌ отклонено",
            }
            lines.append(
                f"• #{sub.id} — задание #{sub.task_id} — "
                f"{status_map.get(sub.status, sub.status.value)} "
                f"({sub.reward_gmp_snapshot} GMP)"
            )

    if withdrawals:
        lines.append("")
        lines.append("<b>Выводы:</b>")
        for withdrawal in withdrawals[:10]:
            status_map = {
                WithdrawalStatus.PENDING: "⏳ на проверке",
                WithdrawalStatus.APPROVED: "✅ выплачено",
                WithdrawalStatus.REJECTED: "❌ отклонено",
            }
            lines.append(
                f"• #{withdrawal.id} — {withdrawal.amount_gmp} GMP — "
                f"{status_map.get(withdrawal.status, withdrawal.status.value)}"
            )

    if not submissions and not withdrawals:
        lines.append("История пока пустая.")

    await msg.answer("\n".join(lines), parse_mode="HTML")


# ============================================================
# 9. ADMIN PANEL
# ============================================================

@dp.message(Command("admin"))
async def cmd_admin(msg: Message, is_admin: bool):
    if not is_admin:
        return

    await msg.answer(
        "👑 <b>Панель управления GMP</b>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )


def admin_only(is_admin: bool) -> bool:
    return is_admin


@dp.callback_query(F.data == "admin_stats")
async def adm_stats(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    stats = await repo.get_stats()

    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{stats['total_users']}</b>\n"
        f"📋 Заданий: <b>{stats['total_tasks']}</b>\n"
        f"⏳ На проверке: <b>{stats['pending_subs']}</b>\n"
        f"💸 Выводов на проверке: <b>{stats['pending_wths']}</b>\n"
        f"💰 GMP на активных балансах: <b>{stats['total_gmp']}</b>\n\n"
        f"🔗 GitHub: <code>{GITHUB_OWNER}/{GITHUB_REPO}</code>\n"
        f"🌿 Ветка: <code>{GITHUB_BRANCH}</code>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "admin_settings")
async def adm_settings_menu(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    rate = await repo.get_setting("gmp_rate", "1.00")
    min_withdraw = await repo.get_setting("min_withdraw", "500")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить курс",
                    callback_data="set_rate",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить минимум вывода",
                    callback_data="set_min_w",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="admin_back",
                )
            ],
        ]
    )

    await call.message.edit_text(
        f"⚙️ <b>Настройки</b>\n\n"
        f"📈 Курс: <b>{rate}</b>\n"
        f"🔻 Минимальный вывод: <b>{min_withdraw} GMP</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "admin_back")
async def adm_back(call: CallbackQuery, is_admin: bool):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await call.message.edit_text(
        "👑 <b>Панель управления GMP</b>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "set_rate")
async def set_rate_start(
    call: CallbackQuery,
    state: FSMContext,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(AdminSettingsFSM.waiting_for_rate)
    await call.message.answer(
        "📈 Введите новый курс, например: <code>1.00</code>",
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(AdminSettingsFSM.waiting_for_rate)
async def set_rate_finish(
    msg: Message,
    state: FSMContext,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await state.clear()
        return

    try:
        rate = float((msg.text or "").strip().replace(",", "."))
        if rate <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введите положительное число, например 1.00.")
        return

    await repo.set_setting("gmp_rate", f"{rate:.4f}".rstrip("0").rstrip("."))
    await state.clear()
    await msg.answer("✅ Курс сохранён.")


@dp.callback_query(F.data == "set_min_w")
async def set_min_w_start(
    call: CallbackQuery,
    state: FSMContext,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(AdminSettingsFSM.waiting_for_min_withdraw)
    await call.message.answer("🔻 Введите новый минимум вывода в GMP:")
    await call.answer()


@dp.message(AdminSettingsFSM.waiting_for_min_withdraw)
async def set_min_w_finish(
    msg: Message,
    state: FSMContext,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await state.clear()
        return

    try:
        minimum = int((msg.text or "").strip())
        if minimum <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введите положительное целое число.")
        return

    await repo.set_setting("min_withdraw", str(minimum))
    await state.clear()
    await msg.answer("✅ Минимальный вывод сохранён.")


# -------------------- CREATE TASK ----------------------------

@dp.callback_query(F.data == "admin_add_task")
async def adm_add_task(
    call: CallbackQuery,
    state: FSMContext,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(CreateTaskFSM.title)
    await call.message.answer("➕ Введите название задания:")
    await call.answer()


@dp.message(CreateTaskFSM.title)
async def adm_t1(
    msg: Message,
    state: FSMContext,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await state.clear()
        return

    title = (msg.text or "").strip()

    if not title:
        await msg.answer("❌ Название не может быть пустым.")
        return

    await state.update_data(title=title[:255])
    await state.set_state(CreateTaskFSM.description)
    await msg.answer("📝 Введите описание задания:")


@dp.message(CreateTaskFSM.description)
async def adm_t2(
    msg: Message,
    state: FSMContext,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await state.clear()
        return

    description = (msg.text or "").strip()

    if not description:
        await msg.answer("❌ Описание не может быть пустым.")
        return

    await state.update_data(description=description)
    await state.set_state(CreateTaskFSM.reward)
    await msg.answer("💰 Введите награду в GMP:")


@dp.message(CreateTaskFSM.reward)
async def adm_t3(
    msg: Message,
    state: FSMContext,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await state.clear()
        return

    try:
        reward = int((msg.text or "").strip())
        if reward <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Награда должна быть положительным целым числом.")
        return

    data = await state.get_data()

    task = Task(
        title=data["title"],
        description=data["description"],
        reward_gmp=reward,
        is_active=True,
    )

    try:
        repo.session.add(task)
        await repo.commit()
        await repo.session.refresh(task)
    except Exception:
        await repo.session.rollback()
        raise

    await state.clear()

    await msg.answer(
        f"✅ Задание <b>#{task.id}</b> создано.\n"
        f"Награда: <b>{reward} GMP</b>",
        parse_mode="HTML",
    )


# -------------------- TASK MANAGEMENT ------------------------

@dp.callback_query(F.data == "admin_manage_tasks")
async def adm_manage_tasks(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    result = await repo.session.execute(
        select(Task).order_by(Task.id.desc())
    )
    tasks = result.scalars().all()

    if not tasks:
        await call.message.edit_text(
            "📋 Заданий пока нет.",
            reply_markup=admin_main_kb(),
        )
        await call.answer()
        return

    rows = []
    for task in tasks:
        status = "🟢" if task.is_active else "🔴"
        rows.append([
            InlineKeyboardButton(
                text=f"{status} #{task.id} {task.title} — {task.reward_gmp} GMP",
                callback_data=f"delete_task_confirm:{task.id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data="admin_back",
        )
    ])

    await call.message.edit_text(
        "🗑 <b>Удаление заданий</b>\n\n"
        "Нажмите на задание, которое хотите удалить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("delete_task_confirm:"))
async def delete_task_confirm(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    task = await repo.get_task(task_id)
    if task is None:
        await call.answer("Задание уже удалено.", show_alert=True)
        await adm_manage_tasks(call, repo, is_admin)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить",
                    callback_data=f"delete_task:{task_id}",
                ),
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data="admin_manage_tasks",
                ),
            ]
        ]
    )

    await call.message.edit_text(
        f"⚠️ <b>Удалить задание?</b>\n\n"
        f"ID: <b>#{task.id}</b>\n"
        f"Название: <b>{task.title}</b>\n"
        f"Награда: <b>{task.reward_gmp} GMP</b>\n\n"
        "Все заявки по этому заданию также будут удалены.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("delete_task:"))
async def delete_task(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        task_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    try:
        task = await repo.get_task(task_id)
        if task is None:
            await call.answer("Задание уже удалено.", show_alert=True)
            await adm_manage_tasks(call, repo, is_admin)
            return

        await repo.session.execute(
            TaskSubmission.__table__.delete().where(
                TaskSubmission.task_id == task_id
            )
        )
        await repo.session.delete(task)
        await repo.session.commit()

    except Exception:
        await repo.session.rollback()
        logger.exception("Failed to delete task #%s", task_id)
        await call.answer(
            "Не удалось удалить задание. Изменения отменены.",
            show_alert=True,
        )
        return

    await call.answer(f"🗑 Задание #{task_id} удалено.")
    await adm_manage_tasks(call, repo, is_admin)


# -------------------- CHECK SUBMISSIONS ----------------------

async def show_next_submission(
    message: Message,
    repo: Repository,
) -> None:
    result = await repo.session.execute(
        select(TaskSubmission)
        .where(TaskSubmission.status == SubmissionStatus.PENDING)
        .order_by(TaskSubmission.id.asc())
        .limit(1)
    )
    submission = result.scalar_one_or_none()

    if submission is None:
        await message.answer("🎉 Все отчёты проверены!")
        return

    user = await repo.session.get(User, submission.user_id)
    user_info = f"<code>{user.telegram_id}</code> (@{user.username or 'нет'})" if user else "Не найден"

    text = (
        f"📝 <b>Заявка #{submission.id}</b>\n"
        f"👤 Пользователь: {user_info}\n"
        f"📋 Задание: <code>#{submission.task_id}</code>\n"
        f"💰 Награда: <b>{submission.reward_gmp_snapshot} GMP</b>\n"
        f"📎 Тип отчёта: <b>{submission.proof_type}</b>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"app_s:{submission.id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"rej_s:{submission.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data="admin_check_tasks",
                ),
            ],
        ]
    )

    if submission.proof_type == "photo":
        await message.answer_photo(
            submission.proof_data,
            caption=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"{text}\n\n"
            f"📄 <b>Отчёт:</b>\n{submission.proof_data}",
            reply_markup=keyboard,
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "admin_check_tasks")
async def adm_check_t(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await show_next_submission(call.message, repo)
    await call.answer()


@dp.callback_query(F.data.startswith("app_s:"))
async def adm_app_sub(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
    bot: Bot,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        submission_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    ok, text, submission = await repo.approve_submission(submission_id)

    if not ok or submission is None:
        await call.answer(text, show_alert=True)
        return

    user = await repo.session.get(User, submission.user_id)

    if user:
        with suppress(Exception):
            await bot.send_message(
                user.telegram_id,
                f"✅ <b>Ваш отчёт #{submission.id} одобрен!</b>\n"
                f"Начислено: <b>{submission.reward_gmp_snapshot} GMP</b>",
                parse_mode="HTML",
            )

    await call.answer("✅ Одобрено.")

    with suppress(TelegramBadRequest):
        await call.message.delete()


@dp.callback_query(F.data.startswith("rej_s:"))
async def adm_rej_sub(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
    bot: Bot,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        submission_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    ok, text, submission = await repo.reject_submission(submission_id)

    if not ok or submission is None:
        await call.answer(text, show_alert=True)
        return

    user = await repo.session.get(User, submission.user_id)

    if user:
        with suppress(Exception):
            await bot.send_message(
                user.telegram_id,
                f"❌ <b>Ваш отчёт #{submission.id} отклонён.</b>\n"
                "GMP за это задание не начислены.",
                parse_mode="HTML",
            )

    await call.answer("❌ Отклонено.")

    with suppress(TelegramBadRequest):
        await call.message.delete()


# -------------------- WITHDRAWAL ADMIN -----------------------

@dp.callback_query(F.data == "admin_withdraws_menu")
async def adm_wth_menu(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    result = await repo.session.execute(
        select(Withdrawal)
        .where(Withdrawal.status == WithdrawalStatus.PENDING)
        .order_by(Withdrawal.id.asc())
        .limit(1)
    )
    withdrawal = result.scalar_one_or_none()

    if withdrawal is None:
        await call.message.edit_text(
            "🎉 Все заявки на вывод обработаны.",
            reply_markup=admin_main_kb(),
        )
        await call.answer()
        return

    user = await repo.session.get(User, withdrawal.user_id)

    text = (
        f"💸 <b>Заявка на вывод #{withdrawal.id}</b>\n\n"
        f"👤 Пользователь: <code>{user.telegram_id if user else 'не найден'}</code> (@{user.username if user else 'нет'})\n"
        f"💰 Сумма: <b>{withdrawal.amount_gmp} GMP</b>\n"
        f"💵 По курсу: <b>{withdrawal.amount_money}</b>\n"
        f"💳 Реквизиты: <code>{withdrawal.requisites}</code>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить выплату",
                    callback_data=f"app_w:{withdrawal.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить и вернуть GMP",
                    callback_data=f"rej_w:{withdrawal.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Следующая",
                    callback_data="admin_withdraws_menu",
                )
            ],
        ]
    )

    await call.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("app_w:"))
async def adm_app_wth(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
    bot: Bot,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        withdrawal_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    ok, text, withdrawal = await repo.approve_withdrawal(withdrawal_id)

    if not ok or withdrawal is None:
        await call.answer(text, show_alert=True)
        return

    user = await repo.session.get(User, withdrawal.user_id)

    if user:
        with suppress(Exception):
            await bot.send_message(
                user.telegram_id,
                f"✅ <b>Вывод #{withdrawal.id} подтверждён!</b>\n"
                f"Сумма: <b>{withdrawal.amount_gmp} GMP</b>",
                parse_mode="HTML",
            )

    await call.answer("✅ Выплата подтверждена.")
    with suppress(TelegramBadRequest):
        await call.message.delete()


@dp.callback_query(F.data.startswith("rej_w:"))
async def adm_rej_wth(
    call: CallbackQuery,
    repo: Repository,
    is_admin: bool,
    bot: Bot,
):
    if not admin_only(is_admin):
        await call.answer("Нет доступа.", show_alert=True)
        return

    try:
        withdrawal_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Ошибка.", show_alert=True)
        return

    ok, text, withdrawal = await repo.reject_withdrawal(withdrawal_id)

    if not ok or withdrawal is None:
        await call.answer(text, show_alert=True)
        return

    user = await repo.session.get(User, withdrawal.user_id)

    if user:
        with suppress(Exception):
            await bot.send_message(
                user.telegram_id,
                f"❌ <b>Вывод #{withdrawal.id} отклонён.</b>\n"
                f"↩️ Возвращено: <b>{withdrawal.amount_gmp} GMP</b>",
                parse_mode="HTML",
            )

    await call.answer("❌ Отклонено, GMP возвращены.")
    with suppress(TelegramBadRequest):
        await call.message.delete()


# ============================================================
# 10. RENDER HEALTH SERVER
# ============================================================

async def health_check(request: web.Request) -> web.Response:
    return web.Response(
        text="GMP Bot is running",
        status=200,
        content_type="text/plain",
    )


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("Render HTTP server started on port %s", port)
    return runner


# ============================================================
# 11. MAIN
# ============================================================

async def main() -> None:
    global bot_instance

    await init_db()

    bot_instance = Bot(token=BOT_TOKEN)

    dp.update.middleware(DbSessionMiddleware())

    runner = await start_web_server()

    try:
        me = await bot_instance.get_me()
        logger.info(
            "Bot started: @%s | admins=%s | DB=%s",
            me.username,
            ADMIN_IDS,
            "PostgreSQL" if DATABASE_URL.startswith("postgresql+") else "SQLite",
        )

        await bot_instance.delete_webhook(drop_pending_updates=False)

        await dp.start_polling(
            bot_instance,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        with suppress(Exception):
            await runner.cleanup()

        with suppress(Exception):
            await bot_instance.session.close()

        with suppress(Exception):
            await engine.dispose()

        bot_instance = None
        logger.info("GMP Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
