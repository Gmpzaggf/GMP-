import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from database.connection import init_db
from utils.logger import setup_logger

from handlers.user_main import router as user_main_router
from handlers.user_tasks import router as user_tasks_router
from handlers.user_withdraw import router as user_withdraw_router
from handlers.user_history import router as user_history_router

from handlers.admin_main import router as admin_main_router
from handlers.admin_tasks import router as admin_tasks_router
from handlers.admin_withdraw import router as admin_withdraw_router
from handlers.admin_users import router as admin_users_router


async def main():
    setup_logger()

    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(user_main_router)
    dp.include_router(user_tasks_router)
    dp.include_router(user_withdraw_router)
    dp.include_router(user_history_router)

    dp.include_router(admin_main_router)
    dp.include_router(admin_tasks_router)
    dp.include_router(admin_withdraw_router)
    dp.include_router(admin_users_router)

    print("GMP bot запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
