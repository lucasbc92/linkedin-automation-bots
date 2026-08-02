"""Tests for reading a person's headline off a search-result card.

The bot must decide whether to invite someone *before* clicking Connect, which
means pulling the headline out of the DOM around the Connect control. These
tests drive that walk with a stub element tree instead of a browser.

Run with:  python -m unittest discover -s tests -t .
"""
import unittest

from selenium.webdriver.common.by import By

from connect.bot import LinkedInConnectBot


class FakeElement:
    """Minimal stand-in for a Selenium WebElement.

    Supports only the three XPath queries the extraction code issues: the
    parent axis, descendant <p>, and descendant profile links.
    """

    PARENT = ".."
    PARAGRAPHS = ".//p"
    PROFILE_LINKS = ".//a[contains(@href, 'linkedin.com/in/')]"

    def __init__(self, tag, text="", href=None, children=()):
        self.tag = tag
        self._text = text
        self.href = href
        self.children = list(children)
        self.parent = None
        for child in self.children:
            child.parent = self

    def _descendants(self):
        for child in self.children:
            yield child
            yield from child._descendants()

    @property
    def text(self):
        if self._text:
            return self._text
        return "\n".join(node._text for node in self._descendants() if node._text)

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def find_element(self, by, selector):
        if by == By.XPATH and selector == self.PARENT:
            if self.parent is None:
                raise LookupError("no parent")
            return self.parent
        raise AssertionError(f"unexpected find_element: {selector}")

    def find_elements(self, by, selector):
        if selector == self.PARAGRAPHS:
            return [n for n in self._descendants() if n.tag == "p"]
        if selector == self.PROFILE_LINKS:
            return [n for n in self._descendants()
                    if n.tag == "a" and n.href and "linkedin.com/in/" in n.href]
        raise AssertionError(f"unexpected find_elements: {selector}")


def build_card(name, slug, headline=None, snippet=None, location="São Paulo, Brazil"):
    """One search-result card, in the same element order LinkedIn renders."""
    paragraphs = [FakeElement("p", children=[
        FakeElement("a", text=name, href=f"https://www.linkedin.com/in/{slug}/")])]
    if headline:
        paragraphs.append(FakeElement("p", text=headline))
    paragraphs.append(FakeElement("p", text=location))
    if snippet:
        paragraphs.append(FakeElement("p", text=snippet))
    connect = FakeElement("a", text="Connect", href="#connect")
    return FakeElement("div", children=[
        FakeElement("div", children=paragraphs),
        FakeElement("div", children=[connect]),
    ]), connect


def build_page(*cards):
    """Wrap cards in a results list, so the walk has somewhere too far to go."""
    return FakeElement("div", children=list(cards))


def make_bot(**overrides):
    bot = object.__new__(LinkedInConnectBot)
    bot.tech_only = True
    bot.min_title_score = 0.80
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


class ExtractTitleTests(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_reads_the_headline_not_the_name_or_location(self):
        card, connect = build_card("Livia Morales", "liviamorales",
                                   headline="Tech Recruiter | Recrutamento e seleção")
        build_page(card)
        self.assertEqual(self.bot.extract_title_texts(connect),
                         ["Tech Recruiter | Recrutamento e seleção"])

    def test_returns_headline_and_role_snippet(self):
        card, connect = build_card("Livia Morales", "liviamorales",
                                   headline="Tech Recruiter",
                                   snippet="Current: Tech recruiter at Beyond")
        build_page(card)
        self.assertEqual(self.bot.extract_title_texts(connect),
                         ["Tech Recruiter", "Current: Tech recruiter at Beyond"])

    def test_falls_back_to_the_snippet_when_there_is_no_headline(self):
        card, connect = build_card("Ligia P.", "ligiap",
                                   snippet="Current: Tech recruiter at Beyond")
        build_page(card)
        titles = self.bot.extract_title_texts(connect)
        self.assertIn("Current: Tech recruiter at Beyond", titles)

    def test_does_not_read_a_neighbouring_card(self):
        mine, connect = build_card("Ana", "ana", headline="Sales Recruiter")
        theirs, _ = build_card("Bruno", "bruno", headline="Tech Recruiter")
        build_page(mine, theirs)
        self.assertEqual(self.bot.extract_title_texts(connect), ["Sales Recruiter"])

    def test_no_card_yields_no_titles(self):
        orphan = FakeElement("a", text="Connect", href="#connect")
        self.assertEqual(self.bot.extract_title_texts(orphan), [])


class EvaluateTitleTests(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()

    def test_tech_recruiter_passes(self):
        card, connect = build_card("Livia", "livia", headline="Tech Recruiter")
        build_page(card)
        verdict = self.bot.evaluate_title(connect, "Invite Livia to connect")
        self.assertTrue(verdict.is_tech_recruiter)

    def test_non_tech_recruiter_fails(self):
        card, connect = build_card("Ana", "ana", headline="Sales Recruiter")
        build_page(card)
        verdict = self.bot.evaluate_title(connect, "Invite Ana to connect")
        self.assertFalse(verdict.is_tech_recruiter)

    def test_snippet_can_rescue_a_vague_headline(self):
        card, connect = build_card("Ligia", "ligia",
                                   headline="Sempre em busca de gente boa",
                                   snippet="Current: Tech recruiter at Beyond")
        build_page(card)
        verdict = self.bot.evaluate_title(connect, "Invite Ligia to connect")
        self.assertTrue(verdict.is_tech_recruiter)

    def test_unreadable_card_returns_none(self):
        orphan = FakeElement("a", text="Connect", href="#connect")
        self.assertIsNone(self.bot.evaluate_title(orphan, "Invite X to connect"))


if __name__ == "__main__":
    unittest.main()
