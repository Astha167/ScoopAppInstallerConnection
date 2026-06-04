import unittest
from types import SimpleNamespace

from myscoop.gui_installer import CLICKABLE_CONTROL_TYPES, GUIInstaller


class TestableGUIInstaller(GUIInstaller):
    def __init__(self):
        super().__init__()
        self.window_titles = {}
        self.edit_windows = set()
        self.license_text_windows = set()
        self.submitted = []

    def _get_window_title(self, hwnd: int) -> str:
        return self.window_titles.get(hwnd, "")

    def _bring_to_foreground(self, hwnd: int) -> bool:
        return True

    def _window_has_edit_control(self, hwnd: int) -> bool:
        return hwnd in self.edit_windows

    def _window_contains_license_text(self, hwnd: int) -> bool:
        return hwnd in self.license_text_windows

    def _submit_self_extract_dialog(self, hwnd: int) -> bool:
        self.submitted.append(hwnd)
        return True


class ProcessWindowInstaller(TestableGUIInstaller):
    def __init__(self):
        super().__init__()
        self.visible_windows = set()
        self.window_pids = {}
        self.process_tree = set()
        self.window_rects = {}

    def _get_visible_windows(self):
        return set(self.visible_windows)

    def _get_process_tree_pids(self, root_pid: int):
        return set(self.process_tree or {root_pid})

    def _get_window_pid(self, hwnd: int) -> int:
        return self.window_pids.get(hwnd, 0)

    def _get_window_rect(self, hwnd: int):
        return self.window_rects.get(hwnd, (0, 0, 640, 480))


class GUIInstallerSpecialCaseTests(unittest.TestCase):
    def test_clickable_types_include_radio_and_checkbox_for_option_pages(self):
        self.assertIn("RadioButton", CLICKABLE_CONTROL_TYPES)
        self.assertIn("CheckBox", CLICKABLE_CONTROL_TYPES)

    def test_self_extract_prompt_is_not_treated_as_busy(self):
        installer = TestableGUIInstaller()
        installer.window_titles[101] = "Autodesk Self-Extract"
        installer.edit_windows.add(101)

        self.assertTrue(installer._is_self_extract_prompt(101))
        self.assertFalse(installer._detect_busy_state(101))

    def test_self_extract_prompt_is_handled_once(self):
        installer = TestableGUIInstaller()
        installer.window_titles[202] = "Autodesk Self-Extract"
        installer.edit_windows.add(202)

        self.assertTrue(installer._handle_special_installer_windows(202))
        self.assertEqual(installer.submitted, [202])
        self.assertFalse(installer._handle_special_installer_windows(202))

    def test_abb_status_labels_are_not_scored_as_buttons(self):
        installer = TestableGUIInstaller()

        self.assertEqual(installer._score_button("Download and install"), 85)
        self.assertEqual(installer._score_button("License Agreement"), -1)
        self.assertEqual(
            installer._score_button("Automation Builder Legacy Installation Manager  [2.5.9.227]"),
            -1,
        )
        self.assertEqual(
            installer._score_button("Please wait while the installer initializes ..."),
            -1,
        )

    def test_invisible_formatting_characters_do_not_break_button_matching(self):
        installer = TestableGUIInstaller()

        self.assertEqual(installer._normalize_label("N\u200bext"), "next")
        self.assertGreater(installer._score_button("N\u200bext"), 0)

    def test_busy_text_marks_installation_page_as_progress(self):
        class BusyTextInstaller(TestableGUIInstaller):
            def _has_busy_text(self, hwnd: int) -> bool:
                return hwnd == 303

            def _detect_progress_bar(self, hwnd: int) -> bool:
                return False

        installer = BusyTextInstaller()
        installer.window_titles[303] = "ABB Automation Builder 2.9.0 Build 322 - Installation Page"

        self.assertTrue(installer._detect_busy_state(303))

    def test_selection_page_does_not_use_busy_text_detection(self):
        installer = TestableGUIInstaller()

        self.assertFalse(
            installer._should_use_busy_text(
                "ABB Automation Builder 2.9.0 Build 322 - Selection Page"
            )
        )
        self.assertTrue(
            installer._should_use_busy_text(
                "ABB Automation Builder 2.9.0 Build 322 - Installation Page"
            )
        )

    def test_remove_actions_are_skipped_during_install(self):
        installer = TestableGUIInstaller()

        self.assertEqual(installer._score_button("Remove"), 0)
        self.assertEqual(installer._score_button("Uninstall"), 0)

    def test_license_page_fallback_accepts_first_unchecked_checkbox(self):
        installer = TestableGUIInstaller()

        class FakeRect:
            def __init__(self, top):
                self.top = top

        class FakeCheckbox:
            def __init__(self):
                self.toggled = False

            def rectangle(self):
                return FakeRect(735)

            def get_toggle_state(self):
                return 0

            def toggle(self):
                self.toggled = True

        checkbox = FakeCheckbox()
        dlg = SimpleNamespace(
            descendants=lambda control_type=None: [checkbox] if control_type == "CheckBox" else []
        )

        self.assertTrue(installer._toggle_first_unchecked_option_on_license_page(dlg, 1))
        self.assertTrue(checkbox.toggled)

    def test_license_rejection_text_is_not_scored_as_acceptance(self):
        installer = TestableGUIInstaller()

        self.assertEqual(
            installer._score_button("I do not accept the terms in the License Agreement"),
            0,
        )
        self.assertFalse(
            installer._looks_like_license_acceptance_text(
                "I do not accept the terms in the License Agreement"
            )
        )

    def test_license_page_detection_uses_body_text_when_title_is_generic(self):
        installer = TestableGUIInstaller()
        installer.window_titles[404] = "GP-Viewer EX - InstallShield Wizard"
        installer.license_text_windows.add(404)

        self.assertTrue(installer._is_license_page(404))

    def test_license_fallback_skips_checked_reject_radio_and_selects_accept(self):
        installer = TestableGUIInstaller()

        class FakeRect:
            def __init__(self, top, left=10):
                self.top = top
                self.left = left
                self.right = left + 12

        class FakeRadio:
            def __init__(self, text, state, top):
                self.text = text
                self.state = state
                self.selected = False
                self.top = top

            def rectangle(self):
                return FakeRect(self.top)

            def window_text(self):
                return self.text

            def get_toggle_state(self):
                return self.state

            def select(self):
                self.state = 1
                self.selected = True

        reject = FakeRadio("I do not accept the terms in the License Agreement", 1, 400)
        accept = FakeRadio("I accept the terms in the License Agreement", 0, 420)
        dlg = SimpleNamespace(
            descendants=lambda control_type=None: (
                [reject, accept] if control_type == "RadioButton" else []
            )
        )

        self.assertTrue(installer._toggle_first_unchecked_option_on_license_page(dlg, 1))
        self.assertTrue(accept.selected)
        self.assertFalse(reject.selected)

    def test_wait_accepts_generic_title_owned_by_launched_process(self):
        installer = ProcessWindowInstaller()
        installer.window_titles[505] = "iX Developer"
        installer.window_pids[505] = 1234
        installer.visible_windows = {505}
        installer.process_tree = {1234}

        self.assertEqual(installer._wait_for_installer_window(1234, set()), 505)

    def test_find_all_keeps_tracking_generic_process_window_after_launch(self):
        installer = ProcessWindowInstaller()
        installer._installer_root_pid = 1234
        installer.window_titles[606] = "iX Developer"
        installer.window_pids[606] = 1234
        installer.visible_windows = {606}
        installer.process_tree = {1234}

        self.assertEqual(installer._find_all_installer_windows(), [(606, "iX Developer")])

    def test_find_all_ignores_zero_size_setup_placeholder_window(self):
        installer = ProcessWindowInstaller()
        installer._installer_root_pid = 1234
        installer.window_titles[707] = "Setup"
        installer.window_pids[707] = 1234
        installer.window_rects[707] = (0, 0, 0, 0)
        installer.visible_windows = {707}
        installer.process_tree = {1234}

        self.assertEqual(installer._find_all_installer_windows(), [])

    def test_wait_ignores_zero_size_title_match(self):
        installer = ProcessWindowInstaller()
        installer.window_titles[808] = "Setup"
        installer.window_pids[808] = 1234
        installer.window_rects[808] = (0, 0, 0, 0)
        installer.visible_windows = {808}
        installer.process_tree = {1234}
        installer.max_wait_for_window = 0.01

        self.assertIsNone(installer._wait_for_installer_window(1234, set()))

    def test_title_matches_product_name_from_installer_filename(self):
        installer = TestableGUIInstaller()
        installer._installer_name = "iVMS-4200(V3.14.0.6_E).exe"

        self.assertTrue(installer._is_installer_window("iVMS-4200"))
        self.assertTrue(installer._is_installer_window("iVMS-4200 Setup"))

    def test_extractor_titles_trigger_handoff_wait(self):
        installer = TestableGUIInstaller()

        self.assertTrue(installer._is_extractor_handoff_title("19% Extracting"))
        self.assertFalse(installer._is_extractor_handoff_title("Mozilla Firefox Setup"))

    def test_dashboard_browser_title_is_not_installer_window(self):
        installer = TestableGUIInstaller()

        self.assertFalse(
            installer._is_installer_window(
                "MakingScoop Installer Dashboard - Profile 1 - Microsoft Edge"
            )
        )

    def test_firefox_setup_title_is_installer_window(self):
        installer = TestableGUIInstaller()

        self.assertTrue(installer._is_installer_window("Mozilla Firefox Setup"))

    def test_find_all_ignores_dashboard_browser_window(self):
        installer = ProcessWindowInstaller()
        installer._installer_root_pid = 1234
        installer.window_titles[707] = (
            "MakingScoop Installer Dashboard - Profile 1 - Microsoft Edge"
        )
        installer.window_pids[707] = 9999
        installer.visible_windows = {707}
        installer.process_tree = {1234}

        self.assertEqual(installer._find_all_installer_windows(), [])

    def test_license_radio_is_not_generic_action_button(self):
        installer = TestableGUIInstaller()

        self.assertFalse(
            installer._is_actionable_control(
                object(),
                "RadioButton",
                "I accept the agreement",
            )
        )


if __name__ == "__main__":
    unittest.main()
