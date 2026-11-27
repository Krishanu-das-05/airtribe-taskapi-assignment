import django_filters
from rest_framework.filters import OrderingFilter

from .models import Task


class TaskFilter(django_filters.FilterSet):
    due_before = django_filters.DateFilter(field_name="due_date", lookup_expr="lte", input_formats=["%Y-%m-%d"])
    due_after = django_filters.DateFilter(field_name="due_date", lookup_expr="gte", input_formats=["%Y-%m-%d"])
    is_subtask = django_filters.BooleanFilter(field_name="parent", lookup_expr="isnull", exclude=True)

    class Meta:
        model = Task
        fields = ["completed", "priority", "recurrence", "due_date", "parent"]


class TaskOrderingFilter(OrderingFilter):
    """`ordering=priority` sorts low < medium < high instead of alphabetically."""

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        return [
            term.replace("priority", "priority_rank") if term.lstrip("-") == "priority" else term
            for term in ordering
        ]
