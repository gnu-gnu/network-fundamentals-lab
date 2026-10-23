from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
import guide_view
import knowledge


class GuideViewTests(unittest.TestCase):
    def document(self):
        return knowledge.source_by_path('04-l3-routing/README.md')

    def test_readable_html_without_repository_path_heading(self):
        page = guide_view.render(self.document())
        self.assertIn('<title>04장 교재</title>', page)
        self.assertIn('<h1 id="L1">', page)
        self.assertIn('<table>', page)
        self.assertIn('<pre', page)
        self.assertNotIn('<h2>04-l3-routing/README.md</h2>', page)
        self.assertNotIn('SHA256:', page)

    def test_html_and_unsafe_links_are_never_executed(self):
        text = '<script>alert(1)</script> [bad](javascript:alert) [bad2](data:text/html,x) `a<b` **bold**'
        result = guide_view.inline(text, self.document())
        self.assertNotIn('<script>', result)
        self.assertNotIn('href="javascript:', result)
        self.assertNotIn('href="data:', result)
        self.assertIn('&lt;script&gt;', result)
        self.assertIn('<strong>bold</strong>', result)
        self.assertIn('<code>a&lt;b</code>', result)

    def test_relative_links_resolve_to_opaque_authenticated_document_urls(self):
        wiki = knowledge.source_by_path('server/knowledge/wiki/routing-and-return-path.md')
        result = guide_view.inline('[04장 라우팅](../../../04-l3-routing/README.md)', wiki)
        self.assertIn(self.document()['id']+'?sha=', result)
        self.assertNotIn('../', result)
        self.assertNotIn('README.md', result)
        self.assertNotIn('href', guide_view.inline('[secret](../../../../etc/passwd)', wiki))

    def test_topology_stays_code_and_friendly_titles_cover_all_sources(self):
        doc = knowledge.source_by_path('04-l3-routing/04-l3-routing.clab.yml')
        self.assertIn('<pre><code>', guide_view.body(doc))
        self.assertEqual(knowledge.display_title(doc), '04장 참고 토폴로지')
        self.assertEqual(knowledge.display_title(self.document()), '04장 교재')
        self.assertTrue(knowledge.display_title(knowledge.source_by_path('server/knowledge/wiki/l2-and-arp.md')).startswith('개념 해설'))
