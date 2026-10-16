import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from interfaces.gui.windows_folders import open_windows_folder


@unittest.skipUnless(os.name == "nt", "Windows Explorer integration")
class WindowsFoldersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path("C:/workspace/项目 files")
        target = SimpleNamespace(
            FullName="C:/Windows/explorer.exe", HWND=123,
            Document=SimpleNamespace(Folder=SimpleNamespace(Self=SimpleNamespace(Path=str(self.directory)))),
        )
        other = SimpleNamespace(
            FullName="C:/Windows/explorer.exe", HWND=456,
            Document=SimpleNamespace(Folder=SimpleNamespace(Self=SimpleNamespace(Path="C:/other/项目 files"))),
        )
        self.com_init = self.patch("pythoncom.CoInitialize")
        self.com_uninit = self.patch("pythoncom.CoUninitialize")
        self.parse = self.patch("win32com.shell.shell.SHILCreateFromPath", return_value=("folder-id", 0))
        self.open_shell = self.patch("win32com.shell.shell.SHOpenFolderAndSelectItems")
        self.patch("win32com.client.Dispatch", return_value=SimpleNamespace(Windows=lambda: [other, target]))
        self.iconic = self.patch("win32gui.IsIconic", return_value=False)
        self.show = self.patch("win32gui.ShowWindow")
        self.position = self.patch("win32gui.SetWindowPos")
        self.raise_window = self.patch("win32gui.BringWindowToTop")

    def patch(self, name: str, **kwargs: object) -> Mock:
        patcher = patch(name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_raises_the_window_for_the_full_workspace_path(self) -> None:
        import win32con

        open_windows_folder(self.directory)

        self.parse.assert_called_once_with(str(self.directory), 0)
        self.open_shell.assert_called_once_with("folder-id", [()], 0)
        self.raise_window.assert_called_once_with(123)
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
        self.assertEqual(self.position.call_args_list, [
            call(123, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags),
            call(123, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags),
        ])
        self.show.assert_not_called()
        self.com_init.assert_called_once()
        self.com_uninit.assert_called_once()

    def test_restores_a_minimized_window(self) -> None:
        import win32con

        self.iconic.return_value = True
        open_windows_folder(self.directory)
        self.show.assert_called_once_with(123, win32con.SW_RESTORE)

    def test_restores_window_order_and_com_after_an_error(self) -> None:
        import pywintypes
        import win32con

        self.raise_window.side_effect = pywintypes.error(5, "BringWindowToTop", "denied")
        with self.assertRaises(OSError):
            open_windows_folder(self.directory)
        self.assertEqual(self.position.call_args_list[-1].args[1], win32con.HWND_NOTOPMOST)
        self.com_uninit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
