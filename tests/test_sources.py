from pathlib import Path
import unittest

from src.sources.farpost import parse_farpost_html
from src.sources.youla import parse_youla_html

FIXTURES = Path(__file__).parent / "fixtures"


class OtherSourceParserTests(unittest.TestCase):
    def test_farpost(self) -> None:
        item = parse_farpost_html((FIXTURES / "farpost.html").read_text())[0]
        self.assertEqual((item.source, item.external_id, item.price, item.location), ("farpost", "123456", 25000, "Хабаровск"))

    def test_youla(self) -> None:
        item = parse_youla_html((FIXTURES / "youla.html").read_text())[0]
        self.assertEqual((item.source, item.external_id, item.price, item.location), ("youla", "abc123", 18000, "Хабаровск"))
