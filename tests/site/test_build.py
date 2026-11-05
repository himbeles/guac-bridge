import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "website", Path(__file__).resolve().parents[2] / "site/build.py"
)
website = importlib.util.module_from_spec(spec)
spec.loader.exec_module(website)


class WebsiteTests(unittest.TestCase):
    def release(self):
        return {
            "tagName": "v5.0.3",
            "url": website.REPOSITORY + "/releases/tag/v5.0.3",
            "assets": [
                {"name": "guac-bridge-5.0.3-" + suffix,
                 "url": website.REPOSITORY + "/releases/download/v5.0.3/" + suffix}
                for suffix, _, _ in website.DOWNLOADS
            ],
        }

    def test_all_platforms_have_direct_downloads(self):
        release = self.release()
        page = website.render(release)
        self.assertNotIn("@@", page)
        self.assertEqual(page.count('class="download"'), 3)
        for asset in release["assets"]:
            self.assertIn('href="' + asset["url"] + '"', page)

    def test_older_release_only_links_to_existing_assets(self):
        release = self.release()
        release["assets"] = release["assets"][:1]
        page = website.render(release)
        self.assertEqual(page.count('class="download"'), 1)
        self.assertNotIn("Apple Silicon", page)

    def test_unpublished_and_unusable_releases_fail(self):
        for field in ("isDraft", "isPrerelease"):
            release = self.release()
            release[field] = True
            with self.assertRaises(ValueError):
                website.render(release)
        release = self.release()
        release["assets"] = []
        with self.assertRaises(ValueError):
            website.render(release)

    def test_unexpected_download_origin_fails(self):
        release = self.release()
        release["assets"][0]["url"] = "https://example.com/untrusted.exe"
        with self.assertRaises(ValueError):
            website.render(release)


if __name__ == "__main__":
    unittest.main()
