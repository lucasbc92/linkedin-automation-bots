"""Tests for the tech-recruiter headline filter.

The fixture cases come from connect/examples/search-results-section-new.html —
a real "recruiter" people-search page, which mixes tech recruiters with people
who are not recruiters at all.

Run with:  python -m unittest discover -s tests -t .
"""
import html
import re
import unittest
from pathlib import Path

from connect.tech_recruiter import (DEFAULT_MIN_SCORE, is_tech_recruiter,
                                    normalize, score_title, split_segments)

FIXTURE = (Path(__file__).resolve().parent.parent
           / "connect" / "examples" / "search-results-section-new.html")

#: Headline of every person on that page, and whether we want to invite them.
FIXTURE_EXPECTATIONS = {
    "Tech Recruiter | Recrutamento e seleção": True,
    "Web Development Student": False,
    "Tech Recruiter | Talent Acquisition | DE&I | Psicóloga": True,
    "Tech Recruiter Sênior na SRM Asset": True,
    "Tech Recruiter | IT Recruiter | Talent Acquisition | Hunting": True,
    "Tech Recruiter / Talent Acquisition Partner / People & Talent / RH": True,
    "Tech Recruiter | Hunting & Talent Acquisition": True,
    "Talent Acquisition | Tech Recruiter | IT Recruiter": True,
    "Acadêmica de Psicologia | Recrutamento & Seleção | Tech Recruiter "
    "| Headhunter": True,
    "Tech Recruiter": True,
}


def fixture_headlines():
    """Pull (name, headline) out of the saved search-results markup."""
    markup = FIXTURE.read_text(encoding="utf-8")
    cards = re.split(r'(?=<a class="_9728bc50 _3b2e5f49 _0b6ecdc1 b48cb19d")',
                     markup)[1:]
    results = []
    for card in cards:
        name = _strip_tags(card[:card.find("</a>")])
        headlines = re.findall(
            r'<p class="_3d4c77c2 _6a703779 _39919fe7[^"]*"><span>(.*?)</span></p>',
            card[:6000], re.S)
        if headlines:
            results.append((name, _strip_tags(headlines[0])))
    return results


def _strip_tags(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


class NormalizationTests(unittest.TestCase):
    def test_strips_accents_case_and_entities(self):
        self.assertEqual(normalize("Recrutação &amp; Seleção"),
                         "recrutacao and selecao")

    def test_empty_input_is_empty_string(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")

    def test_splits_on_bullets_but_not_on_hyphens(self):
        self.assertEqual(split_segments("Tech Recruiter | TA · Hunting"),
                         ["tech recruiter", "ta", "hunting"])
        self.assertEqual(split_segments("Recruiting Manager - Engineering"),
                         ["recruiting manager engineering"])


class FixtureTests(unittest.TestCase):
    """Every headline on the saved page must be classified as expected."""

    def test_fixture_is_parsed(self):
        headlines = fixture_headlines()
        self.assertEqual(len(headlines), len(FIXTURE_EXPECTATIONS))

    def test_every_fixture_headline_classified_correctly(self):
        for name, headline in fixture_headlines():
            with self.subTest(person=name, headline=headline):
                self.assertIn(headline, FIXTURE_EXPECTATIONS,
                              "fixture changed; update FIXTURE_EXPECTATIONS")
                verdict = score_title(headline)
                self.assertEqual(verdict.is_tech_recruiter,
                                 FIXTURE_EXPECTATIONS[headline],
                                 f"score={verdict.score} reason={verdict.reason}")


class AcceptTests(unittest.TestCase):
    ACCEPTED = (
        "Tech Recruiter",
        "IT Recruiter at Google",
        "Technical Recruiter | Series B startup",
        "Recrutadora de TI",
        "Especialista em Recrutamento de TI",
        "Recrutadora | Vagas de TI",
        "Headhunter para vagas de tecnologia",
        "Recruiting Manager - Engineering",
        "Analista de RH | Recrutamento de Desenvolvedores",
        "Talent Acquisition Specialist | Technology",
        "recrutamento e seleção de ti",
        "Tech Recruiter | Vendas | Saúde",  # tech is still in the mix
    )

    def test_accepted(self):
        for title in self.ACCEPTED:
            with self.subTest(title=title):
                verdict = score_title(title)
                self.assertTrue(verdict.is_tech_recruiter,
                                f"score={verdict.score} reason={verdict.reason}")

    def test_tolerates_typos(self):
        self.assertTrue(is_tech_recruiter("Tech Recruter"))
        self.assertTrue(is_tech_recruiter("Techinical Recruiter"))


class RejectTests(unittest.TestCase):
    REJECTED = (
        # Recruiters, but not for technology.
        "Sales Recruiter",
        "Legal Recruiter | Direito",
        "Recruiter | Saúde e Farmacêutico",
        "Talent Acquisition | Healthcare",
        "Recrutador de Engenharia Civil",
        "Recruiter de Marketing e Vendas",
        # Recruiters with no domain stated at all.
        "Analista de Recrutamento e Seleção",
        "Talent Acquisition Specialist",
        "Consultora de RH e Recrutamento",
        # Technology people who do not recruit.
        "Web Development Student",
        "Software Engineer",
        "Data Analyst | Python | SQL",
        # Neither.
        "RH | Departamento Pessoal",
        "Estudante de Psicologia",
        "",
    )

    def test_rejected(self):
        for title in self.REJECTED:
            with self.subTest(title=title):
                verdict = score_title(title)
                self.assertFalse(verdict.is_tech_recruiter,
                                 f"score={verdict.score} reason={verdict.reason}")

    def test_ambiguous_short_word_needs_to_be_near_the_recruiting_word(self):
        """'it' as a pronoun must not be read as information technology."""
        self.assertFalse(is_tech_recruiter("Recruiter | Making it happen"))
        self.assertTrue(is_tech_recruiter("IT Recruiter"))

    def test_tech_word_in_an_unrelated_bullet_does_not_vouch(self):
        self.assertFalse(is_tech_recruiter("Recruiter | Saúde | apaixonada por tech"))


class ScoreTests(unittest.TestCase):
    def test_composite_hit_outranks_co_occurrence(self):
        composite = score_title("Tech Recruiter").score
        co_occurrence = score_title("Recruiting Manager for Engineering").score
        self.assertGreater(composite, co_occurrence)
        self.assertGreaterEqual(co_occurrence, DEFAULT_MIN_SCORE)

    def test_scores_stay_in_range(self):
        for title in ("Tech Recruiter | IT | Software | Cloud | Data", "", "x"):
            score = score_title(title).score
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_threshold_is_configurable(self):
        title = "Recruiting Manager - Engineering"  # co-occurrence, 0.90
        self.assertTrue(is_tech_recruiter(title, min_score=0.85))
        self.assertFalse(is_tech_recruiter(title, min_score=0.95))

    def test_verdict_refuses_to_be_used_as_a_boolean(self):
        """`if verdict:` must not silently mean `if verdict.is_tech_recruiter:`."""
        with self.assertRaises(TypeError):
            bool(score_title("Tech Recruiter"))


if __name__ == "__main__":
    unittest.main()
