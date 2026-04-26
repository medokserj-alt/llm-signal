import importlib
import sys
import types
import unittest

sys.modules.setdefault("feedparser", types.SimpleNamespace(parse=lambda *_args, **_kwargs: types.SimpleNamespace(entries=[])))
news_snapshot = importlib.import_module("news_snapshot")


class TestNewsSnapshot(unittest.TestCase):
    def test_geopolitical_white_house_headline_is_treated_as_macro_even_outside_macro_feed(self) -> None:
        title = "White House talks on Israel-Lebanon ceasefire extension expected later today"
        self.assertTrue(news_snapshot._is_macro_item(title, "", "https://example.test", from_macro_feed=False))

    def test_local_hospital_assault_is_not_treated_as_macro_even_from_macro_feed(self) -> None:
        title = "Patient allegedly attacks several nurses, police and member of the public at Sydney hospital"
        self.assertFalse(news_snapshot._is_macro_item(title, "", "https://example.test", from_macro_feed=True))

    def test_crypto_etf_news_remains_macro_relevant_from_macro_feed(self) -> None:
        title = "Spot Bitcoin ETF inflows jump as custody update clears final SEC hurdle"
        self.assertTrue(news_snapshot._is_macro_item(title, "", "https://example.test", from_macro_feed=True))


if __name__ == "__main__":
    unittest.main()
