import logging

from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.http import Http404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import ai
from .errors import error_response, errors_to_details, validation_error_response
from .filters import TaskFilter
from .idempotency import idempotent
from .models import Task
from .serializers import FromTextSerializer, TaskSerializer

logger = logging.getLogger(__name__)

NESTING_ERROR = ("SUBTASK_NESTING_NOT_ALLOWED", "A subtask cannot have its own subtasks",
                 [{"field": "parent", "issue": "task is already a subtask"}])


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    filterset_class = TaskFilter
    search_fields = ["title", "description"]
    ordering_fields = ["due_date", "priority", "created_at", "updated_at", "title"]
    ordering = ["-created_at", "-id"]

    def get_queryset(self):
        return Task.objects.annotate(
            priority_rank=Case(
                When(priority=Task.Priority.LOW, then=Value(0)),
                When(priority=Task.Priority.MEDIUM, then=Value(1)),
                When(priority=Task.Priority.HIGH, then=Value(2)),
                output_field=IntegerField(),
            )
        )

    def _get_task_or_404(self):
        try:
            return self.get_object(), None
        except Http404:
            return None, error_response("NOT_FOUND", "Task not found", [], status.HTTP_404_NOT_FOUND)

    @idempotent
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        task, error = self._get_task_or_404()
        if error:
            return error
        return Response(self.get_serializer(task).data)

    def update(self, request, *args, **kwargs):
        task, error = self._get_task_or_404()
        if error:
            return error
        serializer = self.get_serializer(task, data=request.data, partial=kwargs.pop("partial", False))
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save()
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        task, error = self._get_task_or_404()
        if error:
            return error
        task.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"], url_path="from-text")
    @idempotent
    def from_text(self, request):
        input_serializer = FromTextSerializer(data=request.data)
        if not input_serializer.is_valid():
            return validation_error_response(input_serializer.errors)

        try:
            parsed = ai.parse_task_text(input_serializer.validated_data["text"], timezone.localdate())
        except ai.ParserUnavailable:
            return error_response("PARSER_UNAVAILABLE", "The task parser is not configured",
                                  status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception:
            logger.exception("parse_task_text failed")
            return error_response("PARSER_ERROR", "The task parser failed", status=status.HTTP_502_BAD_GATEWAY)

        serializer = self.get_serializer(data=parsed)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors, code="INVALID_PARSER_OUTPUT",
                                             message="The task parser returned invalid data",
                                             status=status.HTTP_502_BAD_GATEWAY)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def subtasks(self, request, pk=None):
        task, error = self._get_task_or_404()
        if error:
            return error

        if request.method == "GET":
            return Response(self.get_serializer(task.subtasks.order_by("created_at", "id"), many=True).data)

        if task.parent_id is not None:
            return error_response(*NESTING_ERROR)
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return validation_error_response(serializer.errors)
        serializer.save(parent=task)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    @idempotent
    def breakdown(self, request, pk=None):
        task, error = self._get_task_or_404()
        if error:
            return error
        if task.parent_id is not None:
            return error_response(*NESTING_ERROR)

        try:
            items = ai.break_down_task(task)
        except Exception:
            logger.exception("break_down_task failed")
            return error_response("BREAKDOWN_ERROR", "The task breakdown failed", status=status.HTTP_502_BAD_GATEWAY)

        if not isinstance(items, list) or not items:
            return error_response("INVALID_BREAKDOWN_OUTPUT", "The task breakdown returned invalid data",
                                  [{"field": "subtasks", "issue": "must be a non-empty list"}],
                                  status.HTTP_502_BAD_GATEWAY)

        item_serializers = [self.get_serializer(data=item) for item in items]
        details = []
        for index, serializer in enumerate(item_serializers):
            if not serializer.is_valid():
                details.extend(errors_to_details(serializer.errors, prefix=f"subtasks[{index}]"))
        if details:
            return error_response("INVALID_BREAKDOWN_OUTPUT", "The task breakdown returned invalid data",
                                  details, status.HTTP_502_BAD_GATEWAY)

        with transaction.atomic():
            for serializer in item_serializers:
                serializer.save(parent=task)
        return Response([serializer.data for serializer in item_serializers], status=status.HTTP_201_CREATED)
