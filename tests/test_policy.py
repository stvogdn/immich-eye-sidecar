"""Unit tests for the tag policy and slug rules (no network, no model)."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import eye_sidecar as sc  # noqa: E402


def result(tags, scores=None, kind="photo"):
    return {"tags": tags, "scores": scores or {}, "kind": kind, "model": "eye@test"}


class TestSlug(unittest.TestCase):
    def test_lowercases_and_hyphenates(self):
        self.assertEqual(sc.slug("Eyes_Closed"), "eyes_closed")
        self.assertEqual(sc.slug("chart_or_slide"), "chart_or_slide")

    def test_strips_odd_characters(self):
        self.assertEqual(sc.slug("  traffic light! "), "traffic-light")

    def test_collapses_runs(self):
        self.assertEqual(sc.slug("a   b"), "a-b")


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.p = sc.TagPolicy(prefix="eye", max_scene_tags=3)

    def test_kind_and_quality_tags_at_min_conf(self):
        tags = self.p.select(result({"screenshot": 0.98, "blurry": 0.6, "sharp": 0.9}))
        self.assertIn("eye:screenshot", tags)
        self.assertIn("eye:blurry", tags)
        self.assertNotIn("eye:sharp", tags)  # control tag, never written

    def test_below_cutoff_is_ignored(self):
        tags = self.p.select(result({"screenshot": 0.49, "blurry": 0.2}))
        self.assertEqual(tags, [])

    def test_sensitive_requires_strict_cutoff(self):
        self.assertNotIn("eye:sensitive", self.p.select(result({"sensitive": 0.7})))
        self.assertIn("eye:sensitive", self.p.select(result({"sensitive": 0.95})))
        self.assertNotIn("eye:nsfw_explicit", self.p.select(result({"nsfw_explicit": 0.8})))
        self.assertIn("eye:nsfw_explicit", self.p.select(result({"nsfw_explicit": 0.95})))

    def test_scene_tags_capped_and_ranked(self):
        tags = self.p.select(result({"city": 0.8, "dog": 0.95, "beach": 0.9,
                                    "tree": 0.7, "car": 0.6}))
        scenes = [t for t in tags if t in {"eye:dog", "eye:beach", "eye:city"}]
        self.assertEqual(len(scenes), 3)
        self.assertNotIn("eye:tree", tags)
        self.assertEqual(scenes, ["eye:beach", "eye:city", "eye:dog"])

    def test_scenes_can_be_disabled(self):
        p = sc.TagPolicy(prefix="eye", include_scenes=False)
        self.assertEqual(p.select(result({"dog": 0.99})), [])

    def test_keeper_needs_score_and_sharpness(self):
        self.assertIn("eye:keeper", self.p.select(
            result({"photo": 0.99, "sharp": 1.0}, scores={"keeper": 0.85})))
        self.assertNotIn("eye:keeper", self.p.select(
            result({"photo": 0.99}, scores={"keeper": 0.79})))
        self.assertNotIn("eye:keeper", self.p.select(
            result({"blurry": 0.9}, scores={"keeper": 0.95})))

    def test_people_tags(self):
        tags = self.p.select(result({"group_shot": 0.8, "portrait": 0.7,
                                    "everyone_smiling": 0.6, "people": 0.99}))
        self.assertIn("eye:group_shot", tags)
        self.assertIn("eye:everyone_smiling", tags)
        self.assertNotIn("eye:people", tags)  # control tag

    def test_custom_prefix(self):
        p = sc.TagPolicy(prefix="dant")
        self.assertIn("dant:receipt", p.select(result({"receipt": 0.9})))

    def test_output_is_sorted_and_deduplicated(self):
        tags = self.p.select(result({"screenshot": 0.9, "document": 0.9}))
        self.assertEqual(tags, sorted(set(tags)))


if __name__ == "__main__":
    unittest.main()
