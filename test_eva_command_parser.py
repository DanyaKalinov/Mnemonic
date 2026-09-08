"""
Юнит-тесты разбора команд Евы.
Запуск: python test_eva_command_parser.py
"""
import unittest

from eva_command_parser import parse_eva_command, split_into_subcommands, USAGE_ERROR, NO_FILENAME_ERROR


class TestEvaCommandParser(unittest.TestCase):

    def test_open_app(self):
        r = parse_eva_command("открой блокнот")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "open_app")
        self.assertEqual(r.params["app"], "блокнот")
        self.assertFalse(r.requires_confirmation)

    def test_open_website_explicit(self):
        r = parse_eva_command("открой сайт youtube.com")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "open_website")
        self.assertEqual(r.params["url"], "youtube.com")

    def test_open_website_domain_only(self):
        r = parse_eva_command("зайди на google.com")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "open_website")

    def test_create_file(self):
        r = parse_eva_command("создай файл notes.txt с текстом купить молоко")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "create_file")
        self.assertEqual(r.params["path"], "notes.txt")
        self.assertEqual(r.params["content"], "купить молоко")
        self.assertTrue(r.requires_confirmation)

    def test_read_file(self):
        r = parse_eva_command("прочитай файл notes.txt")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "read_file")
        self.assertFalse(r.requires_confirmation)

    def test_delete_file_requires_confirmation(self):
        r = parse_eva_command("удали файл notes.txt")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "delete_file")
        self.assertTrue(r.requires_confirmation)

    def test_no_filename_error(self):
        r = parse_eva_command("удали файл")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, NO_FILENAME_ERROR)

    def test_screenshot(self):
        r = parse_eva_command("сделай скриншот")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "screenshot")
        self.assertFalse(r.requires_confirmation)

    def test_list_files(self):
        r = parse_eva_command("покажи файлы в загрузках")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "list_files")
        self.assertEqual(r.params["folder"], "загрузках")

    def test_search_file(self):
        r = parse_eva_command("найди файл отчёт")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "search_file")
        self.assertEqual(r.params["query"], "отчёт")

    def test_edit_file_append(self):
        r = parse_eva_command("допиши в файл notes.txt текст ещё одна строка")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "edit_file")
        self.assertEqual(r.params["mode"], "append")
        self.assertTrue(r.requires_confirmation)

    def test_edit_file_overwrite(self):
        r = parse_eva_command("перепиши файл notes.txt текст новый текст")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "edit_file")
        self.assertEqual(r.params["mode"], "overwrite")
        self.assertTrue(r.requires_confirmation)

    def test_copy_file(self):
        r = parse_eva_command("скопируй файл notes.txt в документы")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "copy_file")
        self.assertEqual(r.params["src"], "notes.txt")
        self.assertEqual(r.params["dst"], "документы")
        self.assertTrue(r.requires_confirmation)

    def test_move_file(self):
        r = parse_eva_command("перенеси файл notes.txt в загрузки")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "move_file")
        self.assertEqual(r.params["src"], "notes.txt")
        self.assertEqual(r.params["dst"], "загрузки")
        self.assertTrue(r.requires_confirmation)

    def test_volume_up(self):
        r = parse_eva_command("сделай громче")
        self.assertEqual(r.action, "set_volume")
        self.assertEqual(r.params["direction"], "up")
        self.assertFalse(r.requires_confirmation)

    def test_volume_down(self):
        r = parse_eva_command("сделай тише")
        self.assertEqual(r.params["direction"], "down")

    def test_volume_mute(self):
        r = parse_eva_command("выключи звук")
        self.assertEqual(r.params["direction"], "mute")

    def test_volume_set_level(self):
        r = parse_eva_command("громкость на 50")
        self.assertEqual(r.params["level"], 50)

    def test_power_shutdown_requires_confirmation(self):
        r = parse_eva_command("выключи компьютер")
        self.assertEqual(r.action, "power_action")
        self.assertEqual(r.params["action"], "shutdown")
        self.assertTrue(r.requires_confirmation)

    def test_power_restart(self):
        r = parse_eva_command("перезагрузи пк")
        self.assertEqual(r.params["action"], "restart")
        self.assertTrue(r.requires_confirmation)

    def test_power_lock(self):
        r = parse_eva_command("заблокируй компьютер")
        self.assertEqual(r.params["action"], "lock")
        self.assertTrue(r.requires_confirmation)

    def test_unknown_command(self):
        r = parse_eva_command("какая сегодня погода")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, USAGE_ERROR)

    def test_type_text(self):
        r = parse_eva_command("напиши купить молоко")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "type_text")
        self.assertEqual(r.params["text"], "купить молоко")
        self.assertFalse(r.requires_confirmation)

    def test_press_key(self):
        r = parse_eva_command("нажми ctrl+s")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "press_key")
        self.assertEqual(r.params["keys"], "ctrl+s")

    def test_type_text_does_not_hijack_file_edit(self):
        # "напиши в файл X текст Y" не должно перехватываться как печать
        # на экран — эта фраза про файл, а не про активное окно.
        r = parse_eva_command("напиши в файл notes.txt текст привет")
        self.assertFalse(r.ok)

    def test_web_search_basic(self):
        r = parse_eva_command("найди информацию о питоне")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "web_search")
        self.assertNotIn("engine", r.params)
        self.assertNotIn("browser", r.params)
        self.assertFalse(r.requires_confirmation)

    def test_web_search_with_engine_yandex(self):
        r = parse_eva_command("найди в яндексе погоду")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "web_search")
        self.assertEqual(r.params["engine"], "yandex")
        self.assertEqual(r.params["query"], "погоду")

    def test_web_search_with_browser(self):
        r = parse_eva_command("поищи котиков через опера")
        self.assertTrue(r.ok)
        self.assertEqual(r.params["browser"], "opera")
        self.assertEqual(r.params["query"], "котиков")

    def test_web_search_yandex_browser_not_confused_with_engine(self):
        r = parse_eva_command("загугли рецепт борща в яндекс браузере")
        self.assertTrue(r.ok)
        self.assertEqual(r.params["browser"], "yandex_browser")
        self.assertNotIn("engine", r.params)

    def test_web_search_default_google(self):
        r = parse_eva_command("найди в интернете котики")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "web_search")
        self.assertEqual(r.params["query"], "котики")
        self.assertNotIn("engine", r.params)
        self.assertFalse(r.requires_confirmation)

    def test_web_search_googli_shortcut(self):
        r = parse_eva_command("погугли лучшие рестораны москвы")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "web_search")
        self.assertEqual(r.params["query"], "лучшие рестораны москвы")

    def test_web_search_yandex_engine(self):
        r = parse_eva_command("найди в яндексе погода")
        self.assertTrue(r.ok)
        self.assertEqual(r.params["engine"], "yandex")
        self.assertEqual(r.params["query"], "погода")

    def test_web_search_with_browser_suffix(self):
        r = parse_eva_command("найди новости через оперу")
        self.assertTrue(r.ok)
        self.assertEqual(r.params["query"], "новости")
        self.assertEqual(r.params["browser"], "opera")

    def test_web_search_engine_and_browser_together(self):
        r = parse_eva_command("найди в яндексе курс доллара через firefox")
        self.assertTrue(r.ok)
        self.assertEqual(r.params["engine"], "yandex")
        self.assertEqual(r.params["browser"], "firefox")
        self.assertEqual(r.params["query"], "курс доллара")

    def test_web_search_does_not_hijack_search_file(self):
        r = parse_eva_command("найди файл отчёт")
        self.assertEqual(r.action, "search_file")

    def test_ask_web_uznay(self):
        r = parse_eva_command("узнай курс доллара сегодня")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ask_web")
        self.assertEqual(r.params["query"], "курс доллара сегодня")
        self.assertFalse(r.requires_confirmation)

    def test_ask_web_aktualnaya_info(self):
        r = parse_eva_command("актуальную информацию о погоде в москве")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ask_web")

    def test_ui_click(self):
        r = parse_eva_command("нажми кнопку сохранить в окне блокнот")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ui_click")
        self.assertEqual(r.params["control"], "сохранить")
        self.assertEqual(r.params["window"], "блокнот")
        self.assertFalse(r.requires_confirmation)

    def test_ui_list_controls(self):
        r = parse_eva_command("какие кнопки в окне калькулятор")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ui_list_controls")
        self.assertEqual(r.params["window"], "калькулятор")

    def test_press_key_still_works_after_ui_click_added(self):
        r = parse_eva_command("нажми ctrl+s")
        self.assertEqual(r.action, "press_key")
        self.assertEqual(r.params["keys"], "ctrl+s")

    def test_close_app_requires_confirmation(self):
        r = parse_eva_command("закрой блокнот")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "close_app")
        self.assertEqual(r.params["window"], "блокнот")
        self.assertTrue(r.requires_confirmation)

    def test_minimize_window(self):
        r = parse_eva_command("сверни браузер")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "minimize_window")
        self.assertEqual(r.params["window"], "браузер")
        self.assertFalse(r.requires_confirmation)

    def test_maximize_window(self):
        r = parse_eva_command("разверни блокнот")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "maximize_window")
        self.assertEqual(r.params["window"], "блокнот")
        self.assertFalse(r.requires_confirmation)

    def test_ui_type_text(self):
        r = parse_eva_command("напиши привет в поле сообщение в окне телеграм")
        self.assertTrue(r.ok)
        self.assertEqual(r.action, "ui_type_text")
        self.assertEqual(r.params["text"], "привет")
        self.assertEqual(r.params["control"], "сообщение")
        self.assertEqual(r.params["window"], "телеграм")
        self.assertFalse(r.requires_confirmation)

    def test_plain_type_text_still_works(self):
        r = parse_eva_command("напиши купить молоко")
        self.assertEqual(r.action, "type_text")
        self.assertEqual(r.params["text"], "купить молоко")

    def test_split_by_and(self):
        parts = split_into_subcommands("открой блокнот и напиши привет")
        self.assertEqual(parts, ["открой блокнот", "напиши привет"])

    def test_split_by_potom_zatem(self):
        parts = split_into_subcommands(
            "запусти блокнот, потом напиши купить молоко, затем нажми ctrl+s"
        )
        self.assertEqual(parts, ["запусти блокнот", "напиши купить молоко", "нажми ctrl+s"])

    def test_no_split_when_and_inside_word(self):
        # "и" внутри "найди" не должно считаться разделителем
        parts = split_into_subcommands("найди файл отчёт")
        self.assertEqual(parts, ["найди файл отчёт"])

    def test_single_command_no_split(self):
        parts = split_into_subcommands("сделай скриншот")
        self.assertEqual(parts, ["сделай скриншот"])


class TestLocalSearchRegressions(unittest.TestCase):
    """Регрессии на реальные баги: 'папку' не распознавалось, 'на компьютере'
    не отрезалось от запроса и уводило в веб-поиск вместо локального."""

    def test_find_folder_on_computer(self):
        r = parse_eva_command("найди на компьютере папку eva_agent")
        self.assertEqual(r.action, "search_file")
        self.assertEqual(r.params["query"], "eva_agent")

    def test_find_file_strips_trailing_scope_phrase(self):
        r = parse_eva_command("найди файл brain bot на компьютере")
        self.assertEqual(r.action, "search_file")
        self.assertEqual(r.params["query"], "brain bot")

    def test_find_file_still_works_as_before(self):
        r = parse_eva_command("найди файл отчёт")
        self.assertEqual(r.action, "search_file")
        self.assertEqual(r.params["query"], "отчёт")

    def test_internet_search_not_hijacked_by_local_search(self):
        r = parse_eva_command("найди в интернете котики")
        self.assertEqual(r.action, "web_search")

    def test_yandex_search_not_hijacked_by_local_search(self):
        r = parse_eva_command("найди в яндексе погоду")
        self.assertEqual(r.action, "web_search")
        self.assertEqual(r.params.get("engine"), "yandex")


class TestOpenFolder(unittest.TestCase):
    """Открытие папки по имени — «открой папку X», «покажи папку X» и
    составные «найди и открой папку X» (регрессия на баг, когда такие фразы
    уходили в open_app и искали программу вместо папки)."""

    def test_open_folder_by_name(self):
        r = parse_eva_command("открой папку Проекты")
        self.assertEqual(r.action, "open_folder")
        self.assertEqual(r.params["name"], "Проекты")

    def test_open_folder_alias(self):
        r = parse_eva_command("открой папку загрузки")
        self.assertEqual(r.action, "open_folder")
        self.assertEqual(r.params["name"], "загрузки")

    def test_show_folder_alias(self):
        r = parse_eva_command("покажи папку загрузки")
        self.assertEqual(r.action, "open_folder")

    def test_find_and_open_folder_is_single_step(self):
        parts = split_into_subcommands("найди и открой папку Проекты")
        self.assertEqual(parts, ["найди и открой папку Проекты"])
        r = parse_eva_command(parts[0])
        self.assertEqual(r.action, "open_folder")
        self.assertEqual(r.params["name"], "Проекты")

    def test_open_file_still_routes_to_file(self):
        r = parse_eva_command("открой файл report.pdf")
        self.assertEqual(r.action, "open_file")
        self.assertEqual(r.params["path"], "report.pdf")

    def test_open_folder_not_confused_with_app(self):
        r = parse_eva_command("открой блокнот")
        self.assertEqual(r.action, "open_app")


if __name__ == "__main__":
    unittest.main(verbosity=2)
