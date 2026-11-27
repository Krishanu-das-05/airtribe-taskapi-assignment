from datetime import date
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import ai
from .idempotency import clear_store
from .models import Task

TODAY = date(2026, 11, 27)  # a Friday


class ApiTestCase(TestCase):
    def setUp(self):
        clear_store()
        self.client = APIClient()

    def post(self, url, data, key=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(url, data, format="json", **headers)

    def make_task(self, **overrides):
        fields = {"title": "Write report", "description": "Quarterly report", "priority": "medium"}
        fields.update(overrides)
        return Task.objects.create(**fields)

    def assertErrorShape(self, response, status_code, code=None):
        self.assertEqual(response.status_code, status_code, response.content)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message", "details"})
        self.assertIsInstance(body["error"]["code"], str)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertIsInstance(body["error"]["details"], list)
        for item in body["error"]["details"]:
            self.assertEqual(set(item), {"field", "issue"})
            self.assertIsInstance(item["issue"], str)
        if code:
            self.assertEqual(body["error"]["code"], code)
        return body["error"]

    def fields_with_errors(self, error):
        return {item["field"] for item in error["details"]}


class CrudTests(ApiTestCase):
    def test_create_and_retrieve(self):
        response = self.post("/api/tasks/", {"title": "Buy milk", "description": "2 litres",
                                              "due_date": "2026-12-01", "priority": "high"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["title"], "Buy milk")
        self.assertEqual(body["completed"], False)
        self.assertIsNone(body["parent"])
        self.assertEqual(self.client.get(f"/api/tasks/{body['id']}/").json()["due_date"], "2026-12-01")

    def test_update_partial_update_and_delete(self):
        task = self.make_task()
        response = self.client.put(f"/api/tasks/{task.id}/", {"title": "New", "description": "D",
                                                              "completed": True}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["completed"])
        response = self.client.patch(f"/api/tasks/{task.id}/", {"priority": "low"}, format="json")
        self.assertEqual(response.json()["priority"], "low")
        self.assertEqual(self.client.delete(f"/api/tasks/{task.id}/").status_code, 204)
        self.assertFalse(Task.objects.exists())

    def test_unknown_id_returns_404_shape_for_retrieve_update_delete(self):
        for method in ("get", "put", "patch", "delete"):
            response = getattr(self.client, method)("/api/tasks/9999/", {}, format="json")
            self.assertErrorShape(response, 404, "NOT_FOUND")
        self.assertErrorShape(self.client.get("/api/tasks/abc/"), 404, "NOT_FOUND")


class ValidationTests(ApiTestCase):
    def test_empty_and_whitespace_title_and_description(self):
        error = self.assertErrorShape(self.post("/api/tasks/", {"title": "   ", "description": ""}),
                                      400, "VALIDATION_ERROR")
        self.assertEqual(error["message"], "Validation failed")
        self.assertIn({"field": "title", "issue": "must not be empty"}, error["details"])
        self.assertIn({"field": "description", "issue": "must not be empty"}, error["details"])

    def test_missing_required_fields(self):
        error = self.assertErrorShape(self.post("/api/tasks/", {}), 400, "VALIDATION_ERROR")
        self.assertEqual(self.fields_with_errors(error), {"title", "description"})

    def test_completed_must_be_real_boolean(self):
        for value in ("yes", "true", 1, None):
            response = self.post("/api/tasks/", {"title": "T", "description": "D", "completed": value})
            error = self.assertErrorShape(response, 400, "VALIDATION_ERROR")
            self.assertIn("completed", self.fields_with_errors(error))

    def test_impossible_and_badly_formatted_dates(self):
        for value in ("2026-02-30", "2026-13-01", "01-12-2026", "tomorrow", "2026-12-01T10:00:00"):
            response = self.post("/api/tasks/", {"title": "T", "description": "D", "due_date": value})
            error = self.assertErrorShape(response, 400, "VALIDATION_ERROR")
            self.assertIn("due_date", self.fields_with_errors(error))
        self.assertFalse(Task.objects.exists())

    def test_priority_must_be_known(self):
        error = self.assertErrorShape(self.post("/api/tasks/", {"title": "T", "description": "D",
                                                                "priority": "urgent"}), 400)
        self.assertIn({"field": "priority", "issue": "must be one of low, medium, high"}, error["details"])

    def test_title_wrong_type(self):
        error = self.assertErrorShape(self.post("/api/tasks/", {"title": 123, "description": "D"}), 400)
        self.assertIn({"field": "title", "issue": "must be a string"}, error["details"])

    def test_update_validates_too(self):
        task = self.make_task()
        response = self.client.patch(f"/api/tasks/{task.id}/", {"due_date": "2026-02-30"}, format="json")
        self.assertErrorShape(response, 400, "VALIDATION_ERROR")

    def test_malformed_json_and_non_object_body(self):
        response = self.client.post("/api/tasks/", "{not json", content_type="application/json")
        self.assertErrorShape(response, 400, "MALFORMED_REQUEST")
        self.assertErrorShape(self.post("/api/tasks/", ["a", "b"]), 400, "VALIDATION_ERROR")

    def test_method_not_allowed_uses_shape(self):
        self.assertErrorShape(self.client.delete("/api/tasks/"), 405, "METHOD_NOT_ALLOWED")


class ParserUnitTests(TestCase):
    def test_monthly_rent(self):
        result = ai.parse_task_text("Remind me to pay rent on the 1st of every month", TODAY)
        self.assertEqual(result["title"], "Pay rent")
        self.assertEqual(result["due_date"], "2026-12-01")
        self.assertEqual(result["recurrence"], "monthly")

    def test_relative_dates(self):
        cases = {
            "Call mom tomorrow": "2026-11-28",
            "Ship it today": "2026-11-27",
            "Pay bills in 3 days": "2026-11-30",
            "Submit report by next friday": "2026-12-04",
            "Book flights the day after tomorrow": "2026-11-29",
            "Pay rent on the 31st": "2026-12-31",
            "Renew passport on 15 march": "2027-03-15",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ai.parse_task_text(text, TODAY)["due_date"], expected)

    def test_weekly_and_priority(self):
        result = ai.parse_task_text("Urgent: water plants every monday", TODAY)
        self.assertEqual(result["title"], "Water plants")
        self.assertEqual(result["recurrence"], "weekly")
        self.assertEqual(result["due_date"], "2026-11-30")
        self.assertEqual(result["priority"], "high")

    def test_plain_text(self):
        result = ai.parse_task_text("Buy milk", TODAY)
        self.assertEqual(result, {"title": "Buy milk", "description": "Buy milk", "due_date": None,
                                  "recurrence": None, "priority": "medium"})

    def test_break_down_splits_on_and_and_commas(self):
        task = Task(title="Buy milk, eggs and bread", description="d", priority="high")
        self.assertEqual([item["title"] for item in ai.break_down_task(task)], ["Buy milk", "Eggs", "Bread"])

    def test_break_down_falls_back_to_template(self):
        task = Task(title="Write thesis", description="d", priority="low")
        self.assertEqual([item["title"] for item in ai.break_down_task(task)],
                         ["Plan: Write thesis", "Do: Write thesis", "Review: Write thesis"])


@mock.patch("tasks.views.timezone.localdate", return_value=TODAY)
class FromTextTests(ApiTestCase):
    url = "/api/tasks/from-text/"

    def test_creates_structured_task(self, _today):
        response = self.post(self.url, {"text": "Remind me to pay rent on the 1st of every month"})
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["title"], body["due_date"], body["recurrence"]), ("Pay rent", "2026-12-01", "monthly"))
        self.assertEqual(Task.objects.count(), 1)

    def test_missing_or_empty_text_is_400(self, _today):
        for payload in ({}, {"text": ""}, {"text": "   "}, {"text": 42}):
            self.assertErrorShape(self.post(self.url, payload), 400, "VALIDATION_ERROR")
        self.assertFalse(Task.objects.exists())

    def test_parser_returning_empty_title_is_502_and_saves_nothing(self, _today):
        with mock.patch("tasks.ai.parse_task_text", return_value={"title": "", "description": "x"}):
            error = self.assertErrorShape(self.post(self.url, {"text": "anything"}), 502, "INVALID_PARSER_OUTPUT")
        self.assertIn({"field": "title", "issue": "must not be empty"}, error["details"])
        self.assertFalse(Task.objects.exists())

    def test_parser_returning_wrong_types_is_502(self, _today):
        bad_outputs = [
            {"title": 123, "description": "x"},
            {"title": "T", "description": "x", "due_date": "2026-02-30"},
            {"title": "T", "description": "x", "priority": "critical"},
            {"title": "T", "description": "x", "completed": "yes"},
            ["not", "a", "dict"],
            None,
        ]
        for output in bad_outputs:
            with self.subTest(output=output), mock.patch("tasks.ai.parse_task_text", return_value=output):
                self.assertErrorShape(self.post(self.url, {"text": "anything"}), 502, "INVALID_PARSER_OUTPUT")
        self.assertFalse(Task.objects.exists())

    def test_text_that_parses_to_empty_title_is_502(self, _today):
        self.assertErrorShape(self.post(self.url, {"text": "tomorrow"}), 502, "INVALID_PARSER_OUTPUT")
        self.assertFalse(Task.objects.exists())

    def test_parser_exception_is_502(self, _today):
        with mock.patch("tasks.ai.parse_task_text", side_effect=RuntimeError("boom")):
            error = self.assertErrorShape(self.post(self.url, {"text": "anything"}), 502, "PARSER_ERROR")
        self.assertNotIn("boom", error["message"])
        self.assertFalse(Task.objects.exists())

    @override_settings(TASK_PARSER="llm", LLM_API_KEY=None)
    def test_llm_without_api_key_is_503(self, _today):
        self.assertErrorShape(self.post(self.url, {"text": "Buy milk"}), 503, "PARSER_UNAVAILABLE")
        self.assertFalse(Task.objects.exists())


class SubtaskTests(ApiTestCase):
    def test_create_and_list_subtasks(self):
        parent = self.make_task()
        response = self.post(f"/api/tasks/{parent.id}/subtasks/", {"title": "Outline", "description": "Draft"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["parent"], parent.id)
        listed = self.client.get(f"/api/tasks/{parent.id}/subtasks/").json()
        self.assertEqual([item["title"] for item in listed], ["Outline"])

    def test_subtask_validation_and_unknown_parent(self):
        parent = self.make_task()
        self.assertErrorShape(self.post(f"/api/tasks/{parent.id}/subtasks/", {"title": ""}), 400, "VALIDATION_ERROR")
        self.assertErrorShape(self.post("/api/tasks/9999/subtasks/", {"title": "T", "description": "D"}), 404)
        self.assertErrorShape(self.client.get("/api/tasks/9999/subtasks/"), 404, "NOT_FOUND")
        self.assertEqual(Task.objects.count(), 1)

    def test_subtask_of_subtask_is_rejected(self):
        parent = self.make_task()
        child = self.make_task(parent=parent, title="Child")
        self.assertErrorShape(self.post(f"/api/tasks/{child.id}/subtasks/", {"title": "T", "description": "D"}),
                              400, "SUBTASK_NESTING_NOT_ALLOWED")
        self.assertErrorShape(self.post(f"/api/tasks/{child.id}/breakdown/", {}), 400, "SUBTASK_NESTING_NOT_ALLOWED")
        self.assertEqual(Task.objects.count(), 2)

    def test_parent_cannot_be_set_through_payload(self):
        parent = self.make_task()
        response = self.post("/api/tasks/", {"title": "T", "description": "D", "parent": parent.id})
        self.assertIsNone(response.json()["parent"])

    def test_breakdown_creates_subtasks(self):
        parent = self.make_task(title="Buy milk, eggs and bread", due_date=date(2026, 12, 1), priority="high")
        response = self.post(f"/api/tasks/{parent.id}/breakdown/", {})
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual([item["title"] for item in body], ["Buy milk", "Eggs", "Bread"])
        self.assertTrue(all(item["parent"] == parent.id and item["priority"] == "high" for item in body))
        self.assertEqual(parent.subtasks.count(), 3)

    def test_breakdown_with_one_invalid_item_saves_none(self):
        parent = self.make_task()
        items = [{"title": "Good", "description": "ok"}, {"title": "", "description": "bad"}]
        with mock.patch("tasks.ai.break_down_task", return_value=items):
            error = self.assertErrorShape(self.post(f"/api/tasks/{parent.id}/breakdown/", {}),
                                          502, "INVALID_BREAKDOWN_OUTPUT")
        self.assertIn({"field": "subtasks[1].title", "issue": "must not be empty"}, error["details"])
        self.assertEqual(parent.subtasks.count(), 0)

    def test_breakdown_bad_output_types(self):
        parent = self.make_task()
        for output in ([], None, "text"):
            with self.subTest(output=output), mock.patch("tasks.ai.break_down_task", return_value=output):
                self.assertErrorShape(self.post(f"/api/tasks/{parent.id}/breakdown/", {}), 502)
        with mock.patch("tasks.ai.break_down_task", side_effect=ValueError("boom")):
            self.assertErrorShape(self.post(f"/api/tasks/{parent.id}/breakdown/", {}), 502, "BREAKDOWN_ERROR")
        self.assertEqual(parent.subtasks.count(), 0)

    def test_breakdown_unknown_task_is_404(self):
        self.assertErrorShape(self.post("/api/tasks/9999/breakdown/", {}), 404, "NOT_FOUND")

    def test_deleting_parent_cascades(self):
        parent = self.make_task()
        self.make_task(parent=parent, title="A")
        self.make_task(parent=parent, title="B")
        self.assertEqual(self.client.delete(f"/api/tasks/{parent.id}/").status_code, 204)
        self.assertEqual(Task.objects.count(), 0)


@mock.patch("tasks.views.timezone.localdate", return_value=TODAY)
class IdempotencyTests(ApiTestCase):
    def test_same_key_same_body_replays_without_duplicate(self, _today):
        payload = {"title": "Pay rent", "description": "Monthly"}
        first = self.post("/api/tasks/", payload, key="key-1")
        second = self.post("/api/tasks/", payload, key="key-1")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(Task.objects.count(), 1)

    def test_same_key_different_body_is_422(self, _today):
        self.post("/api/tasks/", {"title": "A", "description": "D"}, key="key-2")
        response = self.post("/api/tasks/", {"title": "B", "description": "D"}, key="key-2")
        self.assertErrorShape(response, 422, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(Task.objects.count(), 1)

    def test_same_key_on_another_endpoint_is_422(self, _today):
        self.post("/api/tasks/", {"text": "Buy milk", "title": "x", "description": "y"}, key="key-x")
        response = self.post("/api/tasks/from-text/", {"text": "Buy milk", "title": "x", "description": "y"},
                             key="key-x")
        self.assertErrorShape(response, 422, "IDEMPOTENCY_KEY_REUSED")

    def test_failed_request_is_not_stored(self, _today):
        self.assertErrorShape(self.post("/api/tasks/", {"title": "", "description": "D"}, key="key-3"), 400)
        response = self.post("/api/tasks/", {"title": "Fixed", "description": "D"}, key="key-3")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Task.objects.count(), 1)

    def test_no_key_creates_each_time(self, _today):
        self.post("/api/tasks/", {"title": "A", "description": "D"})
        self.post("/api/tasks/", {"title": "A", "description": "D"})
        self.assertEqual(Task.objects.count(), 2)

    def test_from_text_replay_does_not_call_parser_again(self, _today):
        payload = {"text": "Call mom tomorrow"}
        with mock.patch("tasks.ai.parse_task_text", wraps=ai.parse_task_text) as parser:
            first = self.post("/api/tasks/from-text/", payload, key="key-4")
            second = self.post("/api/tasks/from-text/", payload, key="key-4")
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(Task.objects.count(), 1)

    def test_breakdown_replay_creates_subtasks_once(self, _today):
        parent = self.make_task(title="Wash car and walk dog")
        first = self.post(f"/api/tasks/{parent.id}/breakdown/", {}, key="key-5")
        second = self.post(f"/api/tasks/{parent.id}/breakdown/", {}, key="key-5")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(parent.subtasks.count(), 2)

    def test_blank_or_huge_key_is_400(self, _today):
        for key in (" ", "k" * 300):
            response = self.client.post("/api/tasks/", {"title": "A", "description": "D"}, format="json",
                                        HTTP_IDEMPOTENCY_KEY=key)
            self.assertErrorShape(response, 400, "VALIDATION_ERROR")
        self.assertFalse(Task.objects.exists())


class ListQueryTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.rent = self.make_task(title="Pay rent", description="landlord", priority="high", due_date=date(2026, 12, 1))
        self.milk = self.make_task(title="Buy milk", description="groceries", priority="low", completed=True)
        self.gym = self.make_task(title="Gym", description="pay membership", priority="medium",
                                  due_date=date(2026, 11, 30))
        self.sub = self.make_task(title="Transfer money", description="bank", parent=self.rent)

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return [item["id"] for item in response.json()["results"]]

    def test_pagination_envelope(self):
        body = self.client.get("/api/tasks/?page_size=2").json()
        self.assertEqual(body["count"], 4)
        self.assertEqual(len(body["results"]), 2)
        self.assertIsNotNone(body["next"])

    def test_filters(self):
        self.assertEqual(self.ids(self.client.get("/api/tasks/?completed=true")), [self.milk.id])
        self.assertEqual(self.ids(self.client.get("/api/tasks/?priority=high")), [self.rent.id])
        self.assertEqual(self.ids(self.client.get(f"/api/tasks/?parent={self.rent.id}")), [self.sub.id])
        self.assertEqual(self.ids(self.client.get("/api/tasks/?is_subtask=true")), [self.sub.id])
        self.assertEqual(self.ids(self.client.get("/api/tasks/?due_before=2026-11-30")), [self.gym.id])

    def test_search_and_priority_ordering(self):
        self.assertEqual(set(self.ids(self.client.get("/api/tasks/?search=pay"))), {self.rent.id, self.gym.id})
        ordered = self.ids(self.client.get("/api/tasks/?is_subtask=false&ordering=-priority"))
        self.assertEqual(ordered, [self.rent.id, self.gym.id, self.milk.id])

    def test_everything_combined(self):
        response = self.client.get(
            "/api/tasks/?completed=false&is_subtask=false&search=pay&ordering=due_date&page_size=1&page=2"
        )
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([item["id"] for item in body["results"]], [self.rent.id])

    def test_invalid_query_values_use_error_shape(self):
        self.assertErrorShape(self.client.get("/api/tasks/?priority=urgent"), 400, "VALIDATION_ERROR")
        self.assertErrorShape(self.client.get("/api/tasks/?due_before=2026-02-30"), 400, "VALIDATION_ERROR")
        self.assertErrorShape(self.client.get("/api/tasks/?page=99"), 404, "NOT_FOUND")
