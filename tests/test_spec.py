import json
import os
import tempfile
import unittest

from tallyboard.spec import fill, load_spec
from tallyboard.store import StoreError


class SpecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for s in ("Batch 3", "Batch 10"):
            os.makedirs(os.path.join(self.root, "src", s))
            for n in ("02_b_X", "01_a_X", "10_c_X"):
                open(os.path.join(self.root, "src", s, n + ".blend"), "w").close()
        os.makedirs(os.path.join(self.root, ".tallyboard"))
        os.makedirs(os.path.join(self.root, ".jobboard"))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, spec, where=".tallyboard/build.json"):
        p = os.path.join(self.root, where)
        with open(p, "w") as fh:
            json.dump(spec, fh)
        return p

    def test_item_sources_expand_naturally_sorted(self):
        p = self.write({"title": "Build {who}", "vars": {"who": "all"},
                        "item_sources": [{"glob": "src/*/*.blend", "id": "{parent}/{stem}",
                                          "group": "{parent}", "label_sub": {"_X$": ""},
                                          "output": "out/{parent}/{name}"}]})
        s = load_spec(p)
        self.assertEqual(s["base_dir"], self.root)  # parent of .tallyboard/
        self.assertEqual(s["title"], "Build all")
        ids = [i["id"] for i in s["items"]]
        self.assertEqual(ids[:3], ["Batch 3/01_a_X", "Batch 3/02_b_X", "Batch 3/10_c_X"])
        self.assertEqual(ids[3], "Batch 10/01_a_X")
        self.assertEqual(s["items"][0]["label"], "01_a")
        self.assertEqual(s["items"][0]["output"], "out/Batch 3/01_a_X.blend")
        self.assertEqual(s["spec"]["key"], "build")

    def test_legacy_jobboard_dir_still_resolves_parent(self):
        p = self.write({"title": "legacy"}, where=".jobboard/build.json")
        self.assertEqual(load_spec(p)["base_dir"], self.root)

    def test_vars_override_and_explicit_items(self):
        p = self.write({"name": "r", "vars": {"n": "1"},
                        "items": [{"id": "frame{n}", "output": "f{n}.png", "cmd": "render {n} {{literal}}"}]})
        s = load_spec(p, overrides={"n": "7"})
        it = s["items"][0]
        self.assertEqual((it["id"], it["output"]), ("frame7", "f7.png"))
        self.assertIn("{{literal}}", it["cmd"])

    def test_empty_glob_is_an_error(self):
        p = self.write({"item_sources": [{"glob": "nope/*.x"}]})
        with self.assertRaises(StoreError):
            load_spec(p)

    def test_spec_outside_spec_dir_uses_its_folder(self):
        p = self.write({"title": "t"}, where="tallyboard.json")
        self.assertEqual(load_spec(p)["base_dir"], self.root)

    def test_fill_leaves_unknown(self):
        self.assertEqual(fill("{a}-{b}", {"a": 1}), "1-{b}")


if __name__ == "__main__":
    unittest.main()
