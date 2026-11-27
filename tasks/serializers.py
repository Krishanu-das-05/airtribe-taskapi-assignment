from rest_framework import serializers

from .models import Task

NOT_EMPTY = "must not be empty"


class StrictCharField(serializers.CharField):
    """CharField that rejects non-string input instead of silently coercing numbers to text."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    """Only accepts JSON true/false, not "yes", "1" or 1."""

    default_error_messages = {"invalid": "must be a boolean"}

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail("invalid")
        return data


def _text_field(**kwargs):
    return StrictCharField(
        error_messages={
            "blank": NOT_EMPTY,
            "required": "is required",
            "null": NOT_EMPTY,
            "invalid": "must be a string",
        },
        **kwargs,
    )


class TaskSerializer(serializers.ModelSerializer):
    title = _text_field(max_length=200)
    description = _text_field()
    completed = StrictBooleanField(required=False)
    due_date = serializers.DateField(
        required=False,
        allow_null=True,
        input_formats=["%Y-%m-%d"],
        format="%Y-%m-%d",
        error_messages={"invalid": "must be a valid date in YYYY-MM-DD format",
                        "datetime": "must be a date (YYYY-MM-DD), not a datetime"},
    )
    priority = serializers.ChoiceField(
        choices=Task.Priority.choices,
        required=False,
        error_messages={"invalid_choice": "must be one of low, medium, high"},
    )
    recurrence = serializers.ChoiceField(
        choices=Task.Recurrence.choices,
        required=False,
        allow_null=True,
        error_messages={"invalid_choice": "must be one of daily, weekly, monthly, yearly"},
    )

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "description",
            "completed",
            "due_date",
            "priority",
            "recurrence",
            "parent",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "parent", "created_at", "updated_at"]


class FromTextSerializer(serializers.Serializer):
    text = _text_field(max_length=1000)
