"""Verify reveal success depends on the actual folder selection, not process launch."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import native_explorer


class NativeExplorerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = (Path(directory.name) / "作品 1,春&秋.png").resolve()
        self.target.write_bytes(b"test image")
        self.shell = Mock()
        self.window = Mock(HWND=123)
        self.window.Document.Folder.Self.Path = str(self.target.parent)
        self.window.Document.SelectedItems.return_value = [SimpleNamespace(Path=str(self.target))]
        self.shell.Windows.return_value = [self.window]

        @contextmanager
        def session():
            yield self.shell

        for patcher in (
            patch.object(native_explorer, "os", SimpleNamespace(name="nt", path=native_explorer.os.path, environ={})),
            patch.object(native_explorer, "_shell_session", session),
            patch.object(native_explorer.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(native_explorer, "_show_window")
        self.show = patcher.start()
        self.addCleanup(patcher.stop)

    def test_reuses_open_folder_and_selects_only_the_exact_file(self):
        native_explorer.reveal_file(self.target)
        self.window.Document.Folder.ParseName.assert_called_once_with(self.target.name)
        self.window.Document.SelectItem.assert_called_once_with(
            self.window.Document.Folder.ParseName.return_value, 29)
        self.shell.ShellExecute.assert_not_called()
        self.show.assert_called_once_with(123)

    def test_opens_folder_with_path_quoted_and_waits_for_selected_item(self):
        self.shell.Windows.side_effect = [[], [], [self.window]]
        native_explorer.reveal_file(self.target)
        self.assertEqual(self.shell.ShellExecute.call_args.args[1], f'/select,"{self.target}"')
        self.show.assert_called_once_with(123)

    def test_opening_a_process_without_the_right_selection_is_a_failure(self):
        self.window.Document.SelectedItems.return_value = [SimpleNamespace(Path=str(self.target.parent / "other.png"))]
        with patch.object(native_explorer.time, "monotonic", side_effect=[0, 13]):
            with self.assertRaisesRegex(OSError, "未能開啟"):
                native_explorer.reveal_file(self.target)
        self.show.assert_not_called()

    def test_unrelated_and_closed_windows_are_ignored(self):
        unrelated = Mock()
        unrelated.Document.Folder.Self.Path = str(self.target.parent / "unrelated")
        closed = Mock()
        closed.Document.SelectItem.side_effect = RuntimeError("window closed")
        closed.Document.Folder.Self.Path = str(self.target.parent)
        self.shell.Windows.return_value = [unrelated, closed, self.window]
        native_explorer.reveal_file(self.target)
        unrelated.Document.SelectItem.assert_not_called()
        self.show.assert_called_once_with(123)

    def test_shell_launch_error_is_reported(self):
        self.shell.Windows.return_value = []
        self.shell.ShellExecute.side_effect = RuntimeError("Shell launch failed")
        with self.assertRaisesRegex(OSError, "Shell launch failed"):
            native_explorer.reveal_file(self.target)
        self.show.assert_not_called()


class ExplorerWindowTests(unittest.TestCase):
    def test_background_request_restores_the_window_after_minimizing(self):
        user32 = Mock()
        user32.SetForegroundWindow.side_effect = [0, 1]
        with patch.object(native_explorer.ctypes, "WinDLL", create=True, return_value=user32):
            native_explorer._show_window(123)
        self.assertEqual([call.args for call in user32.ShowWindow.call_args_list],
                         [(123, 9), (123, 6), (123, 9)])

    def test_successful_foreground_activation_does_not_minimize_the_window(self):
        user32 = Mock()
        user32.SetForegroundWindow.return_value = 1
        with patch.object(native_explorer.ctypes, "WinDLL", create=True, return_value=user32):
            native_explorer._show_window(123)
        user32.ShowWindow.assert_called_once_with(123, 9)


if __name__ == "__main__":
    unittest.main()
