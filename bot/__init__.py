"""Бот компьютерного клуба «Admiral» (aiogram 3.x + aiosqlite).

Пакет содержит всю логику приложения:

* :mod:`bot.config`       — загрузка настроек из ``.env``;
* :mod:`bot.texts`        — все тексты бота (прайс, правила, FAQ, 152-ФЗ, шаблоны);
* :mod:`bot.states`       — FSM-состояния сценария бронирования и навигации;
* :mod:`bot.db`           — схема и репозиторий SQLite (aiosqlite);
* :mod:`bot.ui`           — экраны в стиле HUD, навигатор и отрисовка;
* :mod:`bot.keyboards`    — разбор ``callback_data`` и совместимость;
* :mod:`bot.filters`      — фильтры (админ, приватный чат);
* :mod:`bot.middlewares`  — middleware согласия на обработку ПДн (152-ФЗ);
* :mod:`bot.services`     — чистая бизнес-логика (нормализация данных, рассылки);
* :mod:`bot.handlers`     — обработчики клиента и админ-панели.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
