from collections.abc import Iterable
from datetime import timedelta
from enum import Enum

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils.timezone import now as timezone_now

from confirmation.models import Confirmation, create_confirmation_link
from confirmation.settings import STATUS_REVOKED, STATUS_USED
from zerver.actions.presence import do_update_user_presence
from zerver.lib.avatar import avatar_url
from zerver.lib.cache import (
    cache_delete,
    delete_user_profile_caches,
    flush_user_profile,
    user_profile_by_api_key_cache_key,
)
from zerver.lib.create_user import get_display_email_address
from zerver.lib.i18n import get_language_name
from zerver.lib.queue import queue_event_on_commit
from zerver.lib.send_email import FromAddress, clear_scheduled_emails, send_email
from zerver.lib.timezone import canonicalize_timezone
from zerver.lib.upload import delete_avatar_image
from zerver.lib.users import (
    can_access_delivery_email,
    check_bot_name_available,
    check_full_name,
    get_user_ids_who_can_access_user,
    get_users_with_access_to_real_email,
    user_access_restricted_in_realm,
)
from zerver.lib.utils import generate_api_key
from zerver.models import (
    Draft,
    EmailChangeStatus,
    RealmAuditLog,
    ScheduledEmail,
    ScheduledMessageNotificationEmail,
    UserPresence,
    UserProfile,
)
from zerver.models.clients import get_client
from zerver.models.realm_audit_logs import AuditLogEventType
from zerver.models.users import bot_owner_user_ids, get_user_profile_by_id
from zerver.tornado.django_api import send_event_on_commit


def send_user_email_update_event(user_profile: UserProfile) -> None:
    payload = dict(user_id=user_profile.id, new_email=user_profile.email)
    event = dict(type="realm_user", op="update", person=payload)
    send_event_on_commit(
        user_profile.realm,
        event,
        get_user_ids_who_can_access_user(user_profile),
    )


def send_delivery_email_update_events(
    user_profile: UserProfile, old_visibility_setting: int, visibility_setting: int
) -> None:
    if not user_access_restricted_in_realm(user_profile):
        active_users = user_profile.realm.get_active_users()
    else:
        # The get_user_ids_who_can_access_user returns user IDs and not
        # user objects and we instead do one more query for UserProfile
        # objects. We need complete UserProfile objects only for a couple
        # of cases and it is not worth to query the whole UserProfile
        # objects in all the cases and it is fine to do the extra query
        # wherever needed.
        user_ids_who_can_access_user = get_user_ids_who_can_access_user(user_profile)
        active_users = UserProfile.objects.filter(
            id__in=user_ids_who_can_access_user, is_active=True
        )

    delivery_email_now_visible_user_ids = []
    delivery_email_now_invisible_user_ids = []

    for active_user in active_users:
        could_access_delivery_email_previously = can_access_delivery_email(
            active_user, user_profile.id, old_visibility_setting
        )
        can_access_delivery_email_now = can_access_delivery_email(
            active_user, user_profile.id, visibility_setting
        )

        if could_access_delivery_email_previously != can_access_delivery_email_now:
            if can_access_delivery_email_now:
                delivery_email_now_visible_user_ids.append(active_user.id)
            else:
                delivery_email_now_invisible_user_ids.append(active_user.id)

    if delivery_email_now_visible_user_ids:
        person = dict(user_id=user_profile.id, delivery_email=user_profile.delivery_email)
        event = dict(type="realm_user", op="update", person=person)
        send_event_on_commit(
            user_profile.realm,
            event,
            delivery_email_now_visible_user_ids,
        )
    if delivery_email_now_invisible_user_ids:
        person = dict(user_id=user_profile.id, delivery_email=None)
        event = dict(type="realm_user", op="update", person=person)
        send_event_on_commit(
            user_profile.realm,
            event,
            delivery_email_now_invisible_user_ids,
        )


@transaction.atomic(savepoint=False)
def do_change_user_delivery_email(
    user_profile: UserProfile, new_email: str, *, acting_user: UserProfile | None
) -> None:
    delete_user_profile_caches([user_profile], user_profile.realm_id)

    original_email = user_profile.delivery_email

    user_profile.delivery_email = new_email
    if user_profile.email_address_is_realm_public():
        user_profile.email = new_email
        user_profile.save(update_fields=["email", "delivery_email"])
    else:
        user_profile.save(update_fields=["delivery_email"])

    # We notify all the users who have access to delivery email.
    payload = dict(user_id=user_profile.id, delivery_email=new_email)
    event = dict(type="realm_user", op="update", person=payload)
    delivery_email_visible_user_ids = get_users_with_access_to_real_email(user_profile)

    send_event_on_commit(user_profile.realm, event, delivery_email_visible_user_ids)

    if user_profile.avatar_source == UserProfile.AVATAR_FROM_GRAVATAR:
        # If the user is using Gravatar to manage their email address,
        # their Gravatar just changed, and we need to notify other
        # clients.
        notify_avatar_url_change(user_profile)

    if user_profile.email_address_is_realm_public():
        # Additionally, if we're also changing the publicly visible
        # email, we send a new_email event as well.
        send_user_email_update_event(user_profile)

    event_time = timezone_now()
    RealmAuditLog.objects.create(
        realm=user_profile.realm,
        acting_user=acting_user,
        modified_user=user_profile,
        event_type=AuditLogEventType.USER_EMAIL_CHANGED,
        event_time=event_time,
        extra_data={RealmAuditLog.OLD_VALUE: original_email, RealmAuditLog.NEW_VALUE: new_email},
    )


def do_start_email_change_process(user_profile: UserProfile, new_email: str) -> None:
    old_email = user_profile.delivery_email
    obj = EmailChangeStatus.objects.create(
        new_email=new_email,
        old_email=old_email,
        user_profile=user_profile,
        realm=user_profile.realm,
    )

    # Deactivate existing email change requests
    EmailChangeStatus.objects.filter(realm=user_profile.realm, user_profile=user_profile).exclude(
        id=obj.id,
    ).exclude(status=STATUS_USED).update(status=STATUS_REVOKED)

    activation_url = create_confirmation_link(obj, Confirmation.EMAIL_CHANGE)
    from zerver.context_processors import common_context

    context = common_context(user_profile)
    context.update(
        old_email=old_email,
        new_email=new_email,
        activate_url=activation_url,
        organization_host=user_profile.realm.host,
    )
    language = user_profile.default_language

    email_template = "zerver/emails/confirm_new_email"

    if old_email == "":
        # The assertions here are to help document the only circumstance under which
        # this condition should be possible.
        assert (
            user_profile.realm.demo_organization_scheduled_deletion_date is not None
            and user_profile.is_realm_owner
        )
        email_template = "zerver/emails/confirm_demo_organization_email"

    send_email(
        template_prefix=email_template,
        to_emails=[new_email],
        from_name=FromAddress.security_email_from_name(language=language),
        from_address=FromAddress.tokenized_no_reply_address(),
        language=language,
        context=context,
        realm=user_profile.realm,
    )


def do_change_password(user_profile: UserProfile, password: str, commit: bool = True) -> None:
    user_profile.set_password(password)
    if commit:
        user_profile.save(update_fields=["password"])

    # Imported here to prevent import cycles
    from zproject.backends import RateLimitedAuthenticationByUsername

    RateLimitedAuthenticationByUsername(user_profile.delivery_email).clear_history()
    event_time = timezone_now()
    RealmAuditLog.objects.create(
        realm=user_profile.realm,
        acting_user=user_profile,
        modified_user=user_profile,
        event_type=AuditLogEventType.USER_PASSWORD_CHANGED,
        event_time=event_time,
    )


@transaction.atomic(savepoint=False)
def do_change_full_name(
    user_profile: UserProfile, full_name: str, acting_user: UserProfile | None
) -> None:
    old_name = user_profile.full_name
    if old_name == full_name:
        return

    user_profile.full_name = full_name
    user_profile.save(update_fields=["full_name"])
    event_time = timezone_now()
    RealmAuditLog.objects.create(
        realm=user_profile.realm,
        acting_user=acting_user,
        modified_user=user_profile,
        event_type=AuditLogEventType.USER_FULL_NAME_CHANGED,
        event_time=event_time,
        extra_data={RealmAuditLog.OLD_VALUE: old_name, RealmAuditLog.NEW_VALUE: full_name},
    )
    payload = dict(user_id=user_profile.id, full_name=user_profile.full_name)
    send_event_on_commit(
        user_profile.realm,
        dict(type="realm_user", op="update", person=payload),
        get_user_ids_who_can_access_user(user_profile),
    )
    if user_profile.is_bot:
        send_event_on_commit(
            user_profile.realm,
            dict(type="realm_bot", op="update", bot=payload),
            bot_owner_user_ids(user_profile),
        )


def check_change_full_name(
    user_profile: UserProfile, full_name_raw: str, acting_user: UserProfile | None
) -> str:
    """Verifies that the user's proposed full name is valid.  The caller
    is responsible for checking check permissions.  Returns the new
    full name, which may differ from what was passed in (because this
    function strips whitespace)."""
    new_full_name = check_full_name(
        full_name_raw=full_name_raw, user_profile=user_profile, realm=user_profile.realm
    )
    do_change_full_name(user_profile, new_full_name, acting_user)
    return new_full_name


def check_change_bot_full_name(
    user_profile: UserProfile, full_name_raw: str, acting_user: UserProfile
) -> None:
    new_full_name = check_full_name(
        full_name_raw=full_name_raw, user_profile=user_profile, realm=user_profile.realm
    )

    if new_full_name == user_profile.full_name:
        # Our web app will try to patch full_name even if the user didn't
        # modify the name in the form.  We just silently ignore those
        # situations.
        return

    check_bot_name_available(
        realm_id=user_profile.realm_id,
        full_name=new_full_name,
        is_activation=False,
    )
    do_change_full_name(user_profile, new_full_name, acting_user)


@transaction.atomic(durable=True)
def do_change_tos_version(user_profile: UserProfile, tos_version: str | None) -> None:
    user_profile.tos_version = tos_version
    user_profile.save(update_fields=["tos_version"])
    event_time = timezone_now()
    RealmAuditLog.objects.create(
        realm=user_profile.realm,
        acting_user=user_profile,
        modified_user=user_profile,
        event_type=AuditLogEventType.USER_TERMS_OF_SERVICE_VERSION_CHANGED,
        event_time=event_time,
    )


@transaction.atomic(durable=True)
def do_regenerate_api_key(user_profile: UserProfile, acting_user: UserProfile) -> str:
    old_api_key = user_profile.api_key
    new_api_key = generate_api_key()
    user_profile.api_key = new_api_key
    user_profile.save(update_fields=["api_key"])

    # We need to explicitly delete the old API key from our caches,
    # because the on-save handler for flushing the UserProfile object
    # in zerver/lib/cache.py only has access to the new API key.
    cache_delete(user_profile_by_api_key_cache_key(old_api_key))

    event_time = timezone_now()
    RealmAuditLog.objects.create(
        realm=user_profile.realm,
        acting_user=acting_user,
        modified_user=user_profile,
        event_type=AuditLogEventType.USER_API_KEY_CHANGED,
        event_time=event_time,
    )

    if user_profile.is_bot:
        send_event_on_commit(
            user_profile.realm,
            dict(
                type="realm_bot",
                op="update",
                bot=dict(
                    user_id=user_profile.id,
                    api_key=new_api_key,
                ),
            ),
            bot_owner_user_ids(user_profile),
        )

    event = {"type": "clear_push_device_tokens", "user_profile_id": user_profile.id}
    queue_event_on_commit("deferred_work", event)

    return new_api_key


def bulk_regenerate_api_keys(user_profile_ids: Iterable[int]) -> None:
    for user_profile_id in user_profile_ids:
        user_profile = get_user_profile_by_id(user_profile_id)
        do_regenerate_api_key(user_profile, user_profile)


def notify_avatar_url_change(user_profile: UserProfile) -> None:
    if user_profile.is_bot:
        bot_event = dict(
            type="realm_bot",
            op="update",
            bot=dict(
                user_id=user_profile.id,
                avatar_url=avatar_url(user_profile),
            ),
        )
        send_event_on_commit(
            user_profile.realm,
            bot_event,
            bot_owner_user_ids(user_profile),
        )

    payload = dict(
        avatar_source=user_profile.avatar_source,
        avatar_url=avatar_url(user_profile),
        avatar_url_medium=avatar_url(user_profile, medium=True),
        avatar_version=user_profile.avatar_version,
        # Even clients using client_gravatar don't need the email,
        # since we're sending the URL anyway.
        user_id=user_profile.id,
    )

    event = dict(type="realm_user", op="update", person=payload)
    send_event_on_commit(
        user_profile.realm,
        event,
        get_user_ids_who_can_access_user(user_profile),
    )


@transaction.atomic(savepoint=False)
def do_change_avatar_fields(
    user_profile: UserProfile,
    avatar_source: str,
    skip_notify: bool = False,
    *,
    acting_user: UserProfile | None,
) -> None:
    user_profile.avatar_source = avatar_source
    user_profile.avatar_version += 1
    user_profile.save(update_fields=["avatar_source", "avatar_version"])
    event_time = timezone_now()
    RealmAuditLog.objects.create(
        realm=user_profile.realm,
        modified_user=user_profile,
        event_type=AuditLogEventType.USER_AVATAR_SOURCE_CHANGED,
        extra_data={"avatar_source": avatar_source},
        event_time=event_time,
        acting_user=acting_user,
    )

    if not skip_notify:
        notify_avatar_url_change(user_profile)


def do_delete_avatar_image(user: UserProfile, *, acting_user: UserProfile | None) -> None:
    old_version = user.avatar_version
    do_change_avatar_fields(user, UserProfile.AVATAR_FROM_GRAVATAR, acting_user=acting_user)
    delete_avatar_image(user, old_version)


def update_scheduled_email_notifications_time(
    user_profile: UserProfile, old_batching_period: int, new_batching_period: int
) -> None:
    existing_scheduled_emails = ScheduledMessageNotificationEmail.objects.filter(
        user_profile=user_profile
    )

    scheduled_timestamp_change = timedelta(seconds=new_batching_period) - timedelta(
        seconds=old_batching_period
    )

    existing_scheduled_emails.update(
        scheduled_timestamp=F("scheduled_timestamp") + scheduled_timestamp_change
    )


@transaction.atomic(savepoint=False)
def do_change_user_setting(
    user_profile: UserProfile,
    setting_name: str,
    setting_value: bool | str | int | Enum,
    *,
    acting_user: UserProfile | None,
) -> None:
    old_value = getattr(user_profile, setting_name)
    event_time = timezone_now()

    if isinstance(setting_value, Enum):
        db_setting_value = setting_value.value
        event_value: bool | str | int = setting_value.name
    else:
        db_setting_value = setting_value
        event_value = db_setting_value

    if setting_name == "timezone":
        assert isinstance(db_setting_value, str)
        assert isinstance(event_value, str)
        db_setting_value = canonicalize_timezone(db_setting_value)
    else:
        property_type = UserProfile.property_types[setting_name]
        if isinstance(setting_value, Enum):
            assert isinstance(setting_value, property_type)
            assert isinstance(event_value, str)
        else:
            assert isinstance(db_setting_value, property_type)
            assert isinstance(event_value, property_type)
    setattr(user_profile, setting_name, db_setting_value)

    # TODO: Move these database actions into a transaction.atomic block.
    user_profile.save(update_fields=[setting_name])

    RealmAuditLog.objects.create(
        realm=user_profile.realm,
        event_type=AuditLogEventType.USER_SETTING_CHANGED,
        event_time=event_time,
        acting_user=acting_user,
        modified_user=user_profile,
        extra_data={
            RealmAuditLog.OLD_VALUE: old_value,
            RealmAuditLog.NEW_VALUE: db_setting_value,
            "property": setting_name,
        },
    )

    # Disabling digest emails should clear a user's email queue
    if setting_name == "enable_digest_emails" and not db_setting_value:
        clear_scheduled_emails([user_profile.id], ScheduledEmail.DIGEST)

    if setting_name == "email_notifications_batching_period_seconds":
        assert isinstance(old_value, int)
        assert isinstance(db_setting_value, int)
        update_scheduled_email_notifications_time(user_profile, old_value, db_setting_value)

    event = {
        "type": "user_settings",
        "op": "update",
        "property": setting_name,
        "value": event_value,
    }

    if setting_name == "default_language":
        assert isinstance(db_setting_value, str)
        event["language_name"] = get_language_name(db_setting_value)

    transaction.on_commit(lambda: flush_user_profile(sender=UserProfile, instance=user_profile))

    send_event_on_commit(user_profile.realm, event, [user_profile.id])

    if setting_name in UserProfile.notification_settings_legacy:
        # This legacy event format is for backwards-compatibility with
        # clients that don't support the new user_settings event type.
        # We only send this for settings added before Feature level 89.
        legacy_event = {
            "type": "update_global_notifications",
            "user": user_profile.email,
            "notification_name": setting_name,
            "setting": event_value,
        }
        send_event_on_commit(user_profile.realm, legacy_event, [user_profile.id])

    if setting_name in UserProfile.display_settings_legacy or setting_name == "timezone":
        # This legacy event format is for backwards-compatibility with
        # clients that don't support the new user_settings event type.
        # We only send this for settings added before Feature level 89.
        legacy_event = {
            "type": "update_display_settings",
            "user": user_profile.email,
            "setting_name": setting_name,
            "setting": event_value,
        }
        if setting_name == "default_language":
            assert isinstance(db_setting_value, str)
            legacy_event["language_name"] = get_language_name(db_setting_value)

        send_event_on_commit(user_profile.realm, legacy_event, [user_profile.id])

    if setting_name == "allow_private_data_export":
        realm_export_event = {
            "type": "realm_export_consent",
            "user_id": user_profile.id,
            "consented": event_value,
        }
        send_event_on_commit(
            user_profile.realm,
            realm_export_event,
            list(user_profile.realm.get_human_admin_users().values_list("id", flat=True)),
        )

    # Updates to the time zone display setting are sent to all users
    if setting_name == "timezone":
        payload = dict(
            email=user_profile.email,
            user_id=user_profile.id,
            timezone=canonicalize_timezone(user_profile.timezone),
        )
        timezone_event = dict(type="realm_user", op="update", person=payload)
        send_event_on_commit(
            user_profile.realm,
            timezone_event,
            get_user_ids_who_can_access_user(user_profile),
        )

    if setting_name == "email_address_visibility":
        send_delivery_email_update_events(
            user_profile, old_value, user_profile.email_address_visibility
        )

        if UserProfile.EMAIL_ADDRESS_VISIBILITY_EVERYONE not in [old_value, db_setting_value]:
            # We use real email addresses on UserProfile.email only if
            # EMAIL_ADDRESS_VISIBILITY_EVERYONE is configured, so
            # changes between values that will not require changing
            # that field, so we can save work and return here.
            return

        user_profile.email = get_display_email_address(user_profile)
        user_profile.save(update_fields=["email"])

        send_user_email_update_event(user_profile)
        notify_avatar_url_change(user_profile)

    if setting_name == "enable_drafts_synchronization" and db_setting_value is False:
        # Delete all of the drafts from the backend but don't send delete events
        # for them since all that's happened is that we stopped syncing changes,
        # not deleted every previously synced draft - to do that use the DELETE
        # endpoint.
        Draft.objects.filter(user_profile=user_profile).delete()

    if setting_name == "presence_enabled":
        # The presence_enabled setting's primary function is to stop
        # doing presence updates for the user altogether.
        #
        # When a user toggles the presence_enabled setting, we
        # immediately trigger a presence update, so all users see the
        # user's current presence state as consistent with the new
        # setting; not doing so can make it look like the settings
        # change didn't have any effect.
        if db_setting_value:
            status = UserPresence.LEGACY_STATUS_ACTIVE_INT
            presence_time = timezone_now()
        else:
            # We want to ensure the user's presence data is such that
            # they will be treated as offline by correct clients,
            # There are two cases:
            #
            # (1) If the user's presence was current, we backdate it
            #     so that the user will, for as long as presence
            #     remains disabled, appear to have been last online a
            #     few minutes before they disabled presence.
            #
            # (2) If the user only has a presence older than what our
            #     backdate would be, we keep the original value - it
            #     already guarantees that the user will appear to be
            #     offline.
            backdated_presence_time = timezone_now() - timedelta(
                # We add a small additional offset as a fudge factor in
                # case of clock skew.
                seconds=settings.OFFLINE_THRESHOLD_SECS
                + settings.PRESENCE_UPDATE_MIN_FREQ_SECONDS
                + 10
            )
            minimum_previous_presence_time = backdated_presence_time - timedelta(
                seconds=settings.PRESENCE_UPDATE_MIN_FREQ_SECONDS + 10
            )

            try:
                presence: UserPresence | None = UserPresence.objects.get(user_profile=user_profile)
                assert presence is not None
                assert presence.last_connected_time is not None

                original_last_active_time = presence.last_active_time
                if presence.last_connected_time <= backdated_presence_time:
                    # This is case (2), so we don't need to do
                    # anything. presence is already as intended.
                    # last_active_time <= last_connected_time always holds, so
                    # last_active_time <= backdated_presence_time is also ensured here.
                    return

                # do_update_user_presence will only send an event if
                # the presence event it receives is sufficiently newer
                # than whatever it had before, so we backdate the
                # existing presence data if necessary to ensure that
                # our new update event will be seen as new by clients.
                presence_time = backdated_presence_time
                presence.last_connected_time = min(
                    minimum_previous_presence_time, presence.last_connected_time
                )
                update_fields = ["last_connected_time"]

                if (
                    original_last_active_time is None
                    or original_last_active_time < backdated_presence_time
                ):
                    # If the user's last_active_time was old enough (or nonexistent), we don't want to perturb
                    # it, as it's already in a correct state. Thus we only do_update_user_presence with an
                    # IDLE status - which will only update the last_connected_time.
                    status = UserPresence.LEGACY_STATUS_IDLE_INT
                else:
                    assert presence.last_active_time is not None
                    presence.last_active_time = min(
                        minimum_previous_presence_time, presence.last_active_time
                    )
                    update_fields.append("last_active_time")
                    status = UserPresence.LEGACY_STATUS_ACTIVE_INT
                presence.save(update_fields=update_fields)

            except UserPresence.DoesNotExist:
                # If the user has no presence data at all, that should
                # logically get consistent treatment with having very
                # old presence data (case (2) above). We want to leave
                # their presence state intact - so just return without
                # doing anything.
                return

        # do_update_user_presence doesn't allow being run inside another
        # transaction.atomic block, so we need to use on_commit here to ensure
        # it gets executed outside of our current transaction.
        transaction.on_commit(
            lambda: do_update_user_presence(
                user_profile,
                get_client("website"),
                presence_time,
                status,
                force_send_update=True,
            )
        )
