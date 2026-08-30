"""
Юнит-тесты разбора команд Евы.
Запуск: python test_eva_command_parser.py
"""
import unittest

from eva_command_parser import parse_eva_command, USAGE_ERROR, NO_FILENAME_ERROR


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
