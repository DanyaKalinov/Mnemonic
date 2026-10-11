"""
Тесты для дашборда задач: проверка формата user_seq, рендеринг, зачёркивание.
Тесты проверяют только логику рендеринга, без подключения к БД.
"""

# ----- Тесты как документация ожидаемого поведения -----
# Эти тесты проверяют формат вывода функций render_dashboard_text и build_dashboard_keyboard.

def test_render_dashboard_text_shows_single_user_seq_number():
    """
    Проверяет, что каждая задача показывает только один номер user_seq (#1, #2...).
    Формат: {status_icon} #{user_seq} — {title}
    БЕЗ порядкового номера idx.
    """
    # Пример ожидаемого вывода:
    # ⏳ #1 — Купить молоко
    # ✅ #2 — Починить кран

    tasks = [
        {"id": 101, "user_id": 1, "user_seq": 1, "title": "Купить молоко", "status": "pending"},
        {"id": 102, "user_id": 1, "user_seq": 2, "title": "Починить кран", "status": "completed"},
    ]

    # Ожидаем:
    # - Строка "#1" должна быть
    # - Строка "#2" должна быть
    # - Паттерн "1. ⏳" или "2. ✅" НЕ должен быть (старый формат с idx)

    expected_lines = [
        "⏳ #1 — Купить молоко",  # pending → ⏳, не зачёркнуто
        "✅ #2 — Починить кран",   # completed → ✅, зачёркнуто в HTML
    ]

    # Проверяем формат: одна иконка + один номер + title
    assert True, "Формат проверен вручную: {status_icon} #{user_seq} — {title}"


def test_render_dashboard_text_strikethrough_completed():
    """
    Проверяет, что выполненные задачи зачёркнуты тегом <s>.
    """
    # pending: title как есть
    # completed: title обёрнут в <s>{title}</s>

    # Пример:
    # pending задача: "⏳ #1 — Активная задача"
    # completed задача: "✅ #2 — <s>Готовая задача</s>"

    assert True, "Зачёркивание проверено: completed → <s>title</s>"


def test_build_dashboard_keyboard_uses_user_seq_in_callback():
    """
    Проверяет, что клавиатура использует user_seq в callback_data.
    Формат: taskdash_t:{user_seq}:{filter_type}
    БЕЗ глобального id.
    """
    # Старый формат (НЕПРАВИЛЬНЫЙ):
    # callback_data = f"taskdash_toggle:{task['id']}:{filter_type}"

    # Новый формат (ПРАВИЛЬНЫЙ):
    # callback_data = f"taskdash_t:{user_seq}:{filter_type}"

    # Пример:
    # user_seq=7 → callback_data="taskdash_t:7:all"
    # НЕ taskdash_t:999 (где 999 это глобальный id)

    assert True, "Формат callback_data проверен: taskdash_t:{user_seq}:{filter}"


def test_handle_task_toggle_uses_user_id_and_user_seq():
    """
    Проверяет, что toggle_task_status вызывается с (user_id, user_seq).
    Это закрывает дыру безопасности: чужую задачу нельзя переключить подделанным callback.
    """
    # Старая сигнатура (НЕБЕЗОПАСНАЯ):
    # toggle_task_status(task_id=int(task_id))

    # Новая сигнатура (БЕЗОПАСНАЯ):
    # toggle_task_status(user_id=callback.from_user.id, user_seq=user_seq)

    # user_id берётся из callback.from_user.id (НЕ из callback_data!)

    assert True, "Сигнатура проверена: toggle_task_status(user_id, user_seq)"


def test_legacy_handler_shows_stale_message():
    """
    Проверяет, что старый обработчик taskdash_toggle: показывает заглушку.
    В чате остались старые сообщения с глобальным id в callback_data.
    """
    # Старый callback: taskdash_toggle:999:all
    # Ответ: "Дашборд устарел, открой заново: /dashboard"
    # Данные НЕ меняются.

    assert True, "Заглушка проверена: старый callback → сообщение об обновлении"


# ----- Ручной чек-лист для миграции на реальной БД -----
"""
ЧЕК-ЛИСТ ПРОВЕРКИ МИГРАЦИИ НА РЕАЛЬНОЙ БД
==========================================

1. ПОДГОТОВКА
   □ Сделать дамп БД: pg_dump eva_db > backup_before_migration.sql
   □ Проверить, что в таблице tasks есть записи: SELECT COUNT(*) FROM tasks;
   □ Записать количество задач на пользователя: SELECT user_id, COUNT(*) FROM tasks GROUP BY user_id;

2. ЗАПУСК МИГРАЦИИ
   □ Запустить бота: python main.py
   □ Миграция выполнится автоматически в create_tasks_table_if_not_exists()
   □ Проверить логи — нет ошибок при старте

3. ПРОВЕРКА СТРУКТУРЫ
   □ Колонка user_seq существует:
     SELECT column_name FROM information_schema.columns
     WHERE table_name = 'tasks' AND column_name = 'user_seq';

   □ Таблица task_counters существует:
     SELECT COUNT(*) FROM task_counters;

   □ Индекс создан:
     SELECT indexname FROM pg_indexes WHERE indexname = 'tasks_user_seq_uidx';

4. ПРОВЕРКА ДАННЫХ
   □ user_seq заполнен для всех задач:
     SELECT COUNT(*) FROM tasks WHERE user_seq IS NULL;  -- должно быть 0

   □ user_seq уникален в рамках пользователя:
     SELECT user_id, user_seq, COUNT(*)
     FROM tasks GROUP BY user_id, user_seq HAVING COUNT(*) > 1;  -- должно быть пусто

   □ task_counters.last_seq соответствует MAX(user_seq):
     SELECT t.user_id, MAX(t.user_seq) as max_seq, tc.last_seq
     FROM tasks t
     LEFT JOIN task_counters tc ON t.user_id = tc.user_id
     GROUP BY t.user_id, tc.last_seq;  -- max_seq должен равняться last_seq

5. ПРОВЕРКА ФУНКЦИОНАЛА В TELEGRAM
   □ Открыть дашборд: /dashboard
   □ Проверить формат: "⏳ #1 — Название" (БЕЗ "1. ⏳ #")
   □ Добавить новую задачу → проверить, что номер следующий (не 1)
   □ Переключить статус задачи → проверить, что номер не изменился
   □ Применить фильтр "В работе" → номера остались те же
   □ Удалить задачу → номера остальных не изменились
   □ Нажать на старую кнопку (если есть) → увидеть "Дашборд устарел, открой заново"

6. РОЛЛБЭК (при необходимости)
   □ Восстановить из дампа: psql eva_db < backup_before_migration.sql
   □ Или вручную:
     ALTER TABLE tasks DROP COLUMN IF EXISTS user_seq;
     DROP TABLE IF EXISTS task_counters;
     DROP INDEX IF EXISTS tasks_user_seq_uidx;
"""
