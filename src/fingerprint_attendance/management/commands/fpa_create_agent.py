from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create a desktop enrollment agent and print its key (shown once)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("name")
        parser.add_argument("--algorithm", default="10", help="Template algorithm (10 or 12)")

    def handle(self, *args: Any, **options: Any) -> None:
        from fingerprint_attendance.services import create_agent

        agent, key = create_agent(options["name"], algorithm_version=options["algorithm"])
        self.stdout.write(f"agent {agent.uuid} created")
        self.stdout.write(f"key: {key}")
