import unittest

from invite_languages import (
    format_plex_invite_message,
    format_plex_invite_unavailable_message,
    normalize_invite_language,
    parse_invite_options,
)


class InviteLanguageTests(unittest.TestCase):
    def test_language_codes_and_names_normalize(self):
        self.assertEqual(normalize_invite_language("es"), "es")
        self.assertEqual(normalize_invite_language("Français"), "fr")
        self.assertEqual(normalize_invite_language("Deutsch"), "de")

    def test_telegram_options_accept_duration_and_language(self):
        self.assertEqual(parse_invite_options("14 es"), (14, "es"))
        self.assertEqual(parse_invite_options("fr"), (None, "fr"))
        self.assertEqual(parse_invite_options("days=7 language=de"), (7, "de"))
        self.assertEqual(parse_invite_options("language=en"), (None, "en"))

    def test_telegram_options_reject_invalid_or_duplicate_values(self):
        for value in ("0 es", "foo", "7 14", "es fr", "language=pt"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_invite_options(value)

    def test_invite_message_keeps_link_and_localizes_expiry(self):
        cases = {
            "en": "This Plex invite grants access for 14 days.",
            "es": "Esta invitación de Plex te da acceso durante 14 días.",
            "fr": "Cette invitation Plex vous donne accès pendant 14 jours.",
            "de": "Diese Plex-Einladung gewährt dir 14 Tage Zugriff.",
        }
        for language, translated_expiry in cases.items():
            with self.subTest(language=language):
                message = format_plex_invite_message(
                    "Media", "https://example.test/invite/code", 14, language,
                )
                self.assertIn("https://example.test/invite/code", message)
                self.assertIn("Media", message)
                self.assertIn(translated_expiry, message)

    def test_unlimited_and_unavailable_messages_use_selected_language(self):
        self.assertIn(
            "sin límite",
            format_plex_invite_message("Media", "https://example.test/i", None, "es"),
        )
        self.assertIn(
            "Administrator",
            format_plex_invite_unavailable_message("de"),
        )


if __name__ == "__main__":
    unittest.main()
