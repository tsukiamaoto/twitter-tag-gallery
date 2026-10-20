"""Isolated native-picker tests: no real windows or scheduler jobs are launched."""

import base64
import json
import subprocess
import unittest
from unittest.mock import patch

import native_folder_picker as picker


class NativeFolderPickerTests(unittest.TestCase):
    def run_picker(self, result, initial=""):
        with patch.object(picker.subprocess, "run", return_value=result) as run:
            selected = picker.pick_folder(initial)
        return selected, run.call_args

    def test_unicode_selection_and_paths_are_data_not_code(self):
        path = "D:\\圖片\\繪師 ' $(literal)"
        result = subprocess.CompletedProcess([], 0, json.dumps({"selected_path": path}), "")
        selected, call = self.run_picker(result, path)
        self.assertEqual(selected, path)
        self.assertEqual(call.kwargs["env"]["TAG_GALLERY_PICKER_INITIAL"], path)
        command = call.args[0]
        self.assertNotIn(path, command)
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertNotIn(path, script)
        self.assertIn("0x20u", script)  # FOS_PICKFOLDERS
        self.assertNotIn("FolderBrowserDialog", script)
        self.assertIn("-STA", command)
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["encoding"], "utf-8")

    def test_cancellation_is_an_empty_selection(self):
        selected, _ = self.run_picker(subprocess.CompletedProcess([], 0, '{"selected_path":""}', ""))
        self.assertEqual(selected, "")

    def test_errors_are_not_reported_as_cancellation(self):
        for result in [subprocess.CompletedProcess([], 1, "", "failed"),
                       subprocess.CompletedProcess([], 0, "bad JSON", ""),
                       subprocess.CompletedProcess([], 0, '{"selected_path":null}', "")]:
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                self.run_picker(result)
        self.assertFalse(picker._picker_lock.locked())

    def test_timeout_releases_the_picker_lock(self):
        with patch.object(picker.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 300)):
            with self.assertRaisesRegex(RuntimeError, "逾時"):
                picker.pick_folder()
        self.assertFalse(picker._picker_lock.locked())

    def test_only_one_picker_can_be_open_at_once(self):
        picker._picker_lock.acquire()
        try:
            with patch.object(picker.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "已開啟"):
                    picker.pick_folder()
                run.assert_not_called()
        finally:
            picker._picker_lock.release()


if __name__ == "__main__":
    unittest.main()
