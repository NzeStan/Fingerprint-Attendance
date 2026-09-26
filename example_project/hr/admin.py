from __future__ import annotations

from django.contrib import admin

from .models import Employee, WebhookEvent

admin.site.register(Employee, list_display=("staff_id", "badge_number", "first_name",
                                            "last_name", "department"))
admin.site.register(WebhookEvent, list_display=("received_at", "event", "delivery_id"))
