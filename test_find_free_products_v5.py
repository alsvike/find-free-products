import csv
import gzip
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import find_free_products_emaerket_v5_strict as crawler


class DetectionRegressionTests(unittest.TestCase):
    def detect(self, body: str, path: str = "/product/test"):
        html = f"""
        <html>
          <head><meta property="og:type" content="product"></head>
          <body>{body}</body>
        </html>
        """
        return crawler.detect_findings(
            "shop.dk", f"https://shop.dk{path}", html, "test"
        )

    def test_hidden_ancestor_zero_is_rejected(self):
        findings = self.detect(
            """
            <h1>Test product</h1>
            <button>Læg i kurv</button>
            <div style="display: none">
              <span class="product-price">0 kr.</span>
            </div>
            """
        )
        self.assertEqual(findings, [])

    def test_exact_price_class_is_detected(self):
        findings = self.detect(
            """
            <main class="product-detail">
              <h1>Free sample</h1>
              <span class="price">0 kr.</span>
              <button>Læg i kurv</button>
            </main>
            """
        )
        self.assertEqual(len(findings), 1)

    def test_exact_cart_class_is_rejected(self):
        findings = self.detect(
            """
            <h1>Ordinary product</h1><button>Læg i kurv</button>
            <div class="mini-cart"><span class="price">0 kr.</span></div>
            """
        )
        self.assertEqual(findings, [])

    def test_visible_zero_without_buy_action_is_rejected(self):
        findings = self.detect(
            '<h1>Test product</h1><span class="product-price">0 kr.</span>'
        )
        self.assertEqual(findings, [])

    def test_visible_zero_with_buy_action_is_accepted(self):
        findings = self.detect(
            """
            <main class="product-detail">
              <h1>Free sample</h1>
              <span class="product-price">0 kr.</span>
              <button>Læg i kurv</button>
            </main>
            """
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].match_type, "visible_product_price_zero")

    def test_search_card_is_only_a_discovery_lead(self):
        html = """
        <html><body>
          <article class="product-card">
            <a href="/product/free-sample"><h2>Free sample</h2></a>
            <span class="product-price">0 kr.</span>
            <button>Læg i kurv</button>
          </article>
        </body></html>
        """
        findings = crawler.detect_findings(
            "shop.dk", "https://shop.dk/search?q=gratis", html, "test"
        )
        self.assertEqual(findings, [])

    def test_recommendation_button_does_not_validate_main_product(self):
        findings = self.detect(
            """
            <main class="product-detail">
              <h1>Unavailable sample</h1>
              <span class="product-price">0 kr.</span>
            </main>
            <aside class="related-products">
              <button>Add to cart</button>
            </aside>
            """
        )
        self.assertEqual(findings, [])

    def test_out_of_stock_structured_offer_is_rejected(self):
        findings = self.detect(
            """
            <h1>Old sample</h1><button>Læg i kurv</button>
            <script type="application/ld+json">
            {
              "@type": "Product",
              "name": "Old sample",
              "url": "https://shop.dk/product/test",
              "offers": {
                "@type": "Offer",
                "price": "0.00",
                "priceCurrency": "DKK",
                "availability": "https://schema.org/OutOfStock"
              }
            }
            </script>
            """
        )
        self.assertEqual(findings, [])

    def test_free_service_with_purchase_threshold_is_rejected(self):
        findings = self.detect(
            """
            <main class="single-product">
              <h1>Plant et træ - GRATIS</h1>
              <div class="product-price">Gratis</div>
              <button>Tilføj til kurv</button>
              <section class="product-description">
                Ved køb for over 8.000 kr. planter vi GRATIS et nyt træ.
              </section>
            </main>
            """
        )
        self.assertEqual(findings, [])

    def test_free_product_with_separate_shipping_threshold_is_accepted(self):
        findings = self.detect(
            """
            <main class="single-product">
              <h1>GRATIS Ringmåler</h1>
              <div class="product-price">0 DKK</div>
              <button>Tilføj til kurv</button>
              <section class="product-description">
                Bestil en GRATIS ringmåler. Ved køb under 400 kr. betales fragt.
              </section>
            </main>
            """
        )
        self.assertEqual(len(findings), 1)


class CrawlerSafetyTests(unittest.TestCase):
    def test_subdomain_scope_does_not_expand_to_parent(self):
        self.assertTrue(crawler.same_site("https://cdn.shop.dk/x", "shop.dk"))
        self.assertFalse(crawler.same_site("https://example.dk/x", "shop.example.dk"))
        self.assertFalse(crawler.same_site("https://other.dk/x", "shop.dk"))

    def test_large_gzip_sitemap_is_rejected_after_decompression(self):
        oversized = b" " * (crawler.MAX_SITEMAP_DECOMPRESSED_BYTES + 1)
        pages, nested = crawler.parse_sitemap_content(
            gzip.compress(oversized), "https://shop.dk/sitemap.xml.gz"
        )
        self.assertEqual((pages, nested), ([], []))

    def test_candidate_queue_can_backfill_failed_page_budget(self):
        sitemap_urls = [f"https://shop.dk/product/{i}" for i in range(20)]
        candidates = crawler.build_candidate_urls(
            "https://shop.dk",
            "shop.dk",
            sitemap_urls,
            [],
            max_candidates=12,
            use_search_pages=False,
        )
        self.assertEqual(len(candidates), 12)

    def test_resume_retries_transient_statuses(self):
        rows = [
            {"domain": "done.dk", "status": "ok"},
            {"domain": "retry.dk", "status": "rate_limited"},
            {"domain": "blocked.dk", "status": "blocked"},
            {"domain": "empty.dk", "status": "no_html_pages"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["domain", "status"], delimiter=";")
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(crawler.load_completed(path), {"done.dk"})

    def test_existing_findings_are_loaded_for_cross_run_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "findings.csv"
            fields = [field.name for field in crawler.Finding.__dataclass_fields__.values()]
            finding = crawler.Finding(
                "shop.dk",
                "https://shop.dk/product/free",
                "Free sample",
                "structured_price_zero",
                "0",
                "price=0",
                "sitemap",
            )
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
                writer.writeheader()
                writer.writerow(crawler.asdict(finding))
            self.assertEqual(crawler.load_existing_finding_keys(path), {crawler.finding_key(finding)})

    def test_runtime_limit_stops_scheduling_new_shops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "shops.csv"
            input_path.write_text("web_domain\na.dk\nb.dk\nc.dk\n", encoding="utf-8")

            def slow_scan(domain, *_args):
                time.sleep(0.05)
                return [], crawler.SiteStatus(domain, "ok", 1, 0, "test")

            argv = [
                "crawler",
                str(input_path),
                "--output",
                str(root / "findings.csv"),
                "--status",
                str(root / "status.csv"),
                "--workers",
                "1",
                "--max-runtime-minutes",
                "0.0002",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                crawler, "scan_site", side_effect=slow_scan
            ):
                self.assertEqual(crawler.main(), 0)
            with (root / "status.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                statuses = list(csv.DictReader(handle, delimiter=";"))
            self.assertEqual(len(statuses), 1)


if __name__ == "__main__":
    unittest.main()
