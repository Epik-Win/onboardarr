"""Supported languages for first-contact Plex/Wizarr invitation messages."""
from __future__ import annotations

from typing import Optional


INVITE_LANGUAGE_LABELS = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
}

_LANGUAGE_ALIASES = {
    **{code: code for code in INVITE_LANGUAGE_LABELS},
    "english": "en",
    "espanol": "es",
    "español": "es",
    "spanish": "es",
    "francais": "fr",
    "français": "fr",
    "french": "fr",
    "deutsch": "de",
    "german": "de",
}

_INVITE_TEXT = {
    "en": {
        "opening": "Please join our {plex_name} Plex/Wizarr server first!",
        "heading": "To Join, Here is Your {plex_name} Plex/Wizarr Invite:",
        "temporary": "This Plex invite grants access for {days} days.",
        "unlimited": "This Plex invite grants indefinite access.",
        "unavailable": "I generated the invite, but could not format its URL. Please contact the admin.",
    },
    "es": {
        "opening": "¡Primero, únete a nuestro servidor {plex_name} de Plex/Wizarr!",
        "heading": "Para unirte, aquí tienes tu invitación de Plex/Wizarr a {plex_name}:",
        "temporary": "Esta invitación de Plex te da acceso durante {days} días.",
        "unlimited": "Esta invitación de Plex te da acceso sin límite de tiempo.",
        "unavailable": "Generé la invitación, pero no pude preparar su enlace. Ponte en contacto con el administrador.",
    },
    "fr": {
        "opening": "Rejoignez d'abord notre serveur Plex/Wizarr {plex_name} !",
        "heading": "Pour nous rejoindre, voici votre invitation Plex/Wizarr pour {plex_name} :",
        "temporary": "Cette invitation Plex vous donne accès pendant {days} jours.",
        "unlimited": "Cette invitation Plex vous donne un accès sans limite de durée.",
        "unavailable": "J'ai créé l'invitation, mais je n'ai pas pu préparer son lien. Contactez l'administrateur.",
    },
    "de": {
        "opening": "Tritt zuerst unserem Plex/Wizarr-Server {plex_name} bei!",
        "heading": "Hier ist deine Plex/Wizarr-Einladung für {plex_name}:",
        "temporary": "Diese Plex-Einladung gewährt dir {days} Tage Zugriff.",
        "unlimited": "Diese Plex-Einladung gewährt dir zeitlich unbegrenzten Zugriff.",
        "unavailable": "Die Einladung wurde erstellt, aber ihr Link konnte nicht vorbereitet werden. Bitte wende dich an den Administrator.",
    },
}


def normalize_invite_language(value: str) -> str:
    language = _LANGUAGE_ALIASES.get(value.strip().casefold())
    if language is None:
        supported = ", ".join(
            f"{code} ({label})" for code, label in INVITE_LANGUAGE_LABELS.items()
        )
        raise ValueError(f"Unsupported language. Choose one of: {supported}")
    return language


def parse_invite_options(argument: str) -> tuple[Optional[int], str]:
    """Parse Telegram invite arguments such as ``14 es`` or ``language=fr``."""
    duration_days: Optional[int] = None
    language = "en"
    language_seen = False

    for token in argument.split():
        key, separator, value = token.partition("=")
        if separator and key.casefold() == "days":
            token = value
        elif separator and key.casefold() == "language":
            token = value
        elif separator:
            raise ValueError("Use /plex-invite [days] [language].")

        if token.isdecimal():
            if duration_days is not None:
                raise ValueError("Specify the duration only once.")
            duration_days = int(token)
            if duration_days < 1:
                raise ValueError("Invite duration must be at least one day.")
            continue

        if language_seen:
            raise ValueError("Specify the language only once.")
        language = normalize_invite_language(token)
        language_seen = True

    return duration_days, language


def format_plex_invite_message(
    plex_name: str,
    invite_url: str,
    duration_days: Optional[int],
    language: str,
) -> str:
    language = normalize_invite_language(language)
    text = _INVITE_TEXT[language]
    duration = (
        text["temporary"].format(days=duration_days)
        if duration_days is not None
        else text["unlimited"]
    )
    return (
        f"**{text['opening'].format(plex_name=plex_name)}**\n\n"
        f"**{text['heading'].format(plex_name=plex_name)}**\n"
        f"{invite_url}\n{duration}"
    )


def format_plex_invite_unavailable_message(language: str) -> str:
    language = normalize_invite_language(language)
    return _INVITE_TEXT[language]["unavailable"]
