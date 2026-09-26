from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.utils import timezone

from fingerprint_attendance import crypto
from fingerprint_attendance.constants import TemplateSource
from fingerprint_attendance.crypto import TemplateDecryptionError
from fingerprint_attendance.fields import EmployeeOneToOneField
from fingerprint_attendance.models import (
    Device,
    FingerprintTemplate,
    ImmutableRecordError,
    Punch,
    decode_flags,
    encode_flags,
    normalize_algorithm,
)
from fingerprint_attendance.services import store_template

pytestmark = pytest.mark.django_db


def test_encrypt_roundtrip_and_prefix():
    stored = crypto.encrypt(b"\x01\x02template")
    assert stored.startswith("fernet:")
    assert crypto.is_encrypted(stored)
    assert crypto.decrypt(stored) == b"\x01\x02template"


def test_plaintext_mode(fpa):
    fpa(TEMPLATE_ENCRYPTION_ENABLED=False)
    stored = crypto.encrypt(b"abc")
    assert stored.startswith("plain:")
    assert crypto.decrypt(stored) == b"abc"


def test_key_rotation(fpa, settings):
    old = crypto.encrypt(b"secret")
    new_key = Fernet.generate_key().decode()
    fpa(TEMPLATE_ENCRYPTION_KEYS=[new_key, settings.TEST_FERNET_KEY])
    assert crypto.decrypt(old) == b"secret"  # old key still decrypts
    rotated = crypto.rotate(old)
    fpa(TEMPLATE_ENCRYPTION_KEYS=[new_key])
    assert crypto.decrypt(rotated) == b"secret"
    with pytest.raises(TemplateDecryptionError):
        crypto.decrypt(old)


def test_rotate_converts_plaintext(fpa):
    fpa(TEMPLATE_ENCRYPTION_ENABLED=False)
    stored = crypto.encrypt(b"abc")
    fpa(TEMPLATE_ENCRYPTION_ENABLED=True)
    assert crypto.rotate(stored).startswith("fernet:")
    fpa(TEMPLATE_ENCRYPTION_ENABLED=False)
    assert crypto.rotate(crypto.encrypt(b"x")).startswith("plain:")


def test_decrypt_garbage():
    with pytest.raises(TemplateDecryptionError):
        crypto.decrypt("weird:data")
    with pytest.raises(TemplateDecryptionError):
        crypto.decrypt("fernet:not-a-token")


def test_generate_key_is_valid():
    Fernet(crypto.generate_key())


def test_template_stored_encrypted(make_enrollee):
    enrollee = make_enrollee()
    template, changed = store_template(enrollee, 6, "10", b"T" * 400,
                                       source=TemplateSource.IMPORT)
    assert changed
    raw = FingerprintTemplate.objects.values_list("template_data", flat=True).get()
    assert raw.startswith("fernet:") and "TTTT" not in raw
    assert template.get_data() == b"T" * 400
    assert template.size == 400 and len(template.checksum) == 64
    assert template.finger_name == "right_index"


def test_rotate_keys_command(make_enrollee, fpa, settings):
    enrollee = make_enrollee()
    store_template(enrollee, 1, "10", b"A" * 100, source=TemplateSource.IMPORT)
    new_key = Fernet.generate_key().decode()
    fpa(TEMPLATE_ENCRYPTION_KEYS=[new_key, settings.TEST_FERNET_KEY])
    out = StringIO()
    call_command("fpa_rotate_template_keys", stdout=out)
    assert "re-encrypted 1 of 1" in out.getvalue()
    fpa(TEMPLATE_ENCRYPTION_KEYS=[new_key])
    assert FingerprintTemplate.objects.get().get_data() == b"A" * 100


def test_custom_storage_backend(make_enrollee, fpa):
    fpa(TEMPLATE_STORAGE_BACKEND="tests.testapp.storage.MemoryStorage")
    from tests.testapp.storage import MemoryStorage

    enrollee = make_enrollee()
    template, _ = store_template(enrollee, 2, "10", b"vault", source=TemplateSource.IMPORT)
    assert template.template_data == f"vault:{template.checksum}"
    assert template.get_data() == b"vault"
    assert MemoryStorage.saved


def test_punch_is_immutable(make_device):
    device = make_device()
    punch = Punch.objects.create(raw_pin="1", device=device, punched_at=timezone.now(),
                                 dedupe_hash="x" * 64, source="manual")
    punch.state = "check_out"
    with pytest.raises(ImmutableRecordError):
        punch.save()


def test_flags_helpers(make_device):
    assert encode_flags(["b", "a", "", "a"]) == "|a|b|"
    assert encode_flags([]) == ""
    assert decode_flags("|a|b|") == ["a", "b"]
    device = make_device()
    Punch.objects.create(raw_pin="1", device=device, punched_at=timezone.now(),
                         dedupe_hash="1" * 64, source="adms", flags="|late_sync|")
    Punch.objects.create(raw_pin="1", device=device, punched_at=timezone.now(),
                         dedupe_hash="2" * 64, source="adms", flags="|duplicate|")
    assert Punch.objects.with_flag("late_sync").count() == 1
    assert Punch.objects.effective().count() == 1
    p = Punch.objects.with_flag("late_sync").get()
    assert p.has_flag("late_sync") and p.flag_list == ["late_sync"]
    assert "@" in str(p)


def test_device_online_and_queryset(make_device):
    online = make_device(last_seen_at=timezone.now())
    offline = make_device(last_seen_at=timezone.now() - timedelta(hours=1))
    never = make_device()
    assert online.is_online and not offline.is_online and not never.is_online
    assert offline.offline_duration > timedelta(minutes=59)
    assert online.offline_duration is None
    assert set(Device.objects.online()) == {online}
    assert set(Device.objects.offline()) == {offline, never}
    assert set(Device.objects.active()) == {online, offline, never}
    assert str(online) == online.serial_number
    assert online.effective_log_capacity == 100000


@pytest.mark.parametrize(("raw", "expected"), [("10.0", "10"), ("ZKFinger v12", "12"),
                                               ("", ""), (None, ""), ("12", "12")])
def test_normalize_algorithm(raw, expected):
    assert normalize_algorithm(raw) == expected


def test_employee_field_deconstruct_has_no_target():
    field = EmployeeOneToOneField(related_name="x")
    _name, path, _args, kwargs = field.deconstruct()
    assert path == "fingerprint_attendance.fields.EmployeeOneToOneField"
    assert "to" not in kwargs
    assert field.remote_field.model == "testapp.Employee"


def test_no_missing_migrations():
    out = StringIO()
    call_command("makemigrations", "fingerprint_attendance", "--check", "--dry-run",
                 stdout=out)
    assert "No changes detected" in out.getvalue()


def test_str_methods(make_enrollee, make_device):
    from fingerprint_attendance.models import DeviceGroup

    enrollee = make_enrollee(name="Ada")
    assert "Ada" in str(enrollee)
    assert str(DeviceGroup.objects.create(name="HQ")) == "HQ"
    assert enrollee.has_consent
