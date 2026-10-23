"""Роутеры бота: клиентская часть и админ-панель.

Порядок подключения в :mod:`main` принципиален: сначала ``admin_router`` —
тогда у администратора его команды и кнопки обрабатываются панелью, а всё, что
панель не перехватила, уходит в ``client_router``.
"""

from bot.handlers.admin import router as admin_router
from bot.handlers.client import router as client_router

__all__ = ["admin_router", "client_router"]
