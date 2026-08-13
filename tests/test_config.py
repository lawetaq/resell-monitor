from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.config import load_searches


class ConfigTests(unittest.TestCase):
    def load(self, content: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "searches.json"
            path.write_text(content, encoding="utf-8")
            return load_searches(path)

    def test_rejects_string_terms_instead_of_array(self) -> None:
        with self.assertRaisesRegex(ValueError, "include_terms"):
            self.load('[{"name":"x","source":"avito","url":"https://example.test","include_terms":"rtx"}]')

    def test_rejects_relative_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            self.load('[{"name":"x","source":"avito","url":"/search"}]')

    def test_rejects_inverted_price_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_price"):
            self.load('[{"name":"x","source":"avito","url":"https://example.test","min_price":2,"max_price":1}]')

    def test_accepts_valid_search(self) -> None:
        searches = self.load('[{"name":"x","source":"avito","url":"https://example.test","include_terms":[" rtx "],"brands":[" asus "],"jitter_seconds":30}]')
        self.assertEqual(searches[0].include_terms, ("rtx",))
        self.assertEqual(searches[0].brands, ("asus",))

    def test_rejects_unbounded_jitter_and_multiple_block_retries(self) -> None:
        with self.assertRaisesRegex(ValueError, "jitter_seconds"):
            self.load('[{"name":"x","source":"avito","url":"https://example.test","interval_seconds":100,"jitter_seconds":51}]')
        with self.assertRaisesRegex(ValueError, "max_block_retries"):
            self.load('[{"name":"x","source":"avito","url":"https://example.test","max_block_retries":2}]')

    def test_validates_explicit_network_profiles(self) -> None:
        proxied = self.load('[{"name":"x","source":"avito","url":"https://example.test","network_route":"proxy","proxy_url":"socks5://127.0.0.1:1080","avito_impersonation":"edge","avito_session_mode":"fresh"}]')[0]
        self.assertEqual(proxied.network_route, "proxy")
        self.assertEqual(proxied.avito_session_mode, "fresh")
        with self.assertRaisesRegex(ValueError, "requires proxy_url"):
            self.load('[{"name":"x","source":"avito","url":"https://example.test","network_route":"proxy"}]')
        with self.assertRaisesRegex(ValueError, "must not define proxy_url"):
            self.load('[{"name":"x","source":"avito","url":"https://example.test","proxy_url":"http://127.0.0.1:8080"}]')

    def test_example_profiles_cover_cpu_ssd_and_motherboards_conservatively(self) -> None:
        searches = load_searches(Path(__file__).parents[1] / "searches.example.json")
        by_name = {search.name: search for search in searches}
        expected = {
            "Avito Khabarovsk processors": {"ryzen", "core i3", "core i5", "core i7"},
            "Avito Khabarovsk SSD": {"ssd", "sata", "2.5", "m.2", "nvme"},
            "Avito Khabarovsk motherboards": {"am4", "am5", "lga1200", "lga1700", "b550"},
        }
        for name, terms in expected.items():
            search = by_name[name]
            self.assertFalse(search.enabled)
            self.assertTrue(terms.issubset(set(search.include_terms)))
            self.assertEqual(search.max_block_retries, 1)
            self.assertGreaterEqual(search.interval_seconds, 1800)
