"""Пакетное выполнение всех задач из очереди django-tasks.

Удобно для тестов и одноразовых cron-задач::

    python manage.py drain_tasks --once

    python manage.py drain_tasks --max-iterations 100 --interval 0.5
"""

from __future__ import annotations

from time import sleep

from django.core.management import BaseCommand
from django_tasks import default_task_backend
from django_tasks.backends.database.management.commands.db_worker import Worker
from django_tasks.backends.database.models import DBTaskResult


class Command(BaseCommand):
    help = "Обработать все задачи из очереди django-tasks (пакетный режим) и выйти."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Обработать только текущие задачи и завершиться (по умолчанию).",
        )
        parser.add_argument(
            "--max-iterations",
            type=int,
            default=100,
            help="Максимум итераций опроса очереди (по умолчанию 100).",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=0.5,
            help="Пауза между опросами, секунд (по умолчанию 0.5).",
        )

    def handle(self, *args, **options) -> None:
        max_iterations = options["max_iterations"]
        interval = options["interval"]
        processed = 0
        worker = Worker(
            queue_names=["default"],
            interval=interval,
            batch=True,
            backend_name=default_task_backend.alias,
            startup_delay=False,
        )
        for iteration in range(max_iterations):
            ready = DBTaskResult.objects.ready().filter(backend_name=default_task_backend.alias)
            tasks = list(ready[:5])
            if not tasks:
                self.stdout.write(self.style.SUCCESS(f"Очередь пуста. Обработано: {processed}."))
                return
            for task_result in tasks:
                try:
                    worker.run_task(task_result)
                    processed += 1
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(f"Task {task_result.id} failed: {exc}"))
            if iteration + 1 < max_iterations:
                sleep(interval)
        self.stdout.write(
            self.style.WARNING(f"Достигнут лимит итераций. Обработано: {processed}.")
        )


