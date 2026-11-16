import unittest

import bf_collector as bf

SAMPLE = """<?xml version="1.0"?>
<rss version="2.0" xmlns:ozb="https://www.ozbargain.com.au">
<channel>
<item><title>Nintendo Switch 2 + Mario Kart World Bundle $649 @ Kmart</title>
<link>https://www.ozbargain.com.au/node/980857</link><guid>980857 at https://www.ozbargain.com.au</guid>
<pubDate>Thu, 29 Oct 2026 18:21:34 +1100</pubDate>
<ozb:meta votes-pos="390" votes-neg="7"/></item>
<item><title>LG 77" OLED TV $2,499 @ JB Hi-Fi</title>
<link>https://www.ozbargain.com.au/node/1</link><guid>1</guid><ozb:meta votes-pos="10" votes-neg="0"/></item>
<item><title>15% off TCN Gift Cards @ Coles</title>
<link>https://www.ozbargain.com.au/node/2</link><guid>2</guid></item>
<item><title>Free Coles Plus Trial @ Coles</title>
<link>https://www.ozbargain.com.au/node/3</link><guid>3</guid></item>
<item><title>Old deal $5 @ Kmart</title><link>x</link><guid>4</guid>
<ozb:meta votes-pos="1" votes-neg="0" expiry="expired"/></item>
</channel></rss>"""


class TestCollector(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(bf.first_price("Thing $649 @ Kmart"), 649.0)
        self.assertEqual(bf.first_price("TV $2,499 @ JB"), 2499.0)
        self.assertEqual(bf.first_price("Card $653.60 delivered"), 653.6)
        self.assertEqual(bf.first_price("Was $1,000.5"), 1000.5)
        self.assertIsNone(bf.first_price("Free trial"))

    def test_parse_and_filter(self):
        deals = bf.parse_feed(SAMPLE, "Kmart")
        titles = [d["title"] for d in deals]
        self.assertNotIn("Old deal $5 @ Kmart", titles)  # 過期的排除
        self.assertEqual(deals[0]["votes"], 383)
        self.assertEqual(deals[0]["published"], "2026-10-29")
        kept = [d["title"] for d in deals if bf.keep(d, 1000)]
        self.assertIn("Nintendo Switch 2 + Mario Kart World Bundle $649 @ Kmart", kept)
        self.assertIn("15% off TCN Gift Cards @ Coles", kept)       # % off 促銷保留
        self.assertNotIn('LG 77" OLED TV $2,499 @ JB Hi-Fi', kept)  # 超過上限
        self.assertNotIn("Free Coles Plus Trial @ Coles", kept)     # 沒價格也沒 % off

    def test_boundary(self):
        self.assertTrue(bf.keep({"price": 1000.0, "title": ""}, 1000))
        self.assertFalse(bf.keep({"price": 1000.01, "title": ""}, 1000))

    def test_messages_split(self):
        deals = [{"store": "JB Hi-Fi", "title": "x" * 90, "link": "https://a/" + str(i),
                  "price": 10.0, "votes": i} for i in range(200)]
        old = bf.TOP_PER_STORE
        bf.TOP_PER_STORE = 200
        try:
            msgs = bf.build_messages(deals, ["Coles"], "2026-11-16")
        finally:
            bf.TOP_PER_STORE = old
        self.assertTrue(all(len(m) <= 4096 for m in msgs))
        self.assertGreater(len(msgs), 1)
        self.assertIn("Coles", msgs[-1])


if __name__ == "__main__":
    unittest.main()
