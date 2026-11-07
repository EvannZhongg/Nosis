import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from PIL import Image

from agent_core import AgentCancelled, Workspace
from agent_core.tools.context import DesktopResult, DesktopWindow
from agent_runtime.callbacks import RuntimeCallbacks
from agent_runtime.desktop import DesktopCapability
from agent_runtime.desktop.computer import DesktopComputer
from agent_runtime.desktop.pyautogui_input import PyAutoGUIInput
from agent_runtime.turn_runner import TurnRunner
from agent_runtime.desktop.windows import WindowsDesktop


class PlatformInputTest(unittest.TestCase):
    def test_linux_restores_text_only_while_it_owns_the_clipboard(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed):
                backend = PyAutoGUIInput(Mock())
                board = {"text": "old"}
                with (
                    patch.object(sys, "platform", "linux"),
                    patch("agent_runtime.desktop.pyautogui_input.time.sleep"),
                    patch("pyperclip.paste", side_effect=lambda: board["text"]),
                    patch("pyperclip.copy", side_effect=lambda text: board.update(text=text)),
                ):
                    backend.type_text("agent text")
                    self.assertEqual(board["text"], "agent text")
                    if changed:
                        board["text"] = "user text"
                    backend.release_pending_input()

                self.assertEqual(board["text"], "user text" if changed else "old")
                backend._api.hotkey.assert_called_once_with("ctrl", "v")

    def test_macos_preserves_rich_clipboard_items_and_checks_ownership(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed):
                api = Mock()
                backend = PyAutoGUIInput(api)
                board = Mock()
                board.changeCount.side_effect = [10, 10, 12 if changed else 11]
                board.clearContents.return_value = 11
                item = Mock()
                source = Mock()
                source.types.return_value = ["public.rtf", "public.utf8-plain-text"]
                source.dataForType_.side_effect = [b"rich", b"plain"]
                board.pasteboardItems.return_value = [source]
                appkit = SimpleNamespace(
                    NSPasteboard=Mock(generalPasteboard=Mock(return_value=board)),
                    NSPasteboardItem=Mock(alloc=Mock(return_value=Mock(init=Mock(return_value=item)))),
                    NSPasteboardTypeString="public.utf8-plain-text",
                )
                with (
                    patch.object(sys, "platform", "darwin"),
                    patch.dict(sys.modules, {"AppKit": appkit}),
                    patch("agent_runtime.desktop.pyautogui_input.time.sleep"),
                ):
                    backend.type_text("こんにちは")
                    backend.release_pending_input()

                api.hotkey.assert_called_once_with("command", "v")
                self.assertEqual(item.setData_forType_.call_args_list, [
                    call(b"rich", "public.rtf"), call(b"plain", "public.utf8-plain-text"),
                ])
                if changed:
                    board.writeObjects_.assert_not_called()
                    board.clearContents.assert_called_once()
                else:
                    board.writeObjects_.assert_called_once_with([item])

    def test_pyautogui_rejects_keys_without_a_platform_mapping(self) -> None:
        api = Mock(platformModule=Mock(keyboardMapping={"command": None, "v": 86}))
        backend = PyAutoGUIInput(api)
        with self.assertRaisesRegex(ValueError, "unsupported computer key: command"):
            backend.keys(("command", "v"))
        api.hotkey.assert_not_called()


class _FakeMss:
    def __init__(self, factory: "_FakeMssFactory") -> None:
        self._factory = factory
        self.monitors = [dict(monitor) for monitor in factory.monitors]

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def grab(self, monitor):
        scale = self._factory.scale
        width = monitor["width"] * scale
        height = monitor["height"] * scale
        color = (
            (monitor["left"] + 256) % 256,
            (monitor["top"] + 256) % 256,
            100 + self._factory.tint,
        )
        image = Image.new("RGB", (width, height), color)
        return SimpleNamespace(size=image.size, rgb=image.tobytes())


class _FakeMssFactory:
    def __init__(self, monitors, *, scale=1) -> None:
        self.monitors = monitors
        self.scale = scale
        self.tint = 0

    def __call__(self):
        return _FakeMss(self)


class DesktopComputerTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Workspace(Path(directory.name))
        self.monitors = [
            {"left": -100, "top": -50, "width": 300, "height": 150},
            {"left": -100, "top": -50, "width": 100, "height": 100},
            {
                "left": 0,
                "top": 0,
                "width": 200,
                "height": 100,
                "is_primary": True,
            },
        ]
        self.screenshots = _FakeMssFactory(self.monitors, scale=2)
        self.pyautogui = Mock(platformModule=Mock(keyboardMapping={"enter": 13, "ctrl": 17, "v": 86}))
        self.pyautogui.size.return_value = (200, 100)
        self.pyautogui.position.return_value = (50, 0)
        self.pyautogui.FAILSAFE_POINTS = []
        platform = patch("agent_runtime.desktop.computer.sys.platform", "win32")
        platform.start()
        self.addCleanup(platform.stop)

    def computer(self) -> DesktopComputer:
        return DesktopComputer(
            self.workspace,
            _screenshot_factory=self.screenshots,
            _pyautogui=self.pyautogui,
        )

    def test_normalizes_each_display_into_virtual_desktop_coordinates(self) -> None:
        computer = self.computer()

        path = computer.capture("desktop")

        with Image.open(path.screenshot) as image:
            self.assertEqual(image.size, (300, 150))
            self.assertEqual(image.getpixel((10, 10)), (156, 206, 100))
            self.assertEqual(image.getpixel((150, 50)), (0, 0, 100))

    def test_maps_non_negative_screenshot_coordinates_to_negative_os_coordinates(self) -> None:
        computer = self.computer()

        computer.control({"action": "click", "x": 10, "y": 10})

        self.pyautogui.click.assert_called_once_with(
            x=-90,
            y=-40,
            button="left",
            clicks=1,
            duration=0.0,
        )

    def test_rejects_union_area_that_is_not_on_a_display(self) -> None:
        computer = self.computer()

        with self.assertRaisesRegex(ValueError, "outside every display"):
            computer.control({"action": "click", "x": 10, "y": 120})

        self.pyautogui.click.assert_not_called()

    def test_drag_scroll_wait_and_held_mouse_actions_are_complete(self) -> None:
        computer = self.computer()
        with patch("agent_runtime.desktop.computer.time.sleep") as sleep:
            computer.control(
                {
                    "action": "drag",
                    "x": 10,
                    "y": 10,
                    "to_x": 150,
                    "to_y": 50,
                    "button": "right",
                    "duration": 0.75,
                }
            )
            computer.control({"action": "scroll", "x": 150, "y": 50, "amount": -3})
            computer.control({"action": "wait", "seconds": 1.25})
            computer.control(
                {"action": "mouse_down", "x": 150, "y": 50, "button": "left"}
            )
            computer.release_input()

        self.pyautogui.dragTo.assert_called_once_with(
            50,
            0,
            duration=0.75,
            button="right",
            mouseDownUp=False,
        )
        self.pyautogui.platformModule._scroll.assert_called_once_with(-3, 50, 0)
        sleep.assert_called_once_with(1.25)
        self.assertEqual(self.pyautogui.mouseDown.call_args_list, [
            call(x=-90, y=-40, button="right", duration=0.0),
            call(x=50, y=0, button="left", duration=0.0),
        ])
        self.assertEqual(
            self.pyautogui.platformModule._mouseUp.call_args_list,
            [call(50, 0, "right"), call(50, 0, "left")],
        )
        self.assertIn(call(50, 0, duration=0.0), self.pyautogui.moveTo.call_args_list)

    def test_move_and_mouse_up_emit_drag_motion_while_button_is_held(self) -> None:
        computer = self.computer()

        computer.control({"action": "mouse_down", "x": 150, "y": 50})
        computer.control(
            {"action": "move", "x": 160, "y": 60, "duration": 0.25}
        )
        computer.control({"action": "mouse_up", "x": 170, "y": 70})

        self.assertEqual(
            self.pyautogui.dragTo.call_args_list,
            [
                call(
                    60,
                    10,
                    duration=0.25,
                    button="left",
                    mouseDownUp=False,
                ),
                call(
                    70,
                    20,
                    duration=0.0,
                    button="left",
                    mouseDownUp=False,
                ),
            ],
        )
        self.pyautogui.platformModule._mouseUp.assert_called_once_with(50, 0, "left")

    def test_recovers_only_from_a_failsafe_corner_left_by_the_tool(self) -> None:
        computer = self.computer()
        self.pyautogui.FAILSAFE_POINTS = [
            (0, 0),
            (199, 0),
            (0, 99),
            (199, 99),
        ]

        computer.control({"action": "move", "x": 100, "y": 50})
        self.pyautogui.position.return_value = (0, 0)
        computer.control({"action": "key", "key": "Enter"})

        self.pyautogui.platformModule._moveTo.assert_called_once_with(1, 1)
        self.pyautogui.press.assert_called_once_with("enter")

    def test_does_not_bypass_a_user_owned_failsafe_corner(self) -> None:
        class FailSafeException(Exception):
            pass

        computer = self.computer()
        self.pyautogui.FAILSAFE_POINTS = [(0, 0)]
        self.pyautogui.position.return_value = (0, 0)
        self.pyautogui.press.side_effect = FailSafeException("corner")

        with self.assertRaisesRegex(FailSafeException, "corner"):
            computer.control({"action": "key", "key": "Enter"})

        self.pyautogui.platformModule._moveTo.assert_not_called()

    def test_click_failure_at_corner_is_recoverable_on_the_next_action(self) -> None:
        class FailSafeException(Exception):
            pass

        computer = self.computer()
        self.pyautogui.FAILSAFE_POINTS = [(0, 0)]

        def click_reaches_corner_then_fails(**_kwargs):
            self.pyautogui.position.return_value = (0, 0)
            raise FailSafeException("corner")

        self.pyautogui.click.side_effect = click_reaches_corner_then_fails
        with self.assertRaisesRegex(FailSafeException, "corner"):
            computer.control({"action": "click", "x": 100, "y": 50})

        self.pyautogui.click.side_effect = None
        computer.control({"action": "key", "key": "Enter"})

        self.pyautogui.platformModule._moveTo.assert_called_once_with(1, 1)
        self.pyautogui.press.assert_called_once_with("enter")

    def test_rejects_layout_changes_before_sending_input(self) -> None:
        computer = self.computer()
        self.screenshots.monitors = [
            {"left": 0, "top": 0, "width": 200, "height": 100},
            {
                "left": 0,
                "top": 0,
                "width": 200,
                "height": 100,
                "is_primary": True,
            },
        ]

        with self.assertRaisesRegex(RuntimeError, "layout or resolution changed"):
            computer.control({"action": "click", "x": 10, "y": 10})

        self.pyautogui.click.assert_not_called()

    def test_rejects_screenshot_scale_changes(self) -> None:
        computer = self.computer()
        self.screenshots.scale = 1

        with self.assertRaisesRegex(RuntimeError, "screenshot scale changed"):
            computer.capture("changed")

    def test_startup_rejects_input_and_screenshot_dimension_mismatch(self) -> None:
        self.pyautogui.size.return_value = (100, 50)

        with self.assertRaisesRegex(RuntimeError, "coordinate systems disagree"):
            self.computer()

    def test_key_actions_validate_and_normalize_platform_keys(self) -> None:
        computer = self.computer()

        computer.control({"action": "key", "key": "Enter"})
        computer.control({"action": "hotkey", "keys": ["Control", "V"]})

        self.pyautogui.press.assert_called_once_with("enter")
        self.pyautogui.hotkey.assert_called_once_with("ctrl", "v")

    def test_releases_held_button_even_when_observation_fails(self) -> None:
        computer = self.computer()
        with patch.object(computer, "_save", side_effect=OSError("PNG unavailable")):
            result = computer.control({"action": "mouse_down", "x": 150, "y": 50})
        self.assertIsNone(result.screenshot)
        self.assertIn("Action ran", result.observation_error)
        computer.release_input()
        self.pyautogui.platformModule._mouseUp.assert_called_once_with(50, 0, "left")

    def test_no_screenshot_skips_post_capture_without_replacing_reference(self) -> None:
        computer = self.computer()
        shot = computer.capture("desktop")
        source = shot.screenshot.relative_to(self.workspace.path).as_posix()
        self.screenshots.tint = 90
        with patch.object(computer, "_capture_image", wraps=computer._capture_image) as capture:
            result = computer.control({
                "action": "key", "key": "enter", "screenshot": False, "observation": source,
            })
        capture.assert_called_once()
        self.assertIsNone(result.screenshot)
        self.assertIsNone(result.screen_changed)
        self.assertTrue(result.observation_changed)

        self.screenshots.tint = 0
        result = computer.control({"action": "key", "key": "enter", "observation": source})
        self.assertFalse(result.observation_changed)
        self.assertFalse(result.screen_changed)

    def test_pre_observation_and_post_action_changes_are_independent(self) -> None:
        computer = self.computer()
        shot = computer.capture("desktop")
        source = shot.screenshot.relative_to(self.workspace.path).as_posix()
        self.pyautogui.press.side_effect = lambda _: setattr(self.screenshots, "tint", 90)
        result = computer.control({"action": "key", "key": "enter", "observation": source})
        self.assertFalse(result.observation_changed)
        self.assertTrue(result.screen_changed)
        with self.assertRaisesRegex(ValueError, "no longer current"):
            computer.control({"action": "key", "key": "enter", "observation": source})

    def window_computer(self):
        self.screenshots.scale = 1
        window = DesktopWindow(100, 200, "Editor", "editor.exe")
        other = DesktopWindow(300, 400, "Overlay", "overlay.exe")
        native = Mock(
            spec=WindowsDesktop,
            failsafe_points=(),
            size=Mock(return_value=(200, 100)),
            virtual_bounds=Mock(return_value=(-100, -50, 300, 150)),
            position=Mock(return_value=(20, 20)),
            resolve_window=Mock(return_value=window),
            window_rect=Mock(return_value=(-80, -40, 50, 35)),
            window_at=Mock(return_value=window),
            foreground=Mock(return_value=other),
            pointer_capture=Mock(return_value=None),
        )
        computer = DesktopComputer(self.workspace, _screenshot_factory=self.screenshots, _windows=native)
        return computer, native, window, other

    def test_window_crop_and_action_use_same_negative_origin(self) -> None:
        computer, native, window, other = self.window_computer()
        shot = computer.capture("editor", "editor")
        self.assertEqual(shot.window, window)
        self.assertEqual(shot.origin, (-80, -40))
        with Image.open(shot.screenshot) as image:
            self.assertEqual(image.size, (50, 35))
        source = shot.screenshot.relative_to(self.workspace.path).as_posix()
        result = computer.control({
            "action": "click", "window": "hwnd:100", "observation": source, "x": 10, "y": 12,
        })
        native.click.assert_called_once_with(-70, -28, "left", 1, 0.0)
        self.assertEqual(result.window_at_point, window)
        self.assertEqual(result.foreground_after, other)

    def test_window_scope_rejects_movement_occlusion_capture_and_wrong_foreground(self) -> None:
        computer, native, window, other = self.window_computer()
        shot = computer.capture("editor", "editor")
        source = shot.screenshot.relative_to(self.workspace.path).as_posix()
        args = {"action": "click", "window": "hwnd:100", "observation": source, "x": 10, "y": 12}
        native.window_rect.return_value = (-75, -40, 50, 35)
        with self.assertRaisesRegex(ValueError, "geometry changed"):
            computer.control(args)
        native.window_rect.return_value = (-80, -40, 50, 35)
        native.window_at.return_value = other
        with self.assertRaisesRegex(ValueError, "pointer location"):
            computer.control(args)
        native.window_at.return_value = window
        native.pointer_capture.return_value = other
        with self.assertRaisesRegex(ValueError, "mouse capture"):
            computer.control(args)
        with self.assertRaisesRegex(ValueError, "foreground"):
            computer.control({"action": "key", "key": "enter", "window": "hwnd:100", "observation": source})
        native.click.assert_not_called()
        native.keys.assert_not_called()
        native.window_rect.return_value = (-20, 0, 50, 35)
        with self.assertRaisesRegex(ValueError, "single display"):
            computer.capture("cross-screen", "editor")

    def test_virtual_input_bounds_are_checked_before_injection(self) -> None:
        computer, native, _, _ = self.window_computer()
        native.virtual_bounds.return_value = (0, 0, 200, 100)
        with self.assertRaisesRegex(RuntimeError, "virtual input bounds disagree"):
            computer.control({"action": "click", "x": 10, "y": 10})
        native.click.assert_not_called()

    def test_unscoped_keys_report_foreground_before_and_after(self) -> None:
        computer, native, window, other = self.window_computer()
        native.foreground.side_effect = [window, other]
        result = computer.control({"action": "hotkey", "keys": ["alt", "tab"], "screenshot": False})
        native.keys.assert_called_once_with(("alt", "tab"))
        self.assertEqual(result.foreground_before, window)
        self.assertEqual(result.foreground_after, other)


class DesktopTurnCleanupTest(unittest.TestCase):
    def test_turn_completion_cancellation_and_failure_release_input(self) -> None:
        for expected, error in (("completed", None), ("cancelled", AgentCancelled()), ("failed", ValueError())):
            with self.subTest(status=expected):
                runner = TurnRunner(RuntimeCallbacks())
                computer = Mock()
                agent = Mock()
                agent.run.side_effect = error
                plane = SimpleNamespace(agent=agent, memory=None, computer=computer, runtime_warnings=())
                result = runner.run_turn("turn", "request", lambda: plane, tuple)
                self.assertEqual(result.status, expected)
                computer.release_input.assert_called_once_with()
                self.assertIsNone(runner.turn_id)


class DesktopCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Mock()

    def test_builds_the_desktop_on_first_use_and_reuses_it(self) -> None:
        with patch("agent_runtime.desktop.computer.DesktopComputer") as computer_class:
            capability = DesktopCapability(self.workspace, input_enabled=True)
            computer_class.assert_not_called()
            shot = DesktopResult(screenshot=Path("shot.png"))
            computer_class.return_value.capture.return_value = shot

            self.assertEqual(capability.capture("desktop"), shot)
            capability.control({"action": "wait"})

        computer_class.assert_called_once_with(self.workspace, input_enabled=True)
        computer_class.return_value.capture.assert_called_once_with("desktop", None)
        computer_class.return_value.control.assert_called_once_with(
            {"action": "wait"}
        )

    def test_close_does_not_build_an_unused_desktop(self) -> None:
        with patch("agent_runtime.desktop.computer.DesktopComputer") as computer_class:
            DesktopCapability(self.workspace, input_enabled=False).close()

        computer_class.assert_not_called()

    def test_close_closes_a_desktop_built_on_demand(self) -> None:
        with patch("agent_runtime.desktop.computer.DesktopComputer") as computer_class:
            capability = DesktopCapability(self.workspace, input_enabled=True)
            capability.capture("desktop")

            capability.close()
            capability.close()

        computer_class.return_value.release_input.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
