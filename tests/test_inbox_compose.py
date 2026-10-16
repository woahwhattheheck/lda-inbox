import unittest

from inbox_compose import validate_composition


class ComposerValidationTests(unittest.TestCase):
    def task(self, task_id="alpha"):
        return {
            "id": task_id,
            "created": "2026-10-16T14:00:00Z",
            "kind": "run_task",
            "command": "Review the complete authored inbox",
            "timeout_s": 3600,
            "done": False,
        }

    def test_valid_draft_is_canonical_and_deterministic(self):
        draft = {"v": 1, "label": "Operator handoff", "tasks": [self.task(), self.task("beta")]}
        first = validate_composition(draft)
        second = validate_composition(draft)
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["canonical_text"], second["canonical_text"])
        self.assertEqual(first["task_count"], 2)
        self.assertTrue(first["canonical_text"].endswith("\n"))

    def test_duplicate_id_uses_maintained_protocol_rejection(self):
        draft = {"v": 1, "label": "Duplicate", "tasks": [self.task(), self.task()]}
        with self.assertRaisesRegex(ValueError, "duplicate task id"):
            validate_composition(draft)

    def test_task_must_be_exact_pending_authoring_shape(self):
        task = self.task()
        task["execution"] = {"state": "leased"}
        draft = {"v": 1, "label": "Unsafe", "tasks": [task]}
        with self.assertRaisesRegex(ValueError, "fields must be exactly"):
            validate_composition(draft)
        task = self.task()
        task["done"] = True
        with self.assertRaisesRegex(ValueError, "done=false"):
            validate_composition({"v": 1, "label": "Unsafe", "tasks": [task]})

    def test_root_shape_and_nonempty_task_set_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "exactly v, label and tasks"):
            validate_composition({"v": 1, "label": "Extra", "tasks": [self.task()], "metadata": {}})
        with self.assertRaisesRegex(ValueError, "at least one"):
            validate_composition({"v": 1, "label": "Empty", "tasks": []})

    def test_maintained_timestamp_and_timeout_rules_apply(self):
        bad_time = self.task()
        bad_time["created"] = "not-a-time"
        with self.assertRaisesRegex(ValueError, "invalid ISO-8601"):
            validate_composition({"v": 1, "label": "Bad time", "tasks": [bad_time]})
        bad_timeout = self.task()
        bad_timeout["timeout_s"] = 86401
        with self.assertRaisesRegex(ValueError, "exceeds maximum"):
            validate_composition({"v": 1, "label": "Bad timeout", "tasks": [bad_timeout]})


if __name__ == "__main__":
    unittest.main()
