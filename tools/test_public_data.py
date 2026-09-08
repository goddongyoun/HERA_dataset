"""Fast, synthetic unit checks for the public-data privacy/path safeguards."""
import json
import unittest

from build_public_data import privacy_check, redact_bytes, safe_relative, scientific_equal


class PublicDataSafeguards(unittest.TestCase):
    def test_profile_and_workspace_masks_preserve_length(self):
        for separator in ("/", "\\", "\\\\"):
            value = ("E:" + separator + "WorkArea" + separator + "revision_v3" + separator + "runs" +
                     " C:" + separator + "Users" + separator + "Alice" + separator + "models").encode()
            result, labels = redact_bytes(value)
            self.assertEqual(len(value), len(result))
            self.assertNotIn(b"WorkArea", result)
            self.assertNotIn(b"Alice", result)
            self.assertEqual(set(labels), {"workspace_folder_mask", "user_profile_mask"})
            self.assertEqual(redact_bytes(result), (result, []))
            privacy_check(result, "synthetic")

    def test_gpu_mask_is_length_preserving(self):
        value = ("GPU-" + "12345678-1234-1234-1234-123456789abc").encode()
        result, labels = redact_bytes(value)
        self.assertEqual(len(result), len(value))
        self.assertEqual(labels, ["gpu_uuid_mask"])
        privacy_check(result, "synthetic")

    def test_json_preserves_all_scientific_fields(self):
        value = {"trial_id": "trial-123", "seed": 123, "time": 0.003,
                 "completed": True, "optional": None, "samples": [0, 2.4],
                 "directory": "E:" + "\\" + "WorkArea" + "\\" + "revision_v3"}
        public = json.loads(redact_bytes(json.dumps(value).encode())[0])
        self.assertTrue(scientific_equal(value, public))
        public["time"] = 0.004
        self.assertFalse(scientific_equal(value, public))
        self.assertFalse(scientific_equal({"value": 1}, {"value": True}))
        self.assertFalse(scientific_equal({"value": 1}, {"value": 1.0}))
        self.assertFalse(scientific_equal({"value": [1, 2]}, {"value": [2, 1]}))

    def test_unsafe_archive_paths_rejected(self):
        self.assertEqual(safe_relative("runs/main_001/campaign.json"), "runs/main_001/campaign.json")
        for value in ("", "../escape", "/absolute", "a/../escape", "a//b", "C:/escape", "a\\b", "./a"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_relative(value)

    def test_private_paths_and_credentials_fail_closed(self):
        cases = [("C:" + "\\Users\\" + "Alice" + "\\data").encode(),
                 ("/" + "home" + "/" + "alice" + "/data").encode(),
                 ("ghp_" + "a" * 36).encode(),
                 json.dumps({"api_" + "key": "nonempty-secret"}).encode()]
        for value in cases:
            with self.subTest(case=len(value)), self.assertRaises(ValueError):
                privacy_check(value, "synthetic")


if __name__ == "__main__":
    unittest.main()
