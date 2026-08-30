"""
Юнит-тесты разбора команд Евы.
Запуск:  python test_eva_command_parser.py
"""
import unittest

from eva_command_parser import parse_eva_command, USAGE_ERROR, NO_FILENAME_ERROR


class TestEvaCommandParser(unittest.TestCase):

    def test_open_app(self):
        result = parse_eva_command("открой блокнот")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "open_app")
        self.assertEqual(result.params["app"], "блокнот")
        self.assertFalse(result.requires_confirmation)

    def test_launch_app_synonym(self):
        result = parse_eva_command("запусти калькулятор")
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "open_app")
        self.assertEqual(result.params["app"], "калькулятор")

    def test_create_file_with_content(self):
        result = parse_eva_command("создай файл notes.txt с текстом купить молоко")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "create_file")
        self.assertEqual(result.params["path"], "notes.txt")
        self.assertEqual(result.params["content"], "купить молоко")
        self.assertTrue(result.requires_confirmation)

    def test_create_file_without_content(self):
        result = parse_eva_command("создай файл empty.txt")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "create_file")
        self.assertEqual(result.params["path"], "empty.txt")
        self.assertEqual(result.params["content"], "")

    def test_read_file(self):
        result = parse_eva_command("прочитай файл notes.txt")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "read_file")
        self.assertEqual(result.params["path"], "notes.txt")
        self.assertFalse(result.requires_confirmation)

    def test_show_file_synonym(self):
        result = parse_eva_command("покажи файл notes.txt")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "read_file")

    def test_delete_file(self):
        result = parse_eva_command("удали файл notes.txt")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "delete_file")
        self.assertEqual(result.params["path"], "notes.txt")
        self.assertTrue(result.requires_confirmation)

    # --- Регрессия: "открой файл X" не должно распознаться как open_app ---
    def test_open_file_is_not_open_app(self):
        result = parse_eva_command("открой файл notes.txt")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.action, "read_file")

    def test_no_filename_error(self):
        result = parse_eva_command("удали файл")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, NO_FILENAME_ERROR)

    def test_unknown_command(self):
        result = parse_eva_command("расскажи анекдот")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, USAGE_ERROR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
