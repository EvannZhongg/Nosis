"""Open workspace folders through the Windows Explorer Shell interface."""

from pathlib import Path


def open_windows_folder(directory: Path) -> None:
    import pythoncom
    import pywintypes
    import win32con
    import win32gui
    from win32com.client import Dispatch
    from win32com.shell import shell

    # FastAPI executes this on a worker thread, which needs its own COM apartment.
    pythoncom.CoInitialize()
    try:
        folder, _ = shell.SHILCreateFromPath(str(directory), 0)
        # An empty child PIDL opens this folder instead of selecting it in its parent.
        shell.SHOpenFolderAndSelectItems(folder, [()], 0)
        for window in Dispatch("Shell.Application").Windows():
            if Path(window.FullName).name.lower() != "explorer.exe":
                continue
            if Path(window.Document.Folder.Self.Path) != directory:
                continue
            handle = int(window.HWND)
            if win32gui.IsIconic(handle):
                win32gui.ShowWindow(handle, win32con.SW_RESTORE)
            flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
            # The HTTP worker does not own the browser's foreground input queue.
            # Raise the Explorer window, then immediately restore its normal z-order.
            win32gui.SetWindowPos(handle, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            try:
                win32gui.BringWindowToTop(handle)
            finally:
                win32gui.SetWindowPos(handle, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            return
        raise OSError("无法找到工作区的资源管理器窗口。")
    except (pywintypes.error, pywintypes.com_error) as error:
        raise OSError(str(error)) from error
    finally:
        pythoncom.CoUninitialize()
