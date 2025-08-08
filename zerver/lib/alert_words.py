from collections.abc import Iterable

import ahocorasick
from django.db import transaction

from zerver.lib.cache import (
    cache_with_key,
    realm_alert_words_automaton_cache_key,
    realm_alert_words_cache_key,
)
from zerver.models import AlertWord, Realm, UserProfile
from zerver.models.alert_words import flush_realm_alert_words


@cache_with_key(lambda realm: realm_alert_words_cache_key(realm.id), timeout=3600 * 24)
def alert_words_in_realm(realm: Realm) -> dict[int, list[str]]:
    user_ids_and_words = AlertWord.objects.filter(
        realm=realm, user_profile__is_active=True, deactivated=False
    ).values("user_profile_id", "word")
    user_ids_with_words: dict[int, list[str]] = {}
    for id_and_word in user_ids_and_words:
        user_ids_with_words.setdefault(id_and_word["user_profile_id"], [])
        user_ids_with_words[id_and_word["user_profile_id"]].append(id_and_word["word"])
    return user_ids_with_words


@cache_with_key(lambda realm: realm_alert_words_automaton_cache_key(realm.id), timeout=3600 * 24)
def get_alert_word_automaton(realm: Realm) -> ahocorasick.Automaton:
    user_id_with_words = alert_words_in_realm(realm)
    alert_word_automaton = ahocorasick.Automaton()
    for user_id, alert_words in user_id_with_words.items():
        for alert_word in alert_words:
            alert_word_lower = alert_word.lower()
            if alert_word_automaton.exists(alert_word_lower):
                (key, user_ids_for_alert_word) = alert_word_automaton.get(alert_word_lower)
                user_ids_for_alert_word.add(user_id)
            else:
                alert_word_automaton.add_word(alert_word_lower, (alert_word_lower, {user_id}))
    alert_word_automaton.make_automaton()
    # If the kind is not AHOCORASICK after calling make_automaton, it means there is no key present
    # and hence we cannot call items on the automaton yet. To avoid it we return None for such cases
    # where there is no alert-words in the realm.
    # https://pyahocorasick.readthedocs.io/en/latest/#make-automaton
    if alert_word_automaton.kind != ahocorasick.AHOCORASICK:
        return None
    return alert_word_automaton


def user_alert_words(user_profile: UserProfile) -> list[str]:
    # Only active words for this user.
    return list(
        AlertWord.objects.filter(user_profile=user_profile, deactivated=False)
        .values_list("word", flat=True)
    )
    
    
@transaction.atomic(savepoint=False)
def add_user_alert_words(user_profile: UserProfile, new_words: Iterable[str]) -> list[str]:
    # Normalize and dedupe once.
    lower_words = {w.strip().lower() for w in new_words if w and w.strip()}
    if not lower_words:
        return list(
            AlertWord.objects.filter(user_profile=user_profile, deactivated=False)
            .values_list("word", flat=True)
        )

    # Query only the words being added (includes deactivated rows).
    existing_alert_words = list(
        AlertWord.objects.filter(user_profile=user_profile, word__in=lower_words)
    )
    existing_words_map = {aw.word.lower(): aw for aw in existing_alert_words}

    # Keep original casing for creates; dict ensures case-insensitive dedupe.
    word_dict: dict[str, str] = {}
    to_reactivate: list[AlertWord] = []

    for word in new_words:
        lower_word = word.lower()
        if lower_word in existing_words_map:
            aw = existing_words_map[lower_word]
            if aw.deactivated:
                aw.deactivated = False
                to_reactivate.append(aw)  # bulk_update later
            continue
        word_dict[lower_word] = word  # bulk_create later

    if to_reactivate:
        AlertWord.objects.bulk_update(to_reactivate, ["deactivated"])
    if word_dict:
        AlertWord.objects.bulk_create(
            AlertWord(user_profile=user_profile, word=word, realm=user_profile.realm)
            for word in word_dict.values()
        )
    if to_reactivate or word_dict:
        # Flush once after writes.
        flush_realm_alert_words(user_profile.realm_id)

    return list(
        AlertWord.objects.filter(user_profile=user_profile, deactivated=False)
        .values_list("word", flat=True)
    )


@transaction.atomic(savepoint=False)
def remove_user_alert_words(user_profile: UserProfile, delete_words: Iterable[str]) -> list[str]:
    # Soft-delete: mark as deactivated instead of deleting.
    for delete_word in delete_words:
        AlertWord.objects.filter(user_profile=user_profile, word__iexact=delete_word).update(
            deactivated=True
        )
    # Keep realm cache consistent.
    flush_realm_alert_words(user_profile.realm_id)
    return user_alert_words(user_profile)